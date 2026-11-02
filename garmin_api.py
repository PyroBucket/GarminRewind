"""Thin client for the Garmin Connect Personal API.

Garmin publishes an official client, but it requires Python >=3.12. This module
targets the same endpoints over plain `requests` so the project runs on 3.9+.
Endpoint paths follow garmin/connect-personal-api-python (Apache-2.0).

Auth model: the long-lived personal API key is never sent to data endpoints.
It is exchanged once for a bearer token that is valid 24h, with no refresh
tokens. We cache that token on disk so repeated runs don't re-exchange.
"""

import json
import os
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

ROOT = Path(__file__).parent

TOKEN_TTL_SECONDS = 24 * 60 * 60
REFRESH_MARGIN_SECONDS = 15 * 60
TIMEOUT = 30

# The Personal API rate-limits well below what a tight loop issues. Space
# requests out rather than discovering the limit and backing off from it;
# raise GARMIN_MIN_INTERVAL if 429s still show up.
MIN_REQUEST_INTERVAL = float(os.getenv("GARMIN_MIN_INTERVAL", "0.35"))
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0
RETRY_STATUSES = (429, 500, 502, 503, 504)


class GarminApiError(RuntimeError):
    pass


def _iso(value):
    """Format a date as YYYY-MM-DD; strings pass through unchanged."""
    return value.isoformat() if isinstance(value, date) else value


# Which env file to read. Override with use_env_file() or $GARMIN_ENV_FILE when
# juggling more than one key (a fresh one alongside an expiring one, say).
ENV_FILE = Path(os.getenv("GARMIN_ENV_FILE") or (ROOT / ".env"))


def _token_cache():
    """One cache per env file, so swapping keys never reuses a stale token."""
    stem = ENV_FILE.name
    suffix = "" if stem == ".env" else "." + stem.lstrip(".")
    return ROOT / (".access_token%s.json" % suffix)


def use_env_file(path):
    """Point credential loading at a different env file for this process."""
    global ENV_FILE
    ENV_FILE = Path(path)
    if not ENV_FILE.is_absolute():
        ENV_FILE = ROOT / ENV_FILE
    if not ENV_FILE.exists():
        raise GarminApiError("No such env file: %s" % ENV_FILE)
    return ENV_FILE


def get_api_key():
    """Read the personal API key from the active env file (or the environment)."""
    # override=True so a second env file wins over one already loaded
    load_dotenv(ENV_FILE, override=True)
    key = os.getenv("GARMIN_PERSONAL_API_KEY", "").strip()
    if not key or key == "your-personal-api-key-here":
        raise GarminApiError(
            "GARMIN_PERSONAL_API_KEY is not set in %s.\n"
            "Copy .env.example to .env and paste your key from\n"
            "https://connect.garmin.com/app/settings/personalApiToken" % ENV_FILE.name
        )
    return key


def _read_cached_token():
    cache = _token_cache()
    if not cache.exists():
        return None
    try:
        cached = json.loads(cache.read_text())
    except (ValueError, OSError):
        return None
    if time.time() >= cached.get("expires_at", 0):
        return None
    return cached.get("access_token")


def get_access_token(force_refresh=False):
    """Return a valid bearer token, exchanging the API key only when needed."""
    if not force_refresh:
        cached = _read_cached_token()
        if cached:
            return cached

    response = requests.post(
        BASE_URL + TOKEN_PATH,
        headers={"Authorization": "ApiKey " + get_api_key()},
        timeout=TIMEOUT,
    )
    if not response.ok:
        raise GarminApiError(
            "Token exchange failed (HTTP %s). The key may be expired or revoked;\n"
            "create a new one at https://connect.garmin.com/app/settings/personalApiToken\n"
            "%s" % (response.status_code, response.text[:300])
        )

    # The endpoint returns the bare JWT as text/plain, not JSON.
    token = response.text.strip()
    if not token:
        raise GarminApiError("Token exchange returned an empty body")

    _token_cache().write_text(
        json.dumps(
            {
                "access_token": token,
                "expires_at": time.time() + TOKEN_TTL_SECONDS - REFRESH_MARGIN_SECONDS,
            }
        )
    )
    return token


