"""Quota-aware synthetic Groq benchmark. Does not use DB, WhatsApp or change provider."""
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import statistics
import time
from datetime import datetime, timezone
import httpx
from app import config  # loads backend/.env without exposing credentials
from app.catalog import BASE, key
from app.understanding import PROMPT, Understanding
from .groq_cases import CASES

MODEL = 'qwen/qwen3.8-27b'
CATALOG = [dict(kind=kind,code=code,label=label,aliases=aliases) for kind,code,label,aliases in BASE]


def strict_schema(value):
    if isinstance(value,list):
        return [strict_schema(item) for item in value]
    if not isinstance(value,dict):
        return value
    result={k:strict_schema(v) for k,v in value.items() if k not in ('default','title')}
    if result.get('type')=='object':
        result['additionalProperties']=False
        result['required']=list(result.get('properties',{}))
    return result


def evaluate(item,result):
    failures=[]
    facts=result.facts.model_dump(mode='json')
    rules=item['rules']
    if result.task not in item['expected_tasks']: failures.append('task')
    if item['expected_actions'] and result.action not in item['expected_actions']: failures.append('action')
    if rules.get('not_action')==result.action: failures.append('unsafe_action')
    for field,kind in [('work','skill'),('location','location'),('machine_type','machine')]:
        if field in rules and key(facts.get(field) or '',kind,CATALOG)!=rules[field]: failures.append(field)
    if rules.get('date') and facts.get('preferred_date')!=rules['date']: failures.append('preferred_date')
    for field,expected in rules.get('contains',{}).items():
        if expected not in (facts.get(field) or ''): failures.append(field)
    for field in rules.get('empty',[]):
        if facts.get(field) not in (None,''): failures.append('invented_or_copied_'+field)
    if rules.get('clear') and rules['clear'] not in result.clear_fields: failures.append('clear_fields')
    if rules.get('clarification') and not result.clarification: failures.append('clarification')
    if rules.get('not_known_machine') and facts.get('machine_type'):
        if key(facts['machine_type'],'machine',CATALOG) in [r['code'] for r in CATALOG if r['kind']=='machine']:
            failures.append('invented_catalog_match')
    return list(dict.fromkeys(failures))


