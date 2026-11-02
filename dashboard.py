"""Turn data/garmin.json into two self-contained HTML pages.

Run:  python fetch.py  &&  python dashboard.py
Output: out/rewind.html -- the story of your year
        out/charts.html -- every chart, in full
Both are gitignored: they embed a year of personal data.

All computation happens here; the templates only draw what this hands them.
That split means chart work can iterate against the cache without re-hitting
the API, which matters because tokens last 24h and the API is pull-only.

The two pages get different slices of the payload. Rewind doesn't need the
daily series and the charts page doesn't need the story, so neither file
carries the other's data.
"""

import json
import math
import webbrowser
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "garmin.json"
OUT_DIR = ROOT / "out"

# (template, output file, which payload keys that page actually draws).
# None means "everything except the story".
PAGES = [
    (ROOT / "template_rewind.html", OUT_DIR / "rewind.html", {"meta", "wrapped"}),
    (ROOT / "template_charts.html", OUT_DIR / "charts.html", None),
]

# Activity types that are distance sports; everything else is counted by time.
DISTANCE_SPORTS = {
    "running", "treadmill_running", "trail_running", "cycling", "walking",
    "hiking", "kayaking_v2", "swimming", "lap_swimming",
}
RUN_TYPES = {"running", "treadmill_running", "trail_running"}

# Garmin stores the two self-rated scores on a 0-100 wire scale. RPE maps back
# to the 1-10 the watch asks for; feel is the five-step scale it offers.
FEEL_LABELS = {0: "Very weak", 25: "Weak", 50: "Normal", 75: "Strong",
               100: "Very strong"}

TYPE_LABELS = {
    "running": "Running",
    "treadmill_running": "Treadmill",
    "trail_running": "Trail running",
    "strength_training": "Strength",
    "yoga": "Yoga",
    "hiking": "Hiking",
    "cycling": "Cycling",
    "kayaking_v2": "Kayaking",
    "walking": "Walking",
    "rowing_v2": "Rowing",
    "kayaking_v2": "Kayaking",
    "hiit": "HIIT",
    "pilates": "Pilates",
    "breathwork": "Breathwork",
    "cross_country_skiing_ws": "Cross-country skiing",
    "other": "Other",
}


def label_type(type_key):
    """Garmin's type keys carry internal suffixes (_v2, _ws) -- strip them."""
    if type_key in TYPE_LABELS:
        return TYPE_LABELS[type_key]
    cleaned = type_key
    for suffix in ("_v2", "_ws"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned.replace("_", " ").capitalize()


def parse_day(text):
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def rolling_mean(points, window):
    """7-day style smoothing over [(date, value)]; returns [(date, mean)]."""
    out = []
    for i in range(len(points)):
        chunk = [v for _, v in points[max(0, i - window + 1):i + 1] if v is not None]
        if chunk:
            out.append((points[i][0], sum(chunk) / len(chunk)))
    return out


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def pace_label(seconds_per_km):
    if not seconds_per_km:
        return "-"
    minutes, seconds = divmod(int(round(seconds_per_km)), 60)
    return "%d:%02d /km" % (minutes, seconds)


def plural(count, noun):
    return "%d %s%s" % (count, noun, "" if count == 1 else "s")


def duration_label(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return "%dh %02dm" % (hours, minutes) if hours else "%dm" % minutes


def split_label(seconds):
    """A split time read off a stopwatch -- 25:47, or 2:09:09 once it runs
    past the hour. Distinct from `clock_label`, which formats a time of day."""
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return ("%d:%02d:%02d" % (hours, minutes, secs) if hours
            else "%d:%02d" % (minutes, secs))


# Garmin keeps one standing personal best per race distance, and the activity
# list carries each session's best split at exactly those distances -- which is
# what lets a bare `isPR` flag be resolved back into a named record.
PR_DISTANCES = [
    ("fastestSplit_1000", 1000, "1 km"),
    ("fastestSplit_1609", 1609, "1 mile"),
    ("fastestSplit_5000", 5000, "5 km"),
    ("fastestSplit_10000", 10000, "10 km"),
    ("fastestSplit_21098", 21098, "Half marathon"),
    ("fastestSplit_42195", 42195, "Marathon"),
]


def credible_split(seconds, metres):
    """Garmin occasionally reports a nonsense split -- one run in this cache
    claims a 1-second kilometre. Anything quicker than 2:30/km is faster than
    the 1500 m world record, so it's a device glitch, not a personal best."""
    return bool(seconds) and seconds / (metres / 1000.0) >= 150


# --------------------------------------------------------------------------
# series builders
# --------------------------------------------------------------------------

def build_weekly_volume(activities, start, end):
    """Kilometres and moving hours per ISO week, including empty weeks."""
    by_week = defaultdict(lambda: {"km": 0.0, "hours": 0.0, "count": 0})
    for act in activities:
        day = parse_day(act["startTimeLocal"])
        key = day.isocalendar()[:2]
        bucket = by_week[key]
        bucket["km"] += (act.get("distance") or 0) / 1000.0
        bucket["hours"] += (act.get("movingDuration") or act.get("duration") or 0) / 3600.0
        bucket["count"] += 1

    # Walk real weeks so a zero-training week renders as a gap, not a skip.
    out = []
    cursor = start - timedelta(days=start.weekday())
    while cursor <= end:
        key = cursor.isocalendar()[:2]
        bucket = by_week.get(key, {"km": 0.0, "hours": 0.0, "count": 0})
        out.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%d %b"),
            "km": round(bucket["km"], 1),
            "hours": round(bucket["hours"], 2),
            "count": bucket["count"],
        })
        cursor += timedelta(days=7)
    return out


def build_type_mix(activities):
    rows = defaultdict(lambda: {"count": 0, "km": 0.0, "hours": 0.0})
    for act in activities:
        key = act["activityType"]["typeKey"]
        row = rows[key]
        row["count"] += 1
        row["km"] += (act.get("distance") or 0) / 1000.0
        row["hours"] += (act.get("movingDuration") or act.get("duration") or 0) / 3600.0
    out = [
        {
            "type": label_type(key),
            "count": row["count"],
            "km": round(row["km"], 1),
            "hours": round(row["hours"], 1),
            "distance_sport": key in DISTANCE_SPORTS,
        }
        for key, row in rows.items()
    ]
    return sorted(out, key=lambda r: -r["hours"])


