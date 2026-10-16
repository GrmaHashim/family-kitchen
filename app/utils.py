"""Shared helpers: money, dates/times, phone numbers, statuses."""
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import current_app

STATUSES = {
    "new": "جديد",
    "confirmed": "مؤكَّد",
    "preparing": "قيد التحضير",
    "ready": "جاهز",
    "done": "تم التسليم",
    "cancelled": "ملغى",
}
ACTIVE_STATUSES = ["new", "confirmed", "preparing", "ready"]
NEXT_STATUS = {"new": "confirmed", "confirmed": "preparing", "preparing": "ready", "ready": "done"}
NEXT_ACTION = {
    "new": "تأكيد الطلب",
    "confirmed": "بدء التحضير",
    "preparing": "أصبح جاهزًا",
    "ready": "تم التسليم",
}
SOURCES = {"web": "الموقع", "phone": "مكالمة", "whatsapp": "واتساب", "walkin": "حضوري", "other": "أخرى"}
FULFILLMENT = {"pickup": "استلام", "delivery": "توصيل"}

AR_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


# ---------- money ----------
def money(cents):
    """Format cents as dollars, wrapped in LTR isolate marks so '$' stays put in RTL text."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"\u2066{sign}${cents // 100:,}.{cents % 100:02d}\u2069"


def parse_money(text):
    """'12.5' -> 1250. Returns None for empty/invalid input."""
    if text is None:
        return None
    text = str(text).strip().replace("$", "").replace(",", ".")
    if not text:
        return None
    try:
        value = round(float(text) * 100)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_int(text, default=0, lo=None, hi=None):
    try:
        n = int(str(text).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return lo
    if hi is not None and n > hi:
        return hi
    return n


# ---------- time ----------
def tz():
    return ZoneInfo(current_app.config["KITCHEN_TZ"])


def now_local():
    return datetime.now(tz())


def today():
    return now_local().date()


def today_str():
    return today().isoformat()


def now_iso():
    return now_local().isoformat(timespec="seconds")


def parse_date(text, default=None):
    try:
        return date.fromisoformat(str(text))
    except (TypeError, ValueError):
        return default


def valid_time(text):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(text or "")))


def fmt_time(text):
    if not valid_time(text):
        return ""
    h, m = map(int, text.split(":"))
    suffix = "م" if h >= 12 else "ص"
    h = h % 12 or 12
    return f"{h}:{m:02d} {suffix}"


def fmt_date(d):
    if isinstance(d, str):
        d = parse_date(d)
    if not d:
        return ""
    return f"{AR_WEEKDAYS[d.weekday()]} {d.day} {AR_MONTHS[d.month - 1]}"


def short_date(d):
    if isinstance(d, str):
        d = parse_date(d)
    return f"{d.day} {AR_MONTHS[d.month - 1]}" if d else ""


def day_label(d):
    if isinstance(d, str):
        d = parse_date(d)
    if not d:
        return ""
    t = today()
    if d == t:
        return "اليوم"
    if d == t + timedelta(days=1):
        return "غدًا"
    if d == t - timedelta(days=1):
        return "أمس"
    return fmt_date(d)


# ---------- phone / WhatsApp ----------
def phone_digits(phone):
    return re.sub(r"\D", "", str(phone or ""))


def normalize_phone(phone):
    """Canonical key for a North American number: 10 digits without country code."""
    d = phone_digits(phone)
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def pretty_phone(phone):
    d = normalize_phone(phone)
    if len(d) == 10:
        return f"{d[:3]} {d[3:6]} {d[6:]}"
    return str(phone or "").strip()


def whatsapp_link(phone, message):
    d = normalize_phone(phone)
    if not d:
        return ""
    if len(d) == 10:
        d = "1" + d
    return f"https://wa.me/{d}?text={quote(message)}"
