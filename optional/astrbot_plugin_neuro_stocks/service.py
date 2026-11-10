"""Single-owner async orchestration; public control never has LLM or data credentials."""
import asyncio
import hmac
import json
import time
from pathlib import Path
from aiohttp import web
from .core import Game, decision, week_end, UNIT
from .data import History, DataUnavailable, iso
from .model import Model, BudgetUnavailable, RULES
from .publish import Publisher, PROMPT, model_payload


class Service:
    def __init__(self,root,config,persona,stickers,model=None,clock=time.time):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.config=config;self.clock=clock;self.persona=persona;self.stickers=stickers
        self.game=Game(self.root/'game.sqlite3');self.lock=asyncio.Lock()
        self.model=model or (Model(config) if config.get('gateway_url') and config.get('gateway_token') and config.get('model') else None)
        self.publisher=Publisher(config.get('public_dir',self.root/'public'))
        self.history=None;self.problem='待配置历史数据源';self.budget=None;self.budget_checked=None
        if config.get('history_file'):
            try:
                self.history=History.load(config['history_file']);self.problem=None
            except (OSError,ValueError,KeyError,TypeError):self.problem='历史数据未通过校验，请检查文件与公司行动清单'

    def next_batch_due(self):
        now=self.clock();end=week_end(now)
        s=self.game.state()
        if s:
            pending=self.game.db.execute("SELECT created FROM batches WHERE state NOT IN ('published','failed','interrupted') ORDER BY created LIMIT 1").fetchone()
            if pending:return pending[0]
            row=self.game.db.execute('SELECT state FROM batches WHERE id=?',(s['id']+':'+str(end),)).fetchone()
            if end>=s['created'] and not row:return end
        return end+7*86400

    def status(self):
        s=self.game.state()
        public={'ready':self.history is not None and self.model is not None,
                'problem':self.problem or (None if self.model else '待配置独立预算路由'),
                'budget':self.budget, 'budget_checked':self.budget_checked, 'can_start':self.game.can_start(), 'season':None}
        public['schedule_mode']=self.config.get('schedule_mode','daily')
        if s:
            fields=('id','status','due','session','step','cash','positions','realized','fees','last_nav','max_drawdown','error')
            public['season']={k:s[k] for k in fields}
            if self.config.get('schedule_mode')=='weekly_batch':public['season']['due']=self.next_batch_due()
            public['season']['cash']/=UNIT;public['season']['realized']/=UNIT;public['season']['fees']/=UNIT
            public['season']['last_nav']/=UNIT
            public['season']['positions']={k:{'shares':p['shares'],'cost':p['cost']/UNIT,
                 'unrealized':(p['shares']*s['last_prices'].get(k,0)*UNIT-p['cost'])/UNIT} for k,p in s['positions'].items() if p['shares']}
            public['season']['total_return']=(s['last_nav']/(100000*UNIT)-1)*100
            b=s['benchmark'];public['season']['benchmark_nav']=b['cash']/UNIT+b['shares']*s['last_prices'].get('SPY',0)+sum(r['amount'] for r in b['receivables'])/UNIT
            if self.history:
                i=min(s['session'],len(self.history.sessions)-1)
                public['season']['game_time']=iso(self.history.sessions[i]['close'] if s['status']=='finished' else self.history.sessions[i]['open']+s['step']*1800)
            rows=self.game.db.execute('SELECT at,state,reply FROM decisions WHERE season=? ORDER BY at DESC LIMIT 20',(s['id'],)).fetchall()
            public['decisions']=[{'at':iso(r['at']),'state':r['state'],'reply':json.loads(r['reply']) if r['reply'] else None} for r in rows]
        return public

    def control(self,action):
        if action=='start':
            if not self.history or not self.model:raise ValueError('请先配置数据源和预算路由')
            self.game.start(self.history,self.clock())
        elif action in ('resume','pause'):
            if action=='resume' and (not self.history or not self.model):raise ValueError('数据或预算路由未配置')
            self.game.toggle(action=='resume',self.clock())
        else:raise ValueError('未知操作')
        return self.status()

    async def tick(self):
        if self.config.get('schedule_mode')=='weekly_batch':
            return await self.batch_tick()
        async with self.lock:
            s=self.game.state()
            if not s or s['status']=='paused' or not self.history or not self.model:return
            now=self.clock()
            # All requests, including weekly summaries, must pass the dedicated route gate.
            try:
                status=await self.model.status();self.budget=status.get('route');self.budget_checked=now
            except Exception:
                self.game.error('预算服务不可用或额度不足，游戏进度已冻结');return
            if s['status']=='running':
                try:
                    task=self.game.prepare(self.history,now)
                except DataUnavailable:
                    self.game.error('历史行情或公司行动不完整，游戏进度已冻结');return
                if task:
                    try:
                        persona=await self.persona();stickers=await self.stickers()
                        if not stickers:raise ValueError('stickers unavailable')
                        raw=await self.model.generate(persona+'\n'+RULES,{**task['snapshot'],'emotions':list(stickers)},1536)
                        reply=decision(raw,self.history.symbols,task['snapshot'].get('news',[]))
                        if reply['emotion'] not in stickers:raise ValueError('invalid emotion')
                        self.game.finish(task,reply,self.clock())
                    except BudgetUnavailable:
                        self.game.defer(task,self.clock());self.game.error('预算不足，等待恢复后继续本格');return
                    except Exception:
                        self.game.finish(task,None,self.clock(),'本格模型响应失败，已记录为观望，不自动重试')
            await self.weekly(self.clock())

    async def batch_tick(self):
        """One persisted five-session run per Wednesday; resume without repeating paid slots."""
        async with self.lock:
            s=self.game.state();now=self.clock()
            if not s or s['status']=='paused' or not self.history or not self.model:return
            row=self.game.db.execute("SELECT * FROM batches WHERE id LIKE ? AND state NOT IN ('published','failed','interrupted') ORDER BY created LIMIT 1",(s['id']+':%',)).fetchone()
            if not row:
                end=week_end(now);key=s['id']+':'+str(end)
                if end<s['created'] or s['status']=='finished' or self.game.db.execute('SELECT 1 FROM batches WHERE id=?',(key,)).fetchone():return
                with self.game.db:self.game.db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?)',
                    (key,'running',s['session'],min(s['session']+5,len(self.history.sessions)),now,now,'{}'))
                row=self.game.db.execute('SELECT * FROM batches WHERE id=?',(key,)).fetchone()
            key=row['id']
            if row['state']=='deferred' and now-row['updated']<60:return
            while (s:=self.game.state())['status']=='running' and s['session']<row['target_session']:
                try:
                    status=await self.model.status();self.budget=status.get('route');self.budget_checked=self.clock()
                except Exception:
                    with self.game.db:self.game.db.execute("UPDATE batches SET state='deferred',updated=? WHERE id=?",(self.clock(),key))
                    self.game.error('博客独立费用服务暂不可用，本周回放已保存，等待恢复');return
                try:task=self.game.prepare(self.history,self.clock(),fast=True)
                except DataUnavailable:
                    self.game.error('本周历史行情不完整，已停止推进');return
                if not task:continue
                try:
                    persona=await self.persona();stickers=await self.stickers()
                    raw=await self.model.generate(persona+'\n'+RULES,{**task['snapshot'],'emotions':list(stickers)},1536)
                    reply=decision(raw,self.history.symbols,task['snapshot'].get('news',[]))
                    if reply['emotion'] not in stickers:raise ValueError('invalid emotion')
                    self.game.finish(task,reply,self.clock())
                except BudgetUnavailable:
                    self.game.defer(task,self.clock())
                    with self.game.db:self.game.db.execute("UPDATE batches SET state='deferred',updated=? WHERE id=?",(self.clock(),key))
                    return
                except Exception:
                    self.game.finish(task,None,self.clock(),'本格回复失败，记为观望，不重复付费')
                await asyncio.sleep(0)
            s=self.game.state()
            if s['status']=='paused':return
            # Freeze exactly this batch, not a sliding wall-clock week that could
            # overlap last Wednesday when runs take different amounts of time.
            package=json.loads(row['body'])
            if not package:
                first=self.history.sessions[row['start_session']]['open']
                snapshot=self.game.weekly_snapshot(self.clock()+1,since_at=first)
                snapshot['notes']=[n for n in snapshot['notes'] if n['at']>=first]
                snapshot['trades']=[n for n in snapshot['trades'] if n['at']>=first]
                prior=[n for n in snapshot['navs'] if n['at']<first]
                snapshot['start_nav']=prior[-1]['nav'] if prior else 100000*UNIT
                snapshot['game_start']=first
                package={'snapshot':snapshot}
            if row['state']=='requesting':
                with self.game.db:self.game.db.execute("UPDATE batches SET state='interrupted' WHERE id=?",(key,))
                self.game.error('周记请求曾中断，需检查后恢复，避免重复付费');return
            epoch=s['epoch']
            if 'reply' not in package:
                try:
                    status=await self.model.status();self.budget=status.get('route');self.budget_checked=self.clock()
                except Exception:return
                with self.game.db:self.game.db.execute("UPDATE batches SET state='requesting',body=?,updated=? WHERE id=?",(json.dumps(package,ensure_ascii=False),self.clock(),key))
                try:
                    persona=await self.persona();stickers=await self.stickers()
                    package['reply']=await self.model.generate(persona+'\n'+PROMPT,model_payload(package['snapshot'],stickers),2048)
                except BudgetUnavailable:
                    with self.game.db:self.game.db.execute("UPDATE batches SET state='deferred',updated=? WHERE id=?",(self.clock(),key))
                    return
                except Exception:
                    with self.game.db:self.game.db.execute("UPDATE batches SET state='failed' WHERE id=?",(key,))
                    self.game.error('周记生成失败，交易记录已保存');return
                with self.game.db:self.game.db.execute("UPDATE batches SET state='ready',body=? WHERE id=?",(json.dumps(package,ensure_ascii=False),key))
            current=self.game.state()
            if current['status']=='paused' or current['epoch']!=epoch:return
            try:
                stickers=await self.stickers()
                self.publisher.build(package['snapshot'],package['reply'],stickers)
                self.publisher.index()
                with self.game.db:
                    self.game.db.execute("UPDATE batches SET state='published',updated=? WHERE id=?",(self.clock(),key))
                    # Existing season lifecycle also requires a final published week.
                    week_key=s['id']+':'+str(int(package['snapshot']['end']))
                    self.game.db.execute('INSERT OR REPLACE INTO weeks VALUES (?,?,?,?)',(week_key,'published',json.dumps(package,ensure_ascii=False),int(self.clock())))
                self.game.error(None)
            except (ValueError,KeyError,TypeError):
                with self.game.db:self.game.db.execute("UPDATE batches SET state='failed' WHERE id=?",(key,))
                self.game.error('周记排版数据异常，回复已保存');return

    async def weekly(self,now):
        s=self.game.state()
        if not s or s['status']=='paused':return
        end=week_end(now);key=s['id']+':'+str(end)
        row=self.game.db.execute('SELECT * FROM weeks WHERE id=?',(key,)).fetchone()
        if row and row['state'] in ('published','empty','failed','interrupted'):return
        if row and row['state']=='requesting':
            # A lost response is not automatically charged again after restart.
            with self.game.db:self.game.db.execute("UPDATE weeks SET state='interrupted' WHERE id=?",(key,))
            return
        if row and now-row['attempt']<900:return
        if row:
            package=json.loads(row['body']);snapshot=package['snapshot']
        else:
            snapshot=self.game.weekly_snapshot(end)
            if not snapshot:return
            package={'snapshot':snapshot}
        epoch=s['epoch']
        if 'reply' not in package:
            with self.game.db:self.game.db.execute('INSERT INTO weeks VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,body=excluded.body,attempt=excluded.attempt',
                (key,'requesting',json.dumps(package,ensure_ascii=False),int(now)))
            try:
                persona=await self.persona();stickers=await self.stickers()
                package['reply']=await self.model.generate(persona+'\n'+PROMPT,model_payload(snapshot,stickers),2048)
            except BudgetUnavailable:
                with self.game.db:self.game.db.execute("UPDATE weeks SET state='deferred' WHERE id=?",(key,))
                return
            except Exception:
                with self.game.db:self.game.db.execute("UPDATE weeks SET state='failed' WHERE id=?",(key,))
                return
            with self.game.db:self.game.db.execute("UPDATE weeks SET state='ready',body=? WHERE id=?",(json.dumps(package,ensure_ascii=False),key))
        current=self.game.state()
        if not current or current['id']!=s['id'] or current['status']=='paused' or current['epoch']!=epoch:return
        try:
            stickers=await self.stickers()
            with self.game.db:self.game.db.execute('UPDATE weeks SET attempt=? WHERE id=?',(int(now),key))
            self.publisher.build(snapshot,package['reply'],stickers)
            self.publisher.index()
            with self.game.db:self.game.db.execute("UPDATE weeks SET state='published' WHERE id=?",(key,))
        except (ValueError,KeyError,TypeError):
            with self.game.db:self.game.db.execute("UPDATE weeks SET state='failed' WHERE id=?",(key,))


def control_app(service,token):
    if not isinstance(token,str) or len(token)<32:raise ValueError('control token must have at least 32 characters')
    @web.middleware
    async def auth(request,handler):
        if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):
            raise web.HTTPUnauthorized()
        result=await handler(request)
        result.headers['Cache-Control']='no-store'
        return result
    app=web.Application(middlewares=[auth],client_max_size=4096)
    async def status(request):return web.json_response(service.status())
    async def change(request):
        try:
            body=await request.json()
            return web.json_response(service.control(body.get('action')))
        except (ValueError,AttributeError):return web.json_response({'error':'操作不可用，请先检查配置与赛季状态'},status=400)
    app.router.add_get('/status',status);app.router.add_post('/control',change)
    return app
