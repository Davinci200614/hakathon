"""Small extraction contract and deterministic normalization/date interpretation."""
import json
import re
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    intent: Literal['machinery_request', 'other'] | None = None
    machine_type: str | None = None
    operation: str | None = None
    area_hectares: float | None = Field(default=None, gt=0)
    location: str | None = None
    preferred_date: date | None = None
    notes: str | None = None

    @field_validator('area_hectares', mode='before')
    @classmethod
    def numeric_area(cls, value):
        if isinstance(value, bool):
            raise ValueError('invalid area')
        return value

    @field_validator('machine_type', 'operation', 'location', 'notes')
    @classmethod
    def clean(cls, value):
        return (value.strip() or None) if value is not None else None


def compact_schema():
    def compact(value):
        if isinstance(value, dict):
            return {k:compact(v) for k,v in value.items() if k not in ('title','default')}
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value
    return compact(Extraction.model_json_schema())


def parse_extraction(raw):
    """One syntax-only recovery (code fences); never repair missing facts."""
    try:
        return Extraction.model_validate_json(raw)
    except ValidationError:
        # Retry parsing once, only if the entire response is a fenced JSON value.
        fenced = re.fullmatch(r'\s*```(?:json)?\s*([\s\S]*?)\s*```\s*', raw or '')
        if not fenced:
            raise
        return Extraction.model_validate_json(fenced.group(1))


def casablanca_today():
    return datetime.now(ZoneInfo('Africa/Casablanca')).date()


WEEKDAYS = {
    'الاثنين':0,'الإثنين':0,'lundi':0,'الثلاثاء':1,'الثلاثا':1,'mardi':1,
    'الأربعاء':2,'الاربعاء':2,'mercredi':2,'الخميس':3,'jeudi':3,
    'الجمعة':4,'vendredi':4,'السبت':5,'samedi':5,'الأحد':6,'الاحد':6,'dimanche':6,
}
MONTHS = {'يناير':1,'فبراير':2,'مارس':3,'أبريل':4,'ابريل':4,'ماي':5,'مايو':5,
          'يونيو':6,'يوليوز':7,'غشت':8,'شتنبر':9,'أكتوبر':10,'اكتوبر':10,'نونبر':11,'دجنبر':12,
          'janvier':1,'février':2,'fevrier':2,'mars':3,'avril':4,'mai':5,'juin':6,
          'juillet':7,'août':8,'aout':8,'septembre':9,'octobre':10,'novembre':11,'décembre':12,'decembre':12}


def resolve_date(text, today=None):
    """Return (date, mentioned). A mentioned but ambiguous date stays missing."""
    today = today or casablanca_today()
    text = text.lower().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩','0123456789'))
    if re.search(r'(?<!\w)(?:ولا|أو|ou|or)(?!\w)', text) and re.search(r'غدا|اليوم|demain|نهار|\d', text):
        return None, True
    iso = re.search(r'\b(20\d{2})[/.-](\d{1,2})[/.-](\d{1,2})\b', text)
    numeric = re.search(r'\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b', text)
    try:
        if iso:
            return date(*map(int, iso.groups())).isoformat(), True
        if numeric:
            d,m,y=map(int,numeric.groups())
            return date(y,m,d).isoformat(), True
        for name,month in MONTHS.items():
            match=re.search(r'(?<!\d)(\d{1,2})\s+'+name+r'(?:\s+(20\d{2}))?(?!\w)',text)
            if match:
                candidate = date(int(match[2]) if match[2] else today.year, month, int(match[1]))
                if not match[2] and candidate < today:
                    candidate = date(today.year + 1, month, int(match[1]))
                return candidate.isoformat(), True
    except ValueError:
        return None, True
    for pattern,days in [(r'بعد\s+غدا|بعد\s+غد|après[- ]demain',2),
                         (r'غدا|غداً|demain|\bghdda\b|\bghda\b',1),
                         (r'اليوم|ليوم|aujourd.hui|\b(?:lyoum|lyom)\b',0)]:
        if re.search(pattern,text):
            return (today+timedelta(days=days)).isoformat(),True
    for name,weekday in WEEKDAYS.items():
        if re.search(r'(?<!\w)'+name+r'(?!\w)',text):
            # "Next Tuesday" means the next occurrence strictly after today;
            # the full calendar date is always presented for user confirmation.
            if re.search(r'الجاي|القادم|prochain',text):
                return (today+timedelta(days=(weekday-today.weekday())%7 or 7)).isoformat(),True
            return (today+timedelta(days=(weekday-today.weekday())%7)).isoformat(),True
    ambiguous=bool(re.search(r'\b\d{1,2}[/.-]\d{1,2}\b|الأسبوع|السيمانة|semaine',text))
    return None,ambiguous


