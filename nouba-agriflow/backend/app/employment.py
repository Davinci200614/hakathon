"""Guided employment posts and location searches, independent of machinery drafts."""
import json
import re
import unicodedata
from .db import connect


def normalize(text):
    text = unicodedata.normalize('NFKD', text.casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', text).strip(' .!?؟،')


HELP = ('مرحبا! تقدر تكتب ليا بالدارجة، مثلا:\n'
        '• كنقلب على عمال فمكناس\n'
        '• كنقلب على خدمة ففاس\n'
        '• بغيت نسجل راسي كعامل\n'
        '• بغيت ننشر عرض عمل\n'
        '• إعلاناتي: باش تشوف الإعلانات ديالك\n'
        '• حذف عامل أو حذف خدمة: باش تحيد الإعلان\n'
        'كتب إلغاء باش تخرج من هاد المسودة.')
QUESTIONS = {
    'name': 'شنو سميتك أو اسم المشغل؟',
    'location': 'فاش من مدينة أو دوار كتقلب؟ كتب ليا المكان.',
    'work': 'شنو نوع الخدمة أو المهارات؟',
    'details': 'زيد التفاصيل: التجربة، فوقاش، الأجرة أو شحال من عامل محتاج.',
    'contact': 'شنو وسيلة الاتصال اللي موافق تبان للناس اللي كيقلبو؟ كتبها هنا.',
}
FIELDS = tuple(QUESTIONS)


def summary(data):
    kind = 'عامل' if data['kind'] == 'worker' else 'عرض عمل'
    return (f"{kind}\nالاسم: {data['name']}\n📍 {data['location']}\n"
            f"الخدمة: {data['work']}\nالتفاصيل: {data['details']}\nالاتصال: {data['contact']}\n"
            'هاد المعلومات ووسيلة الاتصال غادي تبان للناس اللي كيقلبو. موافق على النشر؟ جاوب نعم، تعديل أو إلغاء.')


def arm_confirmation(con, phone, reply):
    row = con.execute('SELECT * FROM employment_drafts WHERE phone=?', (phone,)).fetchone()
    if row and row['pending'] == 'confirm' and reply == summary(json.loads(row['payload'])):
        con.execute('UPDATE employment_drafts SET awaiting_confirmation=1 WHERE phone=?', (phone,))


def format_rows(rows):
    if not rows:
        return 'ما لقينا حتى إعلان فهاد المكان. تقدر تنشر إعلان ديالك: كتب نشر عامل إلا كتقلب على خدمة، أو نشر خدمة إلا محتاج عمال.'
    return '\n\n'.join(
        f"{'🧪 تجريبي — ' if 'is_demo' in r.keys() and r['is_demo'] else ''}#{r['id']} {'عامل' if r['kind'] == 'worker' else 'عرض عمل'}: {r['name']}\n"
        f"📍 {r['location']} — {r['work']}\n{r['details'][:180]}{'…' if len(r['details']) > 180 else ''}\n☎ {'بيانات خيالية للتجربة، ماشي جهة اتصال حقيقية' if 'is_demo' in r.keys() and r['is_demo'] else r['contact']}"
        for r in rows)


def natural_intent(value):
    """Recognize common Darija/Arabizi purposes before machinery extraction."""
    if re.search(r'بغيت\s+(?:ننشر|نحط).*?(?:خدمة|عمل)|\b(?:bghit|brit)\s+(?:nposter|ncher|n7et).*?(?:khedma|khadma|offre)', value):
        return 'job'
    if re.search(r'نسجل\s+راسي.*عامل|انا\s+عامل|\bana\s+(?:3amel|kheddam)|\b(?:bghit|brit)\s+nsjel\s+rasi', value):
        return 'worker'
    wants = r'(?:بغيت|محتاج|كنقلب|كنقلبو|خاصني|خصني|\b(?:bghit|brit|bghina|khasni|khassni|kan9leb|kan9lb|kanqleb)\b)'
    people = r'(?:عمال|عامل|خدام|خدامة|واحد\s+(?:دري|السيد|سيد)|\b(?:3omal|3ommal|3amel|kheddam|khedam|drari|ouvriers?|workers?)\b|\b(?:wahed|wa7ed)\s+(?:dri|derri|siyed)\b)'
    if re.search(wants, value) and re.search(people, value):
        return 'search_worker'
    if re.search(wants, value) and re.search(r'(?:خدمة|نخدم|عمل|\b(?:nkhdem|nkhedem|n5dem|n25dm|khedma|khadma|khdma|emploi|job)\b)', value):
        return 'search_job'
    return None


def search(con, kind, location, conversational=False, work=None):
    from .locations import location_key
    from .catalog import terms, key
    entries = terms(con)
    con.create_function('search_normalize', 1, lambda value: key(value,'location',entries) if key(value,'location',entries) != normalize(value) else location_key(value))
    con.create_function('skill_normalize', 1, lambda value: key(value,'skill',entries))
    filters = ["kind=?", "status='active'"]
    params = [kind]
    if location:
        filters.append('instr(search_normalize(location),?)>0')
        place = key(location,'location',entries)
        params.append(place if place != normalize(location) else location_key(location))
    if work:
        filters.append('instr(skill_normalize(work),?)>0')
        params.append(key(work,'skill',entries))
    rows = con.execute('SELECT * FROM employment_listings WHERE '+' AND '.join(filters)+' ORDER BY is_demo,id DESC LIMIT 5', params).fetchall()
    if conversational:
        offer = ('نقدر نسجل بروفايل ديالك باش المشغلين يلقاوك. بغيتي نوجدوه؟' if kind == 'job'
                 else 'نقدر نوجد ليك عرض عمل باش العمال يلقاوه. بغيتي نوجدوه؟')
        return (format_rows(rows) + '\n\n' if rows else 'ما لقينا حتى إعلان فهاد المكان.\n') + offer
    return format_rows(rows) + '\n\nكنوريك حتى لـ 5 إعلانات. تقدر تقلب فمكان آخر أو تنشر إعلان ديالك.'


def handle(phone, text):
    """Return None to continue the existing machinery workflow."""
    value = normalize(text)
    commands = {
        'post worker': 'worker', 'نشر عامل': 'worker', 'انا عامل': 'worker',
        'بغيت نخدم': 'worker', 'je cherche du travail': 'worker',
        'post job': 'job', 'نشر خدمة': 'job', 'نشر عرض عمل': 'job', 'publier emploi': 'job',
        'find workers': 'search_worker', 'بحث عمال': 'search_worker', 'بغيت عمال': 'search_worker',
        'je cherche des ouvriers': 'search_worker',
        'find jobs': 'search_job', 'بحث خدمة': 'search_job', 'بحث عن عمل': 'search_job',
        'chercher emploi': 'search_job',
    }
    command = commands.get(value)
    with connect() as con:
        row = con.execute('SELECT * FROM employment_drafts WHERE phone=?', (phone,)).fetchone()
        # Free-text answers to listing fields must not unexpectedly restart a flow.
        if not command and (not row or row['pending'] in ('confirm', 'search_location')):
            command = natural_intent(value)
        if value in ('emploi', 'jobs', 'workers', 'عمال', 'خدمة', 'help', 'menu', 'مساعدة'):
            return HELP
        if value in ('my listings', 'إعلاناتي', 'اعلاناتي', 'mes annonces'):
            rows = con.execute('SELECT * FROM employment_listings WHERE owner_phone=? AND status=? ORDER BY id DESC', (phone, 'active')).fetchall()
            return format_rows(rows)
        removals = {'remove worker': 'worker', 'حذف عامل': 'worker', 'remove job': 'job', 'حذف خدمة': 'job'}
        if value in removals:
            con.execute("UPDATE employment_listings SET status='inactive' WHERE owner_phone=? AND kind=?", (phone, removals[value]))
            return 'تحيد الإعلان ديالك من البحث.'
        if command:
            pending = 'search_location' if command.startswith('search_') else 'name'
            data = {'kind': command.removeprefix('search_')}
            # Keep the machinery draft, but require a fresh summary before saving it.
            con.execute('UPDATE conversations SET awaiting_confirmation=0 WHERE farmer_phone=?', (phone,))
            if pending == 'search_location':
                place = re.search(r'(?:\s(?:f|fi|fـ)\s+|\sف(?:ي\s+)?)([\w -]{2,60})$', value)
                if place:
                    con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
                    return search(con, data['kind'], place[1])
            con.execute('INSERT INTO employment_drafts(phone,payload,pending) VALUES (?,?,?) ON CONFLICT(phone) DO UPDATE SET payload=excluded.payload,pending=excluded.pending,awaiting_confirmation=0',
                        (phone, json.dumps(data), pending))
            if pending == 'search_location':
                purpose = 'عمال' if data['kind'] == 'worker' else 'عروض الخدمة'
                return f'فهمتك، كتقلب على {purpose}.\n' + QUESTIONS['location']
            return QUESTIONS['name']
        if not row:
            return None
        if value in ('cancel', 'annuler', 'إلغاء', 'الغاء', 'لغي'):
            con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
            return 'تلغات المسودة.'
        data = json.loads(row['payload'])
        pending = row['pending']
        if pending == 'search_location':
            needle = normalize(text)
            if not needle or len(text) > 100:
                return QUESTIONS['location']
            con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
            return search(con, data['kind'], needle)
        if pending == 'confirm':
            if value in ('modifier', 'تعديل', 'no', 'non', 'لا'):
                con.execute("UPDATE employment_drafts SET pending='name',awaiting_confirmation=0 WHERE phone=?", (phone,))
                return QUESTIONS['name']
            if value not in ('yes', 'oui', 'نعم', 'موافق', 'confirm', 'ah', 'iyah', 'iyeh', 'wakha', 'اه', 'ايه', 'واخا') or not row['awaiting_confirmation']:
                return summary(data)
            con.execute('''INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner_phone,kind) DO UPDATE SET
                name=excluded.name,location=excluded.location,work=excluded.work,
                details=excluded.details,contact=excluded.contact,status='active' ''',
                (phone, data['kind'], *(data[field] for field in FIELDS)))
            con.execute('DELETE FROM employment_drafts WHERE phone=?', (phone,))
            return 'تنشر الإعلان ديالك.\nكتب إعلاناتي باش تشوفو، حذف عامل أو حذف خدمة باش تحيدو.'
        limit = 400 if pending == 'details' else 100
        if not text.strip() or len(text.strip()) > limit:
            return f'كتب جواب بين 1 و {limit} حرف.\n' + QUESTIONS[pending]
        data[pending] = text.strip()
        index = FIELDS.index(pending) + 1
        next_field = FIELDS[index] if index < len(FIELDS) else 'confirm'
        con.execute('UPDATE employment_drafts SET payload=?,pending=?,awaiting_confirmation=0 WHERE phone=?',
                    (json.dumps(data, ensure_ascii=False), next_field, phone))
        return summary(data) if next_field == 'confirm' else QUESTIONS[next_field]