def build_hr_zones(activities):
    totals = [0.0] * 5
    for act in activities:
        for zone in range(5):
            totals[zone] += act.get("hrTimeInZone_%d" % (zone + 1)) or 0
    grand = sum(totals) or 1
    return [
        {
            "zone": "Zone %d" % (i + 1),
            "hours": round(totals[i] / 3600.0, 1),
            "share": round(100 * totals[i] / grand, 1),
        }
        for i in range(5)
    ]


def build_calendar(activities, start, end):
    """Per-day training minutes for the heatmap."""
    minutes = defaultdict(float)
    kinds = defaultdict(list)
    for act in activities:
        day = parse_day(act["startTimeLocal"]).isoformat()
        minutes[day] += (act.get("movingDuration") or act.get("duration") or 0) / 60.0
        kinds[day].append(label_type(act["activityType"]["typeKey"]))

    out = []
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        out.append({
            "date": key,
            "minutes": round(minutes.get(key, 0.0)),
            "kinds": sorted(set(kinds.get(key, []))),
        })
        cursor += timedelta(days=1)
    return out


def build_pace(activities):
    out = []
    for act in activities:
        if act["activityType"]["typeKey"] not in RUN_TYPES:
            continue
        speed = act.get("averageSpeed")
        distance = (act.get("distance") or 0) / 1000.0
        # Sub-2km efforts are warm-ups and strides; they distort the trend.
        if not speed or distance < 2:
            continue
        out.append({
            "date": parse_day(act["startTimeLocal"]).isoformat(),
            "pace": round(1000.0 / speed, 1),
            "km": round(distance, 2),
            "hr": act.get("averageHR"),
            "name": act.get("activityName") or "Run",
        })
    return sorted(out, key=lambda r: r["date"])


def build_effort(activities):
    """Self-rated effort and feel, placed at the local clock time of the session.

    Both scores are optional -- the watch only carries them if you answered the
    prompt -- and an older cache predating fetch.py's detail pass has neither,
    so a missing field just drops the session from the chart.
    """
    out = []
    for act in activities:
        rpe, feel = act.get("workoutRpe"), act.get("workoutFeel")
        if rpe is None and feel is None:
            continue
        stamp = act.get("startTimeLocal") or ""
        if len(stamp) < 16:
            continue
        out.append({
            "date": stamp[:10],
            "clock": stamp[11:16],
            "hour": round(int(stamp[11:13]) + int(stamp[14:16]) / 60.0, 3),
            "rpe": round(rpe / 10.0, 1) if rpe is not None else None,
            "feel": feel,
            "feel_label": FEEL_LABELS.get(feel),
            "type": label_type(act["activityType"]["typeKey"]),
            "name": act.get("activityName") or label_type(act["activityType"]["typeKey"]),
            "minutes": round((act.get("movingDuration") or act.get("duration") or 0) / 60.0),
        })
    return sorted(out, key=lambda r: (r["date"], r["clock"]))


def build_records(activities, summaries, sleep, hrv, calendar, weekly):
    records = []

    runs = [a for a in activities if a["activityType"]["typeKey"] in RUN_TYPES
            and (a.get("distance") or 0) > 0]
    if runs:
        longest = max(runs, key=lambda a: a["distance"])
        records.append({
            "label": "Longest run",
            "value": "%.1f km" % (longest["distance"] / 1000.0),
            "detail": "%s · %s" % (longest["startTimeLocal"][:10],
                                   duration_label(longest.get("duration") or 0)),
        })

        qualifying = [a for a in runs if a["distance"] >= 3000 and a.get("averageSpeed")]
        if qualifying:
            fastest = max(qualifying, key=lambda a: a["averageSpeed"])
            records.append({
                "label": "Fastest pace (3 km+)",
                "value": pace_label(1000.0 / fastest["averageSpeed"]),
                "detail": "%s · %.1f km" % (fastest["startTimeLocal"][:10],
                                            fastest["distance"] / 1000.0),
            })

    biggest = max(weekly, key=lambda w: w["km"], default=None)
    if biggest and biggest["km"]:
        records.append({
            "label": "Biggest week",
            "value": "%.1f km" % biggest["km"],
            "detail": "week of %s · %s" % (
                biggest["label"], plural(biggest["count"], "session")),
        })

    step_days = [(s["calendarDate"], ((s.get("movement") or {}).get("steps") or {}).get("value"))
                 for s in summaries]
    step_days = [(d, v) for d, v in step_days if v]
    if step_days:
        day, value = max(step_days, key=lambda kv: kv[1])
        records.append({
            "label": "Most steps in a day",
            "value": "{:,}".format(value),
            "detail": day,
        })

    scored = [(s["calendarDate"], ((s.get("sleepScores") or {}).get("overall") or {}).get("value"))
              for s in sleep]
    scored = [(d, v) for d, v in scored if v]
    if scored:
        day, value = max(scored, key=lambda kv: kv[1])
        records.append({"label": "Best sleep score", "value": str(value), "detail": day})

    hrv_days = [(h["calendarDate"], h.get("lastNightAvg")) for h in hrv]
    hrv_days = [(d, v) for d, v in hrv_days if v]
    if hrv_days:
        day, value = max(hrv_days, key=lambda kv: kv[1])
        records.append({"label": "Peak HRV", "value": "%d ms" % value, "detail": day})

    # Longest run of consecutive days with any recorded training.
    best_streak = streak = 0
    best_end = None
    for entry in calendar:
        if entry["minutes"] > 0:
            streak += 1
            if streak > best_streak:
                best_streak, best_end = streak, entry["date"]
        else:
            streak = 0
    if best_streak:
        records.append({
            "label": "Longest training streak",
            "value": "%d days" % best_streak,
            "detail": "ending %s" % best_end,
        })

    return records


