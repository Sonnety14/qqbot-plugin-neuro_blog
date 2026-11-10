"""Internal, authenticated budget gateway. No upstream retries or public admin API."""
import asyncio,hmac,json,os
from pathlib import Path
import httpx
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,Response
from .ledger import Ledger,Denied,MESSAGE
from .gemini import native_request,completion,cost,rates
from . import gemini


def create_app(config,ledger=None,transport=None):
    gemini.configure(config['pricing'])
    if any(r.get('kind')!='gemini' for r in config['routes'].values()):raise ValueError('This template only accounts for Gemini routes')
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    book=ledger or Ledger(config['database']);app.state.ledger=book
    blog_book = None
    if any(r.get('budget_scope')=='blog' for r in config['routes'].values()):
        if not config.get('blog_database') or Path(config['blog_database']).resolve()==Path(config['database']).resolve():
            raise ValueError('Blog budget requires a separate ledger')
        blog_book=Ledger(config['blog_database'],limit=config.get('blog_daily_limit_nusd',5_000_000_000))
    app.state.blog_ledger=blog_book
    def route_book(route):
        scope=route.get('budget_scope','chat')
        if scope not in ('chat','blog'):raise Denied('unknown_budget_scope')
        return blog_book if scope=='blog' else book
    jobs=set()
    def checked_status(book=book):
        try: rates(book.day())
        except Denied: book.lock('price_review_required')
        return book.status()
    def route_auth(name,request):
        route=config['routes'].get(name)
        auth=request.headers.get('authorization','')
        if not route or not hmac.compare_digest(auth,'Bearer '+route['token']):raise Denied('unauthorized')
        return route
    def sleeping(reason,status=429):
        if reason == 'reserved':
            return JSONResponse({'error':{'code':'NEURO_BUDGET_BUSY','message':'neuro正在回复其他消息，稍等几秒再试试吧！','type':'temporary','reason':reason}},status_code=429,headers={'x-should-retry':'false'})
        return JSONResponse({'error':{'code':'NEURO_DAILY_BUDGET_EXHAUSTED','message':MESSAGE,'type':'budget','reason':reason}},status_code=status,headers={'x-neuro-sleep':'1','x-should-retry':'false'})
    @app.get('/healthz')
    async def health():return {'ok':True}
    @app.get('/{name}/status')
    async def status(name:str,request:Request):
        try:
            route=route_auth(name,request)
            selected=route_book(route)
            result=checked_status(selected)
            result['budget_scope']=route.get('budget_scope','chat')
            if route.get('daily_limit_nusd') is not None:
                result['route']=selected.route_status(name,route['daily_limit_nusd'])
            return result
        except Denied:return JSONResponse({'error':'unauthorized'},status_code=401)
        except Exception:return sleeping('ledger_unavailable',503)
    async def execute(route,name,body):
        book=route_book(route)
        state=checked_status(book)
        if state['sleeping']:raise Denied(state['reason'])
        if route['kind']=='gemini':
            native=native_request(body);day=book.day();inp,out,_=rates(day)
            async with httpx.AsyncClient(proxy=route.get('proxy') or None,timeout=110,trust_env=False,transport=transport) as client:
                headers={'x-goog-api-key':route['key']}
                base='https://generativelanguage.googleapis.com/v1beta/models/'+gemini.MODEL
                counted=await client.post(base+':countTokens',headers=headers,json={'generateContentRequest':{'model':'models/'+gemini.MODEL,**native}})
                counted.raise_for_status();tokens=counted.json().get('totalTokens')
                if type(tokens) is not int or not 0<=tokens<=1048576:raise Denied('invalid_token_count')
                # Native count includes system, media and tools. Add a guard band for
                # documented count/generation overhead differences; no char estimates.
                # Gemini generateContent maxOutputTokens caps thinking + visible output
                # together. Reserve that same transmitted cap, not the model-wide max.
                # https://ai.google.dev/gemini-api/docs/generate-content/thinking
                reserve=(tokens+1024)*inp+native['generationConfig']['maxOutputTokens']*out
                attempt=book.reserve(reserve,name,gemini.MODEL,gemini.PRICE_VERSION,route.get('daily_limit_nusd'))
                try:
                    result=await client.post(base+':generateContent',headers=headers,json=native)
                    result.raise_for_status();raw=result.json();usage=raw.get('usageMetadata',{})
                    actual=cost(usage,day)
                    book.settle(attempt,actual,json.dumps(usage,separators=(',',':')))
                    return completion(raw)
                except BaseException:
                    book.uncertain(attempt,'upstream_outcome_uncertain');raise
    @app.post('/{name}/chat/completions')
    async def chat(name:str,request:Request):
        try:
            route=route_auth(name,request)
            raw=await request.body()
            if len(raw)>12_000_000:raise Denied('request_too_large')
            body=json.loads(raw)
            # Keep work and accounting alive when caller disconnects/cancels.
            task=asyncio.create_task(execute(route,name,body));jobs.add(task)
            def done(t):
                jobs.discard(t)
                if not t.cancelled():t.exception()
            task.add_done_callback(done)
            result=await asyncio.shield(task)
            if body.get('stream'):
                choice=result['choices'][0]
                for i, call in enumerate(choice['message'].get('tool_calls', [])):
                    call['index'] = i
                chunk={**result,'object':'chat.completion.chunk','choices':[{'index':0,'delta':choice['message'],'finish_reason':choice['finish_reason']}]}
                return Response('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\ndata: [DONE]\n\n',media_type='text/event-stream')
            return result
        except Denied as e:return sleeping(e.reason,401 if e.reason=='unauthorized' else 429)
        except Exception:return sleeping('gateway_unavailable',503)
    return app


def load_app():
    return create_app(json.loads(Path(os.environ['NEURO_BUDGET_CONFIG']).read_text(encoding='utf-8-sig')))
