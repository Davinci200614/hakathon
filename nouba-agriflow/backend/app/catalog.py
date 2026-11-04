"""Database-backed entity vocabulary and bounded matching, shared by chat/search."""
import json
import re
from difflib import SequenceMatcher
from .db import connect
from .employment import normalize

BASE = [
    ('machine', 'tractor', 'تراكتور', ['tractor','tracteur','جرار','تراكتور']),
    ('machine', 'harvester', 'حصادة', ['harvester','combine harvester','moissonneuse','حصادة','ماكينة ديال الحصاد','ماكينة الحصاد','ماكينة ديال الجني']),
    ('machine', 'baler', 'مكبس التبن', ['baler','presse a balles','مكبس','مكبس التبن','بريسة','presse']),
    ('machine', 'sprayer', 'رشاشة', ['sprayer','pulverisateur','رشاشة','ماكينة الرش','ماكينة الدوا']),
    ('machine', 'irrigation_pump', 'مضخة الماء', ['water pump','pompe','مضخة','موتور الماء','بومبة']),
    ('skill', 'electrician', 'كهربائي', ['electrician','electricien','تريسيان','تريسيان','نريسيان','كهرباء','كهربائي','تريسيان ديال الدار']),
    ('skill', 'harvesting', 'الحصاد', ['harvesting','harvest','recolte','حصاد','الجني','جني','الحصاد']),
    ('skill', 'plumber', 'سباك', ['plumber','plombier','بلومبي','سباك','سباكة']),
    ('skill', 'carpenter', 'نجار', ['carpenter','menuisier','نجار','نجارة']),
    ('skill', 'driver', 'سائق', ['driver','chauffeur','سائق','سياقة','السياقة','شيفور']),
    ('skill', 'construction', 'البناء', ['construction','macon','بناي','بناء','البناء']),
    ('skill', 'quarry', 'الكاريان', ['quarry','carriere','كاريان','الكاريان']),
    ('location', 'sale_al_jadida', 'سلا الجديدة', ['سلا جديدة','سلا الجديدة','sale al jadida','sale el jadida','sala al jadida']),
    ('location', 'inezgane', 'إنزكان', ['انزكان','إنزكان','انزغان','إنزغان','inezgane','inzegane','inzgan']),
    ('location', 'boulemane', 'بولمان', ['بولمان','boulemane','boulmane']),
    ('location', 'meknes', 'مكناس', ['مكناس','meknes']),
    ('location', 'fes', 'فاس', ['فاس','fes','fez']),
    ('location', 'doukkala', 'دكالة', ['دكالة','doukkala']),
]


def install_catalog(con):
    for kind, code, label, aliases in BASE:
        con.execute('INSERT OR IGNORE INTO catalog_terms(kind,code,label,aliases) VALUES (?,?,?,?)',
                    (kind, code, label, json.dumps(aliases, ensure_ascii=False)))


def terms(con=None):
    if con is None:
        with connect() as con:
            return terms(con)
    entries = [{**dict(row), 'aliases': json.loads(row['aliases'])} for row in con.execute('SELECT * FROM catalog_terms ORDER BY kind,code')]
    # New inventory names and published professions are discoverable immediately;
    # admins only need explicit aliases for alternate spellings/descriptions.
    for row in con.execute('SELECT machine_type,name FROM machines'):
        existing = next((item for item in entries if item['kind']=='machine' and item['code']==row['machine_type']),None)
        if existing:
            if row['name'] not in existing['aliases']:
                existing['aliases'].append(row['name'])
        else:
            entries.append({'kind':'machine','code':row['machine_type'],'label':row['name'],'aliases':[row['name']]})
    for row in con.execute("SELECT DISTINCT work FROM employment_listings WHERE status='active'"):
        if not matches(row['work'],'skill',entries):
            entries.append({'kind':'skill','code':normalize(row['work']),'label':row['work'],'aliases':[row['work']]})
    return entries


def matches(text, kind, entries):
    value = normalize(text.replace('_',' '))
    found = []
    for item in entries:
        if item['kind'] != kind:
            continue
        for alias in [item['code'], item['label'], *item['aliases']]:
            alias = normalize(alias.replace('_',' '))
            if re.search(r'(?<!\w)[وفبلك]?(?:ال)?' + re.escape(alias) + r'(?!\w)', value):
                found.append(item)
                break
    return found


def key(text, kind, entries):
    found = matches(text, kind, entries)
    if found:
        return ' '.join(item['code'] for item in found)
    return normalize(text)


def candidates(text, entries):
    words = normalize(text).split()[:40]
    pieces = [' '.join(words[i:i+n]) for n in (1,2,3) for i in range(len(words)-n+1)]
    ranked = []
    for item in entries:
        score = max((SequenceMatcher(None, piece, normalize(alias)).ratio()
                     for alias in [item['label'], *item['aliases']] for piece in pieces), default=0)
        if score >= 0.55:
            ranked.append((score, item))
    return [dict(kind=item['kind'], code=item['code'], label=item['label'], aliases=item['aliases'][:5])
            for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:8]]


def search_machines(con, machine_type=None, location=None):
    entries = terms(con)
    con.create_function('catalog_machine', 1, lambda value: key(value or '', 'machine', entries))
    con.create_function('catalog_place', 1, lambda value: key(value or '', 'location', entries))
    filters = ['available=1']
    values = []
    for field, kind, value in [('machine_type','machine',machine_type), ('location','location',location)]:
        if value:
            filters.append(f'instr(catalog_{"machine" if kind == "machine" else "place"}({field}),?)>0')
            values.append(key(value,kind,entries))
    return [dict(row) for row in con.execute('SELECT * FROM machines WHERE '+' AND '.join(filters)+' ORDER BY is_demo,id DESC LIMIT 5',values)]
