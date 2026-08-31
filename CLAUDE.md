# Vacant

Find classrooms at UofT St. George with no class currently scheduled in them.

This file is the project charter. It records what we're building, what we decided,
why, and the order to build it in. Read it before starting any task.

---

## 1. Purpose

**The problem.** A student between classes wants somewhere to sit and work. Libraries
are full. There are hundreds of lecture halls and tutorial rooms sitting empty at any
given moment, and no easy way to find out which.

**The insight.** If you know when every class meets and where, then "this room is free"
is the complement of "a class is scheduled here." The timetable already publishes the
positive case. We compute the negative.

**The product.** A website. It asks for your location, and shows you nearby rooms with
no scheduled class, sorted by distance, each showing how long it stays free.

### Non-goals

Explicitly out of scope. Do not build these, and do not add abstractions in
anticipation of them.

- Room booking or reservations
- User accounts, auth, or login
- Real-time occupancy (cameras, sensors, crowd-sourced check-ins)
- Any campus other than St. George
- Mobile native apps
- Notifications

### What "empty" honestly means

We know only what the timetable knows. Specifically we **cannot** see:

- Club events, departmental seminars, midterms, makeup lectures (these go through
  Campus Events, a separate system)
- Whether a door is actually unlocked
- Last-minute cancellations or room changes since the last scrape

**Therefore the UI must say "no class scheduled," never "empty."** Same data, and the
honest phrasing doesn't generate a complaint when someone walks into a seminar. This
is a hard rule, not a preference. It applies to every string in the interface.

---

## 2. Constraints

These drove every decision below. When a future choice is unclear, re-derive it from
here rather than reaching for what's popular.

| Constraint | Value |
|---|---|
| Team | One person. No reviewer, no one to inherit the weird parts. |
| Experience | Comfortable in Python and JS/TS as languages. **First full-stack project.** |
| Purpose | Portfolio piece for internship applications |
| Scale | ~100 users, bursty (everyone at 10:50, nobody at 3am) |
| Latency | Sub-second perceived load. Data may be a day stale by design. |
| Budget | Small VPS, a few dollars a month |
| Deployment | Docker on a VPS, managed by Coolify |
| Timezone | America/Toronto throughout |

**The most important one is "first full-stack project."** Every abstraction added is
another layer that can't be debugged yet. When in doubt, choose the option with fewer
moving parts, even if it's less impressive on paper.

**On "scalable."** 100 users will not teach scaling and nothing here is load-bearing.
What this project does teach is API design, data modeling, migrations, idempotent
loading, caching, and CI. Those are real. Describe the project as **robust**, not
scalable — it's more accurate and it survives follow-up questions.

---

## 3. Data source

### Primary: UofT Timetable Builder

`https://ttb.utoronto.ca` — a client-side app whose backend serves JSON. The relevant
call is a **POST** to the EASI-hosted `getPageableCourses` endpoint. Covers Arts &
Science, Applied Science & Engineering, Music, and Daniels in one place.

**Confirmed:** room data is present in the bulk search response. Each course carries a
`meetingTimes` array; each entry has a `building` object:

```json
"building": {
  "buildingCode": "WW",
  "buildingRoomNumber": "",
  "buildingRoomSuffix": "",
  "buildingUrl": "https://map.utoronto.ca/?id=1809#!m/494547",
  "buildingName": null
}
```

This means **no per-course detail scraper is needed.** Bulk requests only.

**Two notes carried forward:**

1. `buildingName` is null and `buildingRoomNumber` was empty in the sample we
   inspected. **This is expected and is not a problem.** See "TBA and refresh" below.
2. `buildingUrl` embeds a `map.utoronto.ca` id (`?id=1809`). Use this as the join key
   to building reference data. Never join on name strings.

### TBA and refresh

St. George is large and a meaningful share of sections carry no room assignment early
in the year. Room numbers get filled in progressively as the registrar finalises
assignments, and continue shifting through the first weeks of each term.

