"""SQLite-backed conversational drafts; only explicit confirmation creates requests."""
import asyncio
import json
import re
import sqlite3
from collections import defaultdict
from . import config
from .extraction import clean_field, deterministic_patch
from .diagnostics import event, span, trace
from .db import connect, log_database_error
from .ai import AIError, AgriculturalRequest, extract, transcribe
from .whatsapp import download_media, send_text, send_interactive
from . import guided
from . import employment
from . import conversation as controller

REQUIRED = ('machine_type', 'operation', 'area_hectares', 'location', 'preferred_date')
QUESTIONS = {
    'machine_type': 'شنو نوع الماكينة اللي محتاج؟',
    'operation': 'شنو الخدمة اللي باغي تدير؟',
    'area_hectares': 'شحال من هكتار عندك؟',
    'location': 'فين كاينة الأرض ديالك؟',
    'preferred_date': 'فاش بغيتي التراكتور يخدم ليك؟',
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


def missing_fields(data):
    missing = [key for key in REQUIRED if clean_field(key, data.get(key)) is None]
    if data.get('preferred_date_text') and 'preferred_date' not in missing:
        missing.append('preferred_date')
    return missing


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


def merge_fields(old, patch):
    """Non-null new facts update a draft; missing fields never erase memory."""
    merged = {k: clean_field(k, v) for k, v in (old or {'intent':'other'}).items()}
    for key, value in patch.items():
        value = clean_field(key, value)
        if value is not None:
            merged[key] = value
    # Preserve saved facts while requiring clarification of an ambiguous date.
    if patch.get('preferred_date_text') and patch.get('preferred_date') is None:
        if not merged.get('preferred_date'):
            merged['preferred_date'] = None
    elif patch.get('preferred_date'):
        merged['preferred_date_text'] = None
    if merged.get('intent') == 'machinery_request':
        merged['intent'] = 'request_machine'
    if (old and old.get('intent') == 'request_machine') or merged.get('machine_type') or merged.get('operation'):
        merged['intent'] = 'request_machine'
    return AgriculturalRequest.model_validate(merged).model_dump(mode='json')


def is_cancellation(text):
    value = re.sub(r'[.!،؟?\s]+$', '', text.strip().lower())
    return value in ('إلغاء','الغاء','لغي','لغي الطلب','بغيت نلغي الطلب','annuler','annule','cancel')


def save_reply(con, phone, msg_id, reply):
    con.execute("INSERT OR REPLACE INTO conversation_turns(farmer_phone,message_id,role,text) VALUES (?,?, 'assistant',?)", (phone, msg_id, reply))


async def deliver(phone, msg_id, reply):
    with span('reply.delivery'):
        with connect() as con:
            interactive = guided.outgoing(con, msg_id) if config.CONVERSATION_CORE == 'v2' else None
        if interactive:
            await send_interactive(phone, interactive)
        else:
            await send_text(phone, reply)
    try:
        with span('conversation.delivery_commit'), connect() as con:
            if config.CONVERSATION_CORE == 'v2':
                controller.arm_confirmation(con, phone, reply)
            employment.arm_confirmation(con, phone, reply)
            row = con.execute('SELECT payload FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
            if row and reply == summary(json.loads(row['payload'])):
                con.execute('UPDATE conversations SET awaiting_confirmation=1 WHERE farmer_phone=?', (phone,))
                event('conversation.transition', previous_state='summary_pending_delivery', state='awaiting_confirmation')
            con.execute("UPDATE messages SET status='processed',error=NULL WHERE id=?", (msg_id,))
    except (sqlite3.Error, OSError) as exc:
        # Meta already accepted this reply. Do not send a contradictory second
        # message saying that a confirmed request was not saved. A new 'yes'
        # safely re-presents an unarmed summary if the delivery commit failed.
        log_database_error(exc, 'database.after_reply')


async def handle_message(msg):
    phone, msg_id = msg.get('from'), msg.get('id')
    if not phone or not msg_id:
        return
    with trace(), span('message.process', kind=msg.get('type', 'unknown')):
        async with _locks[phone]:
            try:
                await _handle_message(msg, phone, msg_id)
            except (sqlite3.Error, OSError) as exc:
                log_database_error(exc, 'conversation.database_failed')
                # Recovery itself must not depend on the failing database.
                try:
                    with connect() as con:
                        con.execute("UPDATE messages SET status='error',error='database_persistence_failed' WHERE id=?", (msg_id,))
                except (sqlite3.Error, OSError) as recovery_exc:
                    log_database_error(recovery_exc, 'database.recovery_failed')
                try:
                    await send_text(phone, 'سمح لينا، وقع مشكل تقني وماقدرناش نسجلو الطلب ديالك دابا. عاود حاول من بعد.')
                except Exception as send_exc:
                    event('reply.database_fallback_failed', outcome='error', exception_type=type(send_exc).__name__)


async def _handle_message(msg, phone, msg_id):
    kind = msg.get('type', 'unknown')
    media_id = msg.get('audio', {}).get('id') if kind == 'audio' else None
    text = msg.get('text', {}).get('body', '') if kind == 'text' else ''
    selection_id = None
    if kind == 'interactive' and config.CONVERSATION_CORE == 'v2':
        interactive = msg.get('interactive', {})
        selected = interactive.get('list_reply') or interactive.get('button_reply') or {}
        selection_id = selected.get('id')
        text = selected.get('title', '')
    with connect() as con:
        con.execute('INSERT OR IGNORE INTO farmers(phone) VALUES (?)', (phone,))
        row = con.execute('SELECT status FROM messages WHERE id=?', (msg_id,)).fetchone()
        if row and row['status'] not in ('error', 'ai_error', 'needs_text'):
            event('message.deduplication', outcome='skipped')
            return
        con.execute('INSERT OR IGNORE INTO messages(id,farmer_phone,kind,text,media_id) VALUES (?,?,?,?,?)', (msg_id, phone, kind, text, media_id))
        cached = con.execute("SELECT text FROM conversation_turns WHERE message_id=? AND role='assistant'", (msg_id,)).fetchone()
    event('message.deduplication', outcome='accepted', kind=kind)
    try:
        # A retry after delivery failure only resends the persisted reply; it must
        # never repeat an extraction or create a second confirmed request.
        if cached:
            if config.CONVERSATION_CORE == 'v2' and not controller.reply_is_current(phone, msg_id):
                with connect() as con:
                    con.execute("UPDATE messages SET status='processed',error=NULL WHERE id=?", (msg_id,))
                event('dialogue.obsolete_reply', outcome='skipped')
                return
            await deliver(phone, msg_id, cached['text'])
            return
        if kind == 'audio':
            if not media_id:
                raise AIError('audio_media_id_missing')
            with span('meta.media_download'):
                audio = await download_media(media_id)
            with span('conversation.transcription'):
                try:
                    text = await transcribe(audio, msg.get('audio', {}).get('mime_type', 'audio/ogg'))
                except AIError as exc:
                    event('stt.failed', provider=config.STT_PROVIDER, error_code=exc.code, outcome='error')
                    raise AIError('transcription_failed') from None
        elif kind != 'text' and not (kind == 'interactive' and config.CONVERSATION_CORE == 'v2'):
            reply = 'عافاك صيفط ليا مساج مكتوب أو صوتي.'
            with connect() as con:
                save_reply(con, phone, msg_id, reply)
            await deliver(phone, msg_id, reply)
            return
        with connect() as con:
            con.execute('UPDATE messages SET text=? WHERE id=?', (text, msg_id))
        if config.CONVERSATION_CORE == 'v2':
            reply = await controller.respond(phone, msg_id, text, selection_id=selection_id)
            await deliver(phone, msg_id, reply)
            return
        employment_reply = employment.handle(phone, text)
        if employment_reply is not None:
            with connect() as con:
                save_reply(con, phone, msg_id, employment_reply)
            await deliver(phone, msg_id, employment_reply)
            return
        with span('conversation.load'):
            old, history, awaiting = conversation(phone)
            if old:
                old = AgriculturalRequest.model_validate({k:clean_field(k,v) for k,v in old.items()}).model_dump(mode='json')
            with connect() as con:
                state = con.execute('SELECT last_question_field FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
            pending = state['last_question_field'] if state else None
        event('conversation.state', state='awaiting_confirmation' if awaiting else ('collecting' if old else 'new'))
        if is_cancellation(text):
            reply = 'تلغى مسودة الطلب. إلا بغيتي طلب جديد صيفط ليا المعلومات.'
            with connect() as con:
                con.execute('DELETE FROM conversations WHERE farmer_phone=?', (phone,))
                # Keep legacy draft records but prevent them from being resumed.
                con.execute("UPDATE requests SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE farmer_phone=? AND status='draft'", (phone,))
                save_reply(con, phone, msg_id, reply)
            await deliver(phone, msg_id, reply)
            return
        if text.strip().lower() in ('لا', 'no', 'non', 'تعديل', 'modifier', 'correction'):
            reply = 'شنو المعلومة اللي بغيتي تصحح؟'
            with connect() as con:
                con.execute("UPDATE conversations SET awaiting_confirmation=0,last_question_field='correction',last_question=? WHERE farmer_phone=?", (reply, phone))
                save_reply(con, phone, msg_id, reply)
            await deliver(phone, msg_id, reply)
            return
        if is_confirmation(text):
            if old and awaiting:
                data = AgriculturalRequest.model_validate(old).model_dump(mode='json')
                missing = missing_fields(data)
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
                    event('conversation.saved', state='pending_review', outcome='ok')
                    await deliver(phone, msg_id, reply)
                    return
            elif old:
                old = AgriculturalRequest.model_validate(old).model_dump(mode='json')
                missing = missing_fields(old)
                reply = QUESTIONS[missing[0]] if missing else summary(old)
                with connect() as con:
                    con.execute('''INSERT INTO conversations(farmer_phone,payload,history)
                        VALUES (?,?,?) ON CONFLICT(farmer_phone) DO NOTHING''',
                        (phone, json.dumps(old, ensure_ascii=False), json.dumps(history, ensure_ascii=False)))
            else:
                reply = 'مرحبا! شنو نوع الماكينة اللي محتاج؟'
        else:
            with span('conversation.extraction'):
                local = deterministic_patch(text, pending)
                if re.fullmatch(r'\s*20\d{2}[/.-]\d{1,2}[/.-]\d{1,2}\s*', text):
                    data = local
                else:
                    context = [{'role':'context', 'pending_field':pending, 'current_state':old}] + history[-19:]
                    data = await extract(text, old, context)
                    data.update({k:v for k,v in local.items() if v is not None})
            # Validate again at the persistence boundary (including mocked adapters).
            with span('conversation.validation'):
                data = merge_fields(old, data)
            if data['intent'] != 'request_machine' and not old:
                reply = 'مرحبا بك فـ نوبة! شنو نوع الماكينة اللي محتاج؟'
            else:
                missing = missing_fields(data)
                requested = missing[0] if missing else None
                with connect() as con:
                    previous_question = con.execute('SELECT last_question_field,failed_attempts FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
                failures = 0
                if requested and previous_question and previous_question['last_question_field'] == requested:
                    failures = previous_question['failed_attempts'] + 1
                reply = QUESTIONS[requested] if requested else summary(data)
                if failures:
                    if requested == 'preferred_date':
                        reply = ('عطيني النهار اللي بغيتي، مثلاً 5 نونبر، ولا كتب التاريخ بحال 2026-11-05.' if failures == 2
                                 else 'ما واضحش ليا نهار الخدمة. شنو النهار والشهر اللي بغيتي؟')
                    else:
                        reply = f'عافاك وضح ليا: {QUESTIONS[requested]}' if failures == 1 else f'محاولة {failures + 1}: مازال خاصني هاد المعلومة: {QUESTIONS[requested]}'
                if failures > 2:
                    reply = f'محاولة {failures + 1}: {reply}'
                event('conversation.transition', previous_state='awaiting_confirmation' if awaiting else ('collecting' if old else 'new'),
                      state='summary_pending_delivery' if not missing else 'collecting', missing_fields=missing,
                      field_presence=[k for k in REQUIRED if data.get(k) is not None], normalized_date=data.get('preferred_date'),
                      requested_field=requested, failed_attempts=failures)
                history = (history + [{'role': 'user', 'text': text}, {'role': 'assistant', 'text': reply}])[-20:]
                with span('conversation.persist'), connect() as con:
                    con.execute('''INSERT INTO conversations(farmer_phone,payload,history,awaiting_confirmation)
                        VALUES (?,?,?,0) ON CONFLICT(farmer_phone) DO UPDATE SET
                        payload=excluded.payload,history=excluded.history,awaiting_confirmation=0,
                        updated_at=CURRENT_TIMESTAMP''', (phone, json.dumps(data, ensure_ascii=False), json.dumps(history, ensure_ascii=False)))
                    con.execute('UPDATE conversations SET last_question_field=?,last_question=?,failed_attempts=? WHERE farmer_phone=?', (requested, reply, failures, phone))
        with span('reply.persist'), connect() as con:
            save_reply(con, phone, msg_id, reply)
        event('reply.generated', outcome='ok')
        await deliver(phone, msg_id, reply)
    except AIError as exc:
        code = exc.code
        event('conversation.fallback', outcome='ai_error', exception_type=type(exc).__name__, error_code=code, provider=config.LLM_PROVIDER, gemini_error_code=code if code.startswith('gemini_') else None)
        status = 'needs_text' if code.startswith(('audio_', 'transcription_')) else 'ai_error'
        with connect() as con:
            con.execute('UPDATE messages SET status=?,error=? WHERE id=?', (status, code, msg_id))
        if code.startswith(('audio_', 'transcription_')):
            reply = 'ما قدرتش نفرغ الصوت بوضوح. عافاك عاود صيفط صوت واضح أو كتب الطلب ديالك.'
        elif code == 'llm_message_too_long_or_empty':
            reply = 'عافاك صيفط رسالة قصيرة بالمعلومات ديال الطلب.'
        elif code == 'gemini_rate_limited':
            reply = 'الخدمة عليها الضغط دابا. عافاك عاود صيفط الرسالة من بعد شوية.'
        else:
            reply = 'المعالجة ما خداماش دابا. عافاك عاود صيفط الرسالة من بعد شوية.'
        try:
            with connect() as con:
                save_reply(con, phone, msg_id, reply)
            with span('reply.fallback_delivery'):
                await send_text(phone, reply)
            with connect() as con:
                con.execute("UPDATE messages SET status='ai_error_replied' WHERE id=?", (msg_id,))
        except Exception as send_exc:
            event('reply.fallback_failed', outcome='error', exception_type=type(send_exc).__name__, gemini_error_code=code)
    except (sqlite3.Error, OSError):
        # Preserve the actual SQLite cause for the outer safe recovery handler.
        raise
    except Exception as exc:
        event('conversation.failed', outcome='error', exception_type=type(exc).__name__)
        with connect() as con:
            con.execute("UPDATE messages SET status='error',error='message_processing_failed' WHERE id=?", (msg_id,))
        raise RuntimeError('message_processing_failed') from None
