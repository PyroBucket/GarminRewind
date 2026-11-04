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
from statistics import pstdev

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


def build_comparisons(activities, summaries, sleep, promoted=()):
    """Totals restated in units a person can picture.

    `promoted` names the totals that got a panel of their own upstream. The
    same number twice in one deck reads as a mistake the second time, so a
    promoted total drops out of here rather than being restated.
    """
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
    if steps and "steps" not in promoted:
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
    if floors and "floors" not in promoted:
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
    # A year with nothing logged in it has no streak to report, and "longest
    # streak: 0 days, ending None" is worse than no panel at all.
    if not active:
        return None

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


# --------------------------------------------------------------------------
# The everyday panels
#
# Everything above this point needs a logged session behind it. These three
# need a pulse: steps, stairs and stress are counted on the days you did
# nothing at all, which is what lets the deck survive a year -- or a person --
# with no workouts in it.
# --------------------------------------------------------------------------

# Rungs for "how far did your own two feet actually take you". Each is a
# distance most people can picture without being told how long it is.
WALK_LADDER = [
    (408, "Straight up to the ISS"),
    (780, "The Camino de Santiago"),
    (1434, "London to Rome"),
    (3500, "The Appalachian Trail"),
    (3940, "Route 66, end to end"),
    (5837, "Paris to New York"),
    (6400, "The Amazon, source to sea"),
    (9289, "The Trans-Siberian Railway"),
    (21196, "The Great Wall of China"),
    (40075, "One lap of the planet"),
]
MOON_KM = 384400.0

CLIMB_LADDER = [
    (324, "The Eiffel Tower"),
    (443, "The Empire State Building"),
    (828, "The Burj Khalifa"),
    (3776, "Mount Fuji"),
    (4808, "Mont Blanc"),
    (5895, "Kilimanjaro"),
    (8849, "Everest, from sea level"),
    (11000, "A plane's cruising altitude"),
    # The top of the climb ladder is in it so a year that clears everything
    # else still has a rung with somewhere left to go. The walking ladder
    # needs no equivalent -- nobody walks past the last rung but one.
    (100000, "The Kármán line, where space starts"),
]
KARMAN_M = 100000.0     # where the atmosphere officially stops

# How much of a month has to be on record before it can win "your busiest
# month". A rolling window clips its first and last months, and a watch left
# off clips any of them -- either way a handful of days is not a month.
MONTH_MIN_DAYS = 20


def ladder_rungs(total, ladder, limit=4):
    """A few rungs off a ladder of landmarks, spread far enough apart that
    each one says something the last one didn't.

    Printed in full a ladder is a wall of ratios, and neighbouring rungs
    mostly restate each other -- 2.1 Kilimanjaros and 2.5 Mont Blancs is the
    same fact twice. So: the biggest thing the total clears, then only rungs
    at least three times smaller again, and last the first thing it doesn't
    clear, which is the only one with somewhere left to go.
    """
    if not total:
        return []
    rungs = []
    for size, name in reversed([r for r in ladder if total >= r[0]]):
        ratio = total / size
        if rungs and ratio < rungs[-1]["ratio"] * 3:
            continue
        rungs.append({
            "name": name,
            "size": size,
            "ratio": round(ratio, 2),
            "value": ("%.1f×" % ratio) if ratio < 10 else ("%.0f×" % ratio),
            "cleared": True,
        })
        if len(rungs) == limit - 1:
            break
    nxt = next((r for r in ladder if total < r[0]), None)
    if nxt and len(rungs) < limit:
        rungs.append({
            "name": nxt[1],
            "size": nxt[0],
            "ratio": round(total / nxt[0], 2),
            "value": "%d%%" % round(100 * total / nxt[0]),
            "cleared": False,
        })
    # Biggest landmark first, so the ladder reads top-down from the one still
    # out of reach -- and so the meters beside them descend rather than
    # alternating between full and part-full.
    rungs.sort(key=lambda r: -r["size"])
    return rungs