**This does not block the project.** Enough sections have firm rooms and times to make
the map and the list genuinely useful from day one, and coverage improves on its own as
the term approaches. The correct response to a TBA is to skip that meeting and move on,
not to stop.

Consequences that shape the build:

- **The dataset is not static.** Re-scraping through the term is the mechanism by which
  the data gets better. The nightly job isn't just guarding against staleness — early
  in the year it's actively filling the dataset in.
- **Expect the meeting count to grow, sometimes sharply**, in the weeks before and
  after each term starts. Growth is healthy. A sudden *drop* is the failure signal.
  The loader's guard is asymmetric for this reason (§8, Task 9).
- **This makes the committed JSON history more interesting, not less.** Diffing
  `data/meetings.json` week over week shows room assignments resolving in real time.
  That's a genuinely unusual thing to have in a portfolio project.

### Secondary: map.utoronto.ca

Source for building names, short codes, latitude/longitude, address, and per-weekday
operating hours. Scrape **once**, commit the result, hand-correct the ~40 St. George
buildings that matter. This data does not change; do not build a refresh pipeline for
it.

### Fallback (do not build unless Task 3 fails)

The Arts & Science timetable API at `timetable.iit.artsci.utoronto.ca/api` may still be
live (`/api/orgs`, `/api/{session}/courses?org={code}`). A&S coverage only. Test it
only if TTB turns out to be unusable.

### Scraping etiquette

- Pull once daily. Never proxy live user requests to UofT.
- Set a real User-Agent identifying the project with contact info.
- Back off on errors. Do not hammer on failure.
- The A&S host disallows crawlers in robots.txt. Behave accordingly if used.

---

## 4. Tech stack and rationale

One line each on what it beat and why.

| Layer | Choice | Over | Because |
|---|---|---|---|
| Scraper | **Python** | Node/TS | Near coin-flip. The scraper is the most breakage-prone part, so it belongs in the language we debug fastest. |
| Backend | **FastAPI** | Django, Flask | Django's value is auth/admin/forms — none needed. Flask means hand-writing the validation and API docs FastAPI generates. `/docs` is a clickable portfolio artifact. |
| DB access | **`psycopg` raw SQL + Alembic** | SQLAlchemy ORM | 5 tables, ~4 queries, no writes from the app. An ORM here is mostly ceremony between us and a bug we can't yet diagnose. SQL outlives any ORM. Alembic stays — versioned migrations are the real skill. |
| Database | **PostgreSQL** | SQLite | Honestly close to arbitrary at 40k read-only rows; SQLite would work forever. Chosen because it's what production runs, dev/prod parity matters most on a first full-stack project, and it's the name on job postings. |
| Frontend | **Next.js, `output: 'export'`** | Vanilla JS, plain React | Vanilla means hand-rolling re-renders across location + time + filter state. Static export because data comes from FastAPI — no SSR needed, and no deployment friction. |
| Map | **Custom SVG + hand-rolled projection** | Leaflet, Mapbox, Google | Campus is 1km × 1.5km with 40 fixed buildings — an illustration with coordinates, not a map problem. No keys, no attribution, no tile policy. Lets us colour buildings by free-room count, which a tile layer can't. Most distinctive thing in the project. |
| Containers | **Docker + Compose** | Local installs | Dev environment and VPS become identical. Kills the largest category of first-deploy bugs. |
| Scheduling | **cron on the VPS** | Celery, queues | One job, once a day. Celery manages many jobs with retries and workers; here it'd be infrastructure with nothing to do. |
| Hosting | **VPS + Coolify** | Render, Vercel, Fly | Always-on containers, no cold starts, full control, a few dollars a month. Coolify supplies reverse proxy and TLS. |

### Deliberately absent

**Redis, Celery, nginx configs, Kubernetes, GraphQL, an ORM, a CSS framework beyond
whatever ships with the Next.js template.**

