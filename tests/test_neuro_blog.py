import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from astrbot_plugin_neuro_blog.core import TZ, Ledger, Publisher, cutoff, validate_reply


def row(key, born, group='12345', content='小明喜欢画画'):
    return dict(memory_id=key,timestamp=born.isoformat(),group_id=group,content=content,metadata=json.dumps({'user_name':'小明'}))


def test_cutoff_includes_late_sunday_next_time():
    sunday=datetime(2026,10,25,21,tzinfo=TZ)
    assert cutoff(sunday)==sunday
    assert cutoff(sunday-timedelta(seconds=1))==sunday-timedelta(days=7)
    assert cutoff(sunday+timedelta(days=6))==sunday


def test_baseline_edit_private_and_old_import(tmp_path):
    start=datetime(2026,10,24,19,tzinfo=TZ)
    ledger=Ledger(tmp_path/'ledger.db')
    ledger.observe([row('old',start-timedelta(days=1))],start)
    later=start+timedelta(minutes=1)
    rows=[row('old',later),row('new',later),row('private',later,''),row('sensitive',later,content='密码是test'),row('import',start-timedelta(days=30))]
    ledger.observe(rows,later)
    assert [r['id'] for r in ledger.select(rows,datetime(2026,10,25,21,tzinfo=TZ))]==['new','private']
    ledger.db.close()
    reopened=Ledger(tmp_path/'ledger.db')
    reopened.observe(rows,later+timedelta(hours=1))
    assert [r['id'] for r in reopened.select(rows,datetime(2026,10,25,21,tzinfo=TZ))]==['new','private']


def test_deleted_memories_not_reused_and_next_week(tmp_path):
    ledger=Ledger(tmp_path/'ledger.db')
    now=datetime(2026,10,24,19,tzinfo=TZ)
    ledger.observe([],now)
    born=now+timedelta(minutes=1)
    ledger.observe([row('a',born)],born)
    assert not ledger.select([],datetime(2026,10,25,21,tzinfo=TZ))
    assert not ledger.select([row('a',born)],datetime(2026,11,1,21,tzinfo=TZ))


def test_validation_no_missing_or_unknown_emotion():
    records=[{'id':'1','source':'小明'}]
    item={'id':'1','publish':True,'label':'画画','memory':'小明喜欢画画','text':'给我画一张！','emotion':'开心'}
    assert validate_reply(json.dumps({'entries':[item]}),records,['开心'])[0]['source']=='小明'
    assert validate_reply('{"entries":[{"id":"1","publish":false}]}',records,['开心'])==[]
    with pytest.raises(ValueError): validate_reply('{"entries":[]}',records,['开心'])
    with pytest.raises(ValueError): validate_reply(json.dumps({'entries':[item,item]}),records,['开心'])
    with pytest.raises(ValueError): validate_reply(json.dumps({'entries':[item]}),records,['生气'])


def test_static_publish_escapes_and_no_source_ids(tmp_path):
    pub=Publisher(tmp_path,Path(__file__).parents[1]/'templates')
    pub.publish([])
    assert '还没有周记' in (tmp_path/'index.html').read_text(encoding='utf8')
    post={'date':'2026-10-25','title':'test','entries':[{'label':'<script>bad</script>','memory':'画画','text':'不错','emotion':'开心','source':'','image':'/neuro-blog/assets/test.png'}]}
    pub.publish([post])
    page=(tmp_path/'2026-10-25/index.html').read_text(encoding='utf8')
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert 'class="source"' not in page
    assert 'noindex,nofollow,noarchive' in page
    assert '/neuro-blog/2026-10-25/' in (tmp_path/'index.html').read_text(encoding='utf8')


def test_unified_feed_keeps_both_kinds_and_excludes_tests(tmp_path):
    pub=Publisher(tmp_path,Path(__file__).parents[1]/'templates')
    stock=tmp_path/'stocks'/'season-week';stock.mkdir(parents=True)
    manifest={'date':'2026-11-11','kind':'stocks','title':'这一周，neuro玩炒股游戏','url':'/neuro-blog/stocks/season-week/'}
    (stock/'post.json').write_text(json.dumps(manifest),encoding='utf-8')
    test=tmp_path/'stocks'/'tests'/'demo';test.mkdir(parents=True)
    (test/'post.json').write_text(json.dumps({**manifest,'title':'should-not-appear'}))
    pub.publish([{'date':'2026-11-08','title':'记忆','entries':[]}])
    page=(tmp_path/'index.html').read_text(encoding='utf-8')
    assert '这一周，neuro玩炒股游戏' in page and '记忆' in page
    assert 'should-not-appear' not in page and '周末来信' not in page
    assert '<span>✦</span><span>✦</span><span>✦</span>' in page
    pub.publish([{'date':'2026-11-08','title':'记忆','entries':[]}])
    assert 'season-week/' in (tmp_path/'index.html').read_text(encoding='utf-8')
