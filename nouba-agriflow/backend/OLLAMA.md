# Ollama-first Nouba MVP

Text: WhatsApp → validated Ollama extraction → Python state machine → SQLite → WhatsApp.
Voice: WhatsApp audio → existing Gemini STT → same Ollama extraction/state machine.
Only explicitly confirmed requests enter `/api/requests` as `pending_review`.
No automatic availability claim, cooperative approval or booking is created.

## Configuration

The implementation defaults to local Ollama without requiring any `.env` edit.
Existing private `.env` values and Meta sending/HMAC code are unchanged. Add these
non-secret settings to your existing `backend/.env` if you want explicit values;
do not replace the file with `.env.example`:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:1.7b
OLLAMA_TIMEOUT_SECONDS=35
OLLAMA_NUM_CTX=1024
OLLAMA_NUM_PREDICT=180
OLLAMA_KEEP_ALIVE=2m
LLM_FALLBACK_PROVIDER=
STT_PROVIDER=gemini
```

For optional remote text fallback set `LLM_FALLBACK_PROVIDER=gemini`. It runs only
when the primary fails and both existing `GEMINI_API_KEY` and `GEMINI_MODEL` are
configured. No fallback is enabled by default. `LLM_PROVIDER=gemini` explicitly
selects remote text extraction. `STT_PROVIDER=gemini` is independent; retain your
existing Gemini key/model and optional `GEMINI_STT_MODEL` for voice recognition.
No new dependencies or model downloads are required.

## Start on the laptop

If Ollama's existing service is already running, use it; do not start another
server. Otherwise, in terminal 1:

```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=1024 ollama serve
```

These settings apply when launching the server, not just the CLI client. If
Ollama runs as a service, configure equivalent server settings there. Keep this
instance local. No larger model is installed or automatically pulled.

In terminal 2:

```bash
cd /home/davinci/hakathon/nouba-agriflow/backend
.venv/bin/python -m scripts.check_ollama
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --log-level info
```

Stop your old FastAPI process first. Use the same backend port as your existing
ngrok forwarding. Keep ngrok, Meta settings and credentials as they are. Use a
single API worker; its per-farmer and inference locks are in-process.

`check_ollama` calls `/api/version`, `/api/show` for the configured model, then
performs actual extraction of a synthetic Darija sentence. It reports capability
metadata, latency and whether tractor/plowing/2 hectares were extracted without
inventing a location. It never calls Gemini, sends WhatsApp messages or reads
farmer records. An HTTP 404 means the selected model is not available locally.

## Low-memory behavior

- One local inference at a time across farmers, including queue timeout.
- `/api/chat`: `stream:false`, `think:false`, compact JSON Schema in `format`.
- Qwen prompt also includes `/no_think`; no reasoning text is parsed as facts.
- `options`: temperature 0, num_ctx 1024, num_predict 180. keep_alive is 2m.
- Only current message (up to 600 characters) and one question are sent, not
  entire chat history or draft. Python merges non-null new fields into SQLite.
- The local HTTP call is bounded by 35 seconds including generation; no repeated
  local inference on invalid JSON. Parsing gets one fenced-JSON syntax recovery;
  invalid/truncated/schema-invalid output fails safely.
- Optional Gemini text fallback makes **one** request with a 16-second overall
  budget (15-second HTTP timeout), not three long 503/504 retries. STT retains
  its existing independent Gemini implementation.
- Queue waiting plus extraction/fallback is bounded. CPU performance varies;
  use logged `llm.inference.duration_ms` to measure on the actual laptop.
- Model normalization covers tractor/plowing and common variants. Darija number
  words immediately before hectares have a small deterministic supplement.
- Date arithmetic uses `Africa/Casablanca`. Today/tomorrow/day-after-tomorrow and
  “next Tuesday” resolve to explicit dates. “Next weekday” means its next strictly
  future occurrence. Bare weekdays/yearless numeric dates stay missing, so Python
  asks for clarification. Full dates are always shown before confirmation.

## Complete real WhatsApp conversation

From your allowed real test phone, send these as separate messages:

1. `بغيت تراكتور يحرث ليا جوج هكتارات`
   Expected: one location question, with machine/operation/area remembered.
2. `فالرماني`
   Expected: one date question. Verify the place name in the later summary.
3. `الثلاثاء الجاي`
   Expected: complete summary with the resolved YYYY-MM-DD calendar date.
4. `لا، ثلاثة هكتارات`
   Expected: new summary with 3 hectares and the same machine, operation, place
   and date. Nothing should be in the dashboard as a new confirmed request yet.
5. `نعم`
   Expected: one `pending_review` request via the existing dashboard API and an
   acknowledgement that cooperative review is pending, not a confirmed booking.

During any unconfirmed draft, `إلغاء`, `لغي الطلب`, `annuler` or `cancel` cancels
that draft without creating a booking. This does not cancel an already confirmed
request. `تعديل` prompts for the specific correction. Previously supplied fields
survive missing/null fields in later extraction. Failed inference preserves state.

Verify messages on the actual phone. Logs only prove stages/Meta acceptance, not
final delivery. Normal duplicate webhooks are skipped after successful delivery,
including successfully sent error replies. Retryable failed sends reuse cached
replies and cannot insert the same confirmed request again. As with any remote
send API without an idempotency key, an ambiguous network failure after Meta has
accepted a send cannot provide an absolute exactly-once delivery guarantee.

## Verification and limits

Verification result: **51 tests passed, 0 failed**, including installed-Gemini-SDK
serialization, Ollama HTTP contracts, concurrent duplicate handling, voice-to-local
extraction, dashboard visibility and independent provider selection. The frontend
production build also passed after the privacy-copy update.

Automated tests use temporary SQLite and mock HTTP transports; no keys, farmer
messages or production database writes are involved. Actual Qwen quality/speed
and real WhatsApp delivery must also pass the live checks above. In the agent
execution environment, the Ollama CLI was present, but localhost:11434 was not
reachable and `ollama serve` was blocked by read-only Ollama state storage. The
live check returned ConnectError. No model was installed and no latency/accuracy
claim is made for the actual laptop.

Relevant files: `app/llm.py` (provider interface/adapters), `app/extraction.py`
(schema/normalization/dates), `app/ai.py` (facade and Gemini STT), `app/service.py`
(deterministic state/merge/cancel), `app/config.py` (selection/tuning),
`app/diagnostics.py`, `app/main.py`, `.env.example`, `scripts/check_ollama.py`,
and tests. The public privacy copy now describes local extraction and optional
remote fallback; no frontend design or routes changed.

Official API references:
- https://docs.ollama.com/api/chat
- https://docs.ollama.com/capabilities/structured-outputs
- https://docs.ollama.com/capabilities/thinking