def pace_to_target(total, days, target):
    """How long this rate would take to cover `target` -- the honest framing
    for a number the year gets nowhere near.

    A year of walking is a fraction of a percent of the way to the Moon, and
    "0.8% of the way" reads as a failure. "126 years at this pace" is the
    same arithmetic and reads as a joke you're in on.
    """
    # Clearing the target outright makes the whole framing wrong -- "you
    # reach the edge of space in 0 years" when you passed it in March. The
    # ladder already names it as cleared, so this just drops out.
    if not total or not days or total >= target:
        return None
    per_year = total * 365.0 / days
    return {
        "share": round(100 * total / target, 1),
        "years": max(1, int(round(target / per_year))),
    }


def build_steps_story(summaries, start, end):
    """Daily steps against the goal the watch set, month by month.

    The goal is Garmin's own adaptive one and it moves day to day, so it is
    reported as an average rather than a fixed line -- the per-day comparison
    underneath it still uses whatever that day was actually asked for.
    """
    days = []
    for summary in summaries:
        steps = (summary.get("movement") or {}).get("steps") or {}
        if steps.get("value") is None:
            continue
        days.append({
            "date": summary["calendarDate"][:10],
            "steps": steps["value"],
            "goal": steps.get("goal") or 0,
            "metres": steps.get("distanceInMeters") or 0,
        })
    # A month of step counts is a month, not a year.
    if len(days) < 30:
        return None
    days.sort(key=lambda d: d["date"])

    # The longest unbroken run of days that met their goal. Calendar gaps end
    # a run rather than being skipped over: a missing day is a day the watch
    # cannot say you cleared anything.
    best_run = run = 0
    best_end = None
    previous = None
    for day in days:
        current = parse_day(day["date"])
        hit = bool(day["goal"]) and day["steps"] >= day["goal"]
        contiguous = previous is not None and (current - previous).days == 1
        run = (run + 1 if contiguous else 1) if hit else 0
        if run > best_run:
            best_run, best_end = run, day["date"]
        previous = current

    buckets = defaultdict(lambda: {"steps": 0, "days": 0, "hit": 0})
    for day in days:
        when = parse_day(day["date"])
        bucket = buckets[(when.year, when.month)]
        bucket["steps"] += day["steps"]
        bucket["days"] += 1
        if day["goal"] and day["steps"] >= day["goal"]:
            bucket["hit"] += 1

    months = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        bucket = buckets.get((cursor.year, cursor.month),
                             {"steps": 0, "days": 0, "hit": 0})
        nxt = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        months.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%b"),
            "long": cursor.strftime("%B %Y"),
            "avg": round(bucket["steps"] / bucket["days"]) if bucket["days"] else 0,
            "days": bucket["days"],
            "hit": bucket["hit"],
            "whole": cursor >= start and nxt - timedelta(days=1) <= end,
        })
        cursor = nxt

    # A daily average sounds like it makes a short month comparable, and it
    # doesn't: the last month of a rolling window can be two days long, and
    # two good days beat any real month on the board. Only months with most
    # of their days on record are eligible to win -- the rest still get drawn.
    judged = [m for m in months if m["days"] >= MONTH_MIN_DAYS]
    busiest = max(judged, key=lambda m: m["avg"], default=None)

    total = sum(d["steps"] for d in days)
    km = sum(d["metres"] for d in days) / 1000.0
    best = max(days, key=lambda d: d["steps"])
    quietest = min(days, key=lambda d: d["steps"])
    goals = [d["goal"] for d in days if d["goal"]]
    hit = [d for d in days if d["goal"] and d["steps"] >= d["goal"]]

    return {
        "days": len(days),
        "total": total,
        # Divided by days that carry a step count, not by days in the window:
        # a watch left in a drawer for a fortnight would otherwise drag the
        # average down as though those were days you sat still.
        "avg": round(total / len(days)),
        "km": round(km, 1),
        "goal": round(mean(goals)) if goals else None,
        "hit": len(hit),
        "hit_share": round(100 * len(hit) / len(days)),
        "best_run": best_run,
        "best_run_end": best_end,
        "ten_k_days": sum(1 for d in days if d["steps"] >= 10000),
        "best": {"date": best["date"], "steps": best["steps"],
                 "km": round(best["metres"] / 1000.0, 1)},
        "quietest": {"date": quietest["date"], "steps": quietest["steps"]},
        "months": months,
        "busiest_month": busiest,
        "peak_index": months.index(busiest) if busiest else None,
        "rungs": ladder_rungs(km, WALK_LADDER),
        "moon": pace_to_target(km, len(days), MOON_KM),
    }