def build_tiles(summaries, sleep, hrv, activities, end):
    """Stat tiles with a 30-day vs prior-30-day delta and a 12-point sparkline."""

    def window(records, date_key, value_fn, days_back, span=30):
        hi = end - timedelta(days=days_back)
        lo = hi - timedelta(days=span - 1)
        values = []
        for rec in records:
            day = parse_day(rec[date_key])
            if lo <= day <= hi:
                value = value_fn(rec)
                if value is not None:
                    values.append(value)
        return values

    def spark(records, date_key, value_fn, buckets=12, span=360):
        lo = end - timedelta(days=span - 1)
        step = span / float(buckets)
        bins = [[] for _ in range(buckets)]
        for rec in records:
            day = parse_day(rec[date_key])
            if day < lo:
                continue
            index = min(buckets - 1, int((day - lo).days / step))
            value = value_fn(rec)
            if value is not None:
                bins[index].append(value)
        return [round(mean(b), 1) if b else None for b in bins]

    def tile(label, records, date_key, value_fn, unit="", up_is_good=True, digits=0):
        now = mean(window(records, date_key, value_fn, 0))
        before = mean(window(records, date_key, value_fn, 30))
        if now is None:
            return None
        delta = None
        if before:
            delta = round(now - before, 1)
        return {
            "label": label,
            "value": ("%.*f" % (digits, now)).rstrip("0").rstrip(".") if digits else
                     "{:,}".format(int(round(now))),
            "unit": unit,
            "delta": delta,
            "delta_digits": digits,
            "up_is_good": up_is_good,
            "spark": spark(records, date_key, value_fn),
        }

    tiles = []

    tiles.append(tile(
        "Avg resting heart rate", summaries, "calendarDate",
        lambda s: (s.get("heartRate") or {}).get("restingValue"),
        unit="bpm", up_is_good=False,
    ))
    tiles.append(tile(
        "Avg sleep score", sleep, "calendarDate",
        lambda s: ((s.get("sleepScores") or {}).get("overall") or {}).get("value"),
        up_is_good=True,
    ))
    tiles.append(tile(
        "Avg overnight HRV", hrv, "calendarDate",
        lambda h: h.get("lastNightAvg"), unit="ms", up_is_good=True,
    ))
    tiles.append(tile(
        "Avg daily steps", summaries, "calendarDate",
        lambda s: ((s.get("movement") or {}).get("steps") or {}).get("value"),
        up_is_good=True,
    ))
    tiles.append(tile(
        "Avg stress level", summaries, "calendarDate",
        lambda s: (s.get("stress") or {}).get("avgLevel"), up_is_good=False,
    ))
    tiles.append(tile(
        "Avg daily Body Battery high", summaries, "calendarDate",
        lambda s: (s.get("bodyBattery") or {}).get("maxValue"), up_is_good=True,
    ))

    return [t for t in tiles if t]


# --------------------------------------------------------------------------
# "Wrapped" — the story panels
#
# Everything below is derived from the same cache the charts use. The rule is
# that a panel only ships if the data behind it is actually there: a stat with
# three samples behind it reads as a fact and isn't one, so the builders return
# None and the template skips the slide rather than printing a confident zero.
# --------------------------------------------------------------------------

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]

# Each comparison needs a unit big enough that the answer is a small number --
# "412 marathons" is noise, "9.8 marathons" is a fact you can picture.
MARATHON_KM = 42.195
EVEREST_M = 8849.0          # sea level to summit
PIZZA_KCAL = 860.0          # a whole 12" margherita
BOTTLE_L = 1.5


def moving_seconds(act):
    return act.get("movingDuration") or act.get("duration") or 0


def activity_name(act):
    """A session's display name, safe to print.

    Garmin truncates long workout names to a fixed width, which lands mid-word
    and often leaves a bracket hanging open -- "Warsaw Marathon (Marathon (f".
    An unclosed bracket is the one truncation that can be detected, so cut back
    to where it opened rather than printing the stump.
    """
    name = (act.get("activityName")
            or label_type(act["activityType"]["typeKey"])).strip()
    depth, opened_at = 0, None
    for i, char in enumerate(name):
        if char == "(":
            if depth == 0:
                opened_at = i
            depth += 1
        elif char == ")" and depth:
            depth -= 1
    if depth:
        name = name[:opened_at]
    return name.strip(" -(,")


def circular_mean_hour(hours):
    """Mean of clock times, which don't average linearly.

    23:40 and 00:20 average to midnight, not to noon. Treating each time as an
    angle on the 24-hour circle and averaging the unit vectors gets that right.
    """
    hours = [h for h in hours if h is not None]
    if not hours:
        return None
    x = sum(math.cos(h / 24.0 * 2 * math.pi) for h in hours)
    y = sum(math.sin(h / 24.0 * 2 * math.pi) for h in hours)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return (math.atan2(y, x) / (2 * math.pi) * 24.0) % 24.0


def clock_label(hour):
    if hour is None:
        return None
    total = int(round(hour * 60)) % (24 * 60)
    return "%02d:%02d" % divmod(total, 60)


def local_clock(ms):
    """Garmin's *Local timestamps are epoch millis pre-shifted to wall time."""
    if not ms:
        return None
    stamp = datetime.utcfromtimestamp(ms / 1000.0)
    return stamp.hour + stamp.minute / 60.0


def build_podium(activities):
    """Sports ranked by moving time, the way Wrapped ranks artists."""
    rows = build_type_mix(activities)
    total = sum(r["hours"] for r in rows) or 1
    out = []
    for rank, row in enumerate(rows[:5], 1):
        out.append({
            "rank": rank,
            "type": row["type"],
            "hours": row["hours"],
            "count": row["count"],
            "km": row["km"],
            "share": round(100 * row["hours"] / total),
            "distance_sport": row["distance_sport"],
        })
    return out


def build_clock(activities):
    """When in the day training actually happens."""
    if not activities:
        return None

    hours = [0] * 24
    exact = []
    for act in activities:
        stamp = act.get("startTimeLocal") or ""
        if len(stamp) < 16:
            continue
        hour = int(stamp[11:13])
        hours[hour] += 1
        exact.append((hour + int(stamp[14:16]) / 60.0, act))
    if not exact:
        return None

    peak = max(range(24), key=lambda h: hours[h])
    # Name the block the peak sits in rather than the single hour -- an hour
    # bucket can win by one session, a six-hour block doesn't.
    blocks = [
        (range(4, 9),   "Sunrise chaser", "before the world wakes up"),
        (range(9, 12),  "Morning mover", "while the coffee is still hot"),
        (range(12, 16), "Midday breakaway", "straight through the middle of the day"),
        (range(16, 20), "After-work animal", "the second the laptop shuts"),
        (range(20, 24), "Night owl",    "while everyone else winds down"),
    ]
    archetype, phrase = "Night owl", "at hours most people are asleep"
    for span, name, blurb in blocks:
        if peak in span:
            archetype, phrase = name, blurb
            break

    earliest = min(exact, key=lambda kv: kv[0])
    latest = max(exact, key=lambda kv: kv[0])

    def bookend(pair):
        hour, act = pair
        return {
            "clock": clock_label(hour),
            "date": act["startTimeLocal"][:10],
            "name": act.get("activityName") or label_type(act["activityType"]["typeKey"]),
            "type": label_type(act["activityType"]["typeKey"]),
        }

    return {
        "hours": [{"hour": h, "label": "%02d" % h, "count": hours[h]} for h in range(24)],
        "peak": peak,
        "peak_label": "%02d:00" % peak,
        "peak_count": hours[peak],
        "archetype": archetype,
        "phrase": phrase,
        "mean": clock_label(circular_mean_hour([h for h, _ in exact])),
        "earliest": bookend(earliest),
        "latest": bookend(latest),
    }


