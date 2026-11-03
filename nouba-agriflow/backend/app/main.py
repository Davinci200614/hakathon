import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from . import config
from .db import connect, init_db
from .whatsapp import verify_signature
from .service import handle_message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nouba")

@asynccontextmanager
async def lifespan(app):
    init_db()
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
    for message in messages:
        try: await handle_message(message)
        except Exception: log.exception("Failed to process WhatsApp message")

@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "Invalid webhook signature")
    try: payload = json.loads(body)
    except ValueError: raise HTTPException(400, "Invalid JSON")
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            messages.extend(change.get("value", {}).get("messages", []))
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
