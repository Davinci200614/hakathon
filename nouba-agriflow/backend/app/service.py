"""SQLite-backed conversational drafts; only explicit confirmation creates requests."""
import asyncio
import json
import re
from collections import defaultdict
from .db import connect
from .ai import AIError, AgriculturalRequest, extract, transcribe
from .whatsapp import download_media, send_text

REQUIRED = ('machine_type', 'operation', 'area_hectares', 'location', 'preferred_date')
QUESTIONS = {
    'machine_type': 'شنو نوع الماكينة اللي محتاج؟',
    'operation': 'شنو الخدمة اللي باغي تدير؟',
    'area_hectares': 'شحال من هكتار عندك؟',
    'location': 'فين كاينة الأرض ديالك؟',
    'preferred_date': 'شنو التاريخ بالضبط؟ عافاك كتب النهار والشهر والسنة.',
}
# Serialize a farmer's overlapping webhooks in this single-worker MVP.
_locks = defaultdict(asyncio.Lock)


def summary(data):
    return ('واش هاد المعلومات صحيحة؟ جاوب بـ نعم أو تعديل:\n'
            f"🚜 الماكينة: {data.get('machine_type') or '—'}\n"
            f"🌱 الخدمة: {data.get('operation') or '—'}\n"
            f"📐 المساحة: {data.get('area_hectares') or '—'} هكتار\n"
            f"📍 المكان: {data.get('location') or '—'}\n"
            f"📅 التاريخ: {data.get('preferred_date') or data.get('preferred_date_text') or '—'}")


def is_confirmation(text):
    text = re.sub(r'[.!،؟?\s]+$', '', text.strip().lower())
    return text in ('نعم', 'ايه', 'اه', 'oui', 'yes', 'confirm', 'موافق', 'صحيح')