def build_weekdays(activities):
    rows = [{"name": name, "short": name[:3], "count": 0, "hours": 0.0}
            for name in WEEKDAYS]
    for act in activities:
        row = rows[parse_day(act["startTimeLocal"]).weekday()]
        row["count"] += 1
        row["hours"] += moving_seconds(act) / 3600.0
    for row in rows:
        row["hours"] = round(row["hours"], 1)
    best = max(rows, key=lambda r: r["count"])
    quietest = min(rows, key=lambda r: r["count"])
    return {"rows": rows, "best": best, "quietest": quietest}


def build_months(activities, start, end):
    """Calendar months in the window, so "your biggest month" has a shape."""
    buckets = defaultdict(lambda: {"km": 0.0, "hours": 0.0, "count": 0})
    for act in activities:
        day = parse_day(act["startTimeLocal"])
        bucket = buckets[(day.year, day.month)]
        bucket["km"] += (act.get("distance") or 0) / 1000.0
        bucket["hours"] += moving_seconds(act) / 3600.0
        bucket["count"] += 1

    out = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        bucket = buckets.get((cursor.year, cursor.month),
                             {"km": 0.0, "hours": 0.0, "count": 0})
        nxt = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        out.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%b"),
            "long": cursor.strftime("%B %Y"),
            "km": round(bucket["km"], 1),
            "hours": round(bucket["hours"], 1),
            "count": bucket["count"],
            # The window is a rolling 12 months, so the first and last months
            # are clipped. They belong on the strip -- the training happened --
            # but "your biggest month" can't fairly compare 12 days with 31.
            "whole": cursor >= start and nxt - timedelta(days=1) <= end,
        })
        cursor = nxt
    return out


def build_comparisons(activities, summaries, sleep):
    """Totals restated in units a person can picture."""
    out = []

    total_km = sum((a.get("distance") or 0) for a in activities) / 1000.0
    if total_km:
        out.append({
            "value": "%.1f" % (total_km / MARATHON_KM),
            "unit": "marathons",
            "label": "Distance, in marathons",
            "detail": "%s km, all under your own power" % fmt_number(total_km, 1),
        })

    elevation = sum((a.get("elevationGain") or 0) for a in activities)
    if elevation:
        out.append({
            "value": "%s" % fmt_number(elevation, 0),
            "unit": "m climbed",
            "label": "Elevation, stacked up",
            "detail": "that is %.0f%% of Everest, from sea level"
                      % (100 * elevation / EVEREST_M),
        })

    steps = sum(((s.get("movement") or {}).get("steps") or {}).get("value") or 0
                for s in summaries)
    if steps:
        out.append({
            "value": ("%.1fM" % (steps / 1e6) if steps >= 1e6
                      else fmt_number(steps / 1000.0, 0) + "k"),
            "unit": "steps",
            "label": "Steps, all year",
            "detail": "%s a day, every day" % fmt_number(steps / max(1, len(summaries)), 0),
        })

    calories = sum((a.get("calories") or 0) for a in activities)
    if calories:
        out.append({
            "value": fmt_number(calories / PIZZA_KCAL, 0),
            "unit": "pizzas",
            "label": "Calories, in pizzas",
            "detail": "%s kcal torched across every session" % fmt_number(calories, 0),
        })

    water = sum((a.get("waterEstimated") or 0) for a in activities) / 1000.0
    if water:
        out.append({
            "value": fmt_number(water, 0),
            "unit": "litres",
            "label": "Sweat, by the bottle",
            "detail": "roughly %s bottles of water to put back"
                      % fmt_number(water / BOTTLE_L, 0),
        })

    slept = sum((s.get("sleepTimeSeconds") or 0) for s in sleep) / 3600.0
    if slept:
        out.append({
            "value": fmt_number(slept / 24.0, 0),
            "unit": "days asleep",
            "label": "Sleep, in whole days",
            "detail": "%s hours, averaging %s a night" % (
                fmt_number(slept, 0),
                duration_label(slept * 3600 / max(1, len(sleep)))),
        })

    floors = sum(((s.get("movement") or {}).get("floorsAscended") or {}).get("value") or 0
                 for s in summaries)
    if floors:
        out.append({
            "value": fmt_number(floors, 0),
            "unit": "floors",
            "label": "Floors, on foot alone",
            "detail": "%s a day, stairs and hills included"
                      % fmt_number(floors / max(1, len(summaries)), 0),
        })

    load = sum((a.get("activityTrainingLoad") or 0) for a in activities)
    if load:
        out.append({
            "value": fmt_number(load, 0),
            "unit": "training load",
            "label": "Load, the whole pile",
            "detail": "Garmin's own score for everything you asked of your body",
        })

    return out


def fmt_number(value, digits=0):
    return "{:,.{}f}".format(value, digits)


def build_sleep_story(sleep):
    """Average bedtime and wake time -- the quintessential Wrapped stat."""
    nights = [s for s in sleep if s.get("sleepStartTimestampLocal")
              and s.get("sleepEndTimestampLocal")]
    if len(nights) < 14:
        return None

    bed = circular_mean_hour([local_clock(s["sleepStartTimestampLocal"]) for s in nights])
    wake = circular_mean_hour([local_clock(s["sleepEndTimestampLocal"]) for s in nights])

    scored = [(s["calendarDate"], ((s.get("sleepScores") or {}).get("overall") or {}).get("value"),
               s.get("sleepTimeSeconds") or 0)
              for s in sleep]
    scored = [row for row in scored if row[1]]

    story = {
        "nights": len(nights),
        "bedtime": clock_label(bed),
        "waketime": clock_label(wake),
        "avg_hours": round(sum((s.get("sleepTimeSeconds") or 0) for s in nights)
                           / len(nights) / 3600.0, 1),
        "avg_score": round(mean([row[1] for row in scored]) or 0),
        "best": None,
        "longest": None,
    }

    if scored:
        day, score, seconds = max(scored, key=lambda row: row[1])
        story["best"] = {"date": day, "score": score, "hours": round(seconds / 3600.0, 1)}
    longest = max(nights, key=lambda s: s.get("sleepTimeSeconds") or 0)
    story["longest"] = {
        "date": longest["calendarDate"],
        "hours": round((longest.get("sleepTimeSeconds") or 0) / 3600.0, 1),
    }

    # Garmin's own verdict on each night, counted. Free, honest copy.
    verdicts = Counter(s.get("sleepScoreFeedback") for s in sleep
                       if s.get("sleepScoreFeedback")
                       and s["sleepScoreFeedback"] != "NONE")
    if verdicts:
        top, count = verdicts.most_common(1)[0]
        story["verdict"] = {
            "text": top.split("_", 1)[-1].replace("_", " ").lower(),
            "count": count,
            "positive": top.startswith("POSITIVE"),
        }
    return story