def build_climb_story(summaries):
    """Floors climbed -- stairs, hills, everything the barometer caught.

    The elevation line in "Put another way" is what you climbed while
    training. This is what you climbed while living, which in most years is
    comfortably the bigger of the two.
    """
    days = []
    for summary in summaries:
        floors = (summary.get("movement") or {}).get("floorsAscended") or {}
        if floors.get("value") is None:
            continue
        days.append({
            "date": summary["calendarDate"][:10],
            "floors": floors["value"],
            "metres": floors.get("distanceInMeters") or 0,
            "goal": floors.get("goal") or 0,
        })
    if len(days) < 30:
        return None

    total = sum(d["floors"] for d in days)
    metres = sum(d["metres"] for d in days)
    # A watch without a barometer reports the field and fills it with zeroes.
    if not total or not metres:
        return None

    best = max(days, key=lambda d: d["floors"])
    goals = [d["goal"] for d in days if d["goal"]]
    hit = [d for d in days if d["goal"] and d["floors"] >= d["goal"]]

    return {
        "days": len(days),
        "floors": total,
        "metres": round(metres),
        "avg": round(total / len(days)),
        "goal": round(mean(goals)) if goals else None,
        "hit": len(hit),
        "hit_share": round(100 * len(hit) / len(days)),
        "best": {"date": best["date"], "floors": best["floors"],
                 "metres": round(best["metres"])},
        "rungs": ladder_rungs(metres, CLIMB_LADDER),
        "space": pace_to_target(metres, len(days), KARMAN_M),
    }


# Garmin's own verdict on a day, in the order of how the day went. The
# _awake suffix is the same label measured over the waking hours only, so it
# folds into its base band rather than becoming a fifth one.
STRESS_BANDS = [
    ("calm", "Calm"),
    ("balanced", "Balanced"),
    ("stressful", "Stressful"),
    ("very_stressful", "Very stressful"),
]
STRESS_KEYS = {key for key, _ in STRESS_BANDS}


def build_stress_story(summaries):
    """How the year felt, in the watch's own words for each day.

    `qualifier` is Garmin's label, not ours, which is most of why this earns
    a panel: it is a read on the whole year that needs no session behind it.
    """
    days = []
    for summary in summaries:
        stress = summary.get("stress") or {}
        if stress.get("avgLevel") is None:
            continue
        qualifier = (stress.get("qualifier") or "").lower()
        base = qualifier[:-6] if qualifier.endswith("_awake") else qualifier
        days.append({
            "date": summary["calendarDate"][:10],
            "level": stress["avgLevel"],
            "band": base if base in STRESS_KEYS else None,
            "rest": stress.get("restProportion"),
        })
    if len(days) < 30:
        return None
    days.sort(key=lambda d: d["date"])

    counts = Counter(d["band"] for d in days if d["band"])
    judged = sum(counts.values())
    # Without labels this is a chart of a unitless 0-100 score, which is not
    # a story. The qualifier is the whole panel.
    if judged < 30:
        return None

    bands = [{"key": key, "label": label,
              "count": counts.get(key, 0),
              "share": round(100 * counts.get(key, 0) / judged)}
             for key, label in STRESS_BANDS]
    top = max(bands, key=lambda b: b["count"])

    # The longest run of days the watch did not call stressful. Unlabelled
    # days break the run rather than extending it, for the same reason a
    # calendar gap does in the steps panel.
    best_run = run = 0
    best_end = None
    previous = None
    for day in days:
        current = parse_day(day["date"])
        calm = day["band"] in ("calm", "balanced")
        contiguous = previous is not None and (current - previous).days == 1
        run = (run + 1 if contiguous else 1) if calm else 0
        if run > best_run:
            best_run, best_end = run, day["date"]
        previous = current

    buckets = defaultdict(list)
    for day in days:
        when = parse_day(day["date"])
        buckets[(when.year, when.month)].append(day["level"])

    months = []
    for (year, month), levels in sorted(buckets.items()):
        months.append({
            "date": date(year, month, 1).isoformat(),
            "label": date(year, month, 1).strftime("%b"),
            "long": date(year, month, 1).strftime("%B %Y"),
            "avg": round(sum(levels) / len(levels)),
            "days": len(levels),
        })
    # Same rule as the steps strip: a clipped month gets drawn but cannot win.
    judged_months = [m for m in months if m["days"] >= MONTH_MIN_DAYS]
    calmest_month = min(judged_months, key=lambda m: m["avg"], default=None)
    worst_month = max(judged_months, key=lambda m: m["avg"], default=None)

    calmest = min(days, key=lambda d: d["level"])
    worst = max(days, key=lambda d: d["level"])
    rest = [d["rest"] for d in days if d["rest"] is not None]

    return {
        "days": len(days),
        "judged": judged,
        "avg": round(mean([d["level"] for d in days])),
        "bands": bands,
        "top": top,
        # By key, not by position: the template sorts a copy of `bands` by
        # size for display, so two orderings of this list are in play.
        "calm_share": sum(b["share"] for b in bands
                          if b["key"] in ("calm", "balanced")),
        "calm_run": best_run,
        "calm_run_end": best_end,
        "calmest": {"date": calmest["date"], "level": calmest["level"]},
        "worst": {"date": worst["date"], "level": worst["level"]},
        "months": months,
        # The strip plots a monthly average, so the clipped first and last
        # months of the window compete on the same footing as the rest.
        "peak_index": months.index(worst_month) if worst_month else None,
        "calmest_month": calmest_month,
        "worst_month": worst_month,
        "rest_hours": round(mean(rest) * 24, 1) if rest else None,
    }


