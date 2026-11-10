import asyncio
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_neuro_stocks.data import History, DataUnavailable, iso, stamp
from astrbot_plugin_neuro_stocks.core import Game, UNIT, INITIAL, next_slot, decision, week_end
from astrbot_plugin_neuro_stocks.service import Service, control_app
from astrbot_plugin_neuro_stocks.model import BudgetUnavailable, Model
from astrbot_plugin_neuro_stocks.publish import Publisher

TZ=ZoneInfo('Asia/Shanghai')
NOW=int(datetime(2026,11,10,9,tzinfo=TZ).timestamp())
OPEN=stamp('2026-10-12T13:30:00Z')


def bundle(days=1,minutes=390):
    sessions=[];bars={s:[] for s in ('AAPL','SPY')}
    for day in range(days):
        start=OPEN+day*86400
        sessions.append({'open':iso(start),'close':iso(start+minutes*60)})
        for minute in range(minutes):
            for s in bars:
                price=100+day+minute/100
                bars[s].append({'t':iso(start+minute*60),'o':price,'h':price+.02,'l':price-.02,'c':price+.01})
    return {'symbols':['AAPL','SPY'],'sessions':sessions,'bars':bars,'news':[], 'actions':[],
            'source':{'provider':'offline fixture'},
            'corporate_actions_review':{'complete':True,'through':sessions[-1]['close']}}


def reply(action='hold',shares=10,symbol='AAPL'):
    return {'action':action,'shares':shares,'symbol':symbol,'reason':'试探小仓位','risk':'可能回落',
            'plan':'观察下一格','text':'我先看看。','emotion':'happy'}


def active(tmp_path,history=None):
    history=history or History(bundle())
    g=Game(tmp_path/'game.db');g.start(history,NOW-3600);g.toggle(True,NOW-1800)
    return g,history


def tick(g,h,wall,r=None):
    task=g.prepare(h,wall)
    if task:g.finish(task,r or reply(),wall)
    return task


def test_future_bars_and_later_updated_news_are_hidden():
    b=bundle();b['news']=[{'id':1,'created_at':iso(OPEN-100),'updated_at':iso(OPEN+1),'headline':'leak'},
                         {'id':2,'created_at':iso(OPEN-100),'updated_at':iso(OPEN-100),'headline':'allowed'},
                         {'id':3,'created_at':iso(OPEN+60),'updated_at':iso(OPEN+60),'headline':'future'}]
    h=History(b);s=h.snapshot(OPEN,h.sessions[0])
    assert s['prices']['AAPL']==100 # opening print only
    assert s['recent']['AAPL']==[]
    assert [n['id'] for n in s['news']]==['2']
    s=h.snapshot(OPEN+1800,h.sessions[0])
    assert s['prices']['AAPL']==pytest.approx(100.30) # 09:59 close, not 10:00 close
    assert all(stamp(p['at'])+60<=OPEN+1800 for p in s['recent']['AAPL'])


def test_history_does_not_mutate_input_and_rejects_unreviewed():
    b=bundle();original=copy.deepcopy(b);a=History(b);other=History(b)
    assert b==original and a.digest==other.digest
    b['corporate_actions_review']['complete']=False
    with pytest.raises(DataUnavailable):History(b)


def test_next_minute_execution_and_idempotent_slots(tmp_path):
    g,h=active(tmp_path);task=tick(g,h,NOW,reply('buy',10))
    assert g.state()['positions']=={} # cannot fill from a future bar during this decision
    g.finish(task,reply('buy'),NOW+10)
    tick(g,h,NOW+1800)
    fills=g.db.execute("SELECT body FROM events WHERE kind='fill'").fetchall()
    assert len(fills)==1
    assert json.loads(fills[0][0])['price']==int(round(100.01*1.0005*UNIT))
    before=g.state();assert g.prepare(h,NOW+1800) is None;assert g.state()==before
    assert g.state()['benchmark']['shares']>0
    g.close()


@pytest.mark.parametrize('shares,reason',[(500,'position exceeds'),(100000,'insufficient cash')])
def test_position_and_cash_rejections(tmp_path,shares,reason):
    g,h=active(tmp_path);tick(g,h,NOW,reply('buy',shares));tick(g,h,NOW+1800)
    rejected=json.loads(g.db.execute("SELECT body FROM events WHERE kind='rejected'").fetchone()[0])
    assert reason in rejected['reason'];assert g.state()['positions']=={}
    g.close()