Every one of these gets suggested by some tutorial. At 100 users on a read-only
dataset, caching is a Python dict keyed by the current minute. An interviewer who sees
Redis in front of a 40k-row read-only table will ask why, and "I wanted to learn Redis"
is a worse answer than not having it.

**Do not add a dependency without a written reason in this file.**

---

## 5. Architecture

### Data flow

```
  ┌─────────────────────────────────────────────────────────┐
  │  NIGHTLY (cron → docker compose run --rm scraper)       │
  │                                                          │
  │  ttb.utoronto.ca                                         │
  │        │  POST getPageableCourses, per division          │
  │        ▼                                                 │
  │  scraper/raw/*.json          (gitignored, ephemeral)     │
  │        │  normalize + parse rooms + drop non-StG         │
  │        ▼                                                 │
  │  data/meetings.json          (COMMITTED to repo)         │
  │  data/rooms.json             (COMMITTED to repo)         │
  │        │  load into staging → sanity check → swap        │
  │        ▼                                                 │
  │  PostgreSQL                                              │
  └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              FastAPI  ──── reads Postgres, caches in-process
                            │
                            ▼
              Next.js  ──── static, fetches API, sorts by distance
                            │
                            ▼
                         Browser
```

### Why the JSON artifact exists even though we have Postgres

It is not the database. It's an intermediate stage, kept for three reasons:

1. **Audit trail.** Git diffs on `meetings.json` show exactly which rooms got
   reassigned in week two of September. Nobody else's project has this.
2. **Testable loading.** The loader can run against a committed fixture without
   touching the network.
3. **Blast shield.** If TTB changes shape and the scraper returns 40 courses instead of
   4,000, the loader refuses to run rather than silently emptying the database.

### The location decision

**The user's location never goes to the API.**

The server computes vacancy and returns *all* free rooms with their `free_until` times.
The browser does distance sorting.

Consequence: the response is identical for every user at a given minute, so it's
cacheable. During the 10:50 rush, the origin is hit roughly once a minute; everyone
else is served from cache. It also means no user location data is ever transmitted or
logged, which is a privacy property worth stating in the README.

### Repo layout

```
vacant/
├── CLAUDE.md
├── README.md
├── docker-compose.yml
├── docker-compose.prod.yml
├── .env.example
├── data/                    # committed reference + normalized output
│   ├── buildings.json
│   ├── terms.json
│   ├── blackouts.json
│   ├── rooms.json
│   └── meetings.json
├── scraper/
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── src/vacant_scraper/
├── api/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/
│   └── src/vacant_api/
└── web/
    ├── Dockerfile
    ├── package.json
    └── src/
```

---

## 6. Data model

```sql
buildings (
  code           TEXT PRIMARY KEY,      -- 'WW', 'BA', 'MY'
  map_id         INTEGER UNIQUE,        -- from buildingUrl ?id=
  name           TEXT NOT NULL,
  lat            DOUBLE PRECISION NOT NULL,
  lng            DOUBLE PRECISION NOT NULL,
  address        TEXT,
  hours          JSONB NOT NULL         -- {mon: {open: 480, close: 1320}, ...}
)

rooms (
  id             SERIAL PRIMARY KEY,
  building_code  TEXT NOT NULL REFERENCES buildings(code),
  room_number    TEXT NOT NULL,
  capacity       INTEGER,               -- nullable; store if TTB exposes it
  UNIQUE (building_code, room_number)
)

terms (
  code           TEXT PRIMARY KEY,      -- '20269'
  name           TEXT NOT NULL,         -- 'Fall 2026'
  start_date     DATE NOT NULL,
  end_date       DATE NOT NULL
)

blackout_dates (
  term_code      TEXT NOT NULL REFERENCES terms(code),
  date           DATE NOT NULL,
  reason         TEXT NOT NULL,         -- 'Reading week', 'Thanksgiving'
  PRIMARY KEY (term_code, date)
)

meetings (
  id             SERIAL PRIMARY KEY,
  term_code      TEXT NOT NULL REFERENCES terms(code),
  room_id        INTEGER NOT NULL REFERENCES rooms(id),
  course_code    TEXT NOT NULL,         -- 'CSC148H1'
  section        TEXT NOT NULL,         -- 'LEC0101'
  day_of_week    SMALLINT NOT NULL,     -- 0 = Monday
  start_min      SMALLINT NOT NULL,     -- minutes since midnight
  end_min        SMALLINT NOT NULL
)

CREATE INDEX idx_meetings_lookup ON meetings (term_code, day_of_week, room_id, start_min);
```