# --------------------------------------------------------------------------


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


# Garmin counts a vigorous minute twice toward the weekly target, so the
# weighted sum -- not the raw one -- is the number the watch shows you.
INTENSITY_GOAL = 150


def build_intensity(summaries, start, end):
    """Weekly intensity minutes against the 150-a-week target.

    The only figure in this deck measured against something outside you: the
    WHO guideline Garmin ships as the default goal. Weeks the window only
    partly covers are drawn but never judged -- a Thursday-to-Sunday stub
    cannot miss a weekly target it was never given seven days to meet.
    """
    by_day = {}
    for day in summaries:
        minutes = day.get("intensityMinutes") or {}
        by_day[day["calendarDate"][:10]] = (
            minutes.get("moderate") or 0,
            minutes.get("vigorous") or 0,
            minutes.get("goal") or INTENSITY_GOAL,
        )
    if not by_day:
        return None

    weeks = []
    cursor = start - timedelta(days=start.weekday())
    while cursor <= end:
        days = [cursor + timedelta(days=offset) for offset in range(7)]
        rows = [by_day[d.isoformat()] for d in days if d.isoformat() in by_day]
        moderate = sum(r[0] for r in rows)
        vigorous = sum(r[1] for r in rows)
        goal = rows[0][2] if rows else INTENSITY_GOAL
        weeks.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%d %b"),
            "moderate": moderate,
            "vigorous": vigorous,
            "total": moderate + 2 * vigorous,
            "goal": goal,
            "whole": days[0] >= start and days[6] <= end,
        })
        cursor += timedelta(days=7)

    judged = [w for w in weeks if w["whole"]]
    # A watch that barely records intensity minutes still hands back a goal and
    # a row of near-zeroes, which would otherwise render as a confident "you
    # averaged 1, clearing it in 0 of 52 weeks". A panel about meeting a weekly
    # target needs most weeks to have been measured at all before it means
    # anything, so the gate is coverage rather than a non-zero sum.
    measured = sum(1 for w in judged if w["total"])
    if len(judged) < 12 or measured * 2 < len(judged):
        return None

    hit = [w for w in judged if w["total"] >= w["goal"]]
    # The longest unbroken run of on-target weeks, which is a different claim
    # from how many there were -- 20 scattered weeks is not a habit.
    best_run = run = 0
    for week in judged:
        run = run + 1 if week["total"] >= week["goal"] else 0
        best_run = max(best_run, run)

    total_moderate = sum(w["moderate"] for w in weeks)
    total_vigorous = sum(w["vigorous"] for w in weeks)
    peak = max(weeks, key=lambda w: w["total"])
    return {
        "weeks": weeks,
        "goal": judged[0]["goal"],
        "weeks_judged": len(judged),
        "weeks_hit": len(hit),
        "hit_share": round(100 * len(hit) / len(judged)),
        "best_run": best_run,
        "avg": round(sum(w["total"] for w in judged) / len(judged)),
        "moderate": total_moderate,
        "vigorous": total_vigorous,
        "total": total_moderate + 2 * total_vigorous,
        "peak": peak if peak["total"] else None,
        "peak_index": weeks.index(peak) if peak["total"] else None,
        "quiet_weeks": sum(1 for w in judged if not w["total"]),
    }


