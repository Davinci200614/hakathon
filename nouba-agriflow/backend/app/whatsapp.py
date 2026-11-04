import hashlib
import hmac
import httpx
import time
from . import config
from .diagnostics import event

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

async def _send_text(to: str, text: str):
    return await _send_payload(to, {"type":"text","text":{"body":text}})


async def send_interactive(to: str, interactive: dict):
    return await _send_payload(to, {'type': 'interactive', 'interactive': interactive})


async def _send_payload(to, content):
    if not config.ACCESS_TOKEN or not config.PHONE_NUMBER_ID:
        raise RuntimeError("WhatsApp sending is not configured")
    payload = {"messaging_product":"whatsapp","to":to,**content}
    headers = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        started = time.monotonic()
        response = await client.post(
            f"{GRAPH}/{config.GRAPH_VERSION}/{config.PHONE_NUMBER_ID}/messages",
            json=payload, headers=headers)
        response.raise_for_status()
        event('meta.send_http', started, outcome='accepted', meta_http_status=response.status_code)
        return response.json()


async def send_text(to: str, text: str):
    started = time.monotonic()
    try:
        result = await _send_text(to, text)
    except Exception as exc:
        fields = {'outcome':'error', 'exception_type':type(exc).__name__}
        if isinstance(exc, httpx.HTTPStatusError):
            fields['meta_http_status'] = exc.response.status_code
            try:
                code = exc.response.json().get('error', {}).get('code')
                if isinstance(code, int):
                    fields['meta_error_code'] = code
            except (ValueError, AttributeError):
                pass
        event('meta.send', started, **fields)
        raise
    event('meta.send', started, outcome='accepted')
    return result
