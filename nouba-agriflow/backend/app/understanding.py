"""One bounded structured understanding call; no SQL, tool loop or reply generation."""
import asyncio
import json
import time
import hashlib
from datetime import date
from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from . import config
from .ai import AIError
from .diagnostics import event, span


class Facts(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    machine_type: str | None = Field(default=None, max_length=100)
    operation: str | None = Field(default=None, max_length=100)
    area_hectares: float | None = Field(default=None, gt=0)
    location: str | None = Field(default=None, max_length=100)
    preferred_date: date | None = None
    name: str | None = Field(default=None, max_length=100)
    work: str | None = Field(default=None, max_length=100)
    details: str | None = Field(default=None, max_length=400)
    contact: str | None = Field(default=None, max_length=100)
    browse_all: bool | None = None

    @field_validator('machine_type', 'operation', 'location', 'name', 'work', 'details', 'contact')
    @classmethod
    def clean_text(cls, value):
        return value.strip() or None if value is not None else None

    @field_validator('area_hectares', mode='before')
    @classmethod
    def valid_area(cls, value):
        if isinstance(value, bool):
            raise ValueError('invalid area')
        return value


class Understanding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task: Literal['machine', 'search_machines', 'search_workers', 'search_jobs', 'post_worker', 'post_job', 'none'] = 'none'
    action: Literal['continue', 'correct', 'new', 'switch', 'cancel', 'confirm', 'resume', 'help', 'unknown', 'reset', 'clarify', 'menu'] = 'continue'
    facts: Facts = Field(default_factory=Facts)
    clear_fields: list[Literal['name', 'location', 'work', 'details', 'contact']] = Field(default_factory=list)
    clarification: str | None = Field(default=None, max_length=240)


PROMPT = '''Understand a WhatsApp message in Moroccan Darija, Arabic, French or Arabizi.
Return structured intent and ONLY facts explicitly stated in the CURRENT message.
History and draft are context, never facts to copy. Do not write a conversational answer.
If the user explicitly denies having supplied a field, include it in clear_fields.
Contact is a concrete number, email, @handle or profile URL, never just WhatsApp
or Facebook or an intention to provide a contact later. Do not invent an account.
Extract explicit corrections to name, city, occupation and pay even when another
field is pending. Do not leave stale profile fields that contradict the message.
Tasks: machine=agricultural machinery request; search_workers=hire/find somebody;
search_machines=look up available equipment in the database;
search_jobs=find employment; post_worker=publish own worker profile;
post_job=publish an employer job offer; none=unrelated/unclear.
A CLEAR new purpose can switch tasks, including when it starts with ah/أه/yes.
The pending question and active goal take precedence over keywords. While posting
a worker profile, saying "حصاد" answers skills, not a machinery request. Saying
"كنقلب على 4000 dh فشهر وأنا لبغيت نخدم" answers employment details, not a new
job search. "سميتي حميد بغيت نلقى خدمة فشي كاريان" supplies name and work for
the current worker profile. Retain post_worker unless the user clearly abandons it.
Use last_search context for references: after searching jobs, "نشر إعلان ديالي"
or "واش ممكن نبوستي هاد أنونس ومنين شيواحد يسول يلقاني" means post_worker.
After searching workers the same reference means post_job. If unclear, clarify.
Reset means discard ALL unfinished drafts and dialogue context, never published
listings or confirmed requests. Cancel means discard only the current draft.
"بغيت شيواحد إخدم عندي" means search_workers, not machine and not confirmation.
"كنقلب على خدمة" means search_jobs. Do not turn searches into publishing.
action correct for changed facts, new only for an explicitly new request,
resume for an explicitly resumed draft, confirm only for an unambiguous standalone approval.
If ambiguous/unrelated, action unknown. Never invent a supported purpose.
For a question about the current step, action help and optional clarification
can answer it in one or two short Darija sentences, without a command list.
Explain only this service/current field. Never claim a search was performed,
records were saved, contacts were verified, or a booking was made. Python handles
all those operations. Publishing requires name, location, work/skills, details
and a contact method the user chooses to share. Pay can be "قابل للتفاوض" or
not specified in details; never invent a salary. Nothing is published before
the final delivered summary and explicit standalone confirmation.
Extract all explicit details at once; use pending field to understand short answers.
Preserve place names. جوج is a quantity, never a location. مشي/ماشي introduces rejected values.
Normalize tractor/plowing/harvesting. Resolve clear dates using supplied today/timezone.
Use null for missing or ambiguous facts. Never guess contact consent, names, pay or dates.
For employment use work=skills/work, details=availability/experience/pay/headcount/dates.
Never generate SQL or perform actions.'''
PROMPT += '''\nCatalog candidates are real database names/codes and aliases. Map synonyms
or descriptions to their canonical code when supported by the catalog. A request
for a professional (e.g. تريسيان) is a worker search; "بغيت نخدم كتريسيان"
is a job search. For a public directory request set browse_all=true. Never expose
private farmer/message records. Reuse last_search city for follow-up refinements,
not as facts for an unrelated new purpose. If no catalog match is defensible,
ask a short clarification rather than inventing equipment or people.'''

_hosted_slots = asyncio.Semaphore(4)
_local_slots = asyncio.Semaphore(1)
_cooldowns = {}


def selected_provider():
    if config.CONVERSATION_PROVIDER == 'auto':
        return 'gemini' if config.GEMINI_API_KEY else 'ollama'
    return config.CONVERSATION_PROVIDER


def selected_model():
    return config.CONVERSATION_MODEL or (config.CONVERSATION_GEMINI_MODEL if selected_provider() == 'gemini' else config.OLLAMA_MODEL)


async def understand(text, context):
    provider = selected_provider()
    limiter_key = (provider, selected_model(), hashlib.sha256(config.GEMINI_API_KEY.encode()).hexdigest()[:12] if provider == 'gemini' else '')
    if _cooldowns.get(limiter_key, 0) > time.monotonic():
        raise AIError('conversation_rate_limited')
    budget = config.CONVERSATION_TIMEOUT_SECONDS
    contents = json.dumps({'message': text, **context}, ensure_ascii=False)
    # Include queueing, connection, inference and validation in one deadline.
    try:
        with span('dialogue.understanding', provider=provider):
            async with asyncio.timeout(budget):
                async with (_hosted_slots if provider == 'gemini' else _local_slots):
                    if provider == 'gemini':
                        # Direct async transport avoids SDK initialization and
                        # environment proxy discovery on each conversational turn.
                        async with httpx.AsyncClient(timeout=budget, trust_env=False) as client:
                            response = await client.post(
                                f'https://generativelanguage.googleapis.com/v1beta/models/{selected_model()}:generateContent',
                                headers={'x-goog-api-key': config.GEMINI_API_KEY}, json={
                                    'systemInstruction': {'parts': [{'text': PROMPT}]},
                                    'contents': [{'parts': [{'text': contents}]}],
                                    'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json',
                                        'responseJsonSchema': Understanding.model_json_schema(), 'maxOutputTokens': 512,
                                        'thinkingConfig': {'thinkingLevel': config.CONVERSATION_THINKING_LEVEL.upper()}},
                                })
                            response.raise_for_status()
                            candidates = response.json().get('candidates', [])
                            if not candidates or candidates[0].get('finishReason') == 'MAX_TOKENS':
                                raise AIError('conversation_incomplete')
                            raw = ''.join(part.get('text', '') for part in candidates[0].get('content', {}).get('parts', []) if not part.get('thought'))
                            return Understanding.model_validate_json(raw)
                    if provider != 'ollama':
                        raise AIError('conversation_provider_invalid')
                    async with httpx.AsyncClient(timeout=budget, trust_env=False) as client:
                        response = await client.post(config.OLLAMA_BASE_URL + '/api/chat', json={
                            'model': selected_model(),
                            'stream': False, 'think': False, 'format': Understanding.model_json_schema(),
                            'keep_alive': config.OLLAMA_KEEP_ALIVE,
                            'options': {'temperature': 0, 'num_ctx': max(2048, config.OLLAMA_NUM_CTX), 'num_predict': 300},
                            'messages': [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': contents}],
                        })
                        response.raise_for_status()
                        data = response.json()
                        if not data.get('done') or data.get('done_reason') == 'length':
                            raise AIError('conversation_incomplete')
                        return Understanding.model_validate_json(data['message']['content'])
    except (TimeoutError, httpx.TimeoutException):
        raise AIError('conversation_timeout') from None
    except AIError:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            try:
                delay = min(300,max(30,float(exc.response.headers.get('retry-after','60'))))
            except ValueError:
                delay = 60
            _cooldowns[limiter_key] = time.monotonic()+delay
        code = ('conversation_rate_limited' if status == 429 else
                'conversation_auth_error' if status in (401, 403) else
                'conversation_model_unavailable' if status == 404 else
                'conversation_provider_unavailable' if status >= 500 else 'conversation_invalid_request')
        event('dialogue.understanding_failed', outcome='error', provider=provider, error_code=code)
        raise AIError(code) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        event('dialogue.understanding_failed', outcome='error', provider=provider)
        raise AIError('conversation_understanding_failed') from None
