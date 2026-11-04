# Gemini backend setup and verification

Text extraction now defaults to Ollama. This document describes the existing Gemini
STT and older debug history; use [OLLAMA.md](OLLAMA.md) for current startup, provider
selection and acceptance tests. Gemini text selection/fallback now uses one attempt.


## Start

```bash
cd nouba-agriflow/backend
source .venv/bin/activate
pip install -r requirements.txt
```

Edit the existing `backend/.env` locally. Do **not** copy `.env.example` over a
working `.env`. Add or update only these Gemini settings in your editor:

```dotenv
GEMINI_API_KEY=<your private Gemini API key>
GEMINI_MODEL=gemini-flash-latest
# Optional; when blank, transcription uses GEMINI_MODEL:
GEMINI_STT_MODEL=
```

The model must support audio input and structured JSON generation in your account.
The example alias is documented by Google's SDK, but availability and quotas are
account-dependent. Set an explicit available model if preferred. A missing model
is reported as a configuration error, not silently replaced by another provider.
Configuration loads `backend/.env` by its absolute location; shell environment
variables take precedence. Never put the key in a command, frontend or commit.
Existing old AI settings in your private `.env` are ignored; the implementation
does not edit or remove private settings.

```bash
uvicorn app.main:app --reload --workers 1
```

Keep the existing ngrok command, URL registration, verification token, app secret,
access token and phone-number ID. Verification/HMAC and the Meta send request are
preserved; safe diagnostics now wrap parsing, processing and sending.
Do not run multiple API workers for this MVP: per-farmer processing locks are
in-process. A durable queue/ordering strategy is needed before scaling workers.

## Automated tests

```bash
python -m unittest discover -s tests -v
```

Tests use temporary SQLite databases and mock network boundaries. They cover
voice download/transcription wiring, OGG bytes and MIME normalization, Darija
text preservation, schema validation, multi-turn memory, missing-field questions,
corrections, explicit confirmation, duplicate delivery, persisted reply retries,
legacy drafts, rate limits, invalid/empty output and unavailable models.
`test_sdk_smoke.py` additionally checks actual installed SDK types and async client
wiring without contacting Google; it skips if the package is absent.

Current verification: **30 tests passed**, including the installed google-genai
1.75.0 SDK over mock HTTP transports and the full signed-text handler pipeline.
These tests do not prove real WhatsApp delivery. A separate synthetic live Gemini
probe encountered a transport ConnectError in this execution environment.
No real WhatsApp delivery or voice acceptance test was completed here.

## Confirmed schema bug and fix

Effective GEMINI_API_KEY and GEMINI_MODEL matched `backend/.env` at inspection.
The text handler calls `extract`, which uses `config.GEMINI_MODEL`; there is no
hardcoded replacement model. Startup now logs matching booleans and the effective
model, never the key. Restart is required after file changes because configuration
is loaded at process import. Existing exported environment settings take precedence.

The active SQLite database contained seven `gemini_invalid_json` failures.
Reproducing SDK conversion locally with version 1.75.0 raised ValidationError at:
`properties.area_hectares.exclusiveMinimum` (`extra_forbidden`). Pydantic generates
this keyword for `Field(gt=0)`. Passing the Pydantic class through `response_schema`
caused the SDK to convert it through its restricted Schema type **before HTTP**.
The broad ValidationError handler mislabeled that failure as invalid model JSON,
and the service sent a generic fallback. A second send failure was silently swallowed.
The earlier SDK smoke test mocked `generate_content` itself and bypassed this
serialization, which is why it did not detect the problem.

The fix uses `response_json_schema=Model.model_json_schema()` and still validates
the returned JSON with Pydantic. SDK request-configuration errors now have the
separate `gemini_schema_configuration_error` code. The new SDK transport tests run
real serialization and inspect the outbound JSON without contacting a provider.

## Restart and perform one real text test

1. Stop the currently running FastAPI process with Ctrl-C in its terminal. Leave
   the existing ngrok session running and retain all current `.env` values.
2. Start from the backend directory using its existing virtual environment:

```bash
cd /home/davinci/hakathon/nouba-agriflow/backend
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --log-level info
```

Use the same port your existing ngrok tunnel forwards to if it differs from 8000.
Do not start a second backend alongside the old process. No token rotation,
Meta setting change, Vercel change or new ngrok configuration is required.

3. At startup inspect `configuration.gemini`: `key_configured`, `key_matches_env`
   and `model_matches_env` should be true; the effective model should match your
   successful standalone test. Do not paste your key or private `.env` into logs.
4. Send a **new real WhatsApp text** from an allowed test recipient:

   “بغيت تراكتور للحرث، عندي جوج هكتارات فدكالة نهار 10 فبراير 2027”