**Times are integer minutes since midnight, not timestamps.** This makes the interval
math trivial and sidesteps an entire class of timezone bugs. All times are
America/Toronto wall-clock.

`day_of_week` is 0-indexed from Monday. Write this down once and never guess.

---

## 7. API contract

Versioned under `/api/v1`.

```
GET  /health
     → 200 {"status": "ok"}

GET  /api/v1/meta
     → {"last_ingest_at": "...", "term": "20269", "room_count": 412,
        "meeting_count": 38104, "is_blackout": false, "blackout_reason": null}

GET  /api/v1/buildings
     → [{"code": "BA", "name": "Bahen Centre", "lat": ..., "lng": ...,
         "hours": {...}}, ...]

GET  /api/v1/rooms/free?at=<iso8601>&min_minutes=30
     → {"at": "...", "rooms": [
          {"room_id": 88, "building_code": "BA", "room_number": "1180",
           "capacity": 250, "free_until": "16:00", "free_minutes": 95,
           "building_open": true}, ...]}
```

**No location parameter.** See §5.

`Cache-Control: public, max-age=60` on `/rooms/free`.

`/meta` matters more than it looks: it powers the "data updated N hours ago" line in
the UI, and it's how we notice the scraper has been silently dead for a week.

---

## 8. Behaviour rules

Decisions already made. Implement them; don't relitigate them mid-task.

**Vacancy**

- Minimum gap defaults to **30 minutes**, user-adjustable. A room free for 8 minutes is
  noise.
- Every room shows **how long it stays free** — the time the next scheduled class
  starts, not just a boolean.
- A room with nothing left scheduled shows "free until \<building close time\>", not
  "free forever". Bounded by building hours.

**Time**

- Time picker defaults to now, user can query a future time.
- The client sends `at`. The server never trusts its own clock for "now".
- All times America/Toronto.

**Calendar**

- Blackout dates (reading weeks, holidays, exam periods, term gaps) live in
  `data/blackouts.json`, hand-maintained, one edit per term.
- During a blackout the UI shows a banner explaining that the normal weekly schedule
  doesn't apply. It does **not** silently render a full green list.

**Buildings**

- Building hours are **advisory, not a hard filter.** Closed buildings appear dimmed
  with a note. Scraped hours will sometimes be wrong, and hiding a real available room
  is worse than showing a caveat.
- Distance is **straight-line**, not walking distance. No routing service, and at this
  scale the difference is noise.

**Data quality**

- The room-string parser **logs every dropped and unmatched entry.** TBA drops are
  expected and counted separately from parse failures — conflating them hides real
  bugs. If parse failures number 4, fine. If 400, there's a bug, and without the log
  we'd never know.
- **The loader's guard is asymmetric.** Refuse to run if the meeting count *drops* more
  than 20% from the previous load — that's data loss. Allow growth without limit;
  that's TBAs resolving, and it will happen in large jumps around term start.
- Coverage (how many sections have a real room versus TBA) is recorded on every load
  and exposed via `/api/v1/meta`. It's a health metric, not an error.

**Copy**

- "No class scheduled." Never "empty." Applies to every user-facing string.

---

## 9. Working agreement

**Task granularity.** Each task in §10 is sized to be one `git add / commit / push`.
Finish the whole task, then stop. The human reviews and commits.

**Who writes what.** The agent writes everything, implementation and tests both.

