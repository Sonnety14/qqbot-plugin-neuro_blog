import asyncio,importlib,json,logging,sys,types
from pathlib import Path
import httpx
import pytest
from budget_gateway.app import create_app
from budget_gateway.ledger import Ledger
from astrbot_plugin_neuro_blog.resources import load_stickers

ROOT=Path(__file__).resolve().parents[1]


def test_resources_exclude_disabled_and_outside_files(tmp_path):
    root=tmp_path/'images';root.mkdir();(root/'happy1.png').write_bytes(b'test')
    (tmp_path/'outside.png').write_bytes(b'test')
    index=tmp_path/'index.json';index.write_text(json.dumps([
        {'path':'happy1.png','enabled':True},{'path':'../outside.png','enabled':True},
        {'path':'happy2.png','enabled':False}]))
    bank=load_stickers({'sticker_root':str(root),'sticker_index':str(index)})
    assert bank.emotions==['happy'] and bank.select('happy')==root/'happy1.png'


def test_default_plugins_initialize_disabled_without_private_neuro(tmp_path,monkeypatch):
    api=types.ModuleType('astrbot.api');api.logger=logging.getLogger('test')
    star=types.ModuleType('astrbot.api.star')
    class Star:
        def __init__(self,context):self.context=context
    class StarTools:
        @staticmethod
        def get_data_dir(name):
            path=tmp_path/name;path.mkdir(exist_ok=True);return path
    star.Star=Star;star.StarTools=StarTools;star.register=lambda *args:lambda cls:cls
    for name,module in [('astrbot',types.ModuleType('astrbot')),('astrbot.api',api),('astrbot.api.star',star)]:monkeypatch.setitem(sys.modules,name,module)
    async def run():
        blog=importlib.import_module('astrbot_plugin_neuro_blog.main').NeuroBlog(object(),{})
        await blog.initialize();assert blog.model is None;await blog.terminate()
        stocks=importlib.import_module('astrbot_plugin_neuro_stocks.main').NeuroStocks(object(),{})
        await stocks.initialize();assert stocks.service.game.state() is None;await stocks.terminate()
    asyncio.run(run())


def config(tmp_path):
    return {'database':str(tmp_path/'chat.db'),'blog_database':str(tmp_path/'blog.db'),
      'pricing':{'model':'test-gemini','version':'fixture-not-real-price','valid_from':'2026-01-01','valid_until':'2027-01-01',
                 'input_nusd_per_token':1,'output_nusd_per_token':2,'cached_nusd_per_token':1},
      'routes':{'blog':{'kind':'gemini','key':'fake-upstream','token':'fake-route','budget_scope':'blog','daily_limit_nusd':5_000_000_000}}}


def test_configured_gateway_isolated_accounting(tmp_path):
    async def run():
        cfg=config(tmp_path);book=Ledger(cfg['database'],clock=lambda:1794330000)
        held=book.reserve(1_000_000_000,'chat','test','v');book.settle(held,1_000_000_000,'{}')
        def upstream(req):
            assert 'test-gemini' in req.url.path
            if req.url.path.endswith(':countTokens'):return httpx.Response(200,json={'totalTokens':10})
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'{}'}]},'finishReason':'STOP'}],
                'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':2,'totalTokenCount':12}})
        app=create_app(cfg,book,httpx.MockTransport(upstream));app.state.blog_ledger.clock=lambda:1794330000
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://fixture') as c:
            headers={'Authorization':'Bearer fake-route'}
            r=await c.post('/blog/chat/completions',headers=headers,json={'model':'test-gemini','messages':[{'role':'user','content':'test'}]})
            assert r.status_code==200
            status=(await c.get('/blog/status',headers=headers)).json()
            assert status['budget_scope']=='blog' and status['settled_nusd']==14
            assert book.status()['settled_nusd']==1_000_000_000
            assert (await c.get('/blog/status')).status_code==401
    asyncio.run(run())


def test_missing_pricing_and_uncounted_provider_refused(tmp_path):
    with pytest.raises(KeyError):create_app({'database':str(tmp_path/'a.db'),'routes':{}})
    cfg=config(tmp_path);cfg['routes']['blog']['kind']='uncounted'
    with pytest.raises(ValueError):create_app(cfg)
