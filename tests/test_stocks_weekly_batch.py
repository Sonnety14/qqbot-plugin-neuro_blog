import asyncio,json
from datetime import datetime
import pytest
from PIL import Image
from tests.test_neuro_stocks import bundle,reply,TZ,NOW
from astrbot_plugin_neuro_stocks.data import History
from astrbot_plugin_neuro_stocks.service import Service

WED=datetime(2026,11,11,21,tzinfo=TZ).timestamp()

def setup(tmp_path,wall,model,days=10):
    image=tmp_path/'happy.png';Image.new('RGB',(20,20),'pink').save(image)
    async def persona():return 'Neuro'
    async def stickers():return {'happy':image}
    s=Service(tmp_path,{'schedule_mode':'weekly_batch'},persona,stickers,model=model,clock=lambda:wall[0])
    s.history=History(bundle(days=days))
    if not s.game.state():s.game.start(s.history,NOW);s.game.toggle(True,NOW)
    return s

class Fake:
    def __init__(self):self.calls=[];self.hook=None
    async def status(self):return {'route':{'limit_nusd':5000000000,'reserved_nusd':0,'settled_nusd':0}}
    async def generate(self,system,payload,maximum=1024):
        self.calls.append((maximum,payload))
        if self.hook:self.hook(self)
        if maximum==2048:return json.dumps({'title':'小冒险','entries':[{'label':'本周','text':'看看小金库','emotion':'happy'}]*3})
        return json.dumps(reply('buy') if len(self.calls)==1 else reply())

def test_only_wednesday_runs_five_sessions_and_no_duplicate(tmp_path):
    async def run():
        wall=[NOW];model=Fake();s=setup(tmp_path,wall,model)
        await s.tick();assert not model.calls
        wall[0]=WED;await s.tick()
        assert len(model.calls)==66 and s.game.state()['session']==5
        assert len(model.calls[-2][1]['previous'])==64
        assert 'average_cost' in model.calls[2][1]['account']['positions']['AAPL']
        assert model.calls[-1][1]['performance']['fills']==1
        await s.tick();wall[0]+=86400;await s.tick();assert len(model.calls)==66
        wall[0]=WED+7*86400;await s.tick()
        assert len(model.calls)==132 and s.game.state()['status']=='finished'
        assert s.game.db.execute("SELECT COUNT(*) FROM batches WHERE state='published'").fetchone()[0]==2
        assert s.game.can_start()
        s.game.close()
    asyncio.run(run())

def test_pause_restart_resumes_without_repaying_slots(tmp_path):
    async def run():
        wall=[WED];model=Fake();s=setup(tmp_path,wall,model,5)
        model.hook=lambda m:s.game.toggle(False,wall[0]) if len(m.calls)==4 else None
        await s.tick();assert len(model.calls)==4 and s.game.state()['status']=='paused'
        s.game.close();model.hook=None
        s=setup(tmp_path,wall,model,5);s.control('resume');await s.tick()
        assert len(model.calls)==66 and s.game.state()['status']=='finished'
        times=[p['at'] for maximum,p in model.calls if maximum==1536]
        assert len(set(times))==65
        s.game.close()
    asyncio.run(run())
