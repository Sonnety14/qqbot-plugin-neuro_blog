"""Immutable, reviewed historical bundle. No model access to future rows."""
import hashlib
import copy
import json
import math
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = ('AAPL','MSFT','AMZN','GOOGL','META','NVDA','TSLA','AMD','INTC','JPM','V','WMT','KO','DIS','XOM','SPY')


class DataUnavailable(ValueError):
    pass


def stamp(value):
    if not isinstance(value, str):
        raise DataUnavailable('timestamp must be ISO text')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise DataUnavailable('timezone required')
    return int(dt.timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class History:
    def __init__(self, payload):
        payload = copy.deepcopy(payload)
        self.payload = payload
        self.digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',',':')).encode()).hexdigest()
        self.symbols = tuple(payload['symbols'])
        if len(set(self.symbols)) != len(self.symbols) or not set(self.symbols) <= set(SYMBOLS) or 'SPY' not in self.symbols:
            raise DataUnavailable('invalid fixed universe')
        self.sessions = payload['sessions']
        if not 1 <= len(self.sessions) <= 20:
            raise DataUnavailable('invalid season length')
        prev = 0
        for s in self.sessions:
            s['open'] = stamp(s['open']) if isinstance(s['open'],str) else s['open']
            s['close'] = stamp(s['close']) if isinstance(s['close'],str) else s['close']
            if s['open'] <= prev or not 0 < s['close']-s['open'] <= 23400 or (s['close']-s['open']) % 1800:
                raise DataUnavailable('invalid market calendar')
            prev = s['close']
        review = payload.get('corporate_actions_review',{})
        if review.get('complete') is not True or stamp(review['through']) < self.sessions[-1]['close']:
            raise DataUnavailable('corporate actions must be reviewed through season end')
        self.bars, self.times = {}, {}
        for symbol in self.symbols:
            rows = sorted(payload['bars'].get(symbol,[]), key=lambda b: stamp(b['t']))
            times = [stamp(b['t']) for b in rows]
            if len(times) != len(set(times)):
                raise DataUnavailable('duplicate bars')
            for row in rows:
                if any(type(row[k]) not in (int,float) or not math.isfinite(row[k]) or row[k] <= 0 for k in ('o','h','l','c')):
                    raise DataUnavailable('invalid price')
                if not row['l'] <= min(row['o'],row['c']) <= max(row['o'],row['c']) <= row['h']:
                    raise DataUnavailable('invalid OHLC')
            self.bars[symbol], self.times[symbol] = rows, times
        self.news = []
        for n in payload.get('news',[]):
            self.news.append({**n,'created':stamp(n['created_at']),'updated':stamp(n['updated_at'])})
        self.actions = payload.get('actions',[])
        seen = set()
        for a in self.actions:
            if a['id'] in seen or a['symbol'] not in self.symbols or a['type'] not in ('split','dividend'):
                raise DataUnavailable('unsupported corporate action')
            seen.add(a['id'])
            a['at'] = stamp(a['effective_at'])
            if a['type'] == 'split':
                if type(a['numerator']) is not int or type(a['denominator']) is not int or min(a['numerator'],a['denominator'])<=0:
                    raise DataUnavailable('invalid split')
            else:
                a['pay'] = stamp(a['pay_at'])
                if a['pay'] < a['at'] or not math.isfinite(a['cash']) or a['cash']<=0:
                    raise DataUnavailable('invalid dividend')

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    def prices(self, at, session):
        result = {}
        for symbol in self.symbols:
            if at==session['open']:
                # The opening print is known at T; never expose that minute's H/L/C.
                result[symbol]=self.opening(symbol,at)
                continue
            i = bisect_right(self.times[symbol], at-60)-1
            if i < 0:
                raise DataUnavailable('missing previous completed price: '+symbol)
            last = self.times[symbol][i]
            # At the open, prior-session close is expected; inside a session reject stale quotes.
            age = at-last
            limit = 7*86400 if at == session['open'] else 360
            if age > limit:
                raise DataUnavailable('stale price: '+symbol)
            result[symbol] = self.bars[symbol][i]['c']
        return result

    def opening(self, symbol, at):
        i = bisect_right(self.times[symbol],at)-1
        if i < 0 or self.times[symbol][i] != at:
            raise DataUnavailable('missing execution minute')
        return self.bars[symbol][i]['o']

    def snapshot(self, at, session):
        prices = self.prices(at,session)
        recent = {}
        for sym in self.symbols:
            end = bisect_right(self.times[sym], at-60)
            recent[sym] = [{'at':iso(self.times[sym][i]),'close':self.bars[sym][i]['c']}
                           for i in range(max(0,end-180),end,30)]
        news = sorted((n for n in self.news if at-7*86400 <= n['created'] <= at and n['created'] <= n['updated'] <= at),
                      key=lambda n:n['created'])[-8:]
        return {'at':iso(at), 'prices':prices, 'recent':recent,
                'news':[{'id':str(n['id']), 'published':iso(n['created']), 'headline':n.get('headline','')[:200],
                         'summary':n.get('summary','')[:600]} for n in news]}
