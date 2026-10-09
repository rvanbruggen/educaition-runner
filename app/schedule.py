"""Translate between the dashboard's schedule picker and 5-field cron strings.

The picker covers the three shapes the tasks actually use:
  hours   "M */N * * *"      every N hours, at minute M
  daily   "M H * * *"        every day at H:M
  weekly  "M H * * d1,d2"    on the chosen weekdays at H:M
Anything else is a hand-written cron; it keeps working but the picker shows
the raw expression under "Geavanceerd".
"""
import re

DAYS = [("mon", "ma"), ("tue", "di"), ("wed", "wo"), ("thu", "do"),
        ("fri", "vr"), ("sat", "za"), ("sun", "zo")]
DAY_NAMES = {"mon": "maandag", "tue": "dinsdag", "wed": "woensdag", "thu": "donderdag",
             "fri": "vrijdag", "sat": "zaterdag", "sun": "zondag"}
_NUM_TO_DAY = {str(i): d for i, (d, _) in enumerate(DAYS)}   # APScheduler: 0 = monday
_NUM_TO_DAY["7"] = "sun"
EVERY_CHOICES = (1, 2, 3, 4, 6, 8, 12)


def parse(cron: str) -> dict | None:
    """Cron → picker fields, or None when the picker cannot represent it."""
    parts = cron.split()
    if len(parts) != 5 or not parts[0].isdigit():
        return None
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*":
        return None
    m = int(minute)
    if hour.startswith("*/") and hour[2:].isdigit() and dow == "*":
        n = int(hour[2:])
        if n in EVERY_CHOICES:
            return {"freq": "hours", "every": n, "hour": 0, "minute": m, "days": []}
        return None
    if not hour.isdigit():
        return None
    h = int(hour)
    if dow == "*":
        return {"freq": "daily", "every": 1, "hour": h, "minute": m, "days": []}
    days = []
    for tok in dow.lower().split(","):
        tok = _NUM_TO_DAY.get(tok, tok)
        if tok not in DAY_NAMES:
            return None
        days.append(tok)
    days = [d for d, _ in DAYS if d in days]   # canonical order, deduped
    return {"freq": "weekly", "every": 1, "hour": h, "minute": m, "days": days}


def build(freq: str, every: str, time: str, days: list[str], minute: str = "") -> str:
    """Picker fields → cron. Raises ValueError with a Dutch message."""
    if freq == "hours":
        if not every.isdigit() or int(every) not in EVERY_CHOICES:
            raise ValueError("Kies een interval in uren.")
        if not minute.isdigit() or not 0 <= int(minute) <= 59:
            raise ValueError("Kies een minuut.")
        return f"{int(minute)} */{int(every)} * * *"
    mt = re.fullmatch(r"(\d{1,2}):(\d{2})", time or "")
    if not mt or not (0 <= int(mt[1]) <= 23 and 0 <= int(mt[2]) <= 59):
        raise ValueError("Geef een geldig tijdstip (uu:mm).")
    h, m = int(mt[1]), int(mt[2])
    if freq == "daily":
        return f"{m} {h} * * *"
    if freq == "weekly":
        chosen = [d for d, _ in DAYS if d in days]
        if not chosen:
            raise ValueError("Kies minstens één weekdag.")
        return f"{m} {h} * * {','.join(chosen)}"
    raise ValueError("Onbekende frequentie.")


def describe(cron: str) -> str:
    """Human-readable Dutch label for the dashboard; falls back to the raw cron."""
    f = parse(cron)
    if not f:
        return f"cron: {cron}"
    t = f"{f['hour']:02d}:{f['minute']:02d}"
    if f["freq"] == "hours":
        return (f"elk uur (:{f['minute']:02d})" if f["every"] == 1
                else f"elke {f['every']} uur (:{f['minute']:02d})")
    if f["freq"] == "daily":
        return f"dagelijks om {t}"
    if len(f["days"]) == 7:
        return f"dagelijks om {t}"
    if len(f["days"]) == 1:
        return f"{DAY_NAMES[f['days'][0]]} om {t}"
    return ", ".join(dict(DAYS)[d] for d in f["days"]) + f" om {t}"
