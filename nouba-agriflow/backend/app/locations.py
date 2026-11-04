"""Literal location cleanup and a small shared spelling dictionary."""
import re
from .employment import normalize


def clean_location(value):
    value = re.sub(r'^(?:في|ف|fi|f)\s+', '', value.strip(), flags=re.IGNORECASE).strip()
    value = re.sub(r'^(?:فنواحي|نواحي)\s+', '', value).strip()
    return re.sub(r'^ف(?=سلا|مكناس|بولمان|[إا]نز[كغ]ان|دكالة|ال)', '', value)


def location_key(value):
    value = normalize(clean_location(value))
    groups = {
        'سلا الجديدة': ('سلا الجديدة', 'سلا جديدة', 'sale al jadida', 'sale el jadida', 'sala al jadida'),
        'انزكان': ('انزكان', 'انزغان', 'inzegane', 'inezgane', 'inzgan', 'inzgane'),
        'بولمان': ('بولمان', 'boulemane', 'boulmane'),
        'مكناس': ('مكناس', 'meknes'),
        'فاس': ('فاس', 'fes', 'fez'),
    }
    for canonical, variants in groups.items():
        for variant in variants:
            value = re.sub(r'(?<!\w)' + re.escape(variant) + r'(?!\w)', canonical, value)
    return value