def build_streaks(calendar):
    active = [c for c in calendar if c["minutes"] > 0]

    def longest_run(predicate):
        best = run = 0
        best_end = end = None
        for entry in calendar:
            if predicate(entry):
                run += 1
                end = entry["date"]
                if run > best:
                    best, best_end = run, end
            else:
                run = 0
        return best, best_end

    train_days, train_end = longest_run(lambda c: c["minutes"] > 0)
    rest_days, rest_end = longest_run(lambda c: c["minutes"] == 0)

    busiest = max(calendar, key=lambda c: c["minutes"])
    return {
        "active_days": len(active),
        "rest_days": len(calendar) - len(active),
        "total_days": len(calendar),
        "share": round(100 * len(active) / max(1, len(calendar))),
        "longest_streak": train_days,
        "longest_streak_end": train_end,
        "longest_rest": rest_days,
        "longest_rest_end": rest_end,
        "busiest": {"date": busiest["date"], "minutes": busiest["minutes"],
                    "kinds": busiest["kinds"]} if busiest["minutes"] else None,
    }


def build_superlatives(activities):
    """One-line extremes. Each is the single session that owns the record.

    `date` and `name` identify the owning session on purpose: the records panel
    matches these against the watch's own PR flags, which say that a record fell
    but not which one. Matching on the printed `detail` string would work today
    and break the moment its format changes.
    """
    out = []

    def entry(label, act, value, note=None):
        return {
            "label": label,
            "value": value,
            "detail": when(act),
            "note": note,
            "date": act["startTimeLocal"][:10],
            "name": activity_name(act),
        }

    def add(label, pick, value_fn, note=None):
        candidates = [a for a in activities if pick(a)]
        if not candidates:
            return
        best = max(candidates, key=pick)
        out.append(entry(label, best, value_fn(best), note))

    def when(act):
        return "%s · %s" % (act["startTimeLocal"][:10], activity_name(act))

    runs = [a for a in activities if a["activityType"]["typeKey"] in RUN_TYPES]

    splits = [a for a in runs if credible_split(a.get("fastestSplit_1000"), 1000)]
    if splits:
        best = min(splits, key=lambda a: a["fastestSplit_1000"])
        out.append(entry("Fastest kilometre", best,
                         pace_label(best["fastestSplit_1000"]),
                         "your quickest km of the year"))

    miles = [a for a in runs if credible_split(a.get("fastestSplit_1609"), 1609)]
    if miles:
        best = min(miles, key=lambda a: a["fastestSplit_1609"])
        seconds = best["fastestSplit_1609"]
        out.append(entry("Fastest mile", best,
                         "%d:%02d" % divmod(int(round(seconds)), 60),
                         "your quickest mile of the year"))

    add("Longest time out", lambda a: moving_seconds(a),
        lambda a: duration_label(moving_seconds(a)))
    add("Furthest in one go", lambda a: a.get("distance") or 0,
        lambda a: "%.1f km" % ((a.get("distance") or 0) / 1000.0))
    add("Heart rate ceiling", lambda a: a.get("maxHR") or 0,
        lambda a: "%d bpm" % a["maxHR"])
    add("Biggest climb", lambda a: a.get("elevationGain") or 0,
        lambda a: "%d m" % a["elevationGain"])
    add("Biggest single burn", lambda a: a.get("calories") or 0,
        lambda a: "%s kcal" % fmt_number(a["calories"], 0))
    add("Hardest hit of all", lambda a: a.get("activityTrainingLoad") or 0,
        lambda a: fmt_number(a["activityTrainingLoad"], 0),
        note="Garmin's load score for a single session")

    return out


def build_hardest(activities, summaries, sleep):
    """The hardest single session, told with the day that surrounded it.

    Every other panel is an aggregate; this is the one about a moment. Garmin's
    own load score picks the session, and the daily summary plus the night
    before supply the before-and-after that make it a day rather than a row.

    Load is intensity-weighted, so the winner is often a short savage one rather
    than the longest day out -- `rival` carries the longest session so the panel
    can say that out loud instead of quietly implying the two are the same.
    """
    scored = [a for a in activities if a.get("activityTrainingLoad")]
    # One load score isn't a "biggest day" -- it's the only day. Ten sessions is
    # where the superlative starts meaning something.
    if len(scored) < 10:
        return None

    best = max(scored, key=lambda a: a["activityTrainingLoad"])
    day = best["startTimeLocal"][:10]
    # The session is the story; the day around it is garnish, and every field
    # drawn from it is optional below -- so a missing summary thins the panel
    # rather than dropping it.
    summary = next((s for s in summaries if s.get("calendarDate") == day), None) or {}

    battery = summary.get("bodyBattery") or {}
    steps = (summary.get("movement") or {}).get("steps") or {}
    # Garmin dates a night by the morning you woke up on, so the sleep record
    # carrying this calendarDate is the night that led into the session.
    night = next((s for s in sleep if s.get("calendarDate") == day), None)

    km = (best.get("distance") or 0) / 1000.0
    story = {
        "name": activity_name(best),
        "date": day,
        "type": label_type(best["activityType"]["typeKey"]),
        "clock": best["startTimeLocal"][11:16],
        "km": round(km, 1) if km else None,
        "duration": duration_label(moving_seconds(best)),
        "load": round(best["activityTrainingLoad"]),
        "calories": best.get("calories"),
        "avg_hr": int(best["averageHR"]) if best.get("averageHR") else None,
        "max_hr": int(best["maxHR"]) if best.get("maxHR") else None,
        "pace": (pace_label(1000.0 / best["averageSpeed"])
                 if best.get("averageSpeed") and km >= 1 else None),
        "elevation": round(best["elevationGain"]) if best.get("elevationGain") else None,
        "is_pr": bool(best.get("isPR")),
        "rpe": round(best["workoutRpe"] / 10.0, 1) if best.get("workoutRpe") is not None else None,
        "feel": FEEL_LABELS.get(best.get("workoutFeel")),
        "steps": steps.get("value"),
        "battery": ({"high": battery["maxValue"], "low": battery["minValue"]}
                    if battery.get("maxValue") is not None
                    and battery.get("minValue") is not None else None),
        "night": None,
    }
    if night:
        story["night"] = {
            "score": ((night.get("sleepScores") or {}).get("overall") or {}).get("value"),
            "hours": round((night.get("sleepTimeSeconds") or 0) / 3600.0, 1),
        }

    longest = max(activities, key=moving_seconds)
    if longest is not best and moving_seconds(longest) > moving_seconds(best) * 2:
        story["rival"] = {
            "name": activity_name(longest),
            "date": longest["startTimeLocal"][:10],
            "duration": duration_label(moving_seconds(longest)),
            "km": round((longest.get("distance") or 0) / 1000.0, 1) or None,
            "load": round(longest["activityTrainingLoad"]) if longest.get("activityTrainingLoad") else None,
        }
    return story