def test_missing_execution_minute_rejects_not_cherry_picks(tmp_path):
    b=bundle();b['bars']['AAPL'].pop(1);g,h=active(tmp_path,History(b))
    tick(g,h,NOW,reply('buy'));tick(g,h,NOW+1800)
    assert not g.state()['positions']
    assert g.db.execute("SELECT COUNT(*) FROM events WHERE kind='rejected'").fetchone()[0]==1
    g.close()


def test_daily_limit_and_sell_profit_accounting(tmp_path):
    g,h=active(tmp_path)
    for i in range(6):tick(g,h,NOW+i*1800,reply('buy' if i%2==0 else 'sell'))
    assert g.state()['daily_fills']==4
    assert g.state()['realized']!=0 and g.state()['fees']>0
    assert g.db.execute("SELECT COUNT(*) FROM events WHERE kind='rejected'").fetchone()[0]==1
    g.close()


def test_pausing_while_model_running_never_executes_return(tmp_path):
    g,h=active(tmp_path);t=g.prepare(h,NOW);g.toggle(False,NOW+1)
    g.finish(t,reply('buy'),NOW+2)
    assert g.state()['status']=='paused' and g.state()['pending'] is None
    assert g.prepare(h,NOW+1800) is None
    g.toggle(True,NOW+1800);assert g.prepare(h,NOW+3600) is None # consume cancelled slot, no model retry
    assert g.state()['step']==1
    g.close()


def test_restart_does_not_retry_unknown_paid_request(tmp_path):
    g,h=active(tmp_path);g.prepare(h,NOW);g.close()
    g=Game(tmp_path/'game.db');assert g.prepare(h,NOW+1800) is None
    assert g.state()['step']==1
    assert g.db.execute('SELECT state FROM decisions').fetchone()[0]=='interrupted'
    g.close()


def test_second_worker_cannot_recover_live_request(tmp_path):
    g,h=active(tmp_path);g.prepare(h,NOW)
    with pytest.raises(RuntimeError,match='another stock game worker'):
        Game(tmp_path/'game.db')
    assert g.db.execute('SELECT state FROM decisions').fetchone()[0]=='requesting'
    g.close()


def test_restart_after_benchmark_fill_does_not_fill_again(tmp_path):
    g,h=active(tmp_path);tick(g,h,NOW);g.prepare(h,NOW+1800);g.close()
    g=Game(tmp_path/'game.db');g.prepare(h,NOW+3600)
    assert g.state()['step']==2
    assert g.db.execute("SELECT COUNT(*) FROM events WHERE kind='benchmark_fill'").fetchone()[0]==1
    g.close()


def test_short_session_and_finish(tmp_path):
    g,h=active(tmp_path,History(bundle(minutes=210)))
    for i in range(8):tick(g,h,NOW+i*1800)
    assert g.state()['status']=='finished'
    assert g.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]==7
    assert not g.can_start()
    with pytest.raises(ValueError,match='final weekly report'):g.start(h,NOW+86400)
    g.close()


def test_weekend_and_off_hours_no_catchup(tmp_path):
    fri=datetime(2026,11,13,15,30,tzinfo=TZ).timestamp()
    assert datetime.fromtimestamp(next_slot(fri),TZ)==datetime(2026,11,16,9,tzinfo=TZ)
    g,h=active(tmp_path)
    assert g.prepare(h,datetime(2026,11,14,10,tzinfo=TZ).timestamp()) is None
    task=g.prepare(h,datetime(2026,11,16,10,tzinfo=TZ).timestamp())
    assert task['slot']=='0:0'
    g.close()


def test_stale_data_rolls_back_cursor(tmp_path):
    b=bundle();b['bars']['AAPL']=b['bars']['AAPL'][:10]
    g,h=active(tmp_path,History(b));tick(g,h,NOW,reply('buy'))
    before=g.state()
    with pytest.raises(DataUnavailable):g.prepare(h,NOW+1800)
    assert before==g.state()
    g.close()


def test_split_dividend_receivable_payment_and_idempotency(tmp_path):
    b=bundle(days=2)
    b['actions']=[{'id':'split','type':'split','symbol':'AAPL','effective_at':iso(OPEN+86400),'numerator':2,'denominator':1},
                  {'id':'div','type':'dividend','symbol':'AAPL','effective_at':iso(OPEN+86400+60),'pay_at':iso(OPEN+86400+600),'cash':1}]
    g,h=active(tmp_path,History(b));tick(g,h,NOW,reply('buy'));tick(g,h,NOW+1800)
    s=g.state();before=s['cash'];cost=s['positions']['AAPL']['cost']
    with g.transaction():
        g.corporate(s,h,OPEN+86400+60,NOW+86400);g.save(s)
    assert s['positions']['AAPL']=={'shares':20,'cost':cost}
    assert s['receivables'][0]['amount']==20*UNIT and s['cash']==before
    with g.transaction():g.corporate(s,h,OPEN+86400+700,NOW+86400);g.save(s)
    assert s['cash']==before+20*UNIT and not s['receivables']
    with g.transaction():g.corporate(s,h,OPEN+86400+700,NOW+86400)
    assert s['cash']==before+20*UNIT
    g.close()