class GarminClient:
    """Read-only access to the Personal API."""

    def __init__(self, token=None):
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": "Bearer " + (token or get_access_token()),
            }
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.session.close()

    def _throttled_get(self, path, params):
        """GET with a minimum gap since the last request.

        Returns None on a transport failure (timeout, dropped connection) so the
        caller can treat it like a retryable status rather than a fatal error.
        """
        wait = self._last_request_at + MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self.session.get(BASE_URL + path, params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            print("      %s on %s" % (type(exc).__name__, path.rsplit("/", 1)[-1]))
            return None
        finally:
            self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_delay(response, attempt):
        """Honour Retry-After when the server sends one, else back off."""
        header = response.headers.get("Retry-After", "")
        try:
            return max(0.0, float(header))
        except ValueError:
            return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))

    def get(self, path, params=None):
        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._throttled_get(path, params)

            if response is None:
                if attempt == MAX_ATTEMPTS:
                    raise GarminApiError(
                        "GET %s failed to reach the server after %d attempts"
                        % (path, MAX_ATTEMPTS)
                    )
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and not refreshed:
                # Cached token went stale early -- exchange once more and retry.
                refreshed = True
                self.session.headers["Authorization"] = "Bearer " + get_access_token(
                    force_refresh=True
                )
                continue

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                delay = self._retry_delay(response, attempt)
                print(
                    "      HTTP %s on %s -- retrying in %.0fs (attempt %d/%d)"
                    % (response.status_code, path.rsplit("/", 1)[-1], delay,
                       attempt, MAX_ATTEMPTS)
                )
                time.sleep(delay)
                continue

            if not response.ok:
                raise GarminApiError(
                    "GET %s -> HTTP %s: %s" % (path, response.status_code, response.text[:300])
                )
            return response.json() if response.content else None

        raise GarminApiError("GET %s gave up after %d attempts" % (path, MAX_ATTEMPTS))

    # --- profile ---------------------------------------------------------
    def get_user_profile_settings(self):
        return self.get("/userprofile-service/userprofile/settings")

    def get_heart_rate_zones(self):
        return self.get("/biometric-service/heartRateZones/v2")

    def get_fitness_age(self, calendar_date):
        return self.get("/fitnessage-service/fitnessage/v2/%s" % _iso(calendar_date))

    # --- activities ------------------------------------------------------
    def search_activities(self, start=0, limit=20, start_date=None, end_date=None,
                          activity_type=None):
        params = {"start": start, "limit": limit}
        if start_date is not None:
            params["startDate"] = _iso(start_date)
        if end_date is not None:
            params["endDate"] = _iso(end_date)
        if activity_type is not None:
            params["activityType"] = activity_type
        return self.get("/activitylist-service/activities/search/activities", params)

    def get_activity(self, activity_id):
        return self.get("/activity-service/activity/%s" % activity_id)

    # --- daily wellness (date range) -------------------------------------
    def get_daily_summaries(self, start_date, end_date):
        return self.get(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": _iso(start_date), "endDate": _iso(end_date)},
        )

    def get_daily_steps(self, start_date, end_date):
        return self.get(
            "/usersummary-service/stats/steps/daily/%s/%s" % (_iso(start_date), _iso(end_date))
        )

    def get_daily_stress(self, start_date, end_date):
        return self.get(
            "/usersummary-service/stats/stress/daily/%s/%s" % (_iso(start_date), _iso(end_date))
        )

    def get_daily_sleep_summaries(self, start_date, end_date):
        return self.get(
            "/sleep-service/sleep/dailySleepSummariesByDate",
            {"startDate": _iso(start_date), "endDate": _iso(end_date)},
        )

    def get_daily_body_battery(self, start_date, end_date):
        return self.get(
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": _iso(start_date), "endDate": _iso(end_date)},
        )

    def get_daily_hrv_summaries(self, start_date, end_date):
        return self.get(
            "/hrv-service/hrv/daily/%s/%s" % (_iso(start_date), _iso(end_date))
        )

    def get_weight_measurements(self, start_date, end_date):
        return self.get(
            "/weight-service/weight/range/%s/%s" % (_iso(start_date), _iso(end_date)),
            {"includeAll": "true"},
        )

    # --- daily wellness (single date) ------------------------------------
    def get_daily_sleep(self, calendar_date):
        return self.get("/sleep-service/sleep/dailySleep", {"date": _iso(calendar_date)})

    def get_daily_hrv(self, calendar_date):
        return self.get("/hrv-service/hrv/%s" % _iso(calendar_date))

    def get_daily_heart_rate(self, calendar_date):
        return self.get(
            "/wellness-service/wellness/dailyHeartRate", {"date": _iso(calendar_date)}
        )
