import re

import phonenumbers


def normalize_phone(raw: str) -> str:
    """Приводит номер к виду 79XXXXXXXXX. Пустая строка, если номер не распознан.

    В чате и в таблице номера пишут как угодно: «+79…», «89…», «+7 (985) 280-10-92»,
    «+ 7926…», «79153211745.0». Всё это сводится к одному виду, чтобы искать дубли.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    s = re.sub(r"\.0$", "", s)
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    try:
        num = phonenumbers.parse("+" + digits, None)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_possible_number(num):
        return ""
    return f"{num.country_code}{num.national_number}"


def format_phone(norm: str) -> str:
    """79852801092 → +7 985 280-10-92."""
    if len(norm) == 11 and norm.startswith("7"):
        return f"+7 {norm[1:4]} {norm[4:7]}-{norm[7:9]}-{norm[9:11]}"
    return ("+" + norm) if norm else ""
