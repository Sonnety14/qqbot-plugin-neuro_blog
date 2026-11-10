"""Local admin client. Read secrets from a private file, never command arguments."""
import argparse,json
from pathlib import Path
import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('action',choices=['status','start','resume','pause'])
    args=parser.parse_args();cfg=json.loads(args.config.read_text(encoding='utf-8-sig'))
    host=cfg.get('control_host','127.0.0.1')
    if host=='0.0.0.0':host='127.0.0.1'
    base=cfg.get('control_url') or f'http://{host}:{cfg.get("control_port",18791)}'
    with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+cfg['control_token']},timeout=20,trust_env=False) as c:
        r=c.get('/status') if args.action=='status' else c.post('/control',json={'action':args.action})
        r.raise_for_status();print(json.dumps(r.json(),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