def test_week_snapshot_has_no_later_events(tmp_path):
    g,h=active(tmp_path);tick(g,h,NOW,reply('buy'));tick(g,h,NOW+1800)
    snapshot=g.weekly_snapshot(NOW+2000);tick(g,h,NOW+3600)
    assert snapshot==g.weekly_snapshot(NOW+2000)
    assert len(snapshot['navs'])==2
    g.close()


def test_invalid_model_orders():
    for obj in [reply('buy',True),reply('buy',-3),reply('buy',10,'UNKNOWN'),{'action':'hold'}]:
        with pytest.raises(ValueError):decision(json.dumps(obj),['AAPL'])


def test_model_uses_supported_thinking_and_rejects_truncation():
    import httpx
    async def scenario():
        calls=[]
        def handler(request):
            body=json.loads(request.content);calls.append(body)
            # The deployed Gemini rejects MINIMAL; preserve a valid bounded request.
            if body['reasoning_effort']!='low':return httpx.Response(400)
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop' if len(calls)==1 else 'length',
                'message':{'content':json.dumps(reply())}}]})
        m=Model({'gateway_url':'https://example.test/stocks','gateway_token':'test','model':'test-gemini-model'},httpx.MockTransport(handler))
        assert json.loads(await m.generate('persona',{}))['action']=='hold'
        with pytest.raises(ValueError):await m.generate('persona',{})
        assert all(c['max_tokens']==1024 and 'tools' not in c for c in calls)
    asyncio.run(scenario())


def test_control_api_auth_and_pause(tmp_path):
    async def scenario():
        async def cb():return ''
        s=Service(tmp_path,{},cb,cb)
        async with TestClient(TestServer(control_app(s,'x'*32))) as client:
            assert (await client.get('/status')).status==401
            response=await client.get('/status',headers={'Authorization':'Bearer '+'x'*32})
            assert response.status==200 and (await response.json())['ready'] is False
            response=await client.post('/control',headers={'Authorization':'Bearer '+'x'*32},json={'action':'start'})
            assert response.status==400 and s.game.state() is None
        s.game.close()
    asyncio.run(scenario())


def test_budget_refusal_freezes_and_defers_without_double_nav(tmp_path):
    async def scenario():
        class Fake:
            calls=0
            async def status(self):return {'route':{'limit_nusd':200000000,'reserved_nusd':0,'settled_nusd':0}}
            async def generate(self,*a,**kw):
                self.calls+=1
                if self.calls==1:raise BudgetUnavailable()
                return json.dumps(reply())
        async def persona():return 'Neuro'
        async def stickers():return {'happy':'dummy.png'}
        wall=[NOW]
        model=Fake();s=Service(tmp_path,{},persona,stickers,model=model,clock=lambda:wall[0])
        s.history=History(bundle());s.game.start(s.history,NOW-3600);s.game.toggle(True,NOW-1800)
        await s.tick();assert s.game.state()['step']==0
        wall[0]+=1800
        await s.tick();assert s.game.state()['step']==1 and model.calls==2
        assert s.game.db.execute("SELECT COUNT(*) FROM events WHERE kind='nav'").fetchone()[0]==1
        s.game.close()
    asyncio.run(scenario())


def test_report_charts_html_escaping_and_fixed_images(tmp_path):
    g,h=active(tmp_path);tick(g,h,NOW,reply('buy'));tick(g,h,NOW+1800)
    snapshot=g.weekly_snapshot(NOW+2000)
    snapshot['notes'][0]['news']=['Visible headline']
    snapshot['notes'][0]['news_citations']=[{'id':'n1','original':'Visible headline','zh':'当时的中文资讯'}]
    snapshot['notes'][0]['news_translations']=[{'source':'Future headline','zh':'不应展示的内容'}]
    from astrbot_plugin_neuro_stocks.publish import chart
    sticker=tmp_path/'sticker.png';sticker.write_bytes(chart(snapshot))
    raw=json.dumps({'title':'离线演示：Neuro 试营业','entries':[{'label':'复盘','text':'<script>alert(1)</script>','emotion':'happy'}]*3})
    p=Publisher(tmp_path/'public');post=p.build(snapshot,raw,{'happy':sticker},demo=True);p.index()
    content=(tmp_path/'public'/post['key']/'index.html').read_text(encoding='utf-8')
    assert '<script>' not in content and '&lt;script&gt;' in content
    assert content.count('class="sticker"')==3+len(snapshot['notes'])
    assert '风险：' not in content and '后续条件：' not in content
    assert '<details>' in content and '这一周，neuro玩炒股游戏' in content
    assert '独立测试' in content
    assert '当时的中文资讯' in content and '查看英文原文' in content and 'Visible headline' in content
    assert '不应展示的内容' not in content
    assert (tmp_path/'public'/post['key']/'nav.png').stat().st_size>1000
    g.close()


