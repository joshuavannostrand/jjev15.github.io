"""Parse private iCalendar feeds without transmitting feed tokens to the browser.

Supports VEVENT, VEVENT recurrence rules, exceptions, EXDATE, RDATE, folded lines,
local/date-only/UTC timestamps. For proprietary VTIMEZONE aliases we default to
America/New_York; most Brightspace feeds use ordinary standard timestamps.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from dateutil.rrule import rrulestr

NY = ZoneInfo("America/New_York")


def unfold(text: str) -> list[str]:
    result = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def prop(line: str):
    left, sep, val = line.partition(":")
    if not sep:
        return None
    parts = left.split(";")
    opts = {}
    for segment in parts[1:]:
        k, s, v = segment.partition("=")
        if s:
            opts[k.upper()] = v.strip('"')
    return parts[0].upper(), opts, val


def unescape(s: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def dtparse(value: str, options: dict):
    value = value.strip()
    if options.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S") if len(value) >= 15 else datetime.strptime(value, "%Y%m%dT%H%M")
    tzname = options.get("TZID", "America/New_York")
    try:
        tz = ZoneInfo(tzname)
    except (KeyError, ValueError):
        tz = NY
    return parsed.replace(tzinfo=tz)


def get(items, key):
    return next(((opts, val) for name, opts, val in items if name == key), None)


def gets(items, key):
    return [(opts, val) for name, opts, val in items if name == key]


def in_range(dt, lo: datetime, hi: datetime):
    if isinstance(dt, date) and not isinstance(dt, datetime):
        dt = datetime.combine(dt, datetime.min.time(), NY)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return lo <= dt.astimezone(NY) <= hi


def serialise_dt(dt):
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.isoformat(), True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(NY).isoformat(), False


def is_due(title: str, description: str) -> bool:
    # Brightspace may include lectures and events, not only deadlines.
    title = title.lower()
    return bool(re.search(r"\b(due|assignment|quiz|exam|test|submission|deadline|homework|journal|project|paper|deliverable|lab|discussion|midterm|final|checkpoint|presentation)\b", title))


def parse_ics(text: str, source: str, days: int = 21, now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(NY)).astimezone(NY)
    lo = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    hi = lo + timedelta(days=days + 2)
    events = []
    current = None
    for line in unfold(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = []
        elif upper == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None:
            item = prop(line)
            if item:
                current.append(item)

    exceptions = {}
    masters = []
    for items in events:
        uidprop = get(items, "UID")
        uid = uidprop[1] if uidprop else ""
        if get(items, "RECURRENCE-ID"):
            exceptions.setdefault(uid, []).append(items)
        else:
            masters.append(items)

    output = []
    for items in masters:
        startprop = get(items, "DTSTART")
        if not startprop:
            continue
        try:
            start = dtparse(startprop[1], startprop[0])
        except (ValueError, OverflowError):
            continue
        endprop = get(items, "DTEND")
        end = dtparse(endprop[1], endprop[0]) if endprop else None
        if end is None:
            end = start + (timedelta(days=1) if isinstance(start, date) and not isinstance(start, datetime) else timedelta(hours=1))
        duration = end - start
        title = unescape((get(items, "SUMMARY") or ({}, "Untitled event"))[1])
        description = unescape((get(items, "DESCRIPTION") or ({}, ""))[1])
        uid = (get(items, "UID") or ({}, title))[1]
        where = unescape((get(items, "LOCATION") or ({}, ""))[1])
        url = (get(items, "URL") or ({}, ""))[1]
        rrule = get(items, "RRULE")
        dates = [start]
        if rrule:
            try:
                is_date = isinstance(start, date) and not isinstance(start, datetime)
                rule_start = datetime.combine(start, datetime.min.time(), NY) if is_date else start
                rule = rrulestr(rrule[1], dtstart=rule_start)
                expanded = list(rule.between(lo - duration, hi, inc=True))[:300]
                dates = [x.date() for x in expanded] if is_date else expanded
            except (ValueError, TypeError):
                dates = [start]
        for opts, val in gets(items, "RDATE"):
            for raw in val.split(","):
                try:
                    dates.append(dtparse(raw, opts))
                except ValueError:
                    pass
        excluded = set()
        for opts, val in gets(items, "EXDATE"):
            for raw in val.split(","):
                try:
                    excluded.add(str(dtparse(raw, opts)))
                except ValueError:
                    pass
        exception_items = exceptions.get(uid, [])
        for occurrence in dates[:300]:
            if str(occurrence) in excluded:
                continue
            override = None
            for entry in exception_items:
                rp = get(entry, "RECURRENCE-ID")
                try:
                    if rp and dtparse(rp[1], rp[0]) == occurrence:
                        override = entry
                        break
                except ValueError:
                    pass
            if override:
                if (get(override, "STATUS") or ({}, ""))[1].upper() == "CANCELLED":
                    continue
                special = get(override, "DTSTART")
                if special:
                    occurrence = dtparse(special[1], special[0])
                special_end = get(override, "DTEND")
                occurrence_end = dtparse(special_end[1], special_end[0]) if special_end else occurrence + duration
                display_title = unescape((get(override, "SUMMARY") or ({}, title))[1])
            else:
                occurrence_end = occurrence + duration
                display_title = title
            if not in_range(occurrence, lo, hi):
                continue
            if (get(items, "STATUS") or ({}, ""))[1].upper() == "CANCELLED":
                continue
            iso, all_day = serialise_dt(occurrence)
            end_iso, _ = serialise_dt(occurrence_end)
            output.append({"id": f"{source}-{uid}-{iso}", "title": display_title[:220], "start": iso, "end": end_iso,
                           "allDay": all_day, "location": where[:200], "url": url if url.startswith("https://") else "",
                           "source": source, "description": description[:700], "due": is_due(display_title, description) if source == "mycourses" else False})
    return sorted(output, key=lambda e: e["start"])[:400]
