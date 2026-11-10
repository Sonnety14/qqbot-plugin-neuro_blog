"""Restricted OpenAI -> Gemini native adapter; count and generate use identical contents."""
import base64,json,re,time,uuid
from .ledger import Denied
MODEL=''
PRICE_VERSION=''
PRICING={}

def configure(pricing):
    global MODEL,PRICE_VERSION,PRICING
    from datetime import date
    if not isinstance(pricing.get('model'),str) or not pricing['model'].strip():raise ValueError('model required')
    start=date.fromisoformat(pricing['valid_from']);end=date.fromisoformat(pricing['valid_until'])
    if end<=start:raise ValueError('invalid pricing interval')
    for name in ('input_nusd_per_token','output_nusd_per_token','cached_nusd_per_token'):
        if type(pricing.get(name)) is not int or pricing[name]<=0:raise ValueError('review positive integer token prices')
    if not pricing.get('version'):raise ValueError('price version required')
    MODEL=pricing['model'];PRICE_VERSION=pricing['version'];PRICING=dict(pricing)

def rates(day):
    if not PRICING or not PRICING['valid_from']<=day<PRICING['valid_until']:raise Denied('price_review_required')
    return tuple(PRICING[k] for k in ('input_nusd_per_token','output_nusd_per_token','cached_nusd_per_token'))


def native_request(body):
    if body.get('model')!=MODEL:raise Denied('unknown_model')
    allowed={'model','messages','max_tokens','max_completion_tokens','temperature','top_p','stop','stream','stream_options','response_format','tools','tool_choice','parallel_tool_calls','reasoning_effort','n','user','seed'}
    if set(body)-allowed:raise Denied('unsupported_request_fields')
    if body.get('n',1)!=1:raise Denied('multiple_candidates_unsupported')
    contents=[];system=[];names={}
    for msg in body.get('messages',[]):
        role=msg.get('role');parts=[]
        if role not in ('system','developer','user','assistant','tool'):raise Denied('unsupported_role')
        value=msg.get('content')
        if isinstance(value,str):parts.append({'text':value})
        elif isinstance(value,list):
            for item in value:
                if item.get('type')=='text':parts.append({'text':item['text']})
                elif item.get('type')=='image_url':
                    url=item['image_url']['url'];m=re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)',url)
                    if not m:raise Denied('only_inline_images_supported')
                    base64.b64decode(m[2],validate=True)
                    parts.append({'inlineData':{'mimeType':m[1],'data':m[2]}})
                else:raise Denied('unsupported_modality')
        elif value is not None:raise Denied('invalid_content')
        if role in ('system','developer'):
            if any('text' not in p for p in parts):raise Denied('invalid_system')
            system.extend(parts);continue
        if role=='tool':
            name=msg.get('name') or names.get(msg.get('tool_call_id'))
            if not name:raise Denied('missing_tool_name')
            try: response=json.loads(value) if isinstance(value,str) else value
            except (ValueError,TypeError): response=value
            if not isinstance(response,dict):response={'result':response}
            parts=[{'functionResponse':{'name':name,'response':response}}]
        for call in msg.get('tool_calls',[]):
            function=call['function'];names[call['id']]=function['name']
            part={'functionCall':{'name':function['name'],'args':json.loads(function['arguments'])}}
            signature=call.get('extra_content',{}).get('google',{}).get('thought_signature')
            if signature:part['thoughtSignature']=signature
            parts.append(part)
        if parts:contents.append({'role':'model' if role=='assistant' else 'user','parts':parts})
    if not contents:raise Denied('empty_input')
    maximum=body.get('max_completion_tokens',body.get('max_tokens',2048))
    if type(maximum) is not int or not 1<=maximum<=8192:raise Denied('invalid_output_limit')
    config={'maxOutputTokens':maximum,'candidateCount':1}
    for a,b in [('temperature','temperature'),('top_p','topP'),('seed','seed')]:
        if a in body:config[b]=body[a]
    if body.get('stop'):config['stopSequences']=[body['stop']] if isinstance(body['stop'],str) else body['stop']
    effort=body.get('reasoning_effort','low')
    if effort not in ('minimal','low','medium','high'):raise Denied('unsupported_thinking')
    config['thinkingConfig']={'thinkingLevel':effort}
    fmt=body.get('response_format',{})
    if fmt.get('type')=='json_object':config['responseMimeType']='application/json'
    elif fmt.get('type') not in (None,'text'):raise Denied('unsupported_response_format')
    request={'contents':contents,'generationConfig':config}
    if system:request['systemInstruction']={'parts':system}
    if body.get('tools'):
        declarations=[]
        for tool in body['tools']:
            if tool.get('type')!='function':raise Denied('paid_tool_unsupported')
            f=tool['function'];declarations.append({'name':f['name'],'description':f.get('description',''),'parametersJsonSchema':f.get('parameters',{'type':'object','properties':{}})})
        request['tools']=[{'functionDeclarations':declarations}]
        choice=body.get('tool_choice','auto');fc={'mode':{'auto':'AUTO','none':'NONE','required':'ANY'}.get(choice,'AUTO')} if isinstance(choice,str) else {'mode':'ANY','allowedFunctionNames':[choice['function']['name']]}
        request['toolConfig']={'functionCallingConfig':fc}
    return request

def cost(usage,day):
    inp,out,cache=rates(day)
    for key in ('promptTokenCount','totalTokenCount'):
        if type(usage.get(key)) is not int or usage[key]<0:raise ValueError('missing usage')
    prompt=usage['promptTokenCount'];cached=usage.get('cachedContentTokenCount',0)
    candidates=usage.get('candidatesTokenCount',0);thoughts=usage.get('thoughtsTokenCount',0)
    if any(type(v) is not int or v<0 for v in (cached,candidates,thoughts)):raise ValueError('bad usage')
    if cached>prompt or usage.get('toolUsePromptTokenCount',0):raise ValueError('unsupported usage')
    if usage['totalTokenCount']!=prompt+candidates+thoughts:raise ValueError('inconsistent usage')
    return (prompt-cached)*inp+cached*cache+(candidates+thoughts)*out

def completion(payload):
    u=payload['usageMetadata'];c=(payload.get('candidates') or [{}])[0]
    text=[];calls=[]
    for part in c.get('content',{}).get('parts',[]):
        if 'text' in part and not part.get('thought'):text.append(part['text'])
        if 'functionCall' in part:
            f=part['functionCall'];call={'id':'call_'+uuid.uuid4().hex,'type':'function','function':{'name':f['name'],'arguments':json.dumps(f.get('args',{}),ensure_ascii=False)}}
            if part.get('thoughtSignature'):call['extra_content']={'google':{'thought_signature':part['thoughtSignature']}}
            calls.append(call)
    msg={'role':'assistant','content':''.join(text) or None}
    if calls:msg['tool_calls']=calls
    return {'id':'chatcmpl-'+uuid.uuid4().hex,'object':'chat.completion','created':int(time.time()),'model':MODEL,
            'choices':[{'index':0,'message':msg,'finish_reason':'tool_calls' if calls else 'length' if c.get('finishReason')=='MAX_TOKENS' else 'stop'}],
            'usage':{'prompt_tokens':u['promptTokenCount'],'completion_tokens':u.get('candidatesTokenCount',0)+u.get('thoughtsTokenCount',0),'total_tokens':u['totalTokenCount'],
                     'prompt_tokens_details':{'cached_tokens':u.get('cachedContentTokenCount',0)},'completion_tokens_details':{'reasoning_tokens':u.get('thoughtsTokenCount',0)}}}