def build_prs(activities):
    """Sessions the watch itself flagged as carrying a personal record, with
    the record named wherever the splits can name it.

    `isPR` is a bare boolean -- Garmin keeps *which* record fell on a separate
    endpoint this cache doesn't pull, and the flag fires on a split inside the
    session as readily as on the session total. But the standing PB list is one
    entry per race distance, and every session carries its own best split at
    those same distances: if a flagged session owns the quickest 5 km anywhere
    in the window, the 5 km is what fell. One of these is a 7 km run whose
    record was a split inside it, not the 7 km -- `records` says so, `session`
    stays a description of the day and never pretends to be the record.
    """
    # The watch counts treadmill runs towards the same list, so the field being
    # present is the test here, not the activity type.
    best_at = {}
    for key, metres, _ in PR_DISTANCES:
        times = [a[key] for a in activities if credible_split(a.get(key), metres)]
        if times:
            best_at[key] = min(times)
    runs = [a for a in activities if a["activityType"]["typeKey"] in RUN_TYPES]
    furthest = max((a.get("distance") or 0) for a in runs) if runs else 0

    out = []
    for act in sorted((a for a in activities if a.get("isPR")),
                      key=lambda a: a["startTimeLocal"], reverse=True):
        km = (act.get("distance") or 0) / 1000.0
        session = []
        if km >= 0.1:
            session.append("%.1f km" % km)
        session.append(duration_label(moving_seconds(act)))
        if act.get("averageSpeed") and km >= 1:
            session.append(pace_label(1000.0 / act["averageSpeed"]))

        # Several can fall at once and legitimately do: a quick 5 km inside a
        # longer run usually drags the 1 km and the mile down with it.
        records = []
        for key, metres, label in PR_DISTANCES:
            if credible_split(act.get(key), metres) and best_at.get(key) == act[key]:
                records.append({
                    "label": label,
                    "value": split_label(act[key]),
                    # over 1 km the pace and the time are the same number
                    "pace": (pace_label(act[key] / (metres / 1000.0))
                             if metres != 1000 else None),
                })
        if act in runs and furthest and (act.get("distance") or 0) >= furthest:
            records.append({"label": "Longest run", "value": "%.1f km" % km,
                            "pace": None})

        out.append({
            "name": activity_name(act),
            "date": act["startTimeLocal"][:10],
            "type": label_type(act["activityType"]["typeKey"]),
            "session": " · ".join(session),
            "records": records,
        })
    return out


# The watch offers five steps for "how did that feel"; RPE comes back on the
# same 0-100 wire scale and maps to the 1-10 it actually asks for.
FEEL_ORDER = [0, 25, 50, 75, 100]
FEEL_SHORT = {0: "V.weak", 25: "Weak", 50: "Normal", 75: "Strong", 100: "V.strong"}


def build_feel(activities):
    """Effort and feel -- the only data here you supplied rather than wore."""
    felt = [a for a in activities if a.get("workoutFeel") in FEEL_LABELS]
    rpes = [a for a in activities if a.get("workoutRpe") is not None]
    rated = [a for a in activities
             if a.get("workoutFeel") is not None or a.get("workoutRpe") is not None]
    # Below this the mode is an accident of which prompts you happened to answer.
    if len(rated) < 20 or not felt:
        return None

    counts = Counter(a["workoutFeel"] for a in felt)
    rows = [{"value": v, "label": FEEL_LABELS[v], "short": FEEL_SHORT[v],
             "count": counts.get(v, 0)} for v in FEEL_ORDER]
    # Five buckets over fewer than a hundred sessions tie often, and "you said
    # normal most" is a false claim when strong matched it exactly -- hand the
    # template the tie so it can say so instead.
    peak = max(r["count"] for r in rows)
    tied = [r for r in rows if r["count"] == peak]
    top = tied[0]

    hardest = []
    if rpes:
        peak = max(a["workoutRpe"] for a in rpes)
        for act in sorted((a for a in rpes if a["workoutRpe"] == peak),
                          key=lambda a: a["startTimeLocal"], reverse=True)[:2]:
            hardest.append({
                "name": activity_name(act),
                "date": act["startTimeLocal"][:10],
                "rpe": round(peak / 10.0, 1),
            })

    strongest = counts.get(100, 0) + counts.get(75, 0)
    return {
        "rated": len(rated),
        "total": len(activities),
        "felt": len(felt),
        "rows": rows,
        "top": top,
        "tied": [r["label"] for r in tied] if len(tied) > 1 else [],
        "top_share": round(100 * top["count"] / len(felt)),
        "avg_rpe": round(mean([a["workoutRpe"] for a in rpes]) / 10.0, 1) if rpes else None,
        "hardest": hardest,
        "strong_count": strongest,
        "strong_share": round(100 * strongest / len(felt)),
    }


