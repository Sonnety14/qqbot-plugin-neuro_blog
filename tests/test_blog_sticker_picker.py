import json
from astrbot_plugin_neuro_blog.sticker_picker import StickerPicker
from astrbot_plugin_neuro_stocks.core import decision
from tests.test_neuro_stocks import reply


def test_shuffle_bag_exhaustion_boundary_and_stable_rebuild(tmp_path):
    pools={'盯':[tmp_path/f'盯{i}.png' for i in range(1,8)]}
    picker=StickerPicker(pools,'week1')
    drawn=[picker.select('盯') for _ in range(28)]
    assert all(len(set(drawn[i:i+7]))==7 for i in range(0,28,7))
    assert all(a!=b for a,b in zip(drawn,drawn[1:]))
    rebuilt=StickerPicker(pools,'week1')
    assert drawn==[rebuilt.select('盯') for _ in drawn]
    assert StickerPicker({'one':tmp_path/'one.png'},'x').select('one')==tmp_path/'one.png'


def test_invalid_translation_does_not_cancel_trade():
    obj=reply('buy');obj['news_translations']=[{'source':'News','zh':'中文译文'}, {'source':'Bad','zh':None}]
    parsed=decision(json.dumps(obj),['AAPL'])
    assert parsed['action']=='buy' and parsed['news_translations']==[{'source':'News','zh':'中文译文'}]
    obj['news_translations']='bad'
    assert decision(json.dumps(obj),['AAPL'])['action']=='buy'
