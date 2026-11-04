# See WhatsApp messages and database records

Open **http://localhost:8000/inspect** after the frontend has been built
(`cd frontend && npm run build`). The API serves the built local dashboard,
so this URL also works without a running Vite server. Alternatively open the
dashboard URL printed by `./start.sh`, then add `/inspect` to it.
The dashboard also has a **WhatsApp → Database inspector** link. The inspector
refreshes every five seconds and is available only through localhost.
The public ngrok hostname is denied access to these chat logs.

## What to check

1. Send `menu` on WhatsApp. It does not require a model call.
2. Open **messages**. The exact incoming text or selected interactive title,
   timestamp, sender, handling status and persisted assistant reply should appear.
3. Start a worker profile and fill one field. It appears in **drafts**.
4. Complete the profile and confirm its summary. It moves to **listings**.
5. A confirmed machinery request appears in **requests**, not in worker listings.

`processed` means the message/reply handler finished. It does not mean a listing
was published or that model inference succeeded. **Model error** and **route**
show that distinction. `conversation_rate_limited` means the configured provider
returned HTTP 429; `conversation_timeout` means the six-second deadline expired.
Old messages predating the audit table have no model audit entry.
The inspector's duration measures the controller turn, excluding Meta delivery.

From the workspace terminal:

```bash
./inspect.sh --limit 10
./inspect.sh --phone 212YOURNUMBER --limit 20
```

The command prints the actual SQLite path, persisted incoming/outgoing messages,
model errors and table counts. It sends no WhatsApp messages. These logs contain
personal conversation data; keep this local diagnostic view local.

## Catalog matching

`catalog_terms` holds canonical machine, profession and place codes, labels and
aliases. Search uses these codes for matching both queries and stored records:
`تريسيان`, `كهربائي`, and `electrician` match the same profession, while
`فسلا جديدة` and `سلا الجديدة` match the same locality. Searches can filter
profession and place together. Follow-up refinements reuse the last search city.
Public directory queries read only active, published employment listings;
they do not expose the private farmers table or message history.

New inventory names/types and newly published professions are included in the
runtime catalog automatically. Explicit aliases can be added to `catalog_terms`
without editing phrase-routing code. Unknown descriptions sent to the model
include the real catalog's canonical names and nearby aliases for grounding.
The model still needs available provider quota for genuinely unfamiliar wording.
No fine-tuning is performed by adding aliases or demo records.

## Fictional demo records

The requested demo seed adds **8 worker profiles, 6 job offers and 6 machines**.
All have `is_demo=1`, fictional names/details and reserved `.invalid` contacts.
WhatsApp results visibly mark them as fictional and do not present their contact
as real. Real records are returned before demo results. No messages, real
profiles or confirmed requests are overwritten by seeding.

To re-run the idempotent seed from `backend`:

```bash
.venv/bin/python -m scripts.seed_demo
```

The CLI creates a timestamped SQLite backup before migrating/seeding. Demo data
is created only by this explicit command; ordinary server startup does not add it.
Availability and rates in demo equipment are fictional, not actual offers.

## Actual findings during this change

The existing database contained 95 incoming messages, one published worker
profile, zero machinery requests and zero machines. The worker's location was
`سلا جديدة` and occupation `تريسيان`. Fixing attached location prefixes and
catalog matching made that real record discoverable. After the demo seed, a
query for electricians in Salé Al Jadida matched that real record plus two
clearly marked demo workers; the inventory lookup found two demo harvesters.

A synthetic live model request returned HTTP 429. Quota failure is distinct from
poor intent understanding. The client now honors a bounded cooldown instead of
repeatedly calling a limited model. Local catalog searches and guided choices
continue working. Restore provider quota/billing or configure an available
understanding provider/model to regain open-ended inference. No billing settings
or credentials were changed automatically.
