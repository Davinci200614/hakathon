"""Allowlisted JSON diagnostics: no payloads, phone numbers, URLs or secrets."""
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

log = logging.getLogger('nouba.diagnostics')
trace_id = ContextVar('nouba_trace', default=None)
_ALLOWED = {'field_presence', 'normalized_date', 'date_mentioned', 'missing_fields', 'previous_state', 'requested_field', 'failed_attempts','outcome', 'exception_type', 'meta_http_status', 'meta_error_code',
            'gemini_error_code', 'attempt', 'model', 'key_configured',
            'model_matches_env', 'key_matches_env', 'message_count', 'kind',
            'state', 'validation_fields', 'retry_delay_s', 'provider', 'error_code', 'stt_provider', 'database_path', 'sqlite_error_code', 'sqlite_error_name', 'sqlite_message', 'stack_frames', 'schema_problems', 'schema_version'}


def event(stage, started=None, **fields):
    record = {'stage': stage, 'trace_id': trace_id.get(),
              'duration_ms': round((time.monotonic()-started)*1000, 2) if started else 0}
    record.update({k:v for k,v in fields.items() if k in _ALLOWED})
    log.info(json.dumps(record, ensure_ascii=True))


@contextmanager
def span(stage, **fields):
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        event(stage, started, **(fields | {'outcome':'error','exception_type':type(exc).__name__}))
        raise
    else:
        event(stage, started, **(fields | {'outcome':'ok'}))


@contextmanager
def trace():
    token = trace_id.set(uuid4().hex)
    try:
        yield
    finally:
        trace_id.reset(token)