def build_places(activities):
    """Where the training happened. Garmin tags most sessions with a city."""
    tagged = [a for a in activities if a.get("locationName")]
    rows = defaultdict(lambda: {"count": 0, "hours": 0.0, "km": 0.0})
    for act in tagged:
        row = rows[act["locationName"]]
        row["count"] += 1
        row["hours"] += moving_seconds(act) / 3600.0
        row["km"] += (act.get("distance") or 0) / 1000.0
    # One place is a fact about your postcode, not a story about your year.
    if len(tagged) < 10 or len(rows) < 2:
        return None

    # Ranked by hours, not sessions: away trips are mostly one session each, so
    # a session count would give every place but home an identical bar.
    ordered = sorted(
        ({"name": name, "count": row["count"], "hours": round(row["hours"], 1),
          "km": round(row["km"], 1)} for name, row in rows.items()),
        key=lambda r: (-r["hours"], r["name"]))
    home, away = ordered[0], ordered[1:]
    return {
        "rows": ordered[:5],
        "home": home,
        "places": len(ordered),
        "away_sessions": sum(r["count"] for r in away),
        "away_places": len(away),
        "biggest_away": max(away, key=lambda r: r["hours"]) if away else None,
        "tagged": len(tagged),
        # Sessions with no tag at all -- treadmill work and anything indoors.
        # Printed rather than hidden, so the shares above are read fairly.
        "untagged": len(activities) - len(tagged),
        "home_share": round(100 * home["count"] / len(tagged)),
    }


def build_cost(activities, summaries):
    """What training took out of you, in Garmin's own Body Battery points."""
    drains = [a for a in activities if (a.get("differenceBodyBattery") or 0) < 0]
    if len(drains) < 30:
        return None

    total = sum(-a["differenceBodyBattery"] for a in drains)
    worst = min(drains, key=lambda a: a["differenceBodyBattery"])
    cost = {
        "sessions": len(drains),
        "total": total,
        "avg": round(total / len(drains)),
        "worst": {
            "value": -worst["differenceBodyBattery"],
            "name": activity_name(worst),
            "date": worst["startTimeLocal"][:10],
            "type": label_type(worst["activityType"]["typeKey"]),
        },
        "charged": None,
    }

    # The other half of the ledger, and optional: without daily summaries the
    # sum is zero, and "rest put back 0" is exactly the confident zero the rest
    # of this file goes out of its way not to print.
    charged = sum((s.get("bodyBattery") or {}).get("chargedValue") or 0 for s in summaries)
    if charged:
        cost["charged"] = charged
        cost["charged_days"] = len(summaries)
        cost["charged_avg"] = round(charged / len(summaries))
    return cost


# What each heart-rate zone is for, in words rather than numbers.
ZONE_NAMES = ["Warm up", "Easy", "Aerobic", "Threshold", "Maximum"]


def build_effort_mix(activities):
    """How the year's hours split across heart-rate zones, and what Garmin
    thought each session was training."""
    zones = build_hr_zones(activities)
    total = sum(z["hours"] for z in zones)
    # Under five hours on record the split is noise with percentages on it.
    if total < 5:
        return None

    for index, zone in enumerate(zones):
        zone["name"] = ZONE_NAMES[index]
        zone["short"] = "Z%d" % (index + 1)
    easy = sum(z["hours"] for z in zones[:2])
    hard = sum(z["hours"] for z in zones[3:])

    labels = Counter(a.get("trainingEffectLabel") for a in activities
                     if a.get("trainingEffectLabel")
                     and a["trainingEffectLabel"] != "UNKNOWN")
    return {
        "zones": zones,
        "hours": round(total, 1),
        "top": max(zones, key=lambda z: z["hours"]),
        "easy_hours": round(easy, 1),
        "easy_share": round(100 * easy / total),
        "hard_hours": round(hard, 1),
        "hard_label": duration_label(hard * 3600),
        "labels": [{"label": key.replace("_", " ").title(), "count": count}
                   for key, count in labels.most_common(4)],
    }


def build_personality(activities, clock, weekdays, streaks, summaries):
    """A badge assembled from the three things the data says most loudly.

    Nothing here is invented: the title is the time-of-day archetype, and every
    trait line quotes a count that is on the page somewhere else.
    """
    if not activities or not clock:
        return None

    traits = []
    traits.append({
        "key": "Your power hour",
        "value": clock["peak_label"],
        "detail": "%s sessions kicked off right there -- more than any other hour"
                  % clock["peak_count"],
    })
    traits.append({
        "key": "Your big day",
        "value": weekdays["best"]["name"],
        "detail": "%s landed on it · the quietest was %s"
                  % (plural(weekdays["best"]["count"], "session"),
                     weekdays["quietest"]["name"]),
    })

    # Garmin labels the physiological intent of each session; the mode of that
    # label is a fair description of what kind of training year this was.
    effects = Counter(a.get("trainingEffectLabel") for a in activities
                      if a.get("trainingEffectLabel")
                      and a["trainingEffectLabel"] != "UNKNOWN")
    if effects:
        top, count = effects.most_common(1)[0]
        traits.append({
            "key": "Your default gear",
            "value": top.replace("_", " ").title(),
            "detail": "what Garmin called %d of your sessions" % count,
        })

    qualifiers = Counter((s.get("stress") or {}).get("qualifier") for s in summaries
                         if (s.get("stress") or {}).get("qualifier")
                         and (s["stress"]["qualifier"] or "").lower() != "unknown")
    if qualifiers:
        top, count = qualifiers.most_common(1)[0]
        traits.append({
            "key": "Your baseline",
            "value": top.replace("_", " ").title(),
            "detail": "how Garmin read %d of your %d days"
                      % (count, len(summaries)),
        })

    return {
        "title": clock["archetype"],
        "blurb": "You train %s, you own %ss, and you showed up on %d%% of the days this year."
                 % (clock["phrase"], weekdays["best"]["name"], streaks["share"]),
        "traits": traits,
    }


