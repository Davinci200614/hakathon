"""Intent-first controller. Models interpret; Python validates and commits actions."""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from . import employment, extraction
from .ai import AIError
from .db import connect
from .diagnostics import event
from .understanding import Facts, Understanding, understand
from .locations import clean_location
from . import guided
from .contacts import valid_contact, extract_contacts, phone_number, CONTACT_QUESTION
from . import catalog

MACHINE_FIELDS = ('machine_type', 'operation', 'area_hectares', 'location', 'preferred_date')
HELP = ('مرحبا! قول ليا شنو محتاج بالدارجة، مثلا:\n'
        'بغيت تراكتور للحرث — كنقلب على شي حد يخدم عندي — كنقلب على خدمة\n'
        'وتقدر تنشر عرض عمل أو تسجل راسك كعامل.\n'
        'كتب بدا من جديد باش نبداو محادثة جديدة، أو إلغاء باش تلغي المسودة الحالية.')
QUESTIONS = {
    'machine_type': 'شنو نوع الماكينة اللي محتاج؟',
    'operation': 'شنو الخدمة اللي باغي تدير؟',
    'area_hectares': 'شحال من هكتار عندك؟',
    'location': 'فين كاينة الأرض ديالك؟',
    'preferred_date': 'فاش بغيتي الخدمة؟ عطيني نهار من اليوم لقدام، مثلا غدا أو التاريخ كامل.',
}
YES = {'نعم', 'اه', 'ايه', 'أه', 'موافق', 'صحيح', 'yes', 'oui', 'confirm', 'ah', 'iyeh', 'iyah', 'wakha', 'واخا'}
CANCEL = {'إلغاء', 'الغاء', 'لغي', 'لغي الطلب', 'بغيت نلغي الطلب', 'cancel', 'annuler', 'annule'}
EDIT = {'لا', 'no', 'non', 'تعديل', 'modifier', 'correction'}
COMMANDS = {
    'نقلب على عمال': 'search_workers', 'نقلب على خدمة': 'search_jobs',
    'ننشر بروفايل ديالي': 'post_worker', 'ننشر عرض عمل': 'post_job', 'نطلب ماكينة': 'machine',
    'post worker': 'post_worker', 'نشر عامل': 'post_worker',
    'post job': 'post_job', 'نشر خدمة': 'post_job', 'نشر عرض عمل': 'post_job',
    'find workers': 'search_workers', 'بحث عمال': 'search_workers',
    'find jobs': 'search_jobs', 'بحث خدمة': 'search_jobs', 'بحث عن عمل': 'search_jobs',
}
TASK_NAMES = {'machine': 'طلب الماكينة', 'search_workers': 'البحث على عمال',
              'search_jobs': 'البحث على خدمة', 'search_machines': 'البحث على المعدات', 'post_worker': 'بروفايل العامل', 'post_job': 'عرض العمل'}


def catalog_decision(text, active, pending, context, entries):
    value = employment.normalize(text)
    if active.startswith('post_'):
        return None
    skills = catalog.matches(text, 'skill', entries)
    machines = catalog.matches(text, 'machine', entries)
    places = catalog.matches(text, 'location', entries)
    location = places[0]['label'] if places else None
    last = context.get('last_search', {})
    if re.search(r'سجل|لائحة|لايحة|ناس.*(?:طلبو|كيقلبو|باغين).*(?:عمل|خدمة)', value) and re.search(r'ناس|عمال|خدام|عمل', value):
        return Understanding(task='search_workers', facts=Facts(location=location, work=skills[0]['code'] if skills else None, browse_all=not location))
    if machines:
        kind = machines[0]['code']
        operation = {'harvester': 'harvesting', 'sprayer': 'spraying', 'baler': 'baling', 'irrigation_pump': 'irrigation'}.get(kind)
        task = 'search_machines' if re.search(r'(?<!\w)(?:عندكم|كاين|كاينة|كين|نشوف|شوف|متوفر|available)(?!\w)', value) else 'machine'
        return Understanding(task=task, facts=Facts(machine_type=kind, operation=operation, location=location),
            clarification=f"فهمتك، كتقصد {machines[0]['label']}." if employment.normalize(machines[0]['label']) not in value else None)
    if skills and re.search(r'نلقى|نلقا|بغيت|قلب|كاين|كين|شي|find|looking', value):
        task = 'search_jobs' if re.search(r'نخدم|خدمة ليا|نلقى خدمة|نلقا خدمة|على خدمة', value) else 'search_workers'
        return Understanding(task=task, facts=Facts(work=skills[0]['code'], location=location or last.get('location')))
    if active.startswith('search_') and places:
        return Understanding(task=active, facts=Facts(location=location))
    return None


def is_approval(text):
    value = employment.normalize(text)
    if value in {employment.normalize(v) for v in YES}:
        return True
    value = re.sub(r'(?<!\w)(?:عافاك|عفاك|افاك|3afak|afak|please|sf|safi|صافي|mzyan|مزيان)(?!\w)', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' ،,.!')
    return value in ('اه', 'نعم', 'موافق', 'نشرها', 'انشرها', 'نشر هاد لميساج', 'نشر هاد الميساج', 'نشر هاد المساج', 'نشر الاعلان', 'nshrha', 'ncherha', 'yes', 'oui')


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def machine_summary(data):
    names = {'tractor': 'تراكتور', 'harvester': 'حصادة', 'plowing': 'الحرث', 'harvesting': 'الجني'}
    return ('واش هاد المعلومات صحيحة؟ جاوب بنعم، تعديل أو إلغاء:\n'
            f"🚜 الماكينة: {names.get(data.get('machine_type'), data.get('machine_type'))}\n"
            f"🌱 الخدمة: {names.get(data.get('operation'), data.get('operation'))}\n"
            f"📐 المساحة: {data.get('area_hectares')} هكتار\n"
            f"📍 المكان: {data.get('location')}\n📅 التاريخ: {data.get('preferred_date')}")


