"""Validate concrete public contacts; a channel name is not an address."""
import re


def phone_number(value):
    compact = re.sub(r'[\s().-]', '', value)
    if compact.startswith('00'):
        compact = '+' + compact[2:]
    if re.fullmatch(r'0[5-7]\d{8}', compact):
        return compact
    if re.fullmatch(r'\+?[1-9]\d{7,14}', compact):
        return compact if compact.startswith('+') else '+' + compact
    return None


def extract_contacts(text):
    values = re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)
    values += re.findall(r'https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[^\s،,]+', text, flags=re.I)
    values += re.findall(r'(?<![\w@])@[A-Za-z][\w.]{2,}', text)
    invalid_number = False
    for candidate in re.findall(r'(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)', text):
        if re.fullmatch(r'\s*(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2})\s*', candidate):
            continue
        value = phone_number(candidate)
        if value:
            values.insert(0, value)
        else:
            invalid_number = True
    return ' / '.join(dict.fromkeys(values)) or None, invalid_number


def valid_contact(value):
    if not value:
        return None
    result, invalid = extract_contacts(value)
    return result if not invalid else None


CONTACT_QUESTION = ('عطيني وسيلة الاتصال الحقيقية اللي موافق تبان للناس: نمرة كاملة، إيميل، أو رابط الحساب. '
                    'كلمة واتساب أو فيسبوك بوحدها ما كافياش. تقدر تختار نستعملو نمرة هاد الواتساب.')
