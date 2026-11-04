"""Explicit live check using synthetic text only; never falls back to Gemini."""
import asyncio
import json
import time
import httpx
from app import config
from app.ai import AIError
from app.llm import OllamaProvider


async def main():
    started=time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            version=await client.get(config.OLLAMA_BASE_URL+'/api/version')
            version.raise_for_status()
            show=await client.post(config.OLLAMA_BASE_URL+'/api/show',json={'model':config.OLLAMA_MODEL})
            show.raise_for_status()
            info=show.json()
        print(json.dumps({'ollama_version':version.json().get('version'),
                          'model':config.OLLAMA_MODEL,
                          'capabilities':info.get('capabilities',[]),
                          'thinking_controls':info.get('thinking')},ensure_ascii=True))
        result=await OllamaProvider().extract('بغيت تراكتور يحرث ليا جوج هكتارات','')
        print(json.dumps({'live_inference':'completed','duration_seconds':round(time.monotonic()-started,2),
                          'machine_matches':result.machine_type=='tractor',
                          'operation_matches':result.operation in ('plowing','ploughing'),
                          'area_matches':result.area_hectares==2,
                          'missing_location_not_invented':result.location is None},ensure_ascii=True))
        return 0 if (result.machine_type=='tractor' and result.operation in ('plowing','ploughing') and result.area_hectares==2 and result.location is None) else 1
    except AIError as exc:
        print(json.dumps({'live_inference':'failed','safe_code':exc.code}))
    except httpx.HTTPError as exc:
        print(json.dumps({'live_inference':'failed','exception_type':type(exc).__name__}))
    return 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
