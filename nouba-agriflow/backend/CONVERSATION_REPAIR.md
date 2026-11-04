# Conversation intelligence repair

## Root causes

1. **Extraction and missing Python validation.** The old normalizer only mapped machine/operation aliases when the model supplied those fields. It never inferred `تراكتور` or `يحرث` from the message and accepted literal `unknown`. It supplemented `جوج هكتارات` as area but did not reject `جوج` as a location. Thus the false location originates in a model patch (or a previously saved patch), not a Python rule that converts area into location. Python allowed that output to become valid-looking state. The regression injects exactly this malformed Ollama response and proves its rejection and deterministic recovery. Historical raw Ollama responses were not available, so this is a code-path diagnosis, not a claim to have replayed the original live inference.

2. **Missing extraction context and correction interpretation.** `llm.extract` discarded `previous` from the provider prompt and sent only the last assistant question. `لا`/`تعديل` changed only the confirmation flag, leaving the old question/history as extraction context. The actual merge already replaced every non-null field, and SQLite persisted that merged payload; neither implemented a first-value-wins rule. A null or stale extraction therefore retained the old location. The repair persists a correction marker in the existing `last_question_field` column, passes it and the current draft to extraction, and parses explicit positive location clauses before `مشي`/`ماشي` in Python. Valid replacements win; invalid/null patches cannot erase valid facts. No SQLite migration is necessary: pending field, attempts, history, payload, and confirmation flag already have durable columns.

3. **Python date parsing and completeness checks.** Year-first dates accepted only hyphens; slash dates accepted only day-first syntax. `2026/11/03` therefore fell through to the ambiguous-date detector. Confirmation checked `is None`, so `unknown` passed completeness checks. Python now recognizes year-first slash/dot/hyphen dates using `datetime.date`; relative dates use `ZoneInfo('Africa/Casablanca')`. Formatted dates bypass inference. A common missing-fields check rejects placeholders and quantity locations, and treats unresolved date clarification as incomplete. An ambiguous replacement retains the saved date but blocks confirmation until clarified.

## Behavior and boundaries

- Ollama extracts facts only. Python owns normalization, state, corrections, questions and explicit submission.
- Recognition covers existing machinery/operation aliases, Darija area words and numeric hectare values.
- Question attempts persist in SQLite; successive failed attempts receive differing clarification text.
- `لا` and `تعديل` request a correction; cancellation deletes the active draft; `نعم` submits only after delivery of a complete summary.
- Submission retains the existing `requests.status = pending_review` contract. It does not create a confirmed machine booking or change the separate scheduling `bookings` table.
- Existing message deduplication and transactional submission remain intact.
- Meta authentication, delivery code, infrastructure, model selection and frontend were not changed by this repair.

## Example (actual tested wording)

Farmer: بغيت تراكتور يحرث ليا جوج هكتارات
Nouba: فين كاينة الأرض ديالك؟
Farmer: دكالة
Nouba: فاش بغيتي التراكتور يخدم ليك؟
Farmer: 2026/11/05
Nouba: tractor / plowing / 2.0 هكتار / دكالة / 2026-11-05; asks نعم or تعديل.
Farmer: لا
Nouba: شنو المعلومة اللي بغيتي تصحح؟
Farmer: أنا ف بني مسكين مشي دكالة
Nouba: tractor / plowing / 2.0 هكتار / بني مسكين / 2026-11-05; asks نعم or تعديل.
Farmer: نعم
Nouba: تسجل الطلب ديالك وغادي تراجعو التعاونية. هادي ماشي حجز مؤكد.

Exactly one pending-review request is saved, including duplicate delivery of the confirmation.

## Validation

Run from `backend`:

```
.venv/bin/python -m unittest discover -s tests
```

`test_conversation_intelligence.py` exercises the exact conversation with deliberately wrong model output, persisted correction context across service reload, formatted-date inference bypass, duplicate confirmation, unknown-field rejection, null preservation, positive correction clauses and varying clarification questions. Existing suites additionally cover failed extraction, partial messages, actual separate Python-process/FastAPI imports resuming SQLite state, provider transport validation, webhook handling and transaction failures. Provider and Meta HTTP are mocked; no real farmer messages or bookings are sent.
