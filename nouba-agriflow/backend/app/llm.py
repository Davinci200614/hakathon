"""Provider selection for extraction only; no database or conversation actions."""
import asyncio
import json
import time
from typing import Protocol
import httpx
from pydantic import ValidationError
from . import config
from .ai import AIError, _generate
from .diagnostics import event
from .extraction import Extraction, compact_schema, normalized_patch, parse_extraction, casablanca_today

# One complete primary/fallback inference at a time in this one-worker MVP.
_inference_lock = asyncio.Lock()
PROMPT = ('Extract only facts in this farmer message (Darija/Arabic/French/Latin Darija). '
          'Return JSON, omit absent fields or use null; never guess. '
          'intent: machinery_request or other. Normalize machine/operation to English '
          '(تراكتور=tractor, يحرث=plowing, جوج هكتارات=2 ha). Keep place names exactly. '
          'preferred_date: resolve clear natural-language dates using today in Africa/Casablanca; '
          'return ISO YYYY-MM-DD. Ambiguous dates must be null. Never ask for today. '
          'Current state and pending field are context only: never copy saved facts into the patch. '
          'An explicit correction supplies the replacement; مشي/ماشي introduces the rejected value. '
          'جوج is a quantity, never a location. Unknown values must be null. '
          'The question identifies the field for a short answer. No booking/actions. /no_think')


class Provider(Protocol):
    async def extract(self, text: str, question: str) -> Extraction: ...


class OllamaProvider:
    async def extract(self, text, question):
        started=time.monotonic()
        succeeded=False
        try:
            async with asyncio.timeout(config.OLLAMA_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(timeout=httpx.Timeout(config.OLLAMA_TIMEOUT_SECONDS,connect=3),trust_env=False) as client:
                    response=await client.post(config.OLLAMA_BASE_URL+'/api/chat',json={
                        'model':config.OLLAMA_MODEL,'stream':False,'think':False,
                        'format':compact_schema(),'keep_alive':config.OLLAMA_KEEP_ALIVE,
                        'options':{'num_ctx':config.OLLAMA_NUM_CTX,'num_predict':config.OLLAMA_NUM_PREDICT,'temperature':0},
                        'messages':[{'role':'system','content':PROMPT},
                                    {'role':'user','content':json.dumps({'today':casablanca_today().isoformat(),'timezone':'Africa/Casablanca','question':question,'text':text},ensure_ascii=False,separators=(',',':'))}],
                    })
                    response.raise_for_status()
                    payload=response.json()
                    if not payload.get('done') or payload.get('done_reason')=='length':
                        raise AIError('ollama_incomplete_output')
                    result=parse_extraction(payload['message']['content'])
                    succeeded=True
        except (asyncio.TimeoutError,httpx.TimeoutException):
            raise AIError('ollama_timeout') from None
        except httpx.ConnectError:
            raise AIError('ollama_offline') from None
        except httpx.HTTPStatusError as exc:
            raise AIError('ollama_model_unavailable' if exc.response.status_code==404 else 'ollama_http_error') from None
        except (ValidationError,ValueError,KeyError,TypeError,AttributeError):
            raise AIError('ollama_invalid_json') from None
        except httpx.HTTPError:
            raise AIError('ollama_transport_error') from None
        finally:
            event('llm.inference',started,provider='ollama',model=config.OLLAMA_MODEL,outcome='ok' if succeeded else 'error')
        return result


class GeminiProvider:
    async def extract(self, text, question):
        # One bounded attempt for fallback: never three long Gemini retries.
        return await _generate(config.GEMINI_MODEL,
            json.dumps({'today':casablanca_today().isoformat(),'timezone':'Africa/Casablanca','question':question,'text':text},ensure_ascii=False),Extraction,PROMPT,
            max_attempts=1,total_timeout=16)


def provider(name):
    if name=='ollama': return OllamaProvider()
    if name=='gemini': return GeminiProvider()
    raise AIError('llm_provider_invalid')


async def extract(text, previous=None, history=None):
    if not isinstance(text,str) or not text.strip() or len(text)>600:
        raise AIError('llm_message_too_long_or_empty')
    # Send the bounded current draft and persisted pending field, not growing history.
    question=next((turn['text'] for turn in reversed(history or []) if turn.get('role')=='assistant'),'')[:140]
    context = next((t for t in reversed(history or []) if t.get('role') == 'context'), {})
    question = {'question':question, 'current_state':previous, 'pending_field':context.get('pending_field')}
    started=time.monotonic()
    try:
        # Include queueing in a bounded wait; don't retain unbounded background work.
        async with asyncio.timeout(config.OLLAMA_TIMEOUT_SECONDS+18):
            async with _inference_lock:
                try:
                    result=await provider(config.LLM_PROVIDER).extract(text,question)
                except AIError as exc:
                    event('llm.primary_failed',outcome='error',provider=config.LLM_PROVIDER,error_code=exc.code)
                    fallback=config.LLM_FALLBACK_PROVIDER
                    if fallback!='gemini' or fallback==config.LLM_PROVIDER or not config.GEMINI_API_KEY or not config.GEMINI_MODEL:
                        raise
                    event('llm.fallback',provider='gemini',outcome='started')
                    result=await GeminiProvider().extract(text,question)
                event('extraction.validated', field_presence=sorted(k for k,v in result.model_dump().items() if v is not None))
                date_context = bool(previous and all(previous.get(k) for k in ('machine_type','operation','area_hectares','location')) and not previous.get('preferred_date'))
                patch = normalized_patch(result,text,date_context=date_context)
                from .extraction import clean_field, deterministic_patch
                patch = {k:clean_field(k,v) for k,v in patch.items()}
                patch.update(deterministic_patch(text, context.get('pending_field')))
                return patch
    except asyncio.TimeoutError:
        raise AIError('llm_queue_timeout') from None
    except AIError as exc:
        event('llm.failed',started,outcome='error',provider=config.LLM_PROVIDER,error_code=exc.code)
        raise
    finally:
        event('llm.total',started,provider=config.LLM_PROVIDER)
