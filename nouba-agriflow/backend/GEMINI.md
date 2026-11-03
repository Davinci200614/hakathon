# Gemini backend setup and verification

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
access token and phone-number ID. No Meta webhook or sending code was changed.
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

Implementation-environment result: 22 offline tests passed; the installed-SDK
smoke test was skipped. Installing google-genai was blocked by network DNS access.
A live Gemini request and real WhatsApp audio test were not performed here.
Run the suite again after installation; the SDK smoke test should then pass.

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
  `gemini_rate_limited` means 429; transient 429/5xx calls get up to three attempts.
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