# Garmin's aerobic training-effect scale, as the watch itself bands it. The
# upper bound is exclusive; 5.0 is the top of the scale, not a sixth band.
TE_BANDS = [
    (1.0, "No benefit", "None"),
    (2.0, "Minor benefit", "Minor"),
    (3.0, "Maintaining", "Maintain"),
    (4.0, "Improving", "Improve"),
    (5.1, "Highly improving", "High"),
]
# A session needs this much anaerobic effect before Garmin credits it with any.
ANAEROBIC_FLOOR = 1.0


def clean_effect_message(text):
    """Strip the trailing id off an effect message: IMPROVING_AEROBIC_BASE_8."""
    head, _, tail = text.rpartition("_")
    cleaned = head if head and tail.isdigit() else text
    return cleaned.replace("_", " ").capitalize()


def build_training_effect(activities):
    """What each session did to you physiologically, in Garmin's own terms.

    Training effect is a per-session score on a 0-5 scale, not a quantity that
    adds up, so this counts sessions per band rather than summing anything.
    """
    scored = [a for a in activities if a.get("aerobicTrainingEffect")]
    if len(scored) < 15:
        return None

    bands = [{"label": label, "short": short, "count": 0}
             for _, label, short in TE_BANDS]
    for act in scored:
        value = act["aerobicTrainingEffect"]
        for index, (ceiling, _, _) in enumerate(TE_BANDS):
            if value < ceiling:
                bands[index]["count"] += 1
                break

    # "Productive" is everything from maintaining upward -- the sessions that
    # held or moved your aerobic base rather than just passing the time.
    productive = sum(b["count"] for b in bands[2:])
    anaerobic = [a for a in scored
                 if (a.get("anaerobicTrainingEffect") or 0) >= ANAEROBIC_FLOOR]

    messages = Counter(clean_effect_message(a["aerobicTrainingEffectMessage"])
                       for a in scored if a.get("aerobicTrainingEffectMessage"))
    top_band = max(bands, key=lambda b: b["count"])
    peak = max(scored, key=lambda a: a["aerobicTrainingEffect"])
    return {
        "bands": bands,
        "band_index": bands.index(top_band),
        "top_band": top_band,
        "scored": len(scored),
        "unscored": len(activities) - len(scored),
        "avg": round(sum(a["aerobicTrainingEffect"] for a in scored) / len(scored), 1),
        "productive": productive,
        "productive_share": round(100 * productive / len(scored)),
        "anaerobic": len(anaerobic),
        "anaerobic_share": round(100 * len(anaerobic) / len(scored)),
        "messages": [{"label": label, "count": count}
                     for label, count in messages.most_common(3)],
        "peak": {
            "value": round(peak["aerobicTrainingEffect"], 1),
            "name": activity_name(peak),
            "date": peak["startTimeLocal"][:10],
            "message": clean_effect_message(peak["aerobicTrainingEffectMessage"])
                       if peak.get("aerobicTrainingEffectMessage") else None,
        },
    }


# Below this many nights on either side of the split the comparison is two
# small samples staring at each other.
RECOVERY_MIN_NIGHTS = 40
# How far apart the two groups must sit before the gap is called a direction
# rather than noise: half the week-to-week spread of the measure itself.
RECOVERY_NOISE_FRACTION = 0.5