Three pieces still deserve extra care because they're where silent bugs live and
they're what gets asked about in interviews — the loader's staging swap, the vacancy
interval math, and the map projection. For these, write the tests first, make the
reasoning explicit in comments, and explain the approach in the task summary so it can
be reviewed properly rather than skimmed.

**Rules**

- Do not add a dependency that isn't in §4 without asking first.
- Do not build anything from the Non-goals list.
- If a task turns out to be blocked by something upstream, stop and say so rather than
  inventing a workaround.
- If real data contradicts something in this file, stop and flag it. Update this file
  before continuing.
- Prefer boring, readable code over clever code. This is a learning project.

---

## 10. Tasks

Nineteen tasks, ordered. Each is one commit — a coherent unit of working, tested code,
not a fragment. Do not skip ahead. Task 6 is the highest-bug-density work; everything
after it is known-solvable.

### Phase 0 — Foundation

**Task 1 — Repo scaffold**
Directory structure per §5. `README.md` stub, `.gitignore` (include `scraper/raw/`,
`.env`, `__pycache__`, `node_modules`, `.next`), `.env.example`, this `CLAUDE.md` at
root. Python projects use `pyproject.toml`. No code yet.
*Done when:* `tree` matches §5 and `git status` is clean apart from intended files.

**Task 2 — Postgres in Compose**
`docker-compose.yml` with only the `db` service: `postgres:17`, named volume (not a
bind mount — `docker compose down` must not destroy data), healthcheck using
`pg_isready`, env vars from `.env`. Document the `psql` connect command in the README.
*Done when:* `docker compose up -d db` succeeds, healthcheck reports healthy, and you
can `psql` in and create a throwaway table.

### Phase 1 — Data

**Task 3 — TTB API client and probe**
A reusable client module plus a probe command. The client handles the POST to
`getPageableCourses`, pagination, retries with exponential backoff, and an identifying
User-Agent. The probe runs it against two or three divisions and writes findings to
`docs/ttb-api.md`: exact URL, required headers, request body shape, response shape,
pagination mechanism, session/term parameter format, and a sample payload.

Also record a coverage baseline: across all `meetingTimes` returned, the percentage with
a non-empty `buildingRoomNumber`, broken down by division. **This is a baseline to
compare against later in the year, not a gate.** A low number early is expected and is
not a reason to stop or to escalate.

Include unit tests for pagination and retry behaviour against recorded fixtures, so the
client is testable without hitting UofT.
*Done when:* `docs/ttb-api.md` has real measured numbers, the client is importable by
Task 4, and its tests pass offline.

**Task 4 — Full scrape job**
Using the Task 3 client: enumerate every division, fetch all of them for the 2026–27
sessions, rate-limit between requests, and write raw JSON per division to
`scraper/raw/` (gitignored). CLI with `--session` and `--division` flags for partial
re-runs. Structured logging with per-division counts and a run summary.

One-shot job, not a service — in Compose under `profiles: ["jobs"]`, run via
`docker compose run --rm scraper`. Resumable: a failure partway through should not
require re-fetching divisions already written.
*Done when:* a full run completes, the raw files contain every division, and killing it
partway then re-running picks up where it left off.

**Task 5 — Building reference data**
One-time extract from `map.utoronto.ca` → `data/buildings.json`: code, map id, name,
lat, lng, address, per-weekday hours as minutes-since-midnight. Filter to St. George.
Commit the output. The extraction script is disposable; the JSON is the artifact.
*Done when:* `data/buildings.json` has ~40 St. George buildings with plausible
coordinates, spot-checked against the real map.

**Task 6 — Normalizer and room parser** ⚠️ *the highest-bug-density task*
Raw JSON → `data/rooms.json` + `data/meetings.json`.

Handles: joining `building.buildingUrl` map id to `buildings.json`; assembling room
numbers from `buildingRoomNumber` + `buildingRoomSuffix`; day-of-week mapping;
`HH:MM` → minutes since midnight; skipping online/async entries; skipping
non-St-George; skipping TBA rooms.