ALIASES = {
    'machine_type': {'تراكتور':'tractor','جرار':'tractor','tracteur':'tractor','traktor':'tractor',
                     'tractor':'tractor','moissonneuse':'harvester','حصادة':'harvester'},
    'operation': {'الحرث':'plowing','حرث':'plowing','يحرث':'plowing','labour':'plowing',
                  'labourage':'plowing','ploughing':'plowing','plowing':'plowing',
                  'حصاد':'harvesting','récolte':'harvesting'},
}


def normalized_patch(extracted, text, *, today=None, date_context=False):
    data=extracted.model_dump(mode='json',exclude_none=True)
    if data.get('intent')=='machinery_request':
        data['intent']='request_machine'  # preserve existing database/dashboard contract
    for field,aliases in ALIASES.items():
        if field in data:
            data[field]=aliases.get(data[field].lower(),data[field])
    # A narrow deterministic supplement for a common Darija area expression.
    words={'جوج':2,'واحد':1,'ثلاثة':3,'تلاتة':3,'ربعة':4,'خمسة':5}
    match=re.search(r'(جوج|واحد|ثلاثة|تلاتة|ربعة|خمسة)\s+هكتار',text)
    if match:
        data['area_hectares']=words[match[1]]
    resolved,mentioned=resolve_date(text, today)
    # Explicit ambiguous/invalid syntax always wins over a model guess.
    # For other natural-language answers, accept the model only in date context
    # or when the message contains a temporal cue.
    temporal = date_context or bool(re.search(r'تاريخ|نهار|موعد|date|semaine|أسبوع|السيمانة', text.lower()))
    if not temporal:
        data.pop('preferred_date', None)
    if mentioned:
        data['preferred_date']=resolved
        data['preferred_date_text']=None if resolved else text
    data = {k:clean_field(k,v) for k,v in data.items()}
    data.update(deterministic_patch(text, today=today))
    from .diagnostics import event
    event('extraction.normalized', field_presence=sorted(k for k,v in data.items() if v is not None),
          normalized_date=data.get('preferred_date'), date_mentioned=mentioned)
    return data


INVALID_TEXT = {'unknown', 'null', 'none', 'n/a', 'غير معروف', 'مجهول'}
QUANTITIES = {'جوج', 'واحد', 'واحدة', 'ثلاثة', 'تلاتة', 'ربعة', 'خمسة'}


def clean_field(field, value):
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in INVALID_TEXT:
            return None
        if field in ALIASES:
            value = ALIASES[field].get(value.lower(), value.lower())
        if field == 'location' and (value in QUANTITIES or re.fullmatch(r'[\d\s.,]+', value)
                                    or re.search(r'هكتار', value)):
            return None
    return value


def deterministic_patch(text, pending=None, *, today=None):
    """Only unambiguous local facts; corrections refer to the positive clause."""
    patch = {}
    positive = re.split(r'\s+(?:مشي|ماشي)\s+', text.strip())[0]
    for field, aliases in ALIASES.items():
        for alias, canonical in aliases.items():
            if re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', positive.lower()):
                patch[field] = canonical
                break
    words = {'جوج':2, 'واحد':1, 'ثلاثة':3, 'تلاتة':3, 'ربعة':4, 'خمسة':5}
    area = re.search(r'(جوج|واحد|ثلاثة|تلاتة|ربعة|خمسة|\d+(?:[.,]\d+)?)\s*هكتار', positive)
    if area:
        patch['area_hectares'] = words[area[1]] if area[1] in words else float(area[1].replace(',', '.'))
    place = re.fullmatch(r'(?:المكان\s+|(?:أنا|انا)\s+ف(?:ي)?\s+|في\s+|ف\s+)(.+)', positive)
    if place:
        patch['location'] = clean_field('location', place[1])
    elif pending == 'location' and re.fullmatch(r'[\w\s]+', positive) and not patch:
        if not re.search(r'(?<!\w)(?:ما عرفت|ماعرفتش|نعم|لا|تعديل|هكتار|واش|كيفاش)(?!\w)', positive):
            patch['location'] = clean_field('location', re.sub(r'^ف(?=ال)', '', positive))
    resolved, mentioned = resolve_date(text, today)
    if mentioned:
        patch['preferred_date'] = resolved
        patch['preferred_date_text'] = None if resolved else text
    return patch
