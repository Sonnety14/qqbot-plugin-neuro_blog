"""Weekly cutoffs, read-only memory selection and atomic static publication."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
SENSITIVE = re.compile(r'密码|口令|cookie|token|api.?key|身份证|手机号|银行卡|家庭住址|宿舍号|病历|诊断|性取向|政治观点', re.I)


def cutoff(now):
    now = now.astimezone(TZ)
    end = (now - timedelta(days=(now.weekday() - 6) % 7)).replace(hour=21, minute=0, second=0, microsecond=0)
    return end if end <= now else end - timedelta(days=7)


def clean(text):
    text = re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '', str(text))
    text = re.sub(r'\[CQ:[^]]*\]', '', text)
    return re.sub(r'\d{5,}', '（号码已隐藏）', text).strip()


def read_memories(path, persona):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('SELECT memory_id,content,metadata,group_id,timestamp FROM memories WHERE persona_id=?', (persona,))]


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value REAL);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY,born REAL,baseline INTEGER);
        CREATE TABLE IF NOT EXISTS weeks(id TEXT PRIMARY KEY,state TEXT,attempt REAL,payload TEXT);
        CREATE TABLE IF NOT EXISTS generations(week TEXT,part INTEGER,state TEXT,body TEXT,PRIMARY KEY(week,part));''')

    def observe(self, rows, now):
        start = self.db.execute("SELECT value FROM settings WHERE key='start'").fetchone()
        first = start is None
        with self.db:
            if first: self.db.execute("INSERT INTO settings VALUES ('start',?)", (now.timestamp(),))
            for r in rows:
                try:
                    born = datetime.fromisoformat(r['timestamp'])
                    if born.tzinfo is None: born = born.replace(tzinfo=TZ)
                    stamp = born.timestamp()
                except (TypeError, ValueError): stamp = 0
                baseline = first or stamp < (start[0] if start else now.timestamp()) or stamp > now.timestamp() + 300
                self.db.execute('INSERT OR IGNORE INTO seen VALUES (?,?,?)', (r['memory_id'], stamp, int(baseline)))

    def select(self, rows, end):
        begin = (end - timedelta(days=7)).timestamp()
        ids = {r[0] for r in self.db.execute('SELECT id FROM seen WHERE baseline=0 AND born>=? AND born<?', (begin,end.timestamp()))}
        result = []
        for row in rows:
            if row['memory_id'] not in ids: continue
            # Both private and group memories are eligible; never publish source IDs.
            if SENSITIVE.search(row['content']): continue
            try: meta = json.loads(row['metadata'])
            except (ValueError, TypeError): continue
            name = clean(meta.get('user_name') or '')[:60]
            if SENSITIVE.search(name): name = ''
            result.append({'id':row['memory_id'], 'memory':clean(row['content'])[:3000], 'source':name})
        return sorted(result, key=lambda r:r['id'])

    def state(self, key):
        return self.db.execute('SELECT state,attempt,payload FROM weeks WHERE id=?',(key,)).fetchone()

    def save(self, key, state, now, payload=None):
        with self.db:
            self.db.execute('INSERT INTO weeks VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,attempt=excluded.attempt,payload=COALESCE(excluded.payload,weeks.payload)',
                            (key,state,now,json.dumps(payload,ensure_ascii=False) if payload is not None else None))


def validate_reply(raw, records, emotions):
    raw = raw.strip()
    if raw.startswith('```'): raw = raw.partition('\n')[2].rsplit('```',1)[0]
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('entries'),list): raise ValueError('Invalid weekly response')
    source = {r['id']:r for r in records}
    seen, result = set(), []
    for item in data['entries']:
        key = item.get('id')
        if key not in source or key in seen: raise ValueError('Unexpected or duplicate memory')
        seen.add(key)
        if item.get('publish') is False: continue
        if item.get('publish') is not True: raise ValueError('Missing publish decision')
        for field in ('label','memory','text','emotion'):
            if not isinstance(item.get(field),str) or not item[field].strip(): raise ValueError('Missing entry field')
        if item['emotion'] not in emotions: raise ValueError('Unknown sticker')
        if SENSITIVE.search(item['memory']+' '+item['text']): continue
        result.append({'label':clean(item['label'])[:36], 'memory':clean(item['memory'])[:500],
                       'text':clean(item['text'])[:500], 'emotion':item['emotion'], 'source':source[key]['source']})
    if seen != set(source): raise ValueError('Missing memory decisions')
    return result


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(data)
    temp.replace(path)


class Publisher:
    def __init__(self, root, templates):
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        self.root = Path(root)
        self.env = Environment(loader=FileSystemLoader(templates),autoescape=select_autoescape(['html']))

    def publish(self, posts):
        # Each page is atomic; publish the index last so every new link exists.
        for post in posts:
            page = self.env.get_template('blog.html').render(post=post,posts=posts)
            atomic_write(self.root/post['date']/'index.html',page.encode())
            manifest={'date':post['date'],'title':post['title'],'kind':'memory',
                'url':'/neuro-blog/'+post['date']+'/', 'count':len(post['entries'])}
            atomic_write(self.root/post['date']/'post.json',json.dumps(manifest,ensure_ascii=False).encode())
        self.index()

    def index(self):
        posts=[]
        for file in [*self.root.glob('*/post.json'),*self.root.glob('stocks/*/post.json')]:
            item=json.loads(file.read_text(encoding='utf-8'))
            if item.get('kind') not in ('memory','stocks') or not item.get('url','').startswith('/neuro-blog/'):continue
            posts.append(item)
        preview=self.root/'stocks'/'preview.json'
        if not any(p['kind']=='stocks' for p in posts) and preview.exists():
            item=json.loads(preview.read_text(encoding='utf-8'))
            if item.get('preview') is True and item.get('url','').startswith('/neuro-blog/stocks/tests/'):
                posts.append(item)
        posts.sort(key=lambda p:(p['date'],p['kind']),reverse=True)
        page = self.env.get_template('blog.html').render(post=None,posts=posts)
        atomic_write(self.root/'index.html',page.encode())

    def sticker(self, path):
        data=Path(path).read_bytes()
        suffix=Path(path).suffix.lower()
        if suffix not in ('.png','.jpg','.jpeg','.gif','.webp'): raise ValueError('Invalid sticker format')
        filename=hashlib.sha256(data).hexdigest()[:24]+suffix
        atomic_write(self.root/'assets'/filename,data)
        return '/neuro-blog/assets/'+filename
