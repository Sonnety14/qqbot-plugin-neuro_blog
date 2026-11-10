import json
from astrbot_plugin_neuro_stocks.core import decision,note_context
from tests.test_neuro_stocks import reply

NEWS=[{'id':'msft','headline':'Microsoft rises'}, {'id':'other','headline':'Unrelated latest headline'}]


def test_only_explicit_visible_citations_are_published():
    obj={**reply('buy',symbol='MSFT'),'news_ids':['missing','msft','msft'],
         'context_text':'微软上涨，因此试着买一点。',
         'news_translations':[{'source':'Microsoft rises','zh':'微软上涨'}]}
    parsed=decision(json.dumps(obj),['MSFT'],NEWS)
    assert parsed['news_ids']==['msft']
    note=note_context(parsed,{'news':NEWS})
    assert note['news_citations']==[{'id':'msft','original':'Microsoft rises','zh':'微软上涨'}]
    assert 'Unrelated' not in json.dumps(note)


def test_legacy_price_reason_does_not_gain_incidental_news():
    obj={**reply('buy',symbol='MSFT'),'reason':'MSFT站上525后涨至527附近；开仓没有超额。'}
    note=note_context(obj,{'news':NEWS})
    assert note=={'context_text':'MSFT站上525后涨至527附近','news_citations':[]}
    parsed=decision(json.dumps({**obj,'news_ids':['not-visible']}),['MSFT'],NEWS)
    assert parsed['action']=='buy' and note_context(parsed,{'news':NEWS})['news_citations']==[]