def test_full_replay_weekly_publish_and_no_second_paid_summary(tmp_path):
    async def scenario():
        from PIL import Image
        sticker=tmp_path/'happy.png';Image.new('RGB',(60,60),'pink').save(sticker)
        wall=[NOW];calls=[]
        class Fake:
            async def status(self):return {'route':{'limit_nusd':200000000,'settled_nusd':1000,'reserved_nusd':0}}
            async def generate(self,system,payload,maximum=1024):
                calls.append((maximum,payload))
                if maximum==2048:
                    return json.dumps({'title':'离线赛季小结','entries':[{'label':'复盘','text':'这次我记住了。','emotion':'happy'}]*3})
                return json.dumps(reply('buy',10) if len(calls)==1 else reply())
        async def persona():return 'Neuro'
        async def stickers():return {'happy':sticker}
        s=Service(tmp_path,{},persona,stickers,model=Fake(),clock=lambda:wall[0]);s.history=History(bundle(days=2))
        s.game.start(s.history,NOW-3600);s.game.toggle(True,NOW-1800)
        for day in range(2):
            for step in range(14):
                wall[0]=NOW+day*86400+step*1800;await s.tick()
        assert s.game.state()['status']=='finished'
        assert len(calls)==26
        wall[0]=int(datetime(2026,11,11,21,tzinfo=TZ).timestamp());await s.tick();await s.tick()
        assert len(calls)==27
        assert s.game.db.execute("SELECT COUNT(*) FROM weeks WHERE state='published'").fetchone()[0]==1
        assert (tmp_path/'public'/'index.html').exists()
        s.game.close()
    asyncio.run(scenario())


def test_weekly_pause_during_generation_defers_publication(tmp_path):
    async def scenario():
        from PIL import Image
        sticker=tmp_path/'happy.png';Image.new('RGB',(20,20),'pink').save(sticker)
        wall=[NOW]
        class Fake:
            calls=0
            async def status(self):return {'route':{'limit_nusd':200000000,'settled_nusd':0,'reserved_nusd':0}}
            async def generate(self,system,payload,maximum=1024):
                self.calls+=1
                if maximum==2048:
                    s.game.toggle(False,wall[0])
                    return json.dumps({'title':'暂停前的周记','entries':[{'label':'复盘','text':'已保存。','emotion':'happy'}]*3})
                return json.dumps(reply())
        async def persona():return 'Neuro'
        async def stickers():return {'happy':sticker}
        model=Fake();s=Service(tmp_path,{},persona,stickers,model=model,clock=lambda:wall[0]);s.history=History(bundle())
        s.game.start(s.history,NOW-3600);s.game.toggle(True,NOW-1800);await s.tick()
        wall[0]=int(datetime(2026,11,11,21,tzinfo=TZ).timestamp());await s.tick()
        assert not (tmp_path/'public'/'index.html').exists()
        assert model.calls==2
        s.game.toggle(True,wall[0]);wall[0]+=901;await s.tick()
        assert model.calls==2 and (tmp_path/'public'/'index.html').exists()
        s.game.close()
    asyncio.run(scenario())


def test_import_pagination_and_real_calendar():
    import httpx
    import exchange_calendars as xcals
    from astrbot_plugin_neuro_stocks.import_history import pages,month_before
    from datetime import date
    assert month_before(date(2024,3,31))==date(2024,2,29)
    cal=xcals.get_calendar('XNYS')
    assert len(cal.sessions_in_range('2026-11-26','2026-11-26'))==0 # Thanksgiving
    assert (cal.session_close('2026-11-27')-cal.session_open('2026-11-27')).total_seconds()==210*60
    calls=[]
    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200,json={'news':[{'id':len(calls)}],'next_page_token':'next' if len(calls)==1 else None})
    with httpx.Client(base_url='https://example.test',transport=httpx.MockTransport(handler)) as client:
        result=pages(client,'/news',{'start':'old','end':'cutoff'},'news')
    assert len(result)==2 and calls[1]['page_token']=='next' and calls[1]['end']=='cutoff'
