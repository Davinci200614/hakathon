import json
import asyncio
import logging
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from . import config
from .db import connect, init_db
from .whatsapp import verify_signature
from .service import handle_message
from .diagnostics import event, span

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nouba")

@asynccontextmanager
async def lifespan(app):
    with span('database.initialize'):
        init_db()
    event('configuration.gemini', **config.diagnostics())
    if config.CONVERSATION_CORE != 'v2':
        event('configuration.llm', provider=config.LLM_PROVIDER, model=config.OLLAMA_MODEL if config.LLM_PROVIDER == 'ollama' else config.GEMINI_MODEL, stt_provider=config.STT_PROVIDER)
    if config.CONVERSATION_CORE == 'v2':
        from .understanding import selected_provider, selected_model
        provider = selected_provider()
        event('configuration.dialogue', provider=provider, model=selected_model(), state='v2')
    # HTTP library INFO output can contain private media URLs; use our safe logs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    yield

app = FastAPI(title="Nouba × AgriFlow API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET","POST","PATCH"], allow_headers=["*"])

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/webhook/whatsapp")
def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and config.VERIFY_TOKEN and p.get("hub.verify_token") == config.VERIFY_TOKEN:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "Invalid verification token")

async def process_messages(messages):
    async def process_one(message):
        try: await handle_message(message)
        except Exception as exc:
            event('webhook.background_error', outcome='error', exception_type=type(exc).__name__)
    # Each phone is serialized in service; independent senders need not wait.
    await asyncio.gather(*(process_one(message) for message in messages))

@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request, tasks: BackgroundTasks):
    started = time.monotonic()
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256")):
        event('webhook.signature', started, outcome='rejected')
        raise HTTPException(403, "Invalid webhook signature")
    try: payload = json.loads(body)
    except ValueError: raise HTTPException(400, "Invalid JSON")
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            messages.extend(change.get("value", {}).get("messages", []))
    event('webhook.parsed', started, outcome='ok', message_count=len(messages))
    if messages: tasks.add_task(process_messages, messages)
    return {"received":True}

@app.get("/api/requests")
def list_requests():
    with connect() as con:
        rows = con.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 200").fetchall()
    return [{**dict(row),"payload":json.loads(row["payload"])} for row in rows]

@app.patch("/api/requests/{request_id}/status")
def change_status(request_id: int, status: str):
    if status not in ("pending_review","approved","rejected","completed"):
        raise HTTPException(400,"Invalid status")
    with connect() as con:
        row = con.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row: raise HTTPException(404,"Request not found")
        if row["status"] == "draft": raise HTTPException(409,"Draft must be confirmed first")
        con.execute("UPDATE requests SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,request_id))
    return {"id":request_id,"status":status}

@app.get("/api/machines")
def machines():
    with connect() as con:
        return [dict(row) for row in con.execute("SELECT * FROM machines ORDER BY id DESC")]


def require_local_inspector(request):
    allowed = {'127.0.0.1', 'localhost', '::1', 'testserver'}
    forwarded = request.headers.get('x-forwarded-for', '')
    forwarded_host = request.headers.get('x-forwarded-host', '').split(':')[0]
    if (not request.client or request.client.host not in allowed | {'testclient'}
            or request.url.hostname not in allowed
            or any(part.strip() not in allowed for part in forwarded.split(',') if part.strip())
            or (forwarded_host and forwarded_host not in allowed)):
        raise HTTPException(403, 'Database inspector is available only locally')


# A built local dashboard is also served by the API, so inspecting persistence
# does not depend on a second Vite process being available.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
app.mount('/assets', StaticFiles(directory=str(FRONTEND_DIST / 'assets'), check_dir=False), name='dashboard-assets')


@app.get('/', include_in_schema=False)
@app.get('/inspect', include_in_schema=False)
@app.get('/privacy', include_in_schema=False)
@app.get('/terms', include_in_schema=False)
@app.get('/data-deletion', include_in_schema=False)
def local_dashboard(request: Request):
    require_local_inspector(request)
    index = FRONTEND_DIST / 'index.html'
    if not index.exists():
        raise HTTPException(503, 'Build frontend first: cd frontend && npm run build')
    return FileResponse(index, headers={'Cache-Control':'no-store'})


@app.get('/api/debug/snapshot')
def database_snapshot(request: Request, phone: str | None = None):
    require_local_inspector(request)
    if phone and len(phone) > 64:
        raise HTTPException(400, 'Invalid phone filter')
    from .db import database_path, SCHEMA_VERSION
    with connect() as con:
        counts = {name: con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                  for name in ('messages','requests','employment_listings','machines','conversations','dialogue_drafts')}
        condition, params = ('WHERE m.farmer_phone=?', [phone]) if phone else ('', [])
        messages = [dict(row) for row in con.execute(f'''SELECT m.id,m.farmer_phone,m.kind,m.text,m.status,m.error,m.created_at,
            a.route,a.error_code,a.duration_ms,t.text AS reply
            FROM messages m LEFT JOIN dialogue_audit a ON a.message_id=m.id
            LEFT JOIN conversation_turns t ON t.message_id=m.id AND t.role='assistant'
            {condition} ORDER BY m.rowid DESC LIMIT 50''', params)]
        where, values = ('WHERE phone=?', [phone]) if phone else ('', [])
        drafts = [dict(row) for row in con.execute(f'SELECT phone,task,pending,payload FROM dialogue_drafts {where}', values)]
        where_machine = 'WHERE farmer_phone=?' if phone else ''
        drafts += [dict(phone=row['farmer_phone'],task='machine',pending=row['last_question_field'],payload=row['payload'])
                   for row in con.execute(f'SELECT * FROM conversations {where_machine}', values)]
        from .contacts import valid_contact
        records = [{**dict(row), 'contact_format_valid':bool(valid_contact(row['contact']))} for row in con.execute('SELECT id,kind,name,location,work,details,contact,status,is_demo,owner_phone FROM employment_listings ORDER BY is_demo,id DESC LIMIT 100')]
        equipment = [dict(row) for row in con.execute('SELECT * FROM machines ORDER BY is_demo,id DESC LIMIT 100')]
        saved = [dict(row) for row in con.execute('SELECT id,farmer_phone,payload,status,created_at FROM requests ORDER BY id DESC LIMIT 50')]
        from .catalog import terms as catalog_terms
        terms = catalog_terms(con)
    return {'database_path': str(database_path()), 'schema_version': SCHEMA_VERSION, 'counts': counts,
            'messages': messages, 'drafts': drafts, 'listings': records, 'machines': equipment,
            'requests': saved, 'catalog': terms}
