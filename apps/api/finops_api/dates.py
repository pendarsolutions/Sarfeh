"""Dates and digits for people to read: Solar Hijri with Persian digits in Persian, ISO dates in English.

Python's standard library has no Solar Hijri calendar, so the conversion is the usual arithmetic one (the 33-year
cycle that ICU and browsers use too). Iran has kept UTC+03:30 all year since 2022, so a moment is shown in that
offset without needing a time zone database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

TEHRAN = timezone(timedelta(hours=3, minutes=30))
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_DAYS_BEFORE_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def fa_digits(text: object) -> str:
    return str(text).translate(_FA_DIGITS)


def fa_number(text: str) -> str:
    """"20,574,439.5" -> "۲۰٬۵۷۴٬۴۳۹٫۵": Persian digits and separators, as the voice guideline and the app write them."""
    return fa_digits(text.replace(",", "٬").replace(".", "٫"))


def to_jalali(d: date) -> tuple[int, int, int]:
    """Gregorian date to Solar Hijri (year, month, day)."""
    gy2 = d.year + 1 if d.month > 2 else d.year
    days = (355666 + 365 * d.year + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + d.day
            + _DAYS_BEFORE_MONTH[d.month - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def _parse(value: object) -> tuple[date, datetime | None] | None:
    """A calendar day, and the moment if there is one. Strings are ISO; a moment without an offset is UTC."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.date(), moment
    if isinstance(value, date):
        return value, None
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value), None
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return moment.date(), moment


def format_day(value: object, lang: str) -> str:
    """۱۴۰۵/۰۸/۱۷ in Persian (the day in Iran, for a moment), 2026-11-08 in English. Anything else as written."""
    parsed = _parse(value)
    if parsed is None:
        return "—" if value is None else str(value)
    day, moment = parsed
    if lang != "fa":
        return (moment.astimezone(UTC).date() if moment else day).isoformat()
    y, m, d = to_jalali(moment.astimezone(TEHRAN).date() if moment else day)
    return fa_digits(f"{y}/{m:02d}/{d:02d}")


# --------------------------------------------------------------------------- months

# Spend is recorded by Solar Hijri month, because that is how Iranian providers invoice and how the books close.
# A month is written "1405-08" (Aban 1405): sortable, and the same in every language.
MONTHS_FA = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
MONTHS_EN = ("Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar", "Mehr", "Aban", "Azar", "Dey",
             "Bahman", "Esfand")
MONTH_PATTERN = r"^1[34]\d\d-(0[1-9]|1[0-2])$"


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month(key: str) -> tuple[int, int]:
    year, month = key.split("-")
    y, m = int(year), int(month)
    if not (1300 <= y <= 1499 and 1 <= m <= 12):
        raise ValueError(f"not a month: {key}")
    return y, m


def month_of(day: date) -> str:
    """The Solar Hijri month a Gregorian day falls in."""
    y, m, _ = to_jalali(day)
    return month_key(y, m)


def today_in_iran(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(TEHRAN).date()


def current_month(now: datetime | None = None) -> str:
    return month_of(today_in_iran(now))


def shift_month(key: str, months: int) -> str:
    y, m = parse_month(key)
    index = y * 12 + (m - 1) + months
    return month_key(index // 12, index % 12 + 1)


def month_start(key: str) -> date:
    """The Gregorian day a Solar Hijri month begins on.

    Months 1-6 have 31 days and 7-11 have 30, so the first day is within a few days of Nowruz plus that offset; the
    exact day is the one the (tested) conversion above calls the 1st.
    """
    y, m = parse_month(key)
    guess = date(y + 621, 3, 21) + timedelta(days=(m - 1) * 31 if m <= 7 else 186 + (m - 7) * 30)
    for delta in (0, -1, 1, -2, 2, -3, 3):
        day = guess + timedelta(days=delta)
        if to_jalali(day) == (y, m, 1):
            return day
    raise ValueError(f"no first day found for {key}")  # pragma: no cover - the calendar has no such gap


def month_days(key: str) -> tuple[date, date]:
    """The first and last Gregorian day of a Solar Hijri month."""
    return month_start(key), month_start(shift_month(key, 1)) - timedelta(days=1)


def month_label(key: str, lang: str) -> str:
    """«آبان ۱۴۰۵» in Persian, "Aban 1405" in English."""
    y, m = parse_month(key)
    return f"{MONTHS_FA[m - 1]} {fa_digits(y)}" if lang == "fa" else f"{MONTHS_EN[m - 1]} {y}"


_LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_day(text: str) -> date:
    """A day typed by a person: Solar Hijri (۱۴۰۵/۰۸/۲۰, 1405-08-20) or Gregorian (2026-11-10), any digits."""
    import re

    m = re.fullmatch(r"\s*(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*", text.translate(_LATIN_DIGITS))
    if not m:
        raise ValueError("write a date as 1405/08/20 or 2026-11-10")
    y, mo, d = (int(g) for g in m.groups())
    if y >= 1900:
        return date(y, mo, d)
    key = month_key(y, mo)
    first, last = month_days(key)
    if not 1 <= d <= (last - first).days + 1:
        raise ValueError(f"{month_label(key, 'en')} has no day {d}")
    return first + timedelta(days=d - 1)


def parse_month_text(text: str) -> str:
    """A month typed by a person: 1405-08, ۱۴۰۵/۰۸, or a full date in either calendar (its month)."""
    import re

    t = text.translate(_LATIN_DIGITS).strip()
    if m := re.fullmatch(r"(\d{4})\s*[/.-]\s*(\d{1,2})", t):
        y, mo = int(m.group(1)), int(m.group(2))
        if y >= 1900:
            raise ValueError("write the Solar Hijri month (1405-08) or a full date: a Gregorian month spans two")
        key = month_key(y, mo)
        parse_month(key)
        return key
    return month_of(parse_day(t))
