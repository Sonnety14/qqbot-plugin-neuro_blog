"""SQLite admission ledger. Monetary unit: USD / 10**9 (integer nanodollars)."""
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MESSAGE = 'neuro困啦！明天再试试找neuro玩吧！'
LIMIT = 1_000_000_000
TZ = ZoneInfo('Asia/Shanghai')

class Denied(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(MESSAGE)

class Ledger:
    def __init__(self, path, clock=time.time, limit=LIMIT):
        self.path, self.clock, self.limit = str(path), clock, limit
        with closing(self.connect()) as c:
            c.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, reason TEXT);
              CREATE TABLE IF NOT EXISTS attempts(
                id TEXT PRIMARY KEY, day TEXT NOT NULL, route TEXT NOT NULL,
                model TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER,
                state TEXT NOT NULL, created REAL NOT NULL, completed REAL,
                usage TEXT, price_version TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS attempt_day ON attempts(day);
            ''')

    def connect(self):
        c=sqlite3.connect(self.path, timeout=30)
        c.row_factory=sqlite3.Row
        return c

    @contextmanager
    def transaction(self):
        c=self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()

    def day(self):return datetime.fromtimestamp(self.clock(),TZ).date().isoformat()

    def _status(self,c):
        day=self.day()
        c.execute('INSERT OR IGNORE INTO days(day) VALUES (?)',(day,))
        reason=c.execute('SELECT reason FROM days WHERE day=?',(day,)).fetchone()[0]
        settled=c.execute("SELECT COALESCE(SUM(actual),0) FROM attempts WHERE day=? AND state='settled'",(day,)).fetchone()[0]
        # Pending/uncertain attempts from earlier days remain liabilities, never TTL-refunded.
        held=c.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts WHERE state!='settled'").fetchone()[0]
        if settled+held>=self.limit and not reason:
            # In-flight contention is not a permanent lock. Settled exhaustion is.
            if settled>=self.limit:
                reason='exhausted';c.execute('UPDATE days SET reason=? WHERE day=?',(reason,day))
        reset=(datetime.fromisoformat(day).replace(tzinfo=TZ)+timedelta(days=1)).isoformat()
        return dict(day=day, sleeping=bool(reason) or settled+held>=self.limit,
                    reason=reason or ('reserved' if settled+held>=self.limit else None),
                    settled_nusd=settled, reserved_nusd=held, limit_nusd=self.limit,
                    reset_at=reset,message=MESSAGE)

    def status(self):
        with self.transaction() as c:return self._status(c)

    def lock(self,reason):
        with self.transaction() as c:
            self._status(c);c.execute('UPDATE days SET reason=? WHERE day=?',(reason,self.day()))

    def route_status(self, route, limit):
        with self.transaction() as c:
            return self._route_status(c, route, limit)

    def _route_status(self, c, route, limit):
        settled = c.execute("SELECT COALESCE(SUM(actual),0) FROM attempts WHERE route=? AND day=? AND state='settled'", (route,self.day())).fetchone()[0]
        held = c.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts WHERE route=? AND state!='settled'", (route,)).fetchone()[0]
        return dict(settled_nusd=settled, reserved_nusd=held, limit_nusd=limit)

    def reserve(self,amount,route,model,price_version,route_limit=None):
        if not isinstance(amount,int) or amount<0:raise ValueError('amount')
        if route_limit is not None and (type(route_limit) is not int or route_limit<=0):raise ValueError('route_limit')
        denied=None;attempt=None
        with self.transaction() as c:
            s=self._status(c)
            sub = self._route_status(c,route,route_limit) if route_limit is not None else None
            if s['sleeping']:denied=s['reason']
            elif sub and sub['settled_nusd']+sub['reserved_nusd']+amount>route_limit:
                denied='route_budget_exhausted'
            elif s['settled_nusd']+s['reserved_nusd']+amount>self.limit:
                denied='reserved' if s['reserved_nusd'] else 'insufficient_for_request'
                if not s['reserved_nusd']:
                    c.execute('UPDATE days SET reason=? WHERE day=?',(denied,s['day']))
            else:
                attempt=uuid.uuid4().hex
                c.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,?,?,NULL,NULL,?)',
                          (attempt,s['day'],route,model,amount,'pending',self.clock(),price_version))
        if denied:raise Denied(denied)
        return attempt

    def settle(self,attempt,actual,usage):
        if type(actual) is not int or actual<0:raise ValueError('actual')
        with self.transaction() as c:
            row=c.execute('SELECT * FROM attempts WHERE id=?',(attempt,)).fetchone()
            if row is None:raise ValueError('unknown attempt')
            if row['state']=='settled':
                if row['actual']!=actual:raise ValueError('conflicting settlement')
                return
            c.execute("UPDATE attempts SET actual=?,state='settled',completed=?,usage=? WHERE id=?",(actual,self.clock(),usage,attempt))
            if actual>row['reserved']:
                self._status(c);c.execute('UPDATE days SET reason=? WHERE day=?',('cost_bound_violation',self.day()))

    def uncertain(self,attempt,reason):
        with self.transaction() as c:
            c.execute("UPDATE attempts SET state='uncertain',completed=? WHERE id=? AND state='pending'",(self.clock(),attempt))
            self._status(c);c.execute('UPDATE days SET reason=? WHERE day=?',(reason,self.day()))
