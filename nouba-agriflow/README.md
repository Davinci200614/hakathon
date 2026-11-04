# Nouba × AgriFlow — WhatsApp-first MVP

Farmers communicate via the official WhatsApp Cloud API (text or voice). Audio is transcribed, an LLM extracts structured fields, and a cooperative dashboard reviews confirmed requests. **This is a development starter, not a production-ready booking platform.**

## Responsive WhatsApp conversations

Open http://localhost:8000/inspect (built dashboard), or `/inspect` on your running dashboard, to watch WhatsApp inputs, stored
replies, drafts, published records and model errors. Or run `./inspect.sh` from
the workspace. See [backend/DATABASE_INSPECTOR.md](backend/DATABASE_INSPECTOR.md)
for a step-by-step persistence test and the clearly marked fictional demo data.

The v2 conversation controller routes machinery, worker searches, job searches,
worker profiles and job offers separately. Simple answers bypass the model;
other messages use one structured understanding call with a six-second deadline.
With `CONVERSATION_PROVIDER=auto`, configured Gemini is used for understanding;
otherwise Ollama is used. Voice transcription remains Gemini. Existing secrets
and Meta settings are preserved. See [backend/CONVERSATION_CORE.md](backend/CONVERSATION_CORE.md)
for configuration, acceptance tests, latency checks and rollback.

## Start backend (Ubuntu)

To launch the backend, dashboard and local Ollama together from the workspace:

```bash
cd /home/davinci/hakathon
./start.sh
```

The script activates the backend virtual environment and installs dependencies if
missing. Press Ctrl+C to stop the processes it starts. An existing Ollama service
is reused. Install the configured Ollama model beforehand (see backend/OLLAMA.md).
Use `./start.sh --ngrok` to also launch an already configured ngrok tunnel for
WhatsApp; register its HTTPS URL in Meta if the tunnel URL changes.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your own secrets (NEVER commit it)
uvicorn app.main:app --reload
```

API docs: http://localhost:8000/docs; health: http://localhost:8000/health

## Start dashboard (second terminal)

```bash
cd frontend
npm install
npm run dev
```

Dashboard: http://localhost:5173

## WhatsApp setup

### Guided choices alongside natural chat

Send `menu`, `الاختيارات`, or `0` to open a tappable Darija menu. You can also
reply with the menu number:

1. Publish a fresh worker profile.
2. Publish a fresh employer job offer.
3. Search job offers.
4. Search workers.
5. Request machinery.
6. View your listings.
7. Start a fresh conversation.

An eighth choice resumes the current draft when one exists. Guided mode asks for
missing information, offers common work/machinery choices, lets you choose a
contact method, then shows confirm/edit/cancel buttons. Editing opens a field
list. You can write natural Darija instead of using a choice at any point;
both paths share the same controller and validation.

New profiles start fresh. Resuming an unfinished profile is explicit, so a new
search offer does not silently copy a previous profile's city or occupation.
Contact must include an actual number, email, @handle or profile URL; a channel
name such as “WhatsApp” is incomplete. The current WhatsApp number is inserted
only when the user explicitly chooses or requests it, and is shown before
publication. Morocco-local phone numbers must contain ten digits; international
numbers use international format. Validation checks format, not account ownership.
Natural approval such as `Sf mzyan 3afak nshrha` confirms only a current,
delivered, complete summary. Selecting stale choices cannot publish an old draft.

### Workers and employers

Send `emploi` or `عمال` on WhatsApp to see the employment menu. Text and
transcribed voice use the same guided flow:

Common natural Darija and Arabizi phrases also route directly, for example
`ah bghit wahed dri 25dm 3ndi` (search workers), `كنقلب على عمال فمكناس`
(search workers in the given location), `كنقلب على خدمة ففاس` (search jobs),
`بغيت نسجل راسي كعامل` (post a worker profile), and `بغيت ننشر عرض عمل`
(post a job offer). Replies are in Darija. Common clear answers take a fast path;
other messages use the model to classify purpose and extract all supplied facts.
The bot asks for missing details. Switching tasks preserves separate drafts and
disarms old confirmations to prevent accidental saves.

When completing a profile, occupation and salary answers stay in that profile.
After a search, the bot can start the appropriate listing from natural replies
such as `نشر إعلان ديالي`. Send `بدا من جديد` or `reset` to discard all unfinished
drafts and start a fresh conversation; published listings and saved requests stay.

| Command | Action |
| --- | --- |
| `post worker` / `نشر عامل` | Create or update your worker profile |
| `post job` / `نشر خدمة` | Create or update your employer's job offer |
| `find workers` / `بحث عمال` | Search worker profiles by city or village |
| `find jobs` / `بحث خدمة` | Search employers' job offers by city or village |
| `my listings` / `إعلاناتي` | Show your active listings |
| `remove worker` / `حذف عامل` | Remove your worker profile from search |
| `remove job` / `حذف خدمة` | Remove your job offer from search |

Posting asks for name, city/village, work/skills, details (experience,
availability, pay, dates or headcount), and a contact method to publish.
Only a `yes` / `نعم` after the delivered summary publishes these details to
other WhatsApp users. The sender's WhatsApp number is not automatically shared.
Each sender can have one worker profile and one job offer; posting again updates
that listing after confirmation. `cancel` / `إلغاء` discards the employment draft
without affecting machinery requests. `modifier` / `تعديل` at confirmation
restarts collection of the listing fields.

Search returns up to five most recent active listings using literal,
case/accent-insensitive location matching. A small dictionary recognizes spelling
variants for Inzegane, Boulemane, Meknes, Fes and Salé Al Jadida. Listings are self-reported; employer verification,
matching/ranking, multiple job offers per employer, and a dashboard for these
records are not implemented. Data is stored in additive SQLite tables
`employment_drafts` and `employment_listings` created at API startup.

1. Create a Meta developer app with WhatsApp Cloud API and obtain a **test phone number**, phone number ID, access token and app secret.
2. Set `META_VERIFY_TOKEN`, `META_APP_SECRET`, `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID` in `backend/.env`.
3. Set `GEMINI_API_KEY` and `GEMINI_MODEL` for Gemini audio transcription and optional remote extraction. Local Ollama text extraction does not need a Gemini key. Optionally set `GEMINI_STT_MODEL`; otherwise it uses `GEMINI_MODEL`. Without configuration, the bot reports a processing failure and asks the farmer to retry. See [backend/GEMINI.md](backend/GEMINI.md).
4. Expose port 8000 over an HTTPS tunnel (e.g. ngrok). Register `https://YOUR_HOST/webhook/whatsapp` as the Meta webhook URL, with your verify token.
5. Subscribe to the WhatsApp `messages` webhook field. Add your own phone as an allowed recipient in Meta's test configuration.
6. Send a real message to the test number. Inspect `/api/requests` and the dashboard.

