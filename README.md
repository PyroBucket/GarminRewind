# Garmin dashboard

A small local project that pulls your own Garmin Connect data through the
**Personal API** and renders it as a self-contained HTML dashboard.

Nothing leaves your machine: the fetcher writes a JSON cache to `data/`, and
the dashboard is a single file in `out/`. Both are gitignored.

## Setup

1. Create a personal API key at
   [Garmin Connect → Settings → Personal API Key](https://connect.garmin.com/app/settings/personalApiToken).
   Garmin shows the key **once**. Pick an expiry (30 / 90 / 180 / 365 days).

2. Copy the template and paste the key in with an editor:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Install dependencies:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

## Use

```powershell
.venv\Scripts\python.exe fetch.py          # pull 365 days into data/garmin.json
.venv\Scripts\python.exe dashboard.py      # build both pages and open Rewind
```

`fetch.py --days 90` pulls a shorter window. Re-run `dashboard.py` alone to
rebuild the pages from the cache without touching the API — useful when editing
charts, since access tokens only last 24h.

## What's on the pages

`dashboard.py` writes two self-contained files that link to each other:

| Page | What it is |
|---|---|
| `out/rewind.html` | **Garmin Rewind** — your year as a story, opened by default |
| `out/charts.html` | **The full numbers** — every chart, in detail |

Each page carries only the data it draws, so neither file ships the other's
payload.

### Garmin Rewind

Your headline distance · top sports ranked by moving time · the hour of day you
actually train, with an archetype ("After-work athlete") and your earliest and
latest starts · daily steps against Garmin's own moving goal, month by month ·
how far all those steps actually took you, against a ladder of real distances ·
how far up the stairs and hills took you, against a ladder of real heights ·
your busiest weekday · month by month, with your biggest month and biggest week
· weekly intensity minutes against the 150-a-week public health target · what
each session did to you, banded on Garmin's own 0–5 aerobic training-effect
scale · your running mechanics, if the year had enough running in it · the
year's totals restated as things you can picture (marathons, Everests, pizzas,
litres of sweat, days asleep) · how the year felt, in Garmin's own verdict on
each day · average bedtime and wake time on a 24-hour clock face · active days,
longest streak, longest break · whether the heavy training weeks cost you
anything overnight · record sessions (fastest kilometre and mile, furthest,
hardest, highest heart rate) · a closing "training personality" badge built
from the labels Garmin itself puts on your sessions and days.

**You don't have to be a runner.** Steps, stairs and daily stress are counted
whether or not you record a workout, so the three panels built on them carry
the deck on their own: a year with no sessions in it at all still renders,
leading with steps instead of distance, and the training panels simply drop out
one by one. Every panel is independently optional — the deck is only skipped
when the window contains nothing at all.

Three of those panels make claims that are easy to get wrong, so they are built
to be falsifiable. The intensity-minutes target is the only figure in the deck
measured against something outside you, and weeks the window only partly covers
are drawn but never counted as a miss. The big-weeks-versus-quiet-weeks panel
writes its own headline from whichever way the data actually goes — including
"nothing happened", which is the usual answer — and calls a gap real only once
it clears the week-to-week spread of the measure itself. The running-form panel
reports the drift between the two halves of the year and then names the
treadmill share in each, because a change of venue moves all four numbers on
its own.

The two landmark ladders are picked, not listed: neighbouring rungs restate
each other ("2.1 Kilimanjaros" and "2.5 Mont Blancs" is one fact twice), so a
rung has to be at least three times smaller than the last one shown, and the
first landmark you *haven't* cleared is always included because it is the only
one with somewhere left to go. The Moon stays prose rather than becoming a
rung — a year of walking is a fraction of a percent of the way there, and
"0.8% of the way" reads as a failure where "124 years at this pace" reads as
the joke it is. "Your busiest month" needs 20 days on record before a month
can win it, or the two-day stub at the end of a rolling window takes it every
time.

Each panel carries its own colours and stays the same in light and dark mode —
only the page around them follows the theme. Generated squiggles drift behind
the whole story at their own rates, a progress bar tracks how far through you
are, and each panel fades in with its contents staggered behind it while its
numbers count up and its bars grow. All of the motion is off under
`prefers-reduced-motion`.

