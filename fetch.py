"""Pull a year of Garmin data into data/*.json so the dashboard can be rebuilt offline.

Run:  python fetch.py [--days 365]

Endpoint range caps differ (measured against the live API):
  dailysummaries, hrv/daily    accept a full 365-day range
  sleep, bodyBattery           cap at 31 days  -> chunked
  stats/steps, stats/stress    cap at 30 days  -> not used; dailysummaries carries both
Activities page at a hard max of 99 per request.
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import garmin_api
from garmin_api import GarminClient, GarminApiError, use_env_file

DATA_DIR = Path(__file__).parent / "data"

CHUNK_DAYS = 28
ACTIVITY_PAGE = 99

# RPE and feel never change once logged, so they are cached by activity id and
# survive across runs. This is what keeps a re-run from re-issuing one request
# per activity -- and what makes an interrupted run resumable.
DETAIL_CACHE_PATH = DATA_DIR / "activity_details.json"
DETAIL_CACHE_FLUSH_EVERY = 25
# A rating can appear days after the workout, so "no rating yet" is not a
# durable answer for a recent activity -- those get re-checked. Past this
# window an unrated activity is treated as settled and stays cached.
DETAIL_RECHECK_DAYS = 45

# Dropped from cached activities: a home address at 15 decimal places plus
# account identifiers, none of which the dashboard uses. City-level
# `locationName` is deliberately kept -- activityName carries the same city
# ("Warsaw Running"), so dropping it alone would not remove the information.
ACTIVITY_DROP_FIELDS = {
    "startLatitude", "startLongitude", "endLatitude", "endLongitude",
    "ownerId", "ownerDisplayName", "ownerFullName",
    "ownerProfileImageUrlSmall", "ownerProfileImageUrlMedium",
    "ownerProfileImageUrlLarge",
}


def _chunks(start, end, size):
    """Yield (chunk_start, chunk_end) windows of at most `size` days."""
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=size - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _fetch_chunked(client, method, start, end, label):
    out = []
    for chunk_start, chunk_end in _chunks(start, end, CHUNK_DAYS):
        try:
            result = method(chunk_start, chunk_end)
        except GarminApiError as exc:
            print("    %s %s..%s failed: %s" % (label, chunk_start, chunk_end, str(exc)[:120]))
            continue
        if isinstance(result, list):
            out.extend(result)
        elif isinstance(result, dict):
            # Each endpoint wraps its rows under its own key: "data" for sleep
            # and bodyBattery, "dailyWeightSummaries" for weight.
            for key in ("data", "dailyWeightSummaries"):
                rows = result.get(key)
                if isinstance(rows, list):
                    out.extend(rows)
                    break
    return out


def fetch_activities(client, start, end):
    """Page through activities in the window until the server runs dry."""
    activities = []
    offset = 0
    while True:
        # Bounding server-side means no pages of pre-window history are paid for.
        page = client.search_activities(
            start=offset, limit=ACTIVITY_PAGE, start_date=start, end_date=end
        )
        if not page:
            break
        activities.extend(page)
        oldest = page[-1].get("startTimeLocal", "")[:10]
        print("    page at offset %d: %d activities (oldest %s)" % (offset, len(page), oldest))
        if oldest and oldest < start.isoformat():
            break
        if len(page) < ACTIVITY_PAGE:
            break
        offset += len(page)

    cutoff = start.isoformat()
    kept = []
    for activity in activities:
        if activity.get("startTimeLocal", "")[:10] < cutoff:
            continue
        kept.append({k: v for k, v in activity.items() if k not in ACTIVITY_DROP_FIELDS})
    return kept


def _load_detail_cache():
    if not DETAIL_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(DETAIL_CACHE_PATH.read_text())
    except (ValueError, OSError):
        return {}


def _save_detail_cache(cache):
    DETAIL_CACHE_PATH.write_text(json.dumps(cache))


def fetch_subjective(client, activities):
    """Attach the two self-rated scores, which the list endpoint omits.

    RPE and "how did it feel" live on the per-activity detail endpoint only, so
    an uncached activity costs one GET. Only the two scalars are kept -- the
    detail response also carries userProfileId and owner fields. Results are
    cached on disk and flushed periodically, so a run that dies partway keeps
    the requests it already paid for.

    A rating is immutable once entered, but an activity can go from unrated to
    rated, so recent unrated activities are re-checked (see DETAIL_RECHECK_DAYS).
    """
    cache = _load_detail_cache()
    recheck_after = (date.today() - timedelta(days=DETAIL_RECHECK_DAYS)).isoformat()

    def _needs_fetch(activity):
        entry = cache.get(str(activity["activityId"]))
        if entry is None:
            return True
        if entry.get("workoutRpe") is not None or entry.get("workoutFeel") is not None:
            return False
        return activity.get("startTimeLocal", "")[:10] >= recheck_after

    missing = [a for a in activities if _needs_fetch(a)]
    print("    %d activities, %d already cached, %d to fetch"
          % (len(activities), len(activities) - len(missing), len(missing)))

    fetched = 0
    try:
        for index, activity in enumerate(missing, 1):
            try:
                detail = client.get_activity(activity["activityId"]) or {}
            except GarminApiError as exc:
                print("    detail %s failed: %s" % (activity["activityId"], str(exc)[:100]))
                continue
            summary = detail.get("summaryDTO") or {}
            cache[str(activity["activityId"])] = {
                "workoutRpe": summary.get("directWorkoutRpe"),
                "workoutFeel": summary.get("directWorkoutFeel"),
            }
            fetched += 1
            if fetched % DETAIL_CACHE_FLUSH_EVERY == 0:
                _save_detail_cache(cache)
            if index % DETAIL_CACHE_FLUSH_EVERY == 0 or index == len(missing):
                print("    %d/%d" % (index, len(missing)))
    finally:
        if fetched:
            _save_detail_cache(cache)

    for activity in activities:
        entry = cache.get(str(activity["activityId"]), {})
        activity["workoutRpe"] = entry.get("workoutRpe")
        activity["workoutFeel"] = entry.get("workoutFeel")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=365, help="how much history to pull")
    parser.add_argument("--env-file", default=None,
                        help="read the API key from this file instead of .env")
    parser.add_argument("--min-interval", type=float, default=None,
                        help="seconds between API requests (default %.2f); raise if 429s persist"
                             % garmin_api.MIN_REQUEST_INTERVAL)
    args = parser.parse_args()

    if args.env_file:
        print("Using credentials from %s" % use_env_file(args.env_file).name)
    if args.min_interval is not None:
        garmin_api.MIN_REQUEST_INTERVAL = args.min_interval

    end = date.today()
    start = end - timedelta(days=args.days - 1)
    DATA_DIR.mkdir(exist_ok=True)

    print("Fetching %s .. %s" % (start, end))
    complete = False
    profile = {}
    activities, summaries, sleep, body_battery, hrv, weight = [], [], [], [], [], []

    # Every stage is best-effort: a stage that gives up still leaves the
    # earlier ones on disk, so a re-run resumes instead of starting over.
    try:
        with GarminClient() as client:
            print("  profile")
            profile = client.get_user_profile_settings()

            print("  activities")
            activities = fetch_activities(client, start, end)

            print("  effort & feel (one request per uncached activity)")
            fetch_subjective(client, activities)

            print("  daily summaries")
            summaries = client.get_daily_summaries(start, end)
            summaries = summaries.get("data", []) if isinstance(summaries, dict) else summaries

            print("  sleep (chunked)")
            sleep = _fetch_chunked(client, client.get_daily_sleep_summaries, start, end, "sleep")

            print("  body battery (chunked)")
            body_battery = _fetch_chunked(
                client, client.get_daily_body_battery, start, end, "bodyBattery"
            )

            print("  hrv")
            hrv = client.get_daily_hrv_summaries(start, end)
            hrv = hrv.get("hrvSummaries", []) if isinstance(hrv, dict) else (hrv or [])

            print("  weight (chunked)")
            weight = _fetch_chunked(client, client.get_weight_measurements, start, end, "weight")
            complete = True
    except GarminApiError as exc:
        complete = False
        print("\n  stopped early: %s" % str(exc)[:200])
        print("  writing what was fetched -- re-run to fill the rest")

    # bodyBatteryValuesArray is a packed intraday series the dashboard doesn't
    # plot; daily charged/drained is what we keep.
    body_battery = [
        {k: v for k, v in day.items() if k != "bodyBatteryValuesArray"} for day in body_battery
    ]

    bundle = {
        "fetched_through": end.isoformat(),
        "range_start": start.isoformat(),
        "measurement_system": (profile or {}).get("measurementSystem", "metric"),
        "first_day_of_week": ((profile or {}).get("firstDayOfWeek") or {}).get("dayName"),
        "activities": activities,
        "daily_summaries": summaries,
        "sleep": sleep,
        "body_battery": body_battery,
        "hrv": hrv,
        "weight": weight,
    }

    # A partial bundle must never clobber a good one -- it lands beside it.
    out_path = DATA_DIR / "garmin.json"
    if not complete and out_path.exists():
        out_path = DATA_DIR / "garmin.partial.json"
    out_path.write_text(json.dumps(bundle, indent=1))

    print("\nWrote %s" % out_path)
    for key in ("activities", "daily_summaries", "sleep", "body_battery", "hrv", "weight"):
        print("  %-16s %d records" % (key, len(bundle[key])))


if __name__ == "__main__":
    main()