### Data flow

`WhatsApp webhook -> signature validation -> message deduplication -> STT (if audio) -> intent and fact validation -> selected task draft or database search -> Darija follow-up -> delivered summary -> explicit confirmation -> request (pending_review) or published employment listing`

The dashboard currently reads requests and approves/rejects them; scheduling, machine availability, pricing, outbound approval notifications and real operator authentication are **not yet implemented**.

### Security and production TODOs

- Add cooperative-user authentication and authorization to all `/api/*` endpoints **before exposing the dashboard publicly**.
- Replace FastAPI in-process background tasks with a durable job queue; implement retry and dead-letter handling for WhatsApp delivery and AI errors.
- Add explicit farmer consent, retention/deletion rules, encrypted storage, rate limits and audit logs.
- Implement conversation states robustly (multiple simultaneous requests, correction intents, relative dates, language variants), and improved handling of ambiguous dates. Gemini output is now validated with Pydantic.
- Store audio only when necessary; do not expose sensitive phone numbers in dashboards without access control.
- Test STT accuracy on real Moroccan Darija samples, including rural dialects and noisy audio.
- Implement machine inventory, availability and booking conflict checks before offering any confirmed slot.
- Use a supported Meta Graph API version; update `META_GRAPH_VERSION` as needed.

## Project tree

- `backend/app/main.py`: FastAPI routes and Meta webhook
- `backend/app/whatsapp.py`: Meta verification, media download and replies
- `backend/app/ai.py`: STT and structured extraction
- `backend/app/service.py`: basic conversation and request workflow
- `backend/app/db.py`: SQLite schema
- `frontend/src/`: React cooperative dashboard

## Public information pages

The frontend includes `/privacy`, `/terms` and `/data-deletion`, linked from its
footer. These pages describe the current implementation and clearly mark owner
information that still needs confirmation. See [frontend/DEPLOYMENT.md](frontend/DEPLOYMENT.md)
for the manual deletion workflow, local preview and frontend-only HTTPS deployment.

## SQLite database

Persistence uses `backend/nouba.db` by default. `DB_PATH` in `backend/.env` accepts
an absolute path or a backend-relative path, consistently across working directories.
Startup applies safe schema migrations. See [backend/DATABASE.md](backend/DATABASE.md)
for the diagnosed persistence fix, schema, SQLite inspection commands and the
non-destructive `python -m scripts.check_database` check.