def conversation(phone):
    with connect() as con:
        row = con.execute('SELECT * FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
        if row:
            return json.loads(row['payload']), json.loads(row['history']), bool(row['awaiting_confirmation'])
        # Resume old pre-migration drafts, but require a newly delivered summary.
        legacy = con.execute("SELECT payload FROM requests WHERE farmer_phone=? AND status='draft' ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
        return (json.loads(legacy['payload']) if legacy else None), [], False


def save_reply(con, phone, msg_id, reply):
    con.execute("INSERT OR REPLACE INTO conversation_turns(farmer_phone,message_id,role,text) VALUES (?,?, 'assistant',?)", (phone, msg_id, reply))


async def deliver(phone, msg_id, reply):
    await send_text(phone, reply)
    with connect() as con:
        row = con.execute('SELECT payload FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
        if row and reply == summary(json.loads(row['payload'])):
            con.execute('UPDATE conversations SET awaiting_confirmation=1 WHERE farmer_phone=?', (phone,))
        con.execute("UPDATE messages SET status='processed',error=NULL WHERE id=?", (msg_id,))


async def handle_message(msg):
    phone, msg_id = msg.get('from'), msg.get('id')
    if not phone or not msg_id:
        return
    async with _locks[phone]:
        await _handle_message(msg, phone, msg_id)


async def _handle_message(msg, phone, msg_id):
    kind = msg.get('type', 'unknown')
    media_id = msg.get('audio', {}).get('id') if kind == 'audio' else None
    text = msg.get('text', {}).get('body', '') if kind == 'text' else ''
    with connect() as con:
        con.execute('INSERT OR IGNORE INTO farmers(phone) VALUES (?)', (phone,))
        row = con.execute('SELECT status FROM messages WHERE id=?', (msg_id,)).fetchone()
        if row and row['status'] not in ('error', 'ai_error', 'needs_text'):
            return
        con.execute('INSERT OR IGNORE INTO messages(id,farmer_phone,kind,text,media_id) VALUES (?,?,?,?,?)', (msg_id, phone, kind, text, media_id))
        cached = con.execute("SELECT text FROM conversation_turns WHERE message_id=? AND role='assistant'", (msg_id,)).fetchone()
    try:
        # A retry after delivery failure only resends the persisted reply; it must
        # never repeat an extraction or create a second confirmed request.
        if cached:
            await deliver(phone, msg_id, cached['text'])
            return
        if kind == 'audio':
            if not media_id:
                raise AIError('audio_media_id_missing')
            audio = await download_media(media_id)
            text = await transcribe(audio, msg.get('audio', {}).get('mime_type', 'audio/ogg'))
        elif kind != 'text':
            reply = 'عافاك صيفط ليا مساج مكتوب أو صوتي.'
            with connect() as con:
                save_reply(con, phone, msg_id, reply)
            await deliver(phone, msg_id, reply)
            return
        with connect() as con:
            con.execute('UPDATE messages SET text=? WHERE id=?', (text, msg_id))
        old, history, awaiting = conversation(phone)
        if is_confirmation(text):
            if old and awaiting:
                data = AgriculturalRequest.model_validate(old).model_dump(mode='json')
                missing = [key for key in REQUIRED if data.get(key) is None]
                if data['intent'] != 'request_machine':
                    missing = ['machine_type']
                if missing:
                    reply = QUESTIONS[missing[0]]
                else:
                    reply = 'تسجل الطلب ديالك وغادي تراجعو التعاونية. هادي ماشي حجز مؤكد.'
                    with connect() as con:
                        # Reuse an existing legacy draft, if any, without deleting it.
                        legacy = con.execute("SELECT id FROM requests WHERE farmer_phone=? AND status='draft' ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
                        if legacy:
                            con.execute("UPDATE requests SET payload=?,status='pending_review',updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(data, ensure_ascii=False), legacy['id']))
                        else:
                            con.execute("INSERT INTO requests(farmer_phone,payload,status) VALUES (?,?,'pending_review')", (phone, json.dumps(data, ensure_ascii=False)))
                        con.execute('DELETE FROM conversations WHERE farmer_phone=?', (phone,))
                        save_reply(con, phone, msg_id, reply)
                    await deliver(phone, msg_id, reply)
                    return
            elif old:
                old = AgriculturalRequest.model_validate(old).model_dump(mode='json')
                missing = [key for key in REQUIRED if not old.get(key)]
                reply = QUESTIONS[missing[0]] if missing else summary(old)
                with connect() as con:
                    con.execute('''INSERT INTO conversations(farmer_phone,payload,history)
                        VALUES (?,?,?) ON CONFLICT(farmer_phone) DO NOTHING''',
                        (phone, json.dumps(old, ensure_ascii=False), json.dumps(history, ensure_ascii=False)))
            else:
                reply = 'مرحبا! شنو نوع الماكينة اللي محتاج؟'
        else:
            data = await extract(text, old, history[-20:])
            # Validate again at the persistence boundary (including mocked adapters).
            data = AgriculturalRequest.model_validate(data).model_dump(mode='json')
            if old and old.get('intent') == 'request_machine':
                data['intent'] = 'request_machine'
            if data['intent'] != 'request_machine' and not old:
                reply = 'مرحبا بك فـ نوبة! شنو نوع الماكينة اللي محتاج؟'
            else:
                missing = [key for key in REQUIRED if data.get(key) is None]
                reply = QUESTIONS[missing[0]] if missing else summary(data)
                history = (history + [{'role': 'user', 'text': text}, {'role': 'assistant', 'text': reply}])[-20:]
                with connect() as con:
                    con.execute('''INSERT INTO conversations(farmer_phone,payload,history,awaiting_confirmation)
                        VALUES (?,?,?,0) ON CONFLICT(farmer_phone) DO UPDATE SET
                        payload=excluded.payload,history=excluded.history,awaiting_confirmation=0,
                        updated_at=CURRENT_TIMESTAMP''', (phone, json.dumps(data, ensure_ascii=False), json.dumps(history, ensure_ascii=False)))
        with connect() as con:
            save_reply(con, phone, msg_id, reply)
        await deliver(phone, msg_id, reply)
    except AIError as exc:
        code = exc.code
        status = 'needs_text' if code.startswith(('audio_', 'transcription_')) else 'ai_error'
        with connect() as con:
            con.execute('UPDATE messages SET status=?,error=? WHERE id=?', (status, code, msg_id))
        if code.startswith(('audio_', 'transcription_')):
            reply = 'ما قدرتش نفرغ الصوت بوضوح. عافاك عاود صيفط صوت واضح أو كتب الطلب ديالك.'
        elif code == 'gemini_rate_limited':
            reply = 'الخدمة عليها الضغط دابا. عافاك عاود صيفط الرسالة من بعد شوية.'
        else:
            reply = 'معالجة الرسالة ما خداماش دابا. الطلب ما تأكدش. عافاك عاود صيفط الرسالة من بعد.'
        try:
            await send_text(phone, reply)
        except Exception:
            # Do not leak provider/Meta URLs, tokens, or raw payloads to logs.
            pass
    except Exception:
        with connect() as con:
            con.execute("UPDATE messages SET status='error',error='message_processing_failed' WHERE id=?", (msg_id,))
        raise RuntimeError('message_processing_failed') from None
