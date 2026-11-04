# Intent-first WhatsApp conversation core

Schema version 5 adds the database-backed catalog and durable per-message model
audit. Published professions and inventory names are discovered from actual
records; aliases ground search and model interpretation. See
[DATABASE_INSPECTOR.md](DATABASE_INSPECTOR.md) for the local `/inspect` view,
terminal inspection, demo seed and the observed provider quota failure.

## Guided choices and transcript corrections

Send `menu`, `الاختيارات`, `help`, or `0` for a native WhatsApp list. Seven
choices start a fresh worker profile, employer offer, job search, worker search,
machinery request, listings view, or reset. An eighth choice resumes the current
draft when present. Work/machinery choices, contact-method buttons, summary
confirm/edit/cancel buttons and an editable-field list use the same controller
as text and voice. A native full-screen WhatsApp Flow is not required for this
guided chat path. Main-menu numbers and current-option numbers also work;
long phone numbers and numeric fields are not treated as menu selections.

`guided.py` builds native list/button payloads following
[Meta's Cloud API list-message example](https://www.postman.com/meta/whatsapp-business-platform/request/r3ufenw/send-list-message)
and [interactive-message formats](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/messages/interactive/).
The Graph API integration uses these JSON formats directly, not the archived
Node SDK. Schema version 4 adds `dialogue_outgoing` to cache the exact payload
with the persisted assistant turn. Delivery retries reuse it. Option IDs are
bound to the sender's latest prompt; stale or copied IDs are rejected.
Long summaries are sent intact as text instead of truncating confirmation data
to fit an interactive body. Natural confirmation remains available.

Starting a new profile or accepting a search's publication offer creates fresh
profile fields, with only the latest search city prefilled. Explicit resumption
keeps the existing draft. Labeled new facts can correct name, city, occupation
and salary while another field is pending. `clear_fields` lets an explicit
denial remove an old field rather than preserving it as if it were known.

`contacts.py` requires a concrete number, email, @handle or profile URL. A bare
channel name is incomplete. Moroccan local numbers use ten digits; international
numbers use 8–15 digits and a nonzero country-code prefix. Dates are not contacts.
This validates format, not ownership. The sender's WhatsApp number is copied
only after explicit selection or a request such as `زيد نمرة الواتساب ديالي`,
then displayed for final confirmation. An invalid contact invalidates the write
summary and triggers a targeted contact question. It never silently publishes.

Natural approvals including `أه عفاك`, `Sf mzyan 3afak nshrha`, and
`عافاك نشر هاد لميساج` are recognized without inference. They still cannot save
an incomplete draft or bypass the delivered-summary hash and expiry checks.
The new transport tests cover signed list/button callbacks and real Graph API
payload serialization against mocked HTTP; handset delivery remains a live test.

The default `CONVERSATION_CORE=v2` replaces the machinery-only conversation
controller. Transport, message deduplication, SQLite requests, dashboard routes
and Meta verification remain in place. No existing data is deleted during startup.

## Processing

1. Verify and acknowledge the Meta webhook. Different senders can process
   concurrently; a sender's messages are serialized in this single API process.
2. Download/transcribe voice when necessary; text skips that step.
3. Recognize standalone approvals/cancellations, full conversation resets,
   explicit commands, clear slot answers, simple dates and corrections without
   inference. Within a profile, the pending question takes priority: an occupation
   such as `حصاد` is a skill, and salary/availability text is profile detail.
4. Otherwise perform one structured understanding call for task, action and
   newly stated facts. The model receives the current draft, pending question,
   Casablanca date, recent search context and up to six recent conversation entries.
   A short Darija explanation can answer a question about the current step without
   abandoning it. The model never executes SQL or chooses arbitrary database actions.
5. Apply validated facts to the chosen task, search active listings or ask the
   next missing question in Darija. Rich messages can fill multiple fields.
6. For writes, present a summary, arm its payload hash only after Meta accepts
   that exact reply, and require a standalone confirmation. Confirmations expire
   after 30 minutes and are invalidated by revisions and topic switches.

`اه وبغيت شيواحد إخدم عندي` switches to worker search. It does not approve a
machinery request. `المكان دكالة` replaces the location. Past dates cannot be
confirmed. After six hours without activity, old context is no longer applied
automatically; drafts remain available for explicit resumption. Repeated replies
without progress offer clarification and an escape rather than an endless summary.

Inside a profile, an uncertain task switch suggested by the model is clarified
before changing the goal. Explicit commands and clear new machinery requests can
still switch directly. After a search the bot offers the relevant publication
flow: a worker profile after job search, or an employer offer after worker search.
References such as `نشر إعلان ديالي` use that context and can reuse the stated city.
Accepting this offer starts a draft; it never publishes immediately.

`بدا من جديد`, `reset`, `/start`, and phrases such as
`بغيت نلغي المسودة و نبدا مسودة جديدة` clear all unfinished drafts and confirmation
grants for the sender. The reset also sets a history boundary, so prior messages
are not sent to the model and failed replies from before the reset are not resent.
Published listings, confirmed requests and retained audit messages are untouched.
This is a conversation reset, not a request to erase all stored personal data.

## Configuration

```dotenv
CONVERSATION_CORE=v2
CONVERSATION_PROVIDER=auto
CONVERSATION_MODEL=
CONVERSATION_GEMINI_MODEL=gemini-3.6-flash
CONVERSATION_THINKING_LEVEL=minimal
CONVERSATION_TIMEOUT_SECONDS=6
```

`auto` uses Gemini when `GEMINI_API_KEY` is configured,
otherwise Ollama. Explicit `gemini` / `ollama` selects one. The optional
`CONVERSATION_MODEL` overrides only understanding. Gemini understanding otherwise
uses `CONVERSATION_GEMINI_MODEL` (default `gemini-3.6-flash`) with minimal thinking;
Ollama uses `OLLAMA_MODEL`. Transcription's existing model is unchanged.
No secrets are printed. Restart the API after `.env` edits. If choosing a model
that does not support minimal thinking, set its supported level explicitly.

Understanding has one total deadline, including queueing. It makes no retries or
slow fallback chain. Failure preserves the draft and returns a Darija help reply.
This six-second budget excludes voice download/transcription, Meta reply delivery
and waiting behind previous messages from the same sender. A local CPU model may
not finish within the budget. A timeout is not successful intent recognition.

Gemini understanding sends message content, draft and short history to Google;
ordinary API billing/quota applies. The user's WhatsApp number is not explicitly
added to the model context, but may be present if supplied inside message text.
Use `CONVERSATION_PROVIDER=ollama` for local text understanding; voice still uses
the separately configured Gemini transcription service.

## Drafts and actions

Machinery drafts remain in `conversations`; separate employment task drafts are
in `dialogue_drafts`. `dialogue_sessions` stores the active purpose and repetition
state. `dialogue_confirmations` stores summary hashes and expiry timestamps.
`dialogue_context` stores recent search/publication offers, pending switch
clarifications and the reset history boundary. Schema version 3 adds this table.
Employment publication uses `employment_listings`; machinery confirmation creates
a `pending_review` request. Contact details appear in listings only after
confirmation. Search returns at most five location matches. Standalone place
prefixes such as `ف بولمان` and `في إنزكان` are removed for matching. A limited
dictionary handles spellings of Inzegane, Boulemane, Meknes, Fes and Salé Al Jadida; this is not
general geocoding. Skill ranking is not implemented.

Useful phrases:

- `رجع للتراكتور` — resume the machinery draft.
- `رجع لبروفايل العامل` — resume the worker profile.
- `رجع لعرض العمل` — resume the employer offer.
- `طلب جديد` — start a fresh machinery draft.
- `إلغاء` — cancel only the current draft.
- `بدا من جديد` / `reset` — clear unfinished drafts and start a fresh conversation.
- `إعلاناتي` / `حذف عامل` / `حذف خدمة` — manage your own listings.

## Verification

From `backend`:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m scripts.check_conversation
```

The v2 acceptance suite in `tests/test_conversation_core.py` exercises the real
message handler and SQLite using mocked understanding/Meta delivery. It covers
the supplied transcript, topic switching, rich-message extraction, stale dates,
revision-specific confirmation, failed delivery, retries, expiry, cancellation,
independent senders, preserved drafts and bounded provider waits.

Pre-v2 behavioral tests explicitly use the legacy core through
`tests/legacy_support.py`; they remain rollback regression coverage. Provider,
webhook and schema tests remain in the full suite. The synthetic live checker
tests provider latency and task recognition without sending WhatsApp messages
or changing database records. Its timings exclude Meta delivery and are not a
production latency guarantee.

During the implementation check on 2026-11-04, `gemini-3.6-flash` recognized
the worker-search and rich machinery examples in 2.26s and 2.45s respectively
through the final direct async transport, including the structured context and
schema. The previously configured `gemini-3.5-flash-lite` and the tested
`gemini-3.5-flash` were inconsistent and exceeded the six-second deadline on
subsequent checks. These two successful samples are not percentile measurements
or a guarantee of provider availability. No WhatsApp messages were sent in these
checks. Fast-path acceptance tests make no provider calls.

## Rollback and remaining limits

Set `CONVERSATION_CORE=legacy` and restart to use the previous controller.
New tables are additive and retained. Legacy mode uses `LLM_PROVIDER` and its
existing fallback settings. It does not use the new per-task confirmation grants.

This remains one API worker with in-process locks/background tasks, not a durable
queue. Restarting during processing can interrupt a reply. Multiple API workers,
durable delivery retries, stale-message ordering across webhook batches,
moderation, verified employer identities and a dashboard for employment records
are separate work. The model can misclassify unfamiliar wording; acceptance
tests and live conversations should continue expanding beyond known phrases.
