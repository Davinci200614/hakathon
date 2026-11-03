import hashlib
import hmac
import httpx
from . import config

GRAPH = "https://graph.facebook.com"

def verify_signature(body: bytes, header: str | None) -> bool:
    if not config.APP_SECRET or not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(config.APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], digest)

async def download_media(media_id: str):
    if not config.ACCESS_TOKEN: raise RuntimeError("META_ACCESS_TOKEN is missing")
    headers = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await client.get(f"{GRAPH}/{config.GRAPH_VERSION}/{media_id}", headers=headers)
        meta.raise_for_status()
        media = await client.get(meta.json()["url"], headers=headers)
        media.raise_for_status()
        return media.content

async def send_text(to: str, text: str):
    if not config.ACCESS_TOKEN or not config.PHONE_NUMBER_ID:
        raise RuntimeError("WhatsApp sending is not configured")
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    headers = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{GRAPH}/{config.GRAPH_VERSION}/{config.PHONE_NUMBER_ID}/messages",
            json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