**Instagram export.** Every panel has a **Save** button that writes it out as a
PNG card — 1080 × 1350 for a feed post, or 1080 × 1920 for a story; the button
in the header switches between the two, and **Save all cards** walks the whole
story in order so the set drops straight into a carousel. Each card carries the
masthead, the date range and a position indicator, and is rasterised at 2× so
the big type stays crisp. Each panel is fitted to its card rather than dropped
into it: one with more copy than fits is laid out wider and scaled down, one
with room to spare — most of them, on a 9:16 story — is laid out narrower and
scaled up, so the type grows into the space instead of floating in it. On a
phone the share sheet opens instead of a download, which hands the image
directly to Instagram.

The export uses nothing but the browser: the panel is cloned into a fixed-size
card, serialised into an SVG `<foreignObject>` and painted onto a canvas. No
library and no network request, so the page still works straight off the
filesystem. Cards come out identical whether you export from a phone or a
desktop — the card pins its own type scale rather than inheriting the viewport's.

### The full numbers

Distance total and personal bests · 30-day stat tiles with year-long sparklines
(resting HR, sleep score, HRV, steps, stress, Body Battery) · training calendar
heatmap · weekly volume · sport mix · resting-HR trend · overnight HRV against
your balanced range · sleep stages and score · Body Battery charge vs drain ·
daily steps against goal · time in heart-rate zones · running pace trend ·
self-rated effort and feel against the time of day you trained.

Every chart has hover tooltips; the denser ones have a "Table view" toggle. No
squiggles here — this is the sober page. Both pages follow your OS light/dark
setting and have a manual toggle.

Note that the window is a rolling twelve months ending on your last fetch, not a
calendar year — the copy says "your last 12 months" because that's what it is.

## How it works

| File | Role |
|---|---|
| `garmin_api.py` | Thin Personal API client — token exchange, caching, endpoints |
| `fetch.py` | Pulls a year of data into `data/garmin.json` |
| `dashboard.py` | Computes every series and every Rewind stat, injects each page's slice |
| `template_rewind.html` | The story panels, the squiggles and the scroll behaviour |
| `template_charts.html` | Chart layout plus a small inline SVG chart toolkit |

`dashboard.py` does all the arithmetic and the templates only draw what they are
handed — so chart and panel work can iterate against the JSON cache without ever
re-hitting the API, which matters because access tokens only last 24 hours.

Each template carries a `/*__PAYLOAD__*/null` marker that the build replaces
with JSON. `dashboard.py` fails loudly if a template loses it, because the page
would otherwise load with `DATA === null` and render blank.

### Authentication

The personal API key is never sent to data endpoints. It is exchanged once at
`/personalapitoken-service/personal-api/personal-api-key/token` for a bearer
token valid for 24 hours — there are no refresh tokens. `garmin_api.py` caches
that token in `.access_token.json` (gitignored) and re-exchanges on expiry or a
401.

### Endpoint range caps

Measured against the live API; `fetch.py` chunks around them:

| Endpoint | Max range |
|---|---|
| `usersummary-service/v2/dailysummaries` | 365 days |
| `hrv-service/hrv/daily` | 365 days |
| `sleep-service/.../dailySleepSummariesByDate` | 31 days |
| `wellness-service/.../bodyBattery/reports/daily` | 31 days |
| `usersummary-service/stats/steps` and `/stress` | 30 days (unused — daily summaries carry both) |
| `activitylist-service/.../search/activities` | 99 per page, `start` offsets |
| `activity-service/activity/{id}` | one activity per request |

The per-activity detail call is the slow part of a fetch: the list endpoint
omits the two self-rated scores (RPE and "how did that feel?"), so `fetch.py`
asks for each activity's `summaryDTO` separately and keeps only those two
numbers — one request per activity, so a year of training costs roughly a
minute. A failed detail call is reported and skipped, not fatal.

### Why not Garmin's official client

Garmin publishes [`connect-personal-api-python`](https://github.com/garmin/connect-personal-api-python),
but it requires Python ≥ 3.12. `garmin_api.py` targets the same endpoint paths
over `requests` so this runs on 3.9+. If you're on 3.12 or newer you can swap it
out for the official package.

## Privacy

`fetch.py` strips GPS start/end coordinates and account identifiers (`ownerId`,
display name, profile image URLs) from cached activities — a 15-decimal-place
start latitude is your home address.

City-level location **is** kept: `locationName` ("Warsaw") stays in `data/`,
and activity names from the watch usually contain it too ("Warsaw Running"), so
dropping one field would not actually remove it. Bear that in mind before
sharing a screenshot.

`data/`, `out/`, `.env` and the token cache are all gitignored; only
`.env.example` is meant to be committed.

## Licence

[MIT](LICENSE). Garmin Connect and the Personal API are Garmin's; this project
is not affiliated with or endorsed by Garmin.