**Categorised drop log**, written to `data/normalize-report.json` and printed as a
summary. Categories must be separate, not lumped together:

- `tba_room` — expected, will be large early in the year, not an error
- `online_or_async` — expected
- `not_st_george` — expected
- `unknown_building_code` — **a real problem**, investigate
- `unparseable_time` — **a real problem**, investigate

Write unit tests for the parser against a fixture covering each category, including the
malformed cases.
*Done when:* both JSON files are committed, tests pass, and the report shows the two
problem categories at counts you can explain.

**Task 7 — Terms and blackout dates**
Hand-author `data/terms.json` (2026–27 Fall and Winter: codes, start, end) and
`data/blackouts.json` (reading weeks, statutory holidays, exam periods, the December
and April gaps) from the official sessional dates. Human-maintained, one edit per term.
*Done when:* both files are committed and a comment in each names the source URL.

### Phase 2 — Database

**Task 8 — Schema and migrations**
Alembic set up in `api/`. Initial migration creating the §6 schema with all constraints
and the index. Verify `upgrade` and `downgrade` both run cleanly.
*Done when:* `alembic upgrade head` then `downgrade base` then `upgrade head` all
succeed against the Compose database.

**Task 9 — Loader**
Reads the committed JSON, loads into staging tables, sanity-checks, swaps into place in
a single transaction. Must be **idempotent** — running twice produces the same state.

Guard is **asymmetric**: abort if the meeting count drops more than 20% from the
previous load, but allow unlimited growth. Early in the year, TBA rooms resolve and the
count jumps; that's the system working. A drop means the scrape or parse broke.
`--force` flag to override for legitimate term transitions.

Also records a load-run row: timestamp, meeting count, room count, TBA coverage
percentage. This is what `/api/v1/meta` reads.

Write the transactional swap carefully and explain the isolation reasoning in comments.
Tests: idempotency, guard triggering on a synthetic drop, guard allowing a synthetic
jump, and rollback leaving the previous data intact when the swap fails partway.
*Done when:* all tests pass, and running the loader twice leaves the database byte-identical.

### Phase 3 — API

**Task 10 — FastAPI skeleton**
App structure, config from env, `psycopg` connection pool with proper lifespan
management, `/health`, `/api/v1/meta`, Dockerfile, `api` service in Compose with
`depends_on: {db: {condition: service_healthy}}` — plain `depends_on` will cause
confusing crash loops.
*Done when:* `docker compose up` brings up db + api, `/health` returns 200, and
`/docs` renders.

**Task 11 — Vacancy logic**
Given a timestamp and a minimum gap: resolve the term, check blackouts, derive weekday
and minutes-since-midnight, find rooms with no overlapping meeting, compute `free_until`
from the next meeting's start bounded by building close time.

Tests first, and cover all of: back-to-back classes with no gap; a gap shorter than the
minimum; a room with nothing left scheduled today; a room with nothing scheduled all
week; blackout dates; weekends; times before opening and after closing; midnight
boundaries; a query time falling exactly on a class start and exactly on a class end.

The off-by-one cases at interval boundaries are where this will break. Be explicit in
comments about whether intervals are half-open, and be consistent.
*Done when:* every test passes and the boundary convention is documented in the module
docstring.

**Task 12 — Vacancy endpoints**
`GET /api/v1/rooms/free` and `GET /api/v1/buildings`. Pydantic response models.
`Cache-Control: public, max-age=60`. In-process cache keyed by `(minute, min_minutes)`.
CORS configured for the web origin — this will be the first confusing error, so
document the setting in the README.
*Done when:* both endpoints return correct data for several hand-checked times, and
`/docs` shows accurate schemas.

### Phase 4 — Frontend