def missing_machine(data):
    missing = [f for f in MACHINE_FIELDS if extraction.clean_field(f, data.get(f)) is None]
    value = data.get('preferred_date')
    if (data.get('preferred_date_text') or (value and value < extraction.casablanca_today().isoformat())) and 'preferred_date' not in missing:
        missing.append('preferred_date')
    return missing


def load(phone):
    with connect() as con:
        context = context_data(con, phone)
        session = con.execute('SELECT * FROM dialogue_sessions WHERE phone=?', (phone,)).fetchone()
        machine = con.execute('SELECT * FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
        task = session['task'] if session else 'none'
        job = con.execute('SELECT * FROM dialogue_drafts WHERE phone=? AND task=?', (phone, task)).fetchone()
        if not job and not session:
            job = con.execute('SELECT * FROM employment_drafts WHERE phone=?', (phone,)).fetchone()
        history = con.execute('SELECT role,text FROM conversation_turns WHERE farmer_phone=? AND id>? ORDER BY id DESC LIMIT 6', (phone, context.get('history_after_id', 0))).fetchall()
        if not machine:
            legacy = con.execute("SELECT payload FROM requests WHERE farmer_phone=? AND status='draft' ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
            if legacy:
                machine = {'payload': legacy['payload'], 'last_question_field': None}
    if session:
        active = session['task']
    elif job:
        kind = json.loads(job['payload'])['kind']
        active = ('search_workers' if kind == 'worker' else 'search_jobs') if job['pending'] == 'search_location' else 'post_' + kind
    else:
        active = 'machine' if machine else 'none'
    # Retain drafts, but stop applying old context automatically after six hours.
    timestamp = session['updated_at'] if session else (machine['updated_at'] if machine and 'updated_at' in machine.keys() else None)
    if timestamp:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        if age.total_seconds() > 6 * 60 * 60:
            active = 'none'
            history = []
    data = json.loads(machine['payload']) if active == 'machine' and machine else (json.loads(job['payload']) if job and active.startswith(('post_', 'search_')) else {})
    pending = machine['last_question_field'] if active == 'machine' and machine else (job['pending'] if job and active != 'none' else None)
    return active, data, pending, [dict(r) for r in reversed(history)], session


def context_data(con, phone):
    row = con.execute('SELECT payload FROM dialogue_context WHERE phone=?', (phone,)).fetchone()
    return json.loads(row['payload']) if row else {}


def put_context(con, phone, context):
    con.execute('INSERT INTO dialogue_context(phone,payload) VALUES (?,?) ON CONFLICT(phone) DO UPDATE SET payload=excluded.payload',
                (phone, json.dumps(context, ensure_ascii=False)))


def reply_is_current(phone, msg_id):
    with connect() as con:
        boundary = context_data(con, phone).get('history_after_id', 0)
        row = con.execute("SELECT id FROM conversation_turns WHERE message_id=? AND role='assistant'", (msg_id,)).fetchone()
        return not row or row['id'] > boundary


def publication_goal(value, active, context):
    if re.search(r'طلب\s+تشغيل.*(?:نلقا|نلقى|خدمة)', value):
        return 'post_worker'
    if not re.search(r'نشر|نبوست|بوستي|انونس|اعلان|نسجل\s+راسي', value):
        return None
    if re.search(r'راسي|المعلومات ديالي|بروفايل|نلقا\s+خدمة|نلقى\s+خدمة|نخدم', value):
        return 'post_worker'
    if re.search(r'عرض\s+عمل|محتاج\s+عمال|عندي\s+خدمة', value):
        return 'post_job'
    if active.startswith('post_'):
        return active
    previous = context.get('last_search', {}).get('kind')
    return {'job': 'post_worker', 'worker': 'post_job'}.get(previous, 'ambiguous')


def explicit_profile_patch(text):
    fields = {}
    name = re.search(r'(?:سميتي|اسمي)\s+(.+?)(?=\s+(?:ولد|بنت|من|باغي|بغيت|كنقلب|و)\b|$)', text)
    place = re.search(r'(?:ولد|بنت|ساكن في|ساكن ف)\s+(.+?)(?=\s+(?:باغي|بغيت|كنقلب|خدام|و)\b|$)', text)
    if not place:
        place = re.search(r'(?:خدمة|خدما)\s+ف\s*(?:نواحي\s+)?(.+?)(?=\s+و\s|[،,]|$)', text)
    if place and place[1].startswith('شي '):
        fields['work'] = place[1][3:].strip()
        place = None
    work = re.search(r'(?:باغي|بغيت)\s+نخدم\s+(ك[^\s،,]+)', text)
    details = re.search(r'(?:الأجرة|الاجرة)\s+(.+)', text)
    for key, match in [('name', name), ('location', place), ('work', work), ('details', details)]:
        if match and len(match[1]) <= (400 if key == 'details' else 100):
            fields[key] = match[1].strip(' ،,')
    return fields


def local_decision(text, active, data, pending, context=None, phone=None):
    context = context or {}
    value = employment.normalize(text)
    if value in ('menu', 'options', 'help', 'مساعدة', 'اختيارات', 'الاختيارات', 'القائمة', 'لائحة', '0', 'سلام', 'salam'):
        return Understanding(action='menu')
    if (value in ('reset', 'restart', '/start', 'بدا من جديد', 'نبداو من جديد', 'بدا من الاول', 'نسا كولشي')
            or re.search(r'نب[دظض]او?\s+من\s+جديد|مهدرتش معاك من (?:لول|الاول)', value)
            or re.search(r'(?:نبدا|نبداو|بدا|نبد[اأ])\s+(?:من جديد|من الاول|مسودة جديدة|محادثة جديدة)', value)):
        return Understanding(action='reset')
    if context.get('pending_switch') and is_approval(text):
        return Understanding.model_validate(context['pending_switch'])
    if context.get('pending_switch') and value in EDIT:
        return Understanding(task=active)
    if active == 'none' and context.get('offer') and is_approval(text):
        search = context.get('last_search', {})
        return Understanding(task=context['offer'], action='new', facts=Facts(location=search.get('location')))
    # Only standalone acknowledgements can confirm a draft.
    if is_approval(text):
        return Understanding(task=active, action='confirm')
    if value in {employment.normalize(v) for v in CANCEL}:
        return Understanding(task=active, action='cancel')
    if value in EDIT:
        return Understanding(task=active, action='correct')
    if value in ('help', 'menu', 'مساعدة', 'سلام', 'salam', 'مرحبا', 'emploi', 'عمال'):
        return Understanding(action='help')
    if value in ('رجع للتراكتور', 'رجع للماكينة', 'كمل طلب التراكتور', 'resume machine'):
        return Understanding(task='machine', action='resume')
    resumes = {'رجع لبروفايل العامل': 'post_worker', 'رجع لعرض العمل': 'post_job',
               'resume worker': 'post_worker', 'resume job': 'post_job'}
    if value in resumes:
        return Understanding(task=resumes[value], action='resume')
    if value in ('طلب جديد', 'تراكتور جديد', 'new machine'):
        return Understanding(task='machine', action='new')
    task = COMMANDS.get(value)
    if not task:
        with connect() as con:
            entries = catalog.terms(con)
        matched = catalog_decision(text, active, pending, context, entries)
        if matched:
            return matched
    explicit_machine = re.search(r'(?:بغيت|خصني|خاصني|محتاج)\s+(?:شي\s+)?(?:تراكتور|جرار|حصادة)', value)
    if active.startswith('post_'):
        if re.search(r'معطيتكش|ما\s*عطيتكش|باقي.*(?:وسيل|نمرة|رقم|كونت)', value) and re.search(r'اتصال|وسيل|نمرة|رقم|كونت', value):
            return Understanding(task=active, action='correct', clear_fields=['contact'])
        if phone and re.search(r'(?:زيد|استعمل|دير|حط).*(?:نمر|رقم).*(?:ديالي|هاد)', value):
            number = phone_number(phone)
            return Understanding(task=active, action='correct', facts=Facts(contact=number), clear_fields=[] if number else ['contact'])
        contact_context = pending in ('contact', 'confirm', 'correction') or re.search(r'@|https?://|نمرة|نمرتي|رقم|هاتف|واتساب|whatsapp|contact|اتصال', value)
        contact, invalid = extract_contacts(text) if contact_context else (None, False)
        if contact and len(contact) > 100:
            return Understanding(task=active, action='correct', clear_fields=['contact'], clarification='وسيلة الاتصال طويلة بزاف. عطيني نمرة أو إيميل أو رابط مختصر، حتى لـ 100 حرف.')
        if contact or invalid:
            return Understanding(task=active, action='correct', facts=Facts(contact=contact if not invalid else None, **explicit_profile_patch(text)),
                                 clear_fields=['contact'] if invalid else [], clarification='راجع النمرة: الرقم المغربي خاصو 10 أرقام، أو كتب الرقم الدولي كامل.' if invalid else None)
        explicit = explicit_profile_patch(text)
        if explicit and (len(explicit) > 1 or pending not in explicit):
            return Understanding(task=active, action='correct', facts=Facts(**explicit))
        if pending == 'contact' and not task and not explicit_machine and not value.startswith(('واش ', 'كيفاش ')):
            return Understanding(task=active, action='correct', clear_fields=['contact'], clarification=CONTACT_QUESTION)
    if active == 'post_worker' and value in ('بغيت عمال', 'كنقلب على عمال', 'خصني عمال'):
        task = 'search_workers'
    if active.startswith('post_') and pending == 'contact' and re.search(r'ما\s*بغيتش|ما\s*عنديش|مانبغيش', value):
        return Understanding(task=active, action='help', clarification='ما غاديش ننشرو نمرة واتساب ديالك بوحدنا. تقدر تعطينا إيميل أو وسيلة اتصال أخرى موافق تبان للناس، أو تلغي المسودة.')
    if active.startswith('post_') and re.search(r'واش|ضروري', value) and re.search(r'اجرة|ثمن|تفاوض', value):
        return Understanding(task=active, action='help', clarification='ماشي ضروري تحدد الأجرة دابا. تقدر تكتب قابل للتفاوض، ونزيدوها فالتفاصيل ديالك.')
    publish = publication_goal(value, active, context)
    if active.startswith('post_') and pending in ('work', 'details') and not re.search(r'بغيت|بغت|واش|نبوست|ننشر', value):
        publish = None
    if not task and publish:
        if publish == 'ambiguous':
            if len(text.split()) > 8:
                return None
            return Understanding(action='clarify')
        search = context.get('last_search', {})
        place = search.get('location') if active == 'none' and search.get('kind') == ('job' if publish == 'post_worker' else 'worker') else None
        return Understanding(task=publish, action='new' if publish != active else 'continue', facts=Facts(location=place))
    if context.get('choose_publish') and not task:
        if re.search(r'نخدم|خدمة ليا|انا\s+عامل|نلقا\s+خدمة', value):
            task = 'post_worker'
        elif re.search(r'عندي\s+خدمة|محتاج\s+عمال|يخدم\s+عندي', value):
            task = 'post_job'
    # A profile answer belongs to that profile. An occupation such as حصاد
    # must never activate the machine draft. Broad "بغيت نخدم" also describes
    # the current profile; it is not evidence that the user changed purpose.
    if active.startswith('post_') and not task and not explicit_machine:
        if pending in employment.FIELDS and not value.startswith(('واش ', 'كيفاش ', 'علاش ')):
            if pending == 'name':
                name = re.match(r'^(?:سميتي|اسمي|انا اسمي)\s+(.+?)(?=\s+(?:بغيت|كنقلب|وبغيت|وكنقلب)|$)', text.strip())
                if name and len(name[1]) <= 100:
                    fields = {'name': name[1].strip(' ،,')}
                    work = re.search(r'فشي\s+([\w ]+?)(?:[،,.]|$)', text)
                    if work and len(work[1]) <= 100:
                        fields['work'] = work[1].strip()
                    return Understanding(task=active, facts=Facts(**fields))
            limit = 400 if pending == 'details' else 100
            if 0 < len(text.strip()) <= limit and (pending in ('work', 'details', 'contact') or len(text.split()) <= 4):
                answer = clean_location(text) if pending == 'location' else text.strip()
                return Understanding(task=active, facts=Facts(**{pending: answer}))
        return None
    natural = employment.natural_intent(value)
    if not task and natural:
        task = {'worker': 'post_worker', 'job': 'post_job', 'search_worker': 'search_workers', 'search_job': 'search_jobs'}[natural]
    # Common spelling variants from the actual transcript, beyond named workers.
    if not task and re.search(r'بغيت|خصني|خاصني|محتاج|كنقلب', value) and re.search(r'(?:شي\s*واحد|واحد|سدشيواحد).*?(?:ا?خدم|يخدم|إخدم).*?عندي', value):
        task = 'search_workers'
    if task:
        facts = {}
        place = re.search(r'(?:\s(?:f|fi)\s+|\sف(?:ي\s+)?)([\w -]{2,60})$', value)
        if place:
            facts['location'] = place[1]
        action = 'new' if task.startswith('post_') and task != active else ('switch' if task != active else 'continue')
        return Understanding(task=task, action=action, facts=Facts(**facts))
    patch = extraction.deterministic_patch(text, pending if active == 'machine' else None)
    if patch.get('machine_type') or patch.get('operation'):
        # Rich first messages and questions need semantic interpretation. A
        # partial regex patch must not discard a location already in the text.
        if (re.search(r'\sف\S|\sفي\s|\s(?:f|fi|a|à)\s', value)
                or (len(text.split()) > 12)
                or re.search(r'^(?:شنو|واش|كيفاش|علاش)\b', value)):
            return None
    if patch.get('machine_type') or patch.get('operation'):
        task = 'machine'
    elif patch and active == 'machine' and (
            (patch.get('location') and re.match(r'(?:المكان\s|(?:أنا|انا)\s+ف|في\s|ف\s)', text.strip()))
            or (len(text.split()) <= 5 and not re.search(r'بغيت|كنقلب|محتاج|عندي', value))):
        task = 'machine'
    if task == 'machine':
        facts = {k: v for k, v in patch.items() if k in Facts.model_fields and v is not None}
        # Ambiguous dates need controller clarification, never model guessing.
        if patch.get('preferred_date_text'):
            return Understanding(task=task, action='continue', facts=Facts(**facts))
        return Understanding(task=task, action='correct' if 'location' in facts else 'continue', facts=Facts(**facts))
    # Field replies use deterministic parsing only when they are short and literal.
    if active.startswith('search_') and pending == 'search_location' and len(text.split()) <= 4:
        return Understanding(task=active, facts=Facts(location=clean_location(text)))
    return None


def session_task(con, phone, task):
    con.execute('INSERT INTO dialogue_sessions(phone,task) VALUES (?,?) ON CONFLICT(phone) DO UPDATE SET task=excluded.task,updated_at=CURRENT_TIMESTAMP', (phone, task))


def grant(con, phone, task, data):
    con.execute('INSERT INTO dialogue_confirmations(phone,task,payload_hash) VALUES (?,?,?) ON CONFLICT(phone,task) DO UPDATE SET payload_hash=excluded.payload_hash,armed=0,created_at=CURRENT_TIMESTAMP', (phone, task, digest(data)))


def allowed(con, phone, task, data):
    row = con.execute("SELECT * FROM dialogue_confirmations WHERE phone=? AND task=? AND armed=1 AND created_at >= datetime('now','-30 minutes')", (phone, task)).fetchone()
    return bool(row and row['payload_hash'] == digest(data))


def arm_confirmation(con, phone, reply):
    session = con.execute('SELECT task FROM dialogue_sessions WHERE phone=?', (phone,)).fetchone()
    if not session:
        return
    task = session['task']
    if task == 'machine':
        row = con.execute('SELECT payload FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
        data = json.loads(row['payload']) if row else {}
        expected = machine_summary(data) if data and not missing_machine(data) else None
    elif task.startswith('post_'):
        row = con.execute("SELECT payload FROM dialogue_drafts WHERE phone=? AND task=? AND pending='confirm'", (phone, task)).fetchone()
        data = json.loads(row['payload']) if row else {}
        expected = employment.summary(data) if data else None
    else:
        return
    if reply == expected:
        con.execute('UPDATE dialogue_confirmations SET armed=1 WHERE phone=? AND task=? AND payload_hash=?', (phone, task, digest(data)))
        if task == 'machine':
            con.execute('UPDATE conversations SET awaiting_confirmation=1 WHERE farmer_phone=?', (phone,))


def record_reply(con, phone, msg_id, text, reply):
    con.execute("INSERT OR REPLACE INTO conversation_turns(farmer_phone,message_id,role,text) VALUES (?,?,'user',?)", (phone, msg_id, text))
    con.execute("INSERT OR REPLACE INTO conversation_turns(farmer_phone,message_id,role,text) VALUES (?,?,'assistant',?)", (phone, msg_id, reply))
    row = con.execute('SELECT last_reply,repeats FROM dialogue_sessions WHERE phone=?', (phone,)).fetchone()
    repeats = row['repeats'] + 1 if row and row['last_reply'] == reply else 0
    if repeats >= 2:
        reply = 'بان ليا ما تقدمناش فهاد النقطة. قول ليا شنو بغيتي تبدل، أو كتب بدا من جديد باش نحيدو المسودات ونبداو من الاول.'
        con.execute("UPDATE conversation_turns SET text=? WHERE message_id=? AND role='assistant'", (reply, msg_id))
        con.execute('UPDATE dialogue_confirmations SET armed=0 WHERE phone=?', (phone,))
    con.execute('UPDATE dialogue_sessions SET last_reply=?,repeats=? WHERE phone=?', (reply, repeats, phone))
    context = context_data(con, phone)
    con.execute('DELETE FROM dialogue_outgoing WHERE message_id=?', (msg_id,))
    guided.build(con, phone, msg_id, reply, context)
    put_context(con, phone, context)
    return reply


async def respond(phone, msg_id, text, selection_id=None):
    started = time.monotonic()
    reply = await _respond(phone, msg_id, text, selection_id)
    with connect() as con:
        session = con.execute('SELECT task FROM dialogue_sessions WHERE phone=?', (phone,)).fetchone()
        con.execute('''INSERT INTO dialogue_audit(message_id,task,route,duration_ms) VALUES (?,?,?,?)
            ON CONFLICT(message_id) DO UPDATE SET task=excluded.task,duration_ms=excluded.duration_ms''',
            (msg_id, session['task'] if session else 'none', 'fast', round((time.monotonic()-started)*1000,2)))
    return reply


async def _respond(phone, msg_id, text, selection_id=None):
    used_model = False
    choice = None
    active, data, pending, history, session = load(phone)
    with connect() as con:
        context = context_data(con, phone)
        if session and active == 'none':
            age = datetime.now(timezone.utc) - datetime.fromisoformat(session['updated_at']).replace(tzinfo=timezone.utc)
            if age.total_seconds() > 6 * 60 * 60:
                context = {'history_after_id': context.get('history_after_id', 0)}
                put_context(con, phone, context)
    if not text.strip() or len(text) > 1200:
        decision = Understanding(action='unknown')
    else:
        choice = guided.resolve(context, text, selection_id)
        decision = None
        if choice:
            op = choice['op']
            context['guided'] = True
            with connect() as con:
                put_context(con, phone, context)
            if op == 'stale':
                with connect() as con:
                    session_task(con, phone, active)
                    return record_reply(con, phone, msg_id, text, 'هاد الاختيارات قديمة. كتب menu باش نعطيك اللائحة الحالية، أو كمل بالمعلومات ديالك.')
            if op in ('new', 'resume'):
                decision = Understanding(task=choice['task'], action=op)
            elif op in ('reset', 'menu'):
                decision = Understanding(action=op)
            elif op == 'listings':
                text = 'إعلاناتي'
            elif op == 'offer':
                search = context.get('last_search', {})
                decision = Understanding(task=context.get('offer', 'none'), action='new', facts=Facts(location=search.get('location')))
            elif op in ('confirm', 'cancel', 'edit'):
                decision = Understanding(task=active, action={'confirm': 'confirm', 'cancel': 'cancel', 'edit': 'correct'}[op])
                if op == 'edit':
                    with connect() as con:
                        context['show_fields'] = True
                        put_context(con, phone, context)
            elif op == 'reply':
                text = choice['text']
            elif op == 'field':
                field = choice['field']
                with connect() as con:
                    if active == 'machine':
                        con.execute('UPDATE conversations SET last_question_field=?,awaiting_confirmation=0 WHERE farmer_phone=?', (field, phone))
                        reply = QUESTIONS[field]
                    else:
                        con.execute('UPDATE dialogue_drafts SET pending=? WHERE phone=? AND task=?', (field, phone, active))
                        reply = profile_question(active, field)
                    con.execute('DELETE FROM dialogue_confirmations WHERE phone=? AND task=?', (phone, active))
                    return record_reply(con, phone, msg_id, text, reply)
            elif op in ('contact_self', 'contact_other'):
                if op == 'contact_other':
                    with connect() as con:
                        context['contact_mode'] = choice.get('kind', 'email')
                        put_context(con, phone, context)
                number = phone_number(phone) if op == 'contact_self' else None
                decision = Understanding(task=active, action='correct', facts=Facts(contact=number), clear_fields=[] if number else ['contact'])
        if active.startswith(('post_', 'search_')) and not session and data:
            with connect() as con:
                store_draft(con, phone, active, data, pending)
        # Simple owner-only listing commands remain deterministic.
        value = employment.normalize(text)
        if re.search(r'كنت.*(?:تسجلت|مسجل)|اعلاني فين|فين.*اعلاني', value):
            text = value = 'my listings'
        if value in ('my listings', 'اعلاناتي', 'إعلاناتي', 'mes annonces', 'remove worker', 'حذف عامل', 'remove job', 'حذف خدمة'):
            reply = employment.handle(phone, text)
            with connect() as con:
                session_task(con, phone, active)
                return record_reply(con, phone, msg_id, text, reply)
        if decision is None:
            decision = local_decision(text, active, data, pending, context, phone=phone)
        if decision is None:
            used_model = True
            try:
                with connect() as con:
                    entries = catalog.terms(con)
                    candidates = catalog.candidates(text, entries)
                decision = await understand(text, {'today': extraction.casablanca_today().isoformat(),
                    'timezone': 'Africa/Casablanca', 'active_task': active,
                    'draft': data, 'pending_field': pending,
                    'last_search': context.get('last_search'),
                    'catalog_candidates': candidates,
                    'catalog_options': [{key: item[key] for key in ('kind','code','label')} for item in entries][:40],
                    'recent_turns': [{**turn, 'text': turn['text'][:400]} for turn in history]})
            except AIError as exc:
                event('dialogue.fallback', outcome='error', error_code=exc.code)
                with connect() as con:
                    con.execute('INSERT OR REPLACE INTO dialogue_audit(message_id,task,route,error_code) VALUES (?,?,?,?)', (msg_id,active,'model',exc.code))
                    session_task(con, phone, active)
                    context.pop('offer', None)
                    if active == 'none':
                        context['show_menu'] = True
                        put_context(con, phone, context)
                        reply = ('خدمة فهم الرسائل الحرة عليها الضغط دابا. الاختيارات والبحث بالأسماء مازال خدامين.'
                                 if exc.code == 'conversation_rate_limited' else 'تعطلت خدمة فهم الرسالة دابا. تقدر تختار من اللائحة ونكملو بلا انتظار الموديل.')
                    else:
                        put_context(con, phone, context)
                        reply = 'تعطلت خدمة فهم الرسالة دابا، والمسودة ديالك باقية.\n' + next_question(active, pending, {})
                    return record_reply(con, phone, msg_id, text, reply)
    task = decision.task
    event('dialogue.route', previous_state=active, state=task,
          outcome='model' if used_model else 'fast',
          field_presence=sorted(decision.facts.model_dump(exclude_none=True)))
    if active == 'none' and task == 'machine' and decision.action == 'continue':
        decision.action = 'new'
    # Never let a model's interpretation of a mixed message approve a write.
    if decision.action == 'confirm' and not is_approval(text) and not (choice and choice['op'] == 'confirm'):
        decision.action = 'unknown'
    with connect() as con:
        con.execute('INSERT OR REPLACE INTO dialogue_audit(message_id,task,route) VALUES (?,?,?)', (msg_id,task,'model' if used_model else 'fast'))
        context = context_data(con, phone)
        if decision.action == 'menu':
            context['show_menu'] = True
            context['guided'] = True
            put_context(con, phone, context)
            session_task(con, phone, active)
            body = guided.MENU_TEXT + ('\n8. نكمل المسودة الحالية' if active != 'none' else '')
            return record_reply(con, phone, msg_id, text, body)
        if decision.action == 'reset':
            con.execute('DELETE FROM conversations WHERE farmer_phone=?', (phone,))
            con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
            con.execute('DELETE FROM dialogue_drafts WHERE phone=?', (phone,))
            con.execute('DELETE FROM dialogue_confirmations WHERE phone=?', (phone,))
            con.execute("UPDATE requests SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE farmer_phone=? AND status='draft'", (phone,))
            boundary = con.execute('SELECT COALESCE(MAX(id),0) FROM conversation_turns WHERE farmer_phone=?', (phone,)).fetchone()[0]
            put_context(con, phone, {'history_after_id': boundary, 'show_menu': True, 'guided': True})
            session_task(con, phone, 'none')
            con.execute('UPDATE dialogue_sessions SET last_reply=NULL,repeats=0 WHERE phone=?', (phone,))
            return record_reply(con, phone, msg_id, text,
                'واخا، حيدنا المسودات وبداينا محادثة جديدة. شنو محتاج دابا؟ الإعلانات والطلبات اللي سبق تسجلو باقين.')
        if decision.action == 'clarify':
            context['choose_publish'] = True
            put_context(con, phone, context)
            session_task(con, phone, active)
            return record_reply(con, phone, msg_id, text,
                'واش كتقلب على خدمة وبغيتي تنشر بروفايل ديالك، ولا عندك خدمة وبغيتي تلقى عمال؟')
        # A novel switch proposed by the model is checked with the user before
        # abandoning an in-progress profile. Slot answers cannot silently reroute.
        if used_model and active.startswith('post_') and task not in (active, 'none'):
            fields = decision.facts.model_dump(exclude_none=True)
            if pending in fields or 'name' in fields:
                task = decision.task = active
                decision.action = 'continue'
            else:
                context['pending_switch'] = decision.model_dump(mode='json')
                context['pending_switch']['action'] = 'switch'
                put_context(con, phone, context)
                session_task(con, phone, active)
                return record_reply(con, phone, msg_id, text,
                    f"دابا كنوجدو {TASK_NAMES[active]}. واش بغيتي نوقفوه ونمشيو لـ {TASK_NAMES[task]}؟ جاوب نعم باش نبدلو، أو لا باش نكملو.")
        if decision.action not in ('unknown', 'help'):
            context.pop('pending_switch', None)
            context.pop('choose_publish', None)
            context.pop('offer', None)
            if decision.action == 'new':
                context.pop('contact_mode', None)
            put_context(con, phone, context)
        if decision.action == 'cancel':
            if active == 'machine':
                con.execute('DELETE FROM conversations WHERE farmer_phone=?', (phone,))
                con.execute("UPDATE requests SET status='rejected' WHERE farmer_phone=? AND status='draft'", (phone,))
            elif active.startswith(('search_', 'post_')):
                con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
                con.execute('DELETE FROM dialogue_drafts WHERE phone=? AND task=?', (phone, active))
            con.execute('DELETE FROM dialogue_confirmations WHERE phone=? AND task=?', (phone, active))
            session_task(con, phone, 'none')
            return record_reply(con, phone, msg_id, text, 'تلغات المسودة الحالية. شنو بغيتي نديرو دابا؟')
        if decision.action in ('unknown', 'help') or task == 'none':
            session_task(con, phone, active)
            reply = next_question(active, pending, context)
            if decision.clarification:
                reply = decision.clarification.strip() + ('\n' + reply if active != 'none' else '')
            return record_reply(con, phone, msg_id, text, reply)
        if task != active:
            con.execute('UPDATE dialogue_confirmations SET armed=0 WHERE phone=?', (phone,))
            con.execute('UPDATE conversations SET awaiting_confirmation=0 WHERE farmer_phone=?', (phone,))
        session_task(con, phone, task)
        if task == 'search_machines':
            machines = catalog.search_machines(con, decision.facts.machine_type, decision.facts.location)
            session_task(con, phone, 'none')
            reply = ('\n\n'.join(f"{'🧪 تجريبي — ' if row['is_demo'] else ''}{row['name']}\n📍 {row['location']}\nالثمن للساعة: {row['hourly_rate']} درهم" for row in machines)
                     if machines else 'ما لقيتش هاد المعدات فهاد المكان. شنو نوع الخدمة اللي بغيتي تدير؟')
            return record_reply(con, phone, msg_id, text, reply)
        if task == 'machine':
            reply = machine_turn(con, phone, text, decision)
        else:
            reply = employment_turn(con, phone, text, decision, active)
        return record_reply(con, phone, msg_id, text, reply)


def machine_turn(con, phone, text, decision):
    from .service import merge_fields
    row = con.execute('SELECT * FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
    old = json.loads(row['payload']) if row else {}
    if not old:
        legacy = con.execute("SELECT payload FROM requests WHERE farmer_phone=? AND status='draft' ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
        old = json.loads(legacy['payload']) if legacy else {}
    if decision.action == 'new':
        old = {}
    facts = {k: v for k, v in decision.facts.model_dump(mode='json', exclude_none=True).items() if k in MACHINE_FIELDS}
    if facts.get('machine_type'):
        found = catalog.matches(facts['machine_type'], 'machine', catalog.terms(con))
        if found:
            facts['machine_type'] = found[0]['code']
    facts = extraction.normalized_patch(extraction.Extraction(**facts), text,
        date_context=bool(row and row['last_question_field'] == 'preferred_date'))
    local = extraction.deterministic_patch(text)
    facts.update(local)
    facts['intent'] = 'request_machine'
    data = merge_fields(old, facts)
    missing = missing_machine(data)
    if decision.action == 'confirm' and not missing and allowed(con, phone, 'machine', data):
        legacy = con.execute("SELECT id FROM requests WHERE farmer_phone=? AND status='draft' ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
        payload = json.dumps(data, ensure_ascii=False)
        if legacy:
            con.execute("UPDATE requests SET payload=?,status='pending_review',updated_at=CURRENT_TIMESTAMP WHERE id=?", (payload, legacy['id']))
        else:
            con.execute("INSERT INTO requests(farmer_phone,payload,status) VALUES (?,?,'pending_review')", (phone, payload))
        con.execute('DELETE FROM conversations WHERE farmer_phone=?', (phone,))
        con.execute('DELETE FROM dialogue_confirmations WHERE phone=? AND task=?', (phone, 'machine'))
        session_task(con, phone, 'none')
        return 'تسجل الطلب ديالك وغادي تراجعو التعاونية. هادي ماشي حجز مؤكد.'
    # A bare "no" asks what needs changing, without repeating the old summary.
    editing = decision.action == 'correct' and not decision.facts.model_dump(exclude_none=True) and not local
    pending = 'correction' if editing else (missing[0] if missing else None)
    reply = 'شنو المعلومة اللي بغيتي تصحح؟' if editing else (QUESTIONS[pending] if pending else machine_summary(data))
    if decision.clarification and pending and not editing:
        reply = decision.clarification + '\n' + reply
    con.execute('''INSERT INTO conversations(farmer_phone,payload,last_question_field,last_question,awaiting_confirmation)
        VALUES (?,?,?,?,0) ON CONFLICT(farmer_phone) DO UPDATE SET payload=excluded.payload,
        last_question_field=excluded.last_question_field,last_question=excluded.last_question,
        awaiting_confirmation=0,updated_at=CURRENT_TIMESTAMP''', (phone, json.dumps(data, ensure_ascii=False), pending, reply))
    con.execute('DELETE FROM dialogue_confirmations WHERE phone=? AND task=?', (phone, 'machine'))
    if not missing and not editing:
        grant(con, phone, 'machine', data)
    return reply


def employment_turn(con, phone, text, decision, active):
    task = decision.task
    row = con.execute('SELECT * FROM dialogue_drafts WHERE phone=? AND task=?', (phone, task)).fetchone()
    kind = 'worker' if task.endswith(('worker', 'workers')) else 'job'
    data = json.loads(row['payload']) if row and decision.action != 'new' and (task == active or decision.action == 'resume') else {'kind': kind}
    for field in decision.clear_fields:
        data.pop(field, None)
    facts = decision.facts.model_dump(mode='json', exclude_none=True)
    for key in employment.FIELDS:
        if key in facts:
            data[key] = clean_location(facts[key]) if key == 'location' else facts[key]
    if data.get('contact'):
        data['contact'] = valid_contact(data['contact'])
        if data['contact']:
            context = context_data(con, phone)
            context.pop('contact_mode', None)
            put_context(con, phone, context)
    if task.startswith('search_'):
        if data.get('location') or decision.facts.browse_all:
            reply = employment.search(con, kind, data.get('location'), conversational=True, work=data.get('work'))
            context = context_data(con, phone)
            context['last_search'] = {'kind': kind, 'location': data.get('location'), 'work': data.get('work')}
            context['offer'] = 'post_worker' if kind == 'job' else 'post_job'
            put_context(con, phone, context)
            con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
            con.execute('DELETE FROM dialogue_drafts WHERE phone=? AND task=?', (phone, task))
            session_task(con, phone, 'none')
            return reply
        con.execute('INSERT INTO employment_drafts(phone,payload,pending) VALUES (?,?,?) ON CONFLICT(phone) DO UPDATE SET payload=excluded.payload,pending=excluded.pending,awaiting_confirmation=0', (phone, json.dumps(data), 'search_location'))
        store_draft(con, phone, task, data, 'search_location')
        return ('فهمتك، كتقلب على عمال.\n' if kind == 'worker' else 'فهمتك، كتقلب على عروض الخدمة.\n') + employment.QUESTIONS['location']
    missing = [key for key in employment.FIELDS if not data.get(key)]
    if decision.action == 'confirm' and not missing and allowed(con, phone, task, data):
        con.execute('''INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner_phone,kind) DO UPDATE SET name=excluded.name,
            location=excluded.location,work=excluded.work,details=excluded.details,contact=excluded.contact,status='active' ''',
            (phone, kind, *(data[f] for f in employment.FIELDS)))
        con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
        con.execute('DELETE FROM dialogue_drafts WHERE phone=? AND task=?', (phone, task))
        con.execute('DELETE FROM dialogue_confirmations WHERE phone=? AND task=?', (phone, task))
        session_task(con, phone, 'none')
        return 'تنشر الإعلان ديالك. كتب إعلاناتي باش تشوفو، حذف عامل أو حذف خدمة باش تحيدو.'
    editing = decision.action == 'correct' and not facts and not decision.clear_fields
    pending = 'correction' if editing else (missing[0] if missing else 'confirm')
    reply = 'شنو المعلومة اللي بغيتي تصحح؟' if editing else (employment.summary(data) if pending == 'confirm' else profile_question(task, pending))
    if pending == 'contact':
        mode = context_data(con, phone).get('contact_mode')
        if mode:
            reply = 'كتب النمرة كاملة، مثلا 0612345678، أو رقم دولي بدايتو +.' if mode == 'phone' else 'كتب الإيميل أو رابط الحساب كامل، ماشي غير اسم فيسبوك.'
    if decision.clarification and pending != 'confirm':
        reply = decision.clarification + '\n' + reply
    con.execute('INSERT INTO employment_drafts(phone,payload,pending) VALUES (?,?,?) ON CONFLICT(phone) DO UPDATE SET payload=excluded.payload,pending=excluded.pending,awaiting_confirmation=0',
                (phone, json.dumps(data, ensure_ascii=False), pending))
    store_draft(con, phone, task, data, pending)
    con.execute('DELETE FROM dialogue_confirmations WHERE phone=? AND task=?', (phone, task))
    if pending == 'confirm':
        grant(con, phone, task, data)
    return reply


def store_draft(con, phone, task, data, pending):
    con.execute('INSERT INTO dialogue_drafts(phone,task,payload,pending) VALUES (?,?,?,?) ON CONFLICT(phone,task) DO UPDATE SET payload=excluded.payload,pending=excluded.pending',
                (phone, task, json.dumps(data, ensure_ascii=False), pending))


def profile_question(task, pending):
    if pending == 'name':
        return 'واخا، نوجدو بروفايل ديالك باش المشغلين يلقاوك. شنو سميتك؟' if task == 'post_worker' else 'واخا، نوجدو عرض العمل ديالك. شنو سميتك أو اسم المشغل؟'
    if pending == 'location':
        return 'فاش من مدينة أو دوار باغي تخدم؟' if task == 'post_worker' else 'فين كاينة هاد الخدمة؟'
    if pending == 'details':
        return 'زيدني على التجربة ديالك، فوقاش تقدر تبدا، والأجرة اللي باغي.' if task == 'post_worker' else 'شحال من عامل محتاج، فوقاش يبداو، وشنو الأجرة؟'
    if pending == 'contact':
        return CONTACT_QUESTION
    return employment.QUESTIONS.get(pending, 'شنو المعلومة اللي بغيتي تصحح؟')


def next_question(active, pending, context):
    if active.startswith('post_'):
        return f"باقين كنوجدو {TASK_NAMES[active]}. " + profile_question(active, pending) + '\nإلا بغيتي نبداو من الاول، كتب بدا من جديد.'
    if active == 'machine' and pending in QUESTIONS:
        return 'باقين فطلب الماكينة. ' + QUESTIONS[pending] + '\nتقدر تقول بدا من جديد.'
    if active.startswith('search_'):
        return employment.QUESTIONS['location']
    if context.get('last_search'):
        return ('نقدر نسجل بروفايل ديالك باش المشغلين يلقاوك. بغيتي ننشروه؟' if context['last_search']['kind'] == 'job'
                else 'نقدر نوجد ليك عرض عمل باش العمال يلقاوه. بغيتي ننشروه؟')
    return HELP