5. Look for `webhook.parsed`, then one message trace with `conversation.load`,
   `gemini.configuration`, `gemini.inference`, `gemini.response_validation`,
   `conversation.persist`, `reply.persist`, `meta.send_http` and `reply.delivery`.
   Expected result is a summary asking for confirmation (or one question if a
   field could not be extracted). Verify the reply **on the actual phone**.
   `meta_http_status=200` only proves Meta accepted the send, not delivery.
6. Optionally reply `نعم` to complete the confirmation acceptance test: one
   `pending_review` request should be saved, with the no-confirmed-booking reply.

Structured log lines include random trace_id, stage, duration_ms and safe error
metadata. They omit phone numbers, message bodies, keys, URLs and raw provider
exception text. A 503 produces bounded retries; a 429 produces a quota-specific
fallback without immediate retries. A `meta.send` error reports the HTTP status
and numeric Meta error code, including a possible expired temporary token; no
credentials are changed automatically. `reply.fallback_failed` records previously
silent failures to deliver the fallback itself.

## Real WhatsApp voice acceptance test

This test needs your configured Meta test recipient, running backend/ngrok and a
Gemini key with an available model and quota. It uses actual WhatsApp media; do
not replace it with a fabricated audio URL or mocked transcript.

1. From the allowed WhatsApp number, record a normal voice note, for example:
   “بغيت تراكتور للحرث، عندي جوج هكتارات فدكالة”. Use your natural Darija.
2. The bot should download the actual OGG/Opus voice note, transcribe it and ask
   one question for the first missing field (for this example, the exact date).
   It must not translate your wording into a different language or invent a date.
3. Reply with an explicit full date, e.g. `10/02/2027` (or a future date appropriate
   when you run this test). The bot should show the complete request summary.
4. Before confirming, verify `/api/requests` has no **new** request for this flow.
   Draft data belongs to `conversations`; the transcript belongs to `messages`.
   The active conversation and recent assistant/user turns survive a restart.
5. Reply with a correction, e.g. “لا، ثلاثة هكتارات”. Check the new summary shows
   three hectares and still has the other details. It still must not be saved as
   a request. If the transcript is wrong, correct it before confirming.
6. Reply `نعم`. Exactly one new request should appear with `pending_review`.
   The acknowledgement must say that this is not a confirmed booking.
7. Send an unintelligible/silent voice note: expect a request to resend clear
   audio or text, not an invented transcript or confirmed request.

Inspect records only locally with authorized database access; phone numbers,
transcripts and locations are private. The existing API still has no operator
authentication. Do not expose `/api/*` publicly as part of this test.

## Processing and errors

- Audio is sent inline using the official `google-genai` SDK. WhatsApp OGG/Opus
  bytes remain in their OGG container and use `audio/ogg` (codec parameters are
  removed). No transcoding, fake transcript or local audio file storage is used.
- The inline audio limit is 14 MiB to leave room for base64/request overhead.
  Larger clips request shorter audio/text rather than uploading persistent files.
- Gemini returns schema-bound JSON; Pydantic validates types, intent, dates,
  positive finite area and rejects extra fields. Model prompts prohibit invented
  facts, translation and inferred bookings. These controls cannot guarantee model
  accuracy: the farmer must check a full summary and explicitly confirm it.
- Drafts/history live in `conversations`. Assistant replies are also stored in
  `conversation_turns` for delivery retry. Only a confirmation after successful
  summary delivery inserts a `pending_review` request. Old `draft` rows remain
  intact and can be resumed/reconfirmed; confirmed old rows are left intact.
- Errors use safe codes in `messages.error`, never raw provider exception text.
  `gemini_rate_limited` means 429 and is not immediately retried. Transient 5xx
  calls get at most three attempts, 15 seconds per HTTP request, 1/2-second
  backoff and a 50-second overall inference budget.
  `gemini_model_or_input_unavailable` means a 400/404 response: check model access,
  model capabilities and input. Authentication failures, missing config, network
  errors, invalid JSON, empty output and unclear audio have separate codes.
- Message errors remain retryable. Cached successful processing results prevent
  repeated inserts if WhatsApp reply delivery fails. There is no durable queue;
  a process crash can still interrupt background work. A farmer may need to resend
  a message after a failure. No automatic successful delivery is promised.

Review deletion handling for the new `conversations` and `conversation_turns`
tables as well as existing messages/requests. Public privacy content was updated
to disclose Google Gemini, conversation memory and historic provider processing.

Official references:
- https://googleapis.github.io/python-genai/
- https://ai.google.dev/gemini-api/docs/audio
