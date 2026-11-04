"""Persisted WhatsApp list/button choices, sharing the natural-chat controller."""
import hashlib
import json

MENU_TEXT = ('شنو بغيتي نديرو؟ اختار من اللائحة، أو جاوب بالرقم. '
             'تقدر حتى تكتب بالدارجة فاي وقت.\n'
             '1. ننشر بروفايل باش نلقى خدمة\n2. ننشر عرض عمل\n'
             '3. نقلب على خدمة\n4. نقلب على عمال\n5. نطلب ماكينة\n6. إعلاناتي\n7. نبدا من جديد')
MAIN = [
    ('ننشر بروفايل ديالي', {'op': 'new', 'task': 'post_worker'}),
    ('ننشر عرض عمل', {'op': 'new', 'task': 'post_job'}),
    ('نقلب على خدمة', {'op': 'new', 'task': 'search_jobs'}),
    ('نقلب على عمال', {'op': 'new', 'task': 'search_workers'}),
    ('نطلب ماكينة', {'op': 'new', 'task': 'machine'}),
    ('إعلاناتي', {'op': 'listings'}),
    ('نبدا من جديد', {'op': 'reset'}),
]
LABELS = {'name': 'الاسم', 'location': 'المكان', 'work': 'الخدمة والمهارات',
          'details': 'التجربة والأجرة والتوفر', 'contact': 'وسيلة الاتصال',
          'machine_type': 'الماكينة', 'operation': 'الخدمة', 'area_hectares': 'المساحة', 'preferred_date': 'التاريخ'}


def build(con, phone, msg_id, reply, context):
    session = con.execute('SELECT task FROM dialogue_sessions WHERE phone=?', (phone,)).fetchone()
    task = session['task'] if session else 'none'
    row = (con.execute('SELECT last_question_field AS pending FROM conversations WHERE farmer_phone=?', (phone,)).fetchone()
           if task == 'machine' else con.execute('SELECT pending FROM dialogue_drafts WHERE phone=? AND task=?', (phone, task)).fetchone())
    pending = row['pending'] if row else None
    choices = []
    buttons = False
    if context.pop('show_menu', False):
        choices = MAIN + ([('نكمل المسودة الحالية', {'op': 'resume', 'task': task})] if task != 'none' else [])
    elif context.pop('show_fields', False) or pending == 'correction':
        fields = ('machine_type', 'operation', 'area_hectares', 'location', 'preferred_date') if task == 'machine' else ('name', 'location', 'work', 'details', 'contact')
        choices = [(LABELS[f], {'op': 'field', 'field': f}) for f in fields]
    elif reply.startswith(('واش هاد المعلومات صحيحة؟', 'عامل\n', 'عرض عمل\n')) and task in ('machine', 'post_worker', 'post_job'):
        choices = [('نعم، سجل', {'op': 'confirm'}), ('نبدل معلومات', {'op': 'edit'}), ('إلغاء', {'op': 'cancel'})]
        buttons = True
    elif task.startswith('post_') and pending == 'contact' and not context.get('contact_mode'):
        choices = [('واتساب هاد الرقم', {'op': 'contact_self'}), ('نعطي رقم آخر', {'op': 'contact_other', 'kind': 'phone'}), ('إيميل أو رابط حساب', {'op': 'contact_other', 'kind': 'email'})]
        buttons = True
    elif context.get('guided') and task == 'machine' and pending == 'machine_type':
        from .catalog import terms
        machines = sorted((item for item in terms(con) if item['kind'] == 'machine'), key=lambda item: ({'tractor':0,'harvester':1}.get(item['code'],2),item['label']))
        choices = [(item['label'], {'op': 'reply', 'text': item['label']}) for item in machines][:10]
    elif context.get('guided') and task == 'machine' and pending == 'operation':
        choices = [('الحرث', {'op': 'reply', 'text': 'حرث'}), ('الحصاد', {'op': 'reply', 'text': 'حصاد'})]
    elif context.get('guided') and task.startswith('post_') and pending == 'work':
        choices = [(title, {'op': 'reply', 'text': title}) for title in ('الحصاد', 'الكهرباء', 'البناء', 'السياقة')]
    elif task == 'none' and context.get('offer'):
        choices = [('نعم، نوجد الإعلان', {'op': 'offer'}), ('نرجع للاختيارات', {'op': 'menu'})]
        buttons = True
    context.pop('choices', None)
    if not choices or len(reply) > 1024:
        return None
    token = hashlib.sha256(msg_id.encode()).hexdigest()[:16]
    ids = [f'choice:{token}:{index}' for index in range(1, len(choices) + 1)]
    context['choices'] = {key: action for key, (_, action) in zip(ids, choices)}
    body = {'type': 'button' if buttons else 'list', 'body': {'text': reply}}
    if buttons:
        body['action'] = {'buttons': [{'type': 'reply', 'reply': {'id': key, 'title': title[:20]}} for key, (title, _) in zip(ids, choices)]}
    else:
        body['action'] = {'button': 'نختار', 'sections': [{'title': 'الاختيارات', 'rows': [{'id': key, 'title': title[:24]} for key, (title, _) in zip(ids, choices)]}]}
    con.execute('INSERT OR REPLACE INTO dialogue_outgoing(message_id,payload) VALUES (?,?)', (msg_id, json.dumps(body, ensure_ascii=False)))
    return body


def resolve(context, text, selection_id=None):
    choices = context.get('choices', {})
    if selection_id:
        return choices.get(selection_id, {'op': 'stale'})
    if text.strip() == '0':
        return None
    if text.strip().isdigit() and len(text.strip()) <= 2 and choices:
        index = int(text.strip()) - 1
        values = list(choices.values())
        return values[index] if 0 <= index < len(values) else {'op': 'stale'}
    return None


def outgoing(con, msg_id):
    row = con.execute('SELECT payload FROM dialogue_outgoing WHERE message_id=?', (msg_id,)).fetchone()
    return json.loads(row['payload']) if row else None
