# TTB API — measured behaviour

Everything here was measured against the live endpoint on **2026-08-30** by
`python -m vacant_scraper.probe --pages 8`. Raw counts are in
`docs/ttb-probe.json`; one full course object is in
`docs/ttb-sample-course.json`.

**Room-number coverage is 0%, and that changed the product.** TTB publishes
building, day, time and enrolment — but never a room number. The project is now
building-level: rank buildings by how little is scheduled in them, rather than
naming a free room. See [Room coverage](#room-coverage) and
[Is the building-level metric computable?](#is-the-building-level-metric-computable)
below, and CLAUDE.md §1.

Three other things measured here contradicted CLAUDE.md and are corrected in it:
the day/time encoding, the `buildingUrl` join key, and the dead fallback host.

---

## Endpoint

```
POST https://api.easi.utoronto.ca/ttb/getPageableCourses
```

Required headers:

| Header | Value | Why |
|---|---|---|
| `Accept` | `application/json` | **Required.** Omit it and the server returns **XML** with HTTP 200 — a confusing failure, since the status code says success. |
| `Content-Type` | `application/json` | Body is JSON. |
| `User-Agent` | ours | Etiquette; any value is accepted. |

**No authentication, no cookies, no CSRF token.** `Origin` and `Referer` are not
required. The browser sends them; the server does not care.

## Request body

The full filter shape, exactly as TTB's own frontend sends it (see
`build_request_body` in `scraper/src/vacant_scraper/ttb.py`):

```json
{
  "courseCodeAndTitleProps": {"courseCode": "", "courseTitle": "", "courseSectionCode": ""},
  "departmentProps": [], "campuses": ["St. George"],
  "sessions": ["20269", "20271", "20269-20271"],
  "requirementProps": [], "instructor": "", "courseLevels": [],
  "deliveryModes": [], "dayPreferences": [], "timePreferences": [],
  "divisions": ["ARTSC"], "creditWeights": [],
  "availableSpace": false, "waitListable": false,
  "page": 1, "pageSize": 20, "direction": "asc"
}
```

Empty lists/strings mean "no filter on this facet". Whether they can be omitted
is untested — we send the full shape rather than guess.

### Sessions

`20269` = Fall 2026, `20271` = Winter 2027. `20269-20271` is a **pseudo-session
for full-year (Y) courses**; all three must be requested together for complete
coverage. Requesting a past year (`20259`/`20261`) returns `payload: null` —
only the current cycle is served.

### Divisions

Six St. George divisions. Their totals sum exactly to the 4,847 returned when
all six are requested at once, so **divisions partition the result set cleanly**
— per-division scraping loses nothing, and gives Task 4 its resumability unit.

| Division | Name | Courses |
|---|---|---:|
| `ARTSC` | Arts and Science | 3,758 |
| `APSC` | Applied Science and Engineering | 507 |
| `MUSIC` | Music | 348 |
| `ARCLA` | Architecture, Landscape, and Design (Daniels) | 120 |
| `FPEH` | Kinesiology and Physical Education | 93 |
| `FIS` | Information | 21 |
| | **Total** | **4,847** |

## Pagination

**`pageSize` in the request body is ignored.** Requests with `pageSize` of 5,
500 and 2000 all returned exactly 20 courses and echoed `"pageSize": 20`.

Pagination is therefore driven entirely by `page` (1-indexed) and the `total`
in the response:

```
last_page = ceil(total / 20)
```

ARTSC alone is 188 pages; all six divisions is 243 requests. At a 1s delay
that's a ~4 minute full scrape.

## Response shape

```
payload.pageableCourse.courses[]   — the page of courses
payload.pageableCourse.total       — full result count (not per page)
payload.pageableCourse.page        — echoes the requested page
payload.pageableCourse.pageSize    — always 20
payload.divisionalLegends          — big HTML blobs, ignored
payload.divisionalEnrolmentIndicators — ignored
status[]                           — [{"code": 0, "message": "Success"}]
```

A non-zero `status[].code` means failure. `payload` can be `null` (e.g. an
unavailable session) while `status` still says Success, so both must be checked.

### Course → section → meeting

```
course.code           "CSC148H1"
course.campus         "St. George"   (all 714 sampled; the filter works)
course.sections[]
  .name               "LEC0101"      ← this is meetings.section
  .teachMethod        "LEC" | "TUT" | "PRA"
  .cancelInd          "Y" on cancelled sections (12/2,190 sampled) — skip these
  .maxEnrolment       section cap, NOT room capacity
  .deliveryModes[]    per session: {"session": "20269", "mode": "INPER"}
  .meetingTimes[]
     .start {day, millisofday}
     .end   {day, millisofday}
     .sessionCode     "20269" | "20271"
     .repetition      "WEEKLY" | "BI_WEEKLY"
     .building {buildingCode, buildingRoomNumber, buildingRoomSuffix, buildingUrl, buildingName}
```

### Days and times — **not `HH:MM` strings**

CLAUDE.md §10 Task 6 assumes `"HH:MM"`. The real encoding is:

- `start.day` — integer. Across 2,429 sampled meetings the only values were
  **1, 2, 3, 4, 5**. Never 0, never 6 or 7. So **1 = Monday … 5 = Friday**, and
  our 0-indexed `day_of_week = day - 1`.
- `start.millisofday` — milliseconds since midnight. **All 4,858 sampled
  start/end values landed exactly on a minute boundary**, so
  `minutes_since_midnight = millisofday // 60000` is lossless.
  (`36000000` → 600 → 10:00.)

### Full-year courses emit each meeting twice

A Y course carries one `meetingTimes` entry per session with identical times:
one with `sessionCode: "20269"`, one with `"20271"`. This is correct for our
model — each becomes its own `meetings` row under its own `term_code` — but a
naive dedupe on (room, day, time) would wrongly collapse them.

### `repetition: BI_WEEKLY` exists

83 of 2,429 sampled meetings (3.4%) are `BI_WEEKLY` rather than `WEEKLY`. The
response gives no phase information (which week of the pair), and `firstMeeting`
was `null` on every sampled section. **Not covered by CLAUDE.md.** Treating a
biweekly meeting as weekly is the conservative choice — it marks the room busy
on weeks it may be free, which under-reports availability rather than sending
someone into an occupied room.

### `building.buildingUrl` — the join key is NOT `?id=`

```json
"buildingUrl": "https://map.utoronto.ca/?id=1809#!m/494547"
```

CLAUDE.md §3 says to join on the `?id=` value. **That is wrong.** `?id=1809` is
the *campus* (St. George) and is byte-identical on all 48 distinct building
codes we saw. The per-building identifier is the number after `#!m/`
(`494547` = WW). Each of the 48 building codes mapped to exactly one marker id,
and no marker was shared between codes — so either field is a usable key, but
it has to be the marker, not `?id=`.

### Missing building codes

131 of 2,429 meetings (5.4%) have an empty `buildingCode`. These line up with
online delivery. Sampled `deliveryModes.mode` values:

| Mode | Count | Meaning |
|---|---:|---|
| `INPER` | 2,522 | In person |
| `SYNC` | 55 | Online, synchronous |
| `ASYNC` | 9 | Online, asynchronous |
| `HYBR` | 1 | Hybrid |

### No room capacity

Nothing in the response describes a room's capacity. `maxEnrolment` is a
*section* enrolment cap, which is a lower bound at best (a 30-person tutorial
can sit in a 200-seat hall). `rooms.capacity` in CLAUDE.md §6 has no source
here and should stay NULL unless we find one.

---

## Room coverage

This is the baseline CLAUDE.md Task 3 asks for. It is not the number anyone
expected.

Sampled 2026-08-30, 8 pages (160 courses) per division, all six divisions:

| Division | Courses sampled | Meetings | With building code | **With room number** |
|---|---:|---:|---:|---:|
| APSC | 160 | 968 | 968 | **0** |
| ARCLA | 120 | 363 | 340 | **0** |
| ARTSC | 160 | 417 | 388 | **0** |
| FIS | 21 | 52 | 41 | **0** |
| FPEH | 93 | 378 | 375 | **0** |
| MUSIC | 160 | 251 | 186 | **0** |
| **All** | **714** | **2,429** | **2,298 (94.6%)** | **0 (0.0%)** |

`buildingRoomSuffix` was likewise empty everywhere.

**Why this became a pivot rather than a wait.** The original reading was that
rooms are simply TBA this early and would fill in. Three measurements argue the
field is not merely pending:

1. It is empty for courses that certainly have rooms. `CSC148H1` Fall 2026
   returns `buildingCode: "MP"` for lectures and `"BA"` for tutorials — real
   buildings, correct ones — with `buildingRoomNumber: ""`.
2. It is empty at **UTSC** too (`SCAR`, 1,626 courses), so it is not a St.
   George registrar-timing issue.
3. There is no other endpoint. I pulled the full endpoint list out of TTB's
   JavaScript bundle: `getPageableCourses`, `getCoursesByCodeAndSectionCode`,
   `current-session`, `reference-data`. The per-course detail endpoint returns
   no rooms either. And every documented alternative source is gone from DNS —
   `timetable.iit.artsci.utoronto.ca`, `timetable.artsci.utoronto.ca`,
   `coursefinder.utoronto.ca`, `planner.utoronto.ca`. The Cobalt open-data API
   redirects to a GitHub repo whose scraper targets the same dead host.

The field is still tracked on every run. If it moves off zero, room-level
features become possible again and the schema is already shaped to receive them
(`rooms` table kept, `meetings.room_id` nullable).

| Date | Meetings sampled | With room number | Coverage |
|---|---:|---:|---:|
| 2026-08-30 | 2,429 | 0 | 0.0% |

## Is the building-level metric computable?

Yes. Measured on the same sample, and this is the evidence the pivot rests on.

**Enrolment: 100% coverage.** All 2,190 sampled sections carry
`currentEnrolment`, so the estimated-headcount figure is available for every
class. Nothing in the design depends on a field that might be missing.

**Concurrency separates quiet buildings from small ones.** For each building we
compute `peak_concurrent` — the most simultaneous meetings it reaches anywhere
in the week — with a sweep line over start/end events. Load is then
`classes_now / peak_concurrent`. A sample at Monday 10:30, the 10:50 rush:

| Building | Classes now | Peak concurrent | Load | Est. people |
|---|---:|---:|---:|---:|
| MS | 0 | 7 | 0% | 0 |
| GB | 3 | 12 | 25% | 137 |
| MY | 4 | 11 | 36% | 163 |
| SF | 6 | 10 | 60% | 390 |
| BA | 9 | 16 | 56% | 604 |

Without the peak, "3 classes in Galbraith" is meaningless. With it, 3 of a peak
12 is plainly quiet, while 3 in a building that peaks at 4 is plainly busy.

**Caveats, both real:**

- `peak_concurrent` is a **lower bound** on capacity, not a room count. A
  building may own rooms that are never busy at the same moment. These figures
  come from 8 pages per division; a full scrape is 243 pages, so the peaks will
  rise and the load percentages will fall.
- **Low-peak buildings are noisy.** A building that peaks at 1 can only ever
  read 0% or 100%. CLAUDE.md §8 therefore requires a "limited data" marker
  below a peak of 3, so a single seminar room cannot top or bottom the ranking.

## Fallback source is dead

CLAUDE.md §3 lists `timetable.iit.artsci.utoronto.ca/api` as the fallback if TTB
turns out to be unusable. **That hostname no longer resolves in DNS**
(`Could not resolve host`) — not a firewall or robots issue, the host is gone.
`coursefinder.utoronto.ca` does not resolve either, and TTB exposes no sibling
`getSessions`/`getOrgs` endpoints (both 404).

TTB is usable, so this costs us nothing today. It is recorded so nobody plans
around a fallback that isn't there.

## Reproducing

```bash
cd scraper
uv venv --python 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q              # 29 offline tests, no network
.venv/bin/python -m vacant_scraper.probe --pages 8 --delay 0.7
```

Machine-readable output lands in `docs/ttb-probe.json`, including the
`building_activity` block that backs the table above.
