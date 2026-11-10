"""CLI: import Alpaca data without brokerage order permissions or any LLM call."""
import argparse
import calendar
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from .data import History, SYMBOLS, stamp


def month_before(day):
    year,month=(day.year-1,12) if day.month==1 else (day.year,day.month-1)
    return date(year,month,min(day.day,calendar.monthrange(year,month)[1]))


def pages(client,path,params,key):
    items=[];tokens=set()
    for _ in range(1000):
        result=client.get(path,params=params)
        result.raise_for_status()
        obj=result.json();items.append(obj.get(key,{} if key=='bars' else []))
        token=obj.get('next_page_token')
        if not token:return items
        if token in tokens:raise ValueError('repeated pagination token')
        tokens.add(token);params={**params,'page_token':token}
    raise ValueError('pagination exceeds safety limit; narrow import window')


def run(args):
    import exchange_calendars as xcals
    cal=xcals.get_calendar('XNYS')
    start=date.fromisoformat(args.start) if args.start else month_before(datetime.now(ZoneInfo('Asia/Shanghai')).date())
    schedule=cal.sessions_in_range(start.isoformat(),(start+timedelta(days=50)).isoformat())[:20]
    sessions=[{'open':cal.session_open(d).isoformat(),'close':cal.session_close(d).isoformat()} for d in schedule]
    if not sessions or stamp(sessions[-1]['close'])>=datetime.now(timezone.utc).timestamp()-900:
        raise ValueError('season not fully historical; choose an earlier start')
    if not args.actions.exists():
        raise ValueError('supply reviewed corporate actions file; see README')
    action_doc=json.loads(args.actions.read_text(encoding='utf-8'))
    begin=(datetime.fromisoformat(sessions[0]['open'])-timedelta(days=7)).isoformat()
    end=sessions[-1]['close']
    key=os.environ['APCA_API_KEY_ID'];secret=os.environ['APCA_API_SECRET_KEY']
    with httpx.Client(base_url='https://data.alpaca.markets',timeout=60,follow_redirects=False,
                      headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret}) as client:
        bars={s:[] for s in SYMBOLS}
        for chunk in pages(client,'/v2/stocks/bars',{'symbols':','.join(SYMBOLS),'start':begin,'end':end,
                           'timeframe':'1Min','feed':args.feed,'adjustment':'raw','asof':'-', 'limit':10000},'bars'):
            for sym,rows in chunk.items():
                if sym in bars:bars[sym].extend(rows)
        news=[]
        for chunk in pages(client,'/v1beta1/news',{'symbols':','.join(SYMBOLS),'start':begin,'end':end,
                           'limit':50,'include_content':'false','sort':'asc'},'news'):
            news.extend({k:n[k] for k in ('id','created_at','updated_at','headline','summary','symbols') if k in n} for n in chunk)
    # Keep only regular-session bars, including warmup sessions.
    warm=cal.sessions_in_range(begin[:10],end[:10])
    intervals=[(int(cal.session_open(d).timestamp()),int(cal.session_close(d).timestamp())) for d in warm]
    bars={s:[b for b in rows if any(o<=stamp(b['t'])<c for o,c in intervals)] for s,rows in bars.items()}
    bundle={'symbols':list(SYMBOLS),'sessions':sessions,'bars':bars,'news':news,
            'actions':action_doc['actions'],'corporate_actions_review':action_doc['review'],
            'source':{'provider':'alpaca','feed':args.feed,'adjustment':'raw','imported':datetime.now(timezone.utc).isoformat()}}
    history=History(bundle)
    # Verify every decision mark and closing mark before accepting the bundle.
    for s in history.sessions:
        for at in range(s['open'],s['close']+1,1800):history.prices(at,s)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temp=args.output.with_suffix('.tmp')
    temp.write_text(json.dumps(bundle,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    temp.replace(args.output)
    print(json.dumps({'ok':True,'sessions':len(sessions),'bars':sum(map(len,bars.values())),
                      'news':len(news),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start',help='first historical session on/after YYYY-MM-DD; default one calendar month ago')
    p.add_argument('--feed',choices=('iex','sip'),default='iex')
    p.add_argument('--actions',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
