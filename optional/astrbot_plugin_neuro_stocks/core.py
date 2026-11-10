"""Deterministic paper broker, persistent cursor, and immutable decision journal."""
import json
import sqlite3
import uuid
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from .data import DataUnavailable, iso

TZ = ZoneInfo('Asia/Shanghai')
UNIT = 1_000_000
INITIAL = 100_000 * UNIT


def money(value):
    return int((Decimal(str(value))*UNIT).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def next_slot(now):
    dt = datetime.fromtimestamp(now,TZ)
    if dt.weekday() < 5 and dt.hour < 9:
        return dt.replace(hour=9,minute=0,second=0,microsecond=0).timestamp()
    nxt = dt.replace(minute=(dt.minute//30)*30,second=0,microsecond=0)+timedelta(minutes=30)
    if dt.weekday()<5 and nxt.hour*60+nxt.minute <= 15*60+30:
        return nxt.timestamp()
    dt = (dt+timedelta(days=1)).replace(hour=9,minute=0,second=0,microsecond=0)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    return dt.timestamp()


def in_window(now):
    dt = datetime.fromtimestamp(now,TZ)
    return dt.weekday()<5 and 540 <= dt.hour*60+dt.minute <= 930


def week_end(now):
    dt = datetime.fromtimestamp(now,TZ)
    end = (dt-timedelta(days=(dt.weekday()-2)%7)).replace(hour=21,minute=0,second=0,microsecond=0)
    return int((end if end<=dt else end-timedelta(days=7)).timestamp())


def decision(raw, symbols, news=()):
    if len(raw)>16000:
        raise ValueError('oversize decision')
    obj = json.loads(raw)
    if not isinstance(obj,dict) or obj.get('action') not in ('buy','sell','hold'):
        raise ValueError('invalid action')
    for key in ('reason','risk','plan','text','emotion'):
        if not isinstance(obj.get(key),str) or not obj[key].strip() or len(obj[key])>500:
            raise ValueError('missing or oversized '+key)
    if obj['action']!='hold':
        if obj.get('symbol') not in symbols or type(obj.get('shares')) is not int or not 1<=obj['shares']<=1_000_000:
            raise ValueError('invalid order')
    result={k:obj[k] for k in ('action','symbol','shares','reason','risk','plan','text','emotion') if k in obj}
    visible={str(n['id']) for n in news if isinstance(n,dict) and 'id' in n}
    ids=obj.get('news_ids',[])
    result['news_ids']=list(dict.fromkeys(n for n in ids if isinstance(n,str) and n in visible))[:2] if isinstance(ids,list) else []
    context=obj.get('context_text')
    if isinstance(context,str) and 0<len(context.strip())<=100:result['context_text']=context.strip()
    # A missing/invalid cosmetic translation must never change a trading decision.
    translations=obj.get('news_translations',[])
    if isinstance(translations,list):
        result['news_translations']=[{'source':n['source'],'zh':n['zh']} for n in translations[:2]
            if isinstance(n,dict) and isinstance(n.get('source'),str) and 0<len(n['source'])<=1000
            and isinstance(n.get('zh'),str) and 0<len(n['zh'])<=300
            and any('\u4e00'<=c<='\u9fff' for c in n['zh'])]
    return result


def note_context(reply,snapshot):
    """Only explicit, visible citations; legacy records fall back to saved rationale."""
    news={str(n['id']):n for n in snapshot.get('news',[]) if 'id' in n}
    ids=reply.get('news_ids',[])
    ids=list(dict.fromkeys(i for i in ids if isinstance(i,str) and i in news))[:2] if isinstance(ids,list) else []
    translations={n['source']:n['zh'] for n in reply.get('news_translations',[])
                  if isinstance(n,dict) and isinstance(n.get('source'),str) and isinstance(n.get('zh'),str)}
    citations=[{'id':i,'original':news[i].get('headline',''),'zh':translations.get(news[i].get('headline',''),'')} for i in ids]
    reason=reply.get('context_text') or re.split(r'[。；;\n]',reply.get('reason',''),maxsplit=1)[0]
    return {'context_text':reason[:180],'news_citations':citations}


class Game:
    def __init__(self,path):
        # A second worker must not classify the first worker's in-flight request as a crash.
        # OS locks are released on process death, so recovery does not need stale PID heuristics.
        self._owner=open(str(path)+'.owner','a+b')
        try:
            self._owner.seek(0)
            if os.name=='nt':
                import msvcrt
                self._owner.write(b'0');self._owner.flush();self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self._owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise RuntimeError('another stock game worker owns this ledger') from None
        self.db = sqlite3.connect(path,timeout=30,check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS seasons(id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS setting(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS decisions(season TEXT, slot TEXT, at INTEGER, wall INTEGER,
            state TEXT, snapshot TEXT, reply TEXT, PRIMARY KEY(season,slot));
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,season TEXT,kind TEXT,at INTEGER,wall INTEGER,body TEXT);
          CREATE TABLE IF NOT EXISTS weeks(id TEXT PRIMARY KEY,state TEXT,body TEXT,attempt INTEGER);
          CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,state TEXT,start_session INTEGER,target_session INTEGER,created REAL,updated REAL,body TEXT);
        ''')
        # A crash after dispatch must never re-dispatch the same paid request.
        with self.db:
            self.db.execute("UPDATE decisions SET state='interrupted' WHERE state='requesting'")

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def state(self):
        row = self.db.execute("SELECT body FROM seasons WHERE id=(SELECT value FROM setting WHERE key='active')").fetchone()
        return json.loads(row[0]) if row else None

    def save(self,s):
        self.db.execute('UPDATE seasons SET body=? WHERE id=?',(json.dumps(s,ensure_ascii=False),s['id']))

    def start(self,history,now):
        if history.sessions[-1]['close']>=now-900:
            raise DataUnavailable('season must be fully historical')
        with self.transaction():
            old = self.state()
            if old and old['status']!='finished':
                raise ValueError('existing season must be resumed; reset is not implicit')
            if old and not self.can_start():
                raise ValueError('wait for the final weekly report before starting another season')
            s = {'id':uuid.uuid4().hex[:12], 'digest':history.digest, 'status':'paused', 'epoch':0,
                 'session':0,'step':0,'due':next_slot(now),'created':now,'cash':INITIAL,'positions':{},
                 'realized':0,'fees':0,'pending':None,'daily_fills':0,'applied':[], 'receivables':[],
                 'benchmark':{'cash':INITIAL,'shares':0,'cost':0,'receivables':[]},'last_nav':INITIAL,
                 'last_prices':{},'peak':INITIAL,'max_drawdown':0,'error':None}
            self.db.execute('INSERT INTO seasons VALUES (?,?)',(s['id'],json.dumps(s)))
            self.db.execute("INSERT INTO setting VALUES ('active',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(s['id'],))
        return s

    def can_start(self):
        s=self.state()
        if not s:return True
        if s['status']!='finished':return False
        latest=self.db.execute("SELECT MAX(wall) FROM events WHERE season=? AND kind='nav'",(s['id'],)).fetchone()[0]
        published=self.db.execute("SELECT id FROM weeks WHERE state='published' AND id LIKE ?",(s['id']+':%',)).fetchall()
        return latest is None or any(int(r[0].rsplit(':',1)[1])>latest for r in published)

    def toggle(self, enabled, now):
        with self.transaction():
            s = self.state()
            if not s or s['status']=='finished':
                raise ValueError('no active season')
            s['status']='running' if enabled else 'paused'
            s['epoch']+=1
            s['due']=next_slot(now)
            self.save(s)
        return s

    def event(self,s,key,kind,at,wall,body):
        self.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                        (s['id']+':'+key,s['id'],kind,at,wall,json.dumps(body,ensure_ascii=False)))

    def corporate(self,s,history,at,wall):
        for a in sorted(history.actions,key=lambda a:(a['at'],a['id'])):
            if a['at']>at or a['id'] in s['applied']:
                continue
            pos = s['positions'].get(a['symbol'])
            accounts = []
            if pos: accounts.append((s,pos))
            if a['symbol']=='SPY': accounts.append((s['benchmark'],s['benchmark']))
            for account,p in accounts:
                if a['type']=='split':
                    count = p['shares']*a['numerator']
                    if count % a['denominator']:
                        raise DataUnavailable('fractional split requires reviewed cash-in-lieu')
                    p['shares']=count//a['denominator']
                elif p['shares']:
                    account['receivables'].append({'id':a['id'],'pay':a['pay'],'amount':p['shares']*money(a['cash'])})
            s['applied'].append(a['id'])
            self.event(s,'action:'+a['id'],'corporate',a['at'],wall,{'symbol':a['symbol'],'type':a['type']})
        for account in (s,s['benchmark']):
            remaining=[]
            for r in account['receivables']:
                if r['pay']<=at: account['cash']+=r['amount']
                else: remaining.append(r)
            account['receivables']=remaining

    @staticmethod
    def nav(s,prices):
        return s['cash']+sum(p['shares']*money(prices[k]) for k,p in s['positions'].items())+sum(r['amount'] for r in s['receivables'])

    def fill(self,s,order,history,marks,wall,benchmark=False):
        at=order['execute_at'];sym=order['symbol'];side=order['action'];qty=order['shares']
        try:
            raw=money(history.opening(sym,at))
            price=(raw*(10005 if side=='buy' else 9995)+5000)//10000
            account=s['benchmark'] if benchmark else s
            if benchmark: qty=max(0,account['cash']//(price+max(1,price//10000)))
            total=price*qty;fee=max(UNIT,(total+5000)//10000)
            if benchmark and total+fee>account['cash']:
                qty-=1;total=price*qty;fee=max(UNIT,(total+5000)//10000)
            if qty<=0: raise ValueError('zero shares')
            if not benchmark and s['daily_fills']>=4:raise ValueError('daily trade limit')
            p=account if benchmark else s['positions'].get(sym,{'shares':0,'cost':0})
            if side=='buy':
                if total+fee>account['cash']:raise ValueError('insufficient cash')
                valuation={**marks,sym:raw/UNIT}
                post_nav=self.nav(s,valuation)-fee-(price-raw)*qty if not benchmark else 0
                if not benchmark and (p['shares']+qty)*raw*4>post_nav:
                    raise ValueError('position exceeds 25 percent')
                account['cash']-=total+fee;p['cost']+=total+fee;p['shares']+=qty
                realized=0
            else:
                if qty>p['shares']:raise ValueError('insufficient shares')
                basis=p['cost']*qty//p['shares'];p['cost']-=basis;p['shares']-=qty
                account['cash']+=total-fee;realized=total-fee-basis;s['realized']+=realized
            if not benchmark:
                s['positions'][sym]=p;s['fees']+=fee;s['daily_fills']+=1
            self.event(s,order['id'],'benchmark_fill' if benchmark else 'fill',at,wall,
                       {'symbol':sym,'side':side,'shares':qty,'price':price,'fee':fee,'realized':realized,'slot':order['id']})
        except (DataUnavailable,ValueError) as exc:
            if benchmark: raise DataUnavailable('benchmark execution unavailable') from exc
            self.event(s,order['id'],'rejected',at,wall,{'symbol':sym,'reason':str(exc)})

    def prepare(self,history,now,*,fast=False):
        """Apply only past fills and produce an as-of snapshot, once per cursor."""
        with self.transaction():
            s=self.state()
            if not s or s['status']!='running' or (not fast and (now<s['due'] or not in_window(now))): return None
            if s['digest']!=history.digest:raise DataUnavailable('dataset changed during season')
            session=history.sessions[s['session']]
            at=session['open']+s['step']*1800
            slot=f"{s['session']}:{s['step']}"
            existing=self.db.execute('SELECT state,reply,snapshot,at FROM decisions WHERE season=? AND slot=?',(s['id'],slot)).fetchone()
            if existing:
                if existing['state']=='deferred':
                    self.db.execute("UPDATE decisions SET state='requesting' WHERE season=? AND slot=?",(s['id'],slot))
                    return {'season':s['id'],'slot':slot,'epoch':s['epoch'],'at':existing['at'],'snapshot':json.loads(existing['snapshot'])}
                s['step']+=1;s['due']=next_slot(now);self.save(s)
                return None
            snapshot=history.snapshot(at,session)
            self.corporate(s,history,at,now)
            if s['pending']:
                order=s['pending']
                # Value other positions at the execution time, never with this later slot's price.
                marks=history.prices(order['execute_at'],session)
                self.fill(s,order,history,marks,now)
                s['pending']=None
            if s['session']==0 and s['step']==1:
                self.fill(s,{'id':'benchmark','execute_at':session['open']+60,'symbol':'SPY','action':'buy','shares':0},
                          history,history.prices(session['open']+60,session),now,True)
            s['last_prices']=snapshot['prices'];s['last_nav']=self.nav(s,snapshot['prices'])
            s['peak']=max(s['peak'],s['last_nav'])
            s['max_drawdown']=max(s['max_drawdown'],1-s['last_nav']/s['peak'])
            b=s['benchmark']
            benchmark=b['cash']+b['shares']*money(snapshot['prices']['SPY'])+sum(r['amount'] for r in b['receivables'])
            if not existing:
                self.event(s,'nav:'+slot,'nav',at,now,{'nav':s['last_nav'],'benchmark':benchmark,'prices':snapshot['prices'],
                                                    'realized':s['realized'],'fees':s['fees'],'drawdown':s['max_drawdown']})
            if at==session['close']:
                s['session']+=1;s['step']=0;s['daily_fills']=0
                # One historical session per real replay day, including early closes.
                tomorrow=datetime.fromtimestamp(now,TZ).replace(hour=16,minute=0,second=0,microsecond=0).timestamp()
                s['due']=next_slot(tomorrow)
                if s['session']==len(history.sessions):s['status']='finished'
                self.save(s)
                return None
            if existing:
                # Interrupted request is observed without charging or ordering again.
                s['step']+=1;s['due']=next_slot(now);self.save(s)
                return None
            recent=self.db.execute("SELECT reply FROM decisions WHERE season=? AND state='done' AND reply IS NOT NULL ORDER BY at DESC LIMIT ?",(s['id'],65 if fast else 3)).fetchall()
            snapshot['account']={'cash':s['cash']/UNIT,'nav':s['last_nav']/UNIT,
                                 'positions':{k:{'shares':p['shares'],'cost_usd':p['cost']/UNIT,
                                    'average_cost':p['cost']/UNIT/p['shares'],
                                    'unrealized_pnl':(p['shares']*money(snapshot['prices'][k])-p['cost'])/UNIT} for k,p in s['positions'].items() if p['shares']},
                                 'daily_fills':s['daily_fills'],'max_orders':4}
            snapshot['previous']=[json.loads(r[0]) for r in reversed(recent)]
            if fast:
                # Older context stays explicit in SQLite rather than depending on provider caches.
                snapshot['previous']=[{k:n[k] for k in ('action','symbol','shares','plan') if k in n}
                    if i<len(snapshot['previous'])-3 else n for i,n in enumerate(snapshot['previous'])]
                fills=self.db.execute("SELECT body FROM events WHERE season=? AND kind='fill' ORDER BY at DESC LIMIT 20",(s['id'],)).fetchall()
                snapshot['executed_trades']=[{**{k:t[k] for k in ('symbol','side','shares')},
                    **{k+'_usd':t[k]/UNIT for k in ('price','fee','realized')}} for r in reversed(fills) if (t:=json.loads(r[0]))]
            self.db.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,NULL)',
                            (s['id'],slot,at,now,'requesting',json.dumps(snapshot,ensure_ascii=False)))
            self.save(s)
            return {'season':s['id'],'slot':slot,'epoch':s['epoch'],'at':at,'snapshot':snapshot}

    def finish(self,task,reply,now,error=None):
        with self.transaction():
            s=self.state()
            row=self.db.execute('SELECT state FROM decisions WHERE season=? AND slot=?',(task['season'],task['slot'])).fetchone()
            if not row or row[0]!='requesting':return
            active=s and s['id']==task['season'] and s['epoch']==task['epoch'] and s['status']=='running'
            state='done' if active and not error else ('failed' if active else 'cancelled')
            self.db.execute('UPDATE decisions SET state=?,reply=?,wall=? WHERE season=? AND slot=?',
                            (state,json.dumps(reply,ensure_ascii=False) if reply else None,int(now),task['season'],task['slot']))
            if active:
                if reply and reply['action']!='hold' and not error:
                    s['pending']={**reply,'id':'order:'+task['slot'],'execute_at':task['at']+60}
                s['step']+=1;s['due']=next_slot(now);s['error']=error
                self.save(s)

    def error(self,text):
        with self.transaction():
            s=self.state()
            if s:s['error']=text;self.save(s)

    def defer(self,task,now):
        with self.transaction():
            self.db.execute("UPDATE decisions SET state='deferred' WHERE season=? AND slot=? AND state='requesting'",(task['season'],task['slot']))
            s=self.state()
            if s and s['id']==task['season']:
                s['due']=next_slot(now);self.save(s)

    def weekly_snapshot(self,end,since_at=None):
        s=self.state()
        if not s or s['status']=='paused':return None
        rows=self.db.execute('SELECT * FROM events WHERE season=? AND wall<? ORDER BY at,id',(s['id'],end)).fetchall()
        period=[r for r in rows if (r['at']>=since_at if since_at is not None else r['wall']>=end-7*86400)]
        if not any(r['kind']=='nav' for r in period):return None
        navs=[{'at':r['at'],**json.loads(r['body'])} for r in rows if r['kind']=='nav']
        trades=[{'at':r['at'],**json.loads(r['body'])} for r in period if r['kind'] in ('fill','rejected')]
        if since_at is None:
            notes=self.db.execute('SELECT slot,at,reply,state,snapshot FROM decisions WHERE season=? AND wall>=? AND wall<? AND reply IS NOT NULL ORDER BY at',(s['id'],end-7*86400,end)).fetchall()
        else:
            notes=self.db.execute('SELECT slot,at,reply,state,snapshot FROM decisions WHERE season=? AND at>=? AND wall<? AND reply IS NOT NULL ORDER BY at',(s['id'],since_at,end)).fetchall()
        return {'season':s['id'],'end':end,'navs':navs,'trades':trades,
                'notes':[{'slot':r['slot'],'at':r['at'],'state':r['state'],
                          **json.loads(r['reply']),
                          **note_context(json.loads(r['reply']),json.loads(r['snapshot']))} for r in notes],
                'start_nav':next((json.loads(r['body'])['nav'] for r in reversed(rows) if r['kind']=='nav' and r['wall']<end-7*86400),INITIAL)}

    def close(self):
        self.db.close()
        if not self._owner.closed:
            self._owner.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self._owner.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self._owner,fcntl.LOCK_UN)
            self._owner.close()