def build_wrapped(bundle, activities, summaries, sleep, calendar, weekly, start, end):
    # No sessions means no story -- the template drops the whole section rather
    # than printing a page of confident zeroes.
    if not activities or not calendar:
        return None

    clock = build_clock(activities)
    weekdays = build_weekdays(activities)
    streaks = build_streaks(calendar)
    months = build_months(activities, start, end)

    total_km = sum((a.get("distance") or 0) for a in activities) / 1000.0
    total_hours = sum(moving_seconds(a) for a in activities) / 3600.0
    whole_months = [m for m in months if m["whole"]] or months
    biggest_month = max(whole_months, key=lambda m: m["hours"], default=None)
    biggest_week = max(weekly, key=lambda w: w["km"], default=None)

    return {
        "opening": {
            "km": round(total_km, 1),
            "hours": round(total_hours, 1),
            "hours_label": duration_label(total_hours * 3600),
            "sessions": len(activities),
            "days": (end - start).days + 1,
            "calories": sum((a.get("calories") or 0) for a in activities),
        },
        "hardest": build_hardest(activities, summaries, sleep),
        "podium": build_podium(activities),
        "clock": clock,
        "feel": build_feel(activities),
        "effort_mix": build_effort_mix(activities),
        "weekdays": weekdays,
        "months": months,
        "biggest_month": biggest_month if biggest_month and biggest_month["count"] else None,
        "biggest_week": biggest_week if biggest_week and biggest_week["km"] else None,
        "comparisons": build_comparisons(activities, summaries, sleep),
        "sleep": build_sleep_story(sleep),
        "streaks": streaks,
        "cost": build_cost(activities, summaries),
        "places": build_places(activities),
        "superlatives": build_superlatives(activities),
        "prs": build_prs(activities),
        "personality": build_personality(activities, clock, weekdays, streaks, summaries),
    }


# --------------------------------------------------------------------------

def build_payload(bundle):
    start = parse_day(bundle["range_start"])
    end = parse_day(bundle["fetched_through"])

    activities = bundle["activities"]
    summaries = bundle["daily_summaries"]
    sleep = [s for s in bundle["sleep"] if s.get("sleepTimeSeconds")]
    hrv = bundle["hrv"]

    weekly = build_weekly_volume(activities, start, end)
    calendar = build_calendar(activities, start, end)

    total_km = sum((a.get("distance") or 0) for a in activities) / 1000.0
    total_hours = sum((a.get("movingDuration") or a.get("duration") or 0)
                      for a in activities) / 3600.0

    resting = [(parse_day(s["calendarDate"]), (s.get("heartRate") or {}).get("restingValue"))
               for s in summaries]
    resting_smooth = rolling_mean(resting, 7)

    sleep_rows = []
    for record in sleep:
        scores = record.get("sleepScores") or {}
        sleep_rows.append({
            "date": record["calendarDate"],
            "deep": round((record.get("deepSleepSeconds") or 0) / 3600.0, 2),
            "rem": round((record.get("remSleepSeconds") or 0) / 3600.0, 2),
            "light": round((record.get("lightSleepSeconds") or 0) / 3600.0, 2),
            "awake": round((record.get("awakeSleepSeconds") or 0) / 3600.0, 2),
            "score": (scores.get("overall") or {}).get("value"),
            "total": round((record.get("sleepTimeSeconds") or 0) / 3600.0, 2),
            "resting_hr": record.get("avgHeartRate"),
        })
    sleep_rows.sort(key=lambda r: r["date"])

    hrv_rows = []
    for record in hrv:
        baseline = record.get("baseline") or {}
        if record.get("lastNightAvg") is None:
            continue
        hrv_rows.append({
            "date": record["calendarDate"],
            "value": record["lastNightAvg"],
            "low": baseline.get("balancedLow"),
            "high": baseline.get("balancedUpper"),
            "status": record.get("status"),
        })
    hrv_rows.sort(key=lambda r: r["date"])

    battery_rows = [
        {
            "date": s["calendarDate"],
            "charged": (s.get("bodyBattery") or {}).get("chargedValue") or 0,
            "drained": (s.get("bodyBattery") or {}).get("drainedValue") or 0,
        }
        for s in summaries
    ]

    step_rows = [
        {
            "date": s["calendarDate"],
            "value": ((s.get("movement") or {}).get("steps") or {}).get("value") or 0,
            "goal": ((s.get("movement") or {}).get("steps") or {}).get("goal") or 0,
        }
        for s in summaries
    ]

    # status NONE means Garmin recorded no reading -- not a fourth band
    hrv_status_mix = Counter(
        h["status"] for h in hrv
        if h.get("status") and h["status"].upper() != "NONE"
    )

    return {
        "meta": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
            "generated": datetime.now().strftime("%d %b %Y, %H:%M"),
        },
        "hero": {
            "label": "Distance covered in the last 12 months",
            "value": round(total_km, 1),
            "unit": "km",
            "detail": "%d sessions · %s moving · %s burned" % (
                len(activities),
                duration_label(total_hours * 3600),
                "{:,} kcal".format(int(sum((a.get("calories") or 0) for a in activities))),
            ),
        },
        "tiles": build_tiles(summaries, sleep, hrv, activities, end),
        "weekly": weekly,
        "type_mix": build_type_mix(activities),
        "hr_zones": build_hr_zones(activities),
        "calendar": calendar,
        "pace": build_pace(activities),
        "effort": build_effort(activities),
        "effort_sessions": len(activities),
        "resting_hr": [{"date": d.isoformat(), "value": round(v, 1)} for d, v in resting_smooth],
        "resting_hr_raw": [{"date": d.isoformat(), "value": v} for d, v in resting if v],
        "sleep": sleep_rows,
        "hrv": hrv_rows,
        "hrv_status_mix": [{"status": k.title(), "count": v}
                           for k, v in hrv_status_mix.most_common()],
        "battery": battery_rows,
        "steps": step_rows,
        "records": build_records(activities, summaries, sleep, hrv, calendar, weekly),
        "wrapped": build_wrapped(bundle, activities, summaries, sleep,
                                 calendar, weekly, start, end),
    }


SENTINEL = "/*__PAYLOAD__*/null"


def write_page(template, out_file, payload):
    html = template.read_text(encoding="utf-8")
    if SENTINEL not in html:
        # Without this the page loads with DATA === null and renders blank,
        # which is a much harder thing to notice than a failed build.
        raise SystemExit("%s has lost its %s marker." % (template.name, SENTINEL))
    out_file.write_text(
        html.replace(SENTINEL, json.dumps(payload, separators=(",", ":"))),
        encoding="utf-8")
    print("Wrote %s (%.0f KB)" % (out_file, out_file.stat().st_size / 1024))


def main():
    if not DATA_FILE.exists():
        raise SystemExit("No cache at %s -- run `python fetch.py` first." % DATA_FILE)

    payload = build_payload(json.loads(DATA_FILE.read_text()))

    OUT_DIR.mkdir(exist_ok=True)
    for template, out_file, keys in PAGES:
        slice_ = ({k: payload[k] for k in keys} if keys
                  else {k: v for k, v in payload.items() if k != "wrapped"})
        write_page(template, out_file, slice_)

    webbrowser.open(PAGES[0][1].resolve().as_uri())


if __name__ == "__main__":
    main()