async def run(args):
    secret=os.getenv('GROQ_API_KEY','').strip()
    if not secret:
        raise SystemExit('GROQ_API_KEY is missing; save it in backend/.env.')
    report={'model':MODEL,'reasoning_effort':args.reasoning,'format':args.format,'created_at':datetime.now(timezone.utc).isoformat(),
            'scope':'Synthetic direct NLU tests; production prompt, strict transport schema, local full validation. No DB or WhatsApp calls.',
            'cases':[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    wait_seconds=0
    wall_start=time.monotonic()
    selected=[item for item in CASES if not args.cases or item['id'] in args.cases.split(',')][:args.limit]
    report['selected_case_ids']=[item['id'] for item in selected]
    async with httpx.AsyncClient(timeout=15,trust_env=False) as client:
        for index,item in enumerate(selected,1):
            if wait_seconds>0:
                print(f'Pacing {wait_seconds:.1f}s for token quota',flush=True)
                await asyncio.sleep(wait_seconds)
            context={**item['context'],'catalog_options':[{k:r[k] for k in ('kind','code','label')} for r in CATALOG]}
            system_prompt=PROMPT
            if args.format=='json':
                system_prompt+='\nReturn a JSON object matching this schema; absent facts must be null, never copied from the draft:\n'+json.dumps(Understanding.model_json_schema(),ensure_ascii=False,separators=(',',':'))
            payload={'model':MODEL,'temperature':0,'reasoning_effort':args.reasoning,'max_completion_tokens':1536 if args.reasoning=='low' else 768,
                     'messages':[{'role':'system','content':system_prompt},{'role':'user','content':json.dumps({'message':item['message'],**context},ensure_ascii=False,separators=(',',':'))}],
                     'response_format':({'type':'json_schema','json_schema':{'name':'conversation_understanding','strict':True,'schema':strict_schema(Understanding.model_json_schema())}} if args.format=='strict' else {'type':'json_object'})}
            started=time.monotonic()
            entry={'id':item['id'],'message':item['message'],'context':context,'expected_tasks':item['expected_tasks'],
                   'expected_actions':item['expected_actions'],'rules':item['rules']}
            try:
                response=await client.post('https://api.groq.com/openai/v1/chat/completions',headers={'Authorization':'Bearer '+secret},json=payload)
                entry['latency_seconds']=round(time.monotonic()-started,3)
                entry['http_status']=response.status_code
                entry['rate_headers']={k:v for k,v in response.headers.items() if k.startswith('x-ratelimit-') or k=='retry-after'}
                if response.status_code!=200:
                    error=response.json().get('error',{})
                    entry['error']={k:re.sub(r'\borg_[A-Za-z0-9]+','[organization]',str(error[k]).replace(secret,'[REDACTED]'))[:2000] for k in ('code','type','message','failed_generation') if k in error}
                    entry['passed']=False
                    report['cases'].append(entry)
                    save()
                    print(f"{index:02} {item['id']}: HTTP {response.status_code} {entry['error']}",flush=True)
                    if response.status_code==400 and error.get('code')=='json_validate_failed':
                        remaining=float(response.headers.get('x-ratelimit-remaining-tokens','0'))
                        limit=float(response.headers.get('x-ratelimit-limit-tokens','8000'))
                        wait_seconds=max(15,min(60,max(0,(2500-remaining)/(limit/60))+0.5))
                        continue  # Count the failed case, then test the next one; no retry.
                    break  # No retry loop, alternate model or quota amplification.
                data=response.json()
                entry['usage']=data.get('usage',{})
                entry['finish_reason']=data['choices'][0].get('finish_reason')
                raw=data['choices'][0]['message'].get('content') or ''
                entry['raw_output']=raw
                try:
                    result=Understanding.model_validate_json(raw)
                    entry['output']=result.model_dump(mode='json')
                    entry['schema_valid']=True
                    entry['failures']=evaluate(item,result)
                    entry['task_correct']=result.task in item['expected_tasks']
                    if entry['finish_reason']=='length': entry['failures'].append('truncated')
                    entry['passed']=not entry['failures']
                except ValueError:
                    entry.update(schema_valid=False,passed=False,failures=['local_schema_validation'])
                report['cases'].append(entry)
                save()
                print(f"{index:02} {item['id']}: {'PASS' if entry['passed'] else 'FAIL '+','.join(entry.get('failures',[]))} {entry['latency_seconds']}s tokens={entry['usage'].get('total_tokens')} task={entry.get('output',{}).get('task')}",flush=True)
                remaining=float(response.headers.get('x-ratelimit-remaining-tokens','8000'))
                limit=float(response.headers.get('x-ratelimit-limit-tokens','8000'))
                needed=entry['usage'].get('prompt_tokens',3500)+payload['max_completion_tokens']
                wait_seconds=max(0,(needed-remaining)/(limit/60))+0.5 if remaining<needed else 0
                wait_seconds=min(60,wait_seconds)
                # Groq can enforce a separate input-token bucket (observed 7K
                # ITPM) beyond its combined 8K TPM response header. Stay below it.
                wait_seconds=max(wait_seconds,max(15,entry['usage'].get('prompt_tokens',2000)/6000*60+1)-entry['latency_seconds'])
            except httpx.HTTPError as exc:
                entry.update(http_status=None,error={'type':type(exc).__name__},latency_seconds=round(time.monotonic()-started,3),passed=False)
                report['cases'].append(entry)
                save()
                print(f"{index:02} {item['id']}: {type(exc).__name__}",flush=True)
                break
    completed=[entry for entry in report['cases'] if entry.get('http_status')==200]
    latencies=[entry['latency_seconds'] for entry in completed]
    report['summary']={'planned_cases':len(selected),'attempted_cases':len(report['cases']),'completed_cases':len(completed),
        'schema_valid_cases':sum(bool(entry.get('schema_valid')) for entry in completed),
        'passed_cases':sum(bool(entry.get('passed')) for entry in completed),
        'task_correct_cases':sum(bool(entry.get('task_correct')) for entry in completed),
        'median_seconds':round(statistics.median(latencies),3) if latencies else None,
        'max_seconds':max(latencies) if latencies else None,
        'within_6_seconds':sum(value<=6 for value in latencies),
        'prompt_tokens':sum(entry.get('usage',{}).get('prompt_tokens',0) for entry in completed),
        'completion_tokens':sum(entry.get('usage',{}).get('completion_tokens',0) for entry in completed),
        'wall_seconds_including_quota_pacing':round(time.monotonic()-wall_start,2)}
    save()
    print('SUMMARY '+json.dumps(report['summary']),flush=True)
    print('Report:',args.output,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int,choices=range(1,21),default=20)
    parser.add_argument('--reasoning',choices=['none','low'],default='none')
    parser.add_argument('--format',choices=['strict','json'],default='strict')
    parser.add_argument('--cases',help='Optional comma-separated case IDs')
    parser.add_argument('--output',type=Path,default=Path('reports/groq-qwen38-benchmark.json'))
    asyncio.run(run(parser.parse_args()))
