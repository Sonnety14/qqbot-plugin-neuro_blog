"""Blog-only client: deliberately bypasses chat sleep hooks, never its own ledger."""
import json
import httpx


class BlogModel:
    def __init__(self, config):self.config=config

    async def generate(self, system, payload):
        async with httpx.AsyncClient(base_url=self.config['gateway_url'].rstrip('/')+'/',
            headers={'Authorization':'Bearer '+self.config['gateway_token']},
            timeout=120,trust_env=False,follow_redirects=False) as c:
            r=await c.get('status');r.raise_for_status();status=r.json()
            if status.get('budget_scope')!='blog' or status.get('sleeping'):
                raise RuntimeError('independent blog budget unavailable')
            r=await c.post('chat/completions',json={'model':self.config['model'],
                'messages':[{'role':'system','content':system},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
                'max_tokens':4096,'reasoning_effort':'low','response_format':{'type':'json_object'}})
            r.raise_for_status();choice=r.json()['choices'][0]
            if choice['finish_reason']!='stop' or choice['message'].get('tool_calls'):
                raise ValueError('incomplete blog response')
            return choice['message']['content']