def build_recovery(weekly, hrv, sleep):
    """Whether the big training weeks cost you anything overnight.

    Weeks are split into the heaviest third and the rest, then each group is
    described by the nights inside it. Nothing here is a causal claim and the
    direction is read off the data rather than assumed -- a year where the big
    weeks slept *better* gets the same panel, saying so.
    """
    trained = [w for w in weekly if w["hours"] > 0]
    if len(trained) < 12:
        return None

    status_by_day = {h["calendarDate"][:10]: (h.get("status") or "").upper()
                     for h in hrv if h.get("status")}
    score_by_day = {}
    for night in sleep:
        score = ((night.get("sleepScores") or {}).get("overall") or {}).get("value")
        if score:
            score_by_day[night["calendarDate"][:10]] = score

    def nights_of(week):
        monday = parse_day(week["date"])
        days = [(monday + timedelta(days=offset)).isoformat() for offset in range(7)]
        return ([status_by_day[d] for d in days if d in status_by_day],
                [score_by_day[d] for d in days if d in score_by_day])

    def describe(weeks):
        statuses, scores = [], []
        for week in weeks:
            week_statuses, week_scores = nights_of(week)
            statuses.extend(week_statuses)
            scores.extend(week_scores)
        off = [s for s in statuses if s and s != "BALANCED"]
        return {
            "weeks": len(weeks),
            "avg_hours": round(mean([w["hours"] for w in weeks]) or 0, 1),
            "avg_km": round(mean([w["km"] for w in weeks]) or 0, 1),
            "nights": len(statuses),
            "off_balance": len(off),
            "off_share": round(100 * len(off) / len(statuses)) if statuses else None,
            "scored_nights": len(scores),
            "sleep_score": round(mean(scores)) if scores else None,
        }

    ordered = sorted(trained, key=lambda w: -w["hours"])
    cut = max(4, len(ordered) // 3)
    big, quiet = describe(ordered[:cut]), describe(ordered[cut:])
    if (big["nights"] < RECOVERY_MIN_NIGHTS or quiet["nights"] < RECOVERY_MIN_NIGHTS
            or big["off_share"] is None or quiet["off_share"] is None):
        return None

    # The yardstick for "is this gap real" is the measure's own week-to-week
    # spread, not a number picked here.
    per_week = []
    for week in trained:
        week_statuses, _ = nights_of(week)
        if week_statuses:
            off = [s for s in week_statuses if s != "BALANCED"]
            per_week.append(100.0 * len(off) / len(week_statuses))
    noise = pstdev(per_week) if len(per_week) > 1 else 0.0
    gap = big["off_share"] - quiet["off_share"]
    threshold = max(5.0, RECOVERY_NOISE_FRACTION * noise)
    direction = "flat" if abs(gap) < threshold else ("worse" if gap > 0 else "better")

    sleep_gap = None
    if big["sleep_score"] is not None and quiet["sleep_score"] is not None:
        sleep_gap = big["sleep_score"] - quiet["sleep_score"]
    return {
        "big": big,
        "quiet": quiet,
        "gap": round(gap),
        "sleep_gap": sleep_gap,
        "direction": direction,
        "threshold": round(threshold),
        "statuses": [{"label": key.title(), "count": count}
                     for key, count in Counter(
                         s for s in status_by_day.values() if s).most_common()],
    }


# A dead panel is worse than a missing one, so form needs a real running year
# behind it before it earns a place in the deck.
FORM_MIN_RUNS = 12
FORM_FIELDS = [
    ("averageRunningCadenceInStepsPerMinute", "Cadence", "spm", 0),
    ("avgStrideLength", "Stride length", "cm", 0),
    ("avgGroundContactTime", "Ground contact", "ms", 0),
    ("avgVerticalOscillation", "Vertical bounce", "cm", 1),
]


def build_form(activities):
    """Running form, for the years that have enough running in them.

    The halves are reported because the trend is the interesting part, but
    they are not a claim about form: a half with more treadmill work or more
    tempo work in it moves these numbers on composition alone, so the share of
    each is carried alongside and the copy says so.
    """
    runs = [a for a in activities
            if a["activityType"]["typeKey"] in RUN_TYPES
            and a.get("averageRunningCadenceInStepsPerMinute")]
    if len(runs) < FORM_MIN_RUNS:
        return None

    runs.sort(key=lambda a: a["startTimeLocal"])
    midpoint = len(runs) // 2
    halves = [runs[:midpoint], runs[midpoint:]]

    def treadmill_share(group):
        indoor = sum(1 for a in group if "treadmill" in a["activityType"]["typeKey"])
        return round(100 * indoor / len(group)) if group else 0

    metrics = []
    for field, label, unit, digits in FORM_FIELDS:
        values = [a[field] for a in runs if a.get(field)]
        if len(values) < FORM_MIN_RUNS:
            continue
        first, second = [
            mean([a[field] for a in half if a.get(field)]) for half in halves
        ]
        metrics.append({
            "label": label,
            "unit": unit,
            "value": round(mean(values), digits),
            "low": round(min(values), digits),
            "high": round(max(values), digits),
            "first": round(first, digits) if first is not None else None,
            "second": round(second, digits) if second is not None else None,
            "change": (round(second - first, digits)
                       if first is not None and second is not None else None),
        })
    if not metrics:
        return None

    return {
        "runs": len(runs),
        "metrics": metrics,
        "cadence": metrics[0],
        "first_half": {
            "count": len(halves[0]),
            "treadmill_share": treadmill_share(halves[0]),
            "until": halves[0][-1]["startTimeLocal"][:10] if halves[0] else None,
        },
        "second_half": {
            "count": len(halves[1]),
            "treadmill_share": treadmill_share(halves[1]),
            "from": halves[1][0]["startTimeLocal"][:10] if halves[1] else None,
        },
        "indoor_share": treadmill_share(runs),
    }


def build_personality(activities, clock, weekdays, streaks, summaries):
    """A badge assembled from the three things the data says most loudly.

    Nothing here is invented: the title is the time-of-day archetype, and every
    trait line quotes a count that is on the page somewhere else.
    """
    if not activities or not clock or not weekdays or not streaks:
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
    # A year with no sessions in it is still a year the watch was on your
    # wrist: steps, stairs and stress are counted whether or not you train.
    # So the gate is "was anything recorded at all", and an empty activity
    # list drops the training panels one at a time rather than the deck --
    # every panel below is individually optional for exactly this reason.
    if not activities and not summaries:
        return None

    clock = build_clock(activities)
    weekdays = build_weekdays(activities) if activities else None
    streaks = build_streaks(calendar) if calendar else None
    months = build_months(activities, start, end) if activities else None

    total_km = sum((a.get("distance") or 0) for a in activities) / 1000.0
    total_hours = sum(moving_seconds(a) for a in activities) / 3600.0
    whole_months = [m for m in months if m["whole"]] or months if months else []
    biggest_month = max(whole_months, key=lambda m: m["hours"], default=None)
    biggest_week = max(weekly, key=lambda w: w["km"], default=None)

    steps_story = build_steps_story(summaries, start, end)
    climb_story = build_climb_story(summaries)
    promoted = ({"steps"} if steps_story else set()) | \
               ({"floors"} if climb_story else set())

    return {
        "opening": {
            "km": round(total_km, 1),
            "hours": round(total_hours, 1),
            "hours_label": duration_label(total_hours * 3600),
            "sessions": len(activities),
            "days": (end - start).days + 1,
            "calories": sum((a.get("calories") or 0) for a in activities),
        } if activities else None,
        "steps": steps_story,
        "climb": climb_story,
        "stress": build_stress_story(summaries),
        "hardest": build_hardest(activities, summaries, sleep),
        "podium": build_podium(activities),
        "clock": clock,
        "feel": build_feel(activities),
        "effort_mix": build_effort_mix(activities),
        "intensity": build_intensity(summaries, start, end),
        "training_effect": build_training_effect(activities),
        "recovery": build_recovery(weekly, bundle["hrv"], sleep),
        "form": build_form(activities),
        "weekdays": weekdays,
        "months": months,
        "biggest_month": biggest_month if biggest_month and biggest_month["count"] else None,
        "biggest_week": biggest_week if biggest_week and biggest_week["km"] else None,
        "comparisons": build_comparisons(activities, summaries, sleep, promoted),
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