**Task 13 — Next.js scaffold**
`create-next-app` with TypeScript, `output: 'export'` in config, typed API client,
`NEXT_PUBLIC_API_URL` from env, Dockerfile, `web` service in Compose.
*Done when:* the built static site loads and successfully fetches `/api/v1/meta` from
the running API.

**Task 14 — Room list view**
The core screen. Time picker (defaults to now), minimum-gap filter (defaults to 30),
room list grouped by building showing room number, capacity if known, and free-until.
Loading and empty states. Copy per §8 — "no class scheduled."
*Done when:* the list is usable end to end against real data with no map and no
geolocation.

**Task 15 — Location and distance**
Browser geolocation with permission prompt, straight-line distance to each building,
distance-sorted list. **Fallback: a building picker**, used when permission is denied,
geolocation fails, or accuracy is poor. Treat the picker as a first-class path, not an
error state — a meaningful share of users will land there.
*Done when:* both paths work, and denying permission produces a usable app rather than
a dead end.

**Task 16 — Map projection and render**
A standalone projection module converting lat/lng to SVG coordinates. Equirectangular
with longitude correction:

```
x = (lng - lng₀) × cos(lat₀)
y = -(lat - lat₀)
```

then scale and translate to the viewBox. At Toronto's latitude the correction is ~0.72
and the error across campus is centimetres. Tests assert that known buildings land in
correct relative positions and that the campus corners produce a sane bounding box.

Then the map itself: building markers, colour-coded by free-room count, with labels and
the user's location dot through the same projection. No pan or zoom — the whole campus
fits on one screen, and skipping gestures removes most of the fiddly work. Clicking a
building filters the list.
*Done when:* projection tests pass, the map renders recognizably against the real
campus, and it stays in sync with the list.

**Task 17 — Polish and honesty surfaces**
Blackout banner. "Data updated N hours ago" from `/meta`, with a visible warning past 48
hours. Dimmed closed buildings. Empty states. A coverage note where relevant — early in
the term, plenty of sections are still TBA, and saying so is better than implying the
room list is exhaustive. An "about" section stating plainly what the data can't see,
per §1.
*Done when:* every §8 behaviour rule is visibly implemented.

### Phase 5 — Ship

**Task 18 — Production setup**
`docker-compose.prod.yml`, Coolify deployment notes, cron entry chaining scrape →
normalize → load, backup strategy for the Postgres volume, and a documented manual-run
procedure for when the nightly job fails.

Nightly is the default cadence. Note in the docs that the first weeks of each term are
when the data changes most (TBAs resolving, rooms reassigned), so that's the period to
actually watch the logs rather than assume it's working.
*Done when:* a fresh clone plus the documented steps produces a running stack.

**Task 19 — README**
The portfolio artifact. Cover: the problem, the complement insight, architecture
diagram, why there's no location in the API request, why the loader swap is
transactional, the honest limitations, and local setup instructions. Someone should be
able to read it in five minutes and understand both what it does and why it's built
this way.
*Done when:* a stranger could clone, run, and explain the project from the README
alone.

---

## 11. Decision log

Changes to any decision above get appended here with a date and a reason. If real data
contradicts this document, update the document.

| Date | Decision | Reason |
|---|---|---|
| — | Postgres over SQLite | Close to arbitrary at this scale; chosen for dev/prod parity and learning value, not performance |
| — | Custom SVG over Leaflet | Fixed 40-building campus is an illustration, not a map problem; enables data-driven building colouring |
| — | Location stays client-side | Makes API responses cacheable; also a genuine privacy property |
| — | JSON artifact kept despite having Postgres | Audit trail, offline-testable loader, blast shield against bad scrapes |
| — | TBA rooms are expected, not a blocker | St. George assigns rooms progressively; enough sections have firm rooms to be useful from day one, and coverage improves on its own |
| — | Loader guard is asymmetric | Growth is TBAs resolving and is healthy; only a drop indicates data loss |
| — | Agent implements everything | Chosen for velocity; the three delicate pieces get tests-first treatment and explicit review instead of a human handoff |
