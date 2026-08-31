# Vacant

Find the quietest buildings at UofT St. George — the ones with the fewest classes
scheduled right now — so you can walk over and find somewhere to sit.

> **"No class scheduled"** — not "empty." We know what the timetable knows, nothing more.

## What it does

Every class at St. George is published with a building, a day and a time. Flip that
around and you know where classes *aren't*. Vacant ranks buildings by how little is
scheduled in them at a given moment, sorted by how far you'd have to walk.

For each building it shows:

- **Load** — classes running now, as a share of the most that building ever runs at
  once. This is what makes the number meaningful: 3 classes in a building that peaks
  at 12 is quiet; 3 in one that peaks at 4 is not.
- **Estimated people** — summed enrolment of the classes running. An estimate from
  registration, not attendance.

### Why buildings and not rooms

The original plan was to name the exact free room. **UofT's Timetable Builder doesn't
publish room numbers** — measured at 0 of 2,429 meetings, across every division, and
every other public UofT timetable source is now offline. It does publish the building,
so that's the honest unit of the answer.

The measurements behind that decision are in [`docs/ttb-api.md`](docs/ttb-api.md).
Room coverage is re-checked on every scrape; if it ever appears, the database schema
is already shaped to store it.

### What it can't tell you

- Which specific room is free
- Whether a door is unlocked
- Club events, seminars, midterms, or makeup lectures — those live in a different
  system entirely

## Status

🚧 Under construction. Tasks 1–3 complete: repo scaffold, Postgres, and a tested
Timetable Builder client.

## Local development

### Prerequisites

Docker and Docker Compose.

### Database (Task 2)

```bash
cp .env.example .env
docker compose up -d db
```

Check it came up healthy:

```bash
docker compose ps
# NAME         SERVICE   STATUS
# vacant-db-1  db        Up (healthy)
```

Connect with `psql`:

```bash
docker compose exec db psql -U vacant -d vacant
```

Postgres is also published on `localhost:5432`, so a host-installed `psql` works too:

```bash
psql postgresql://vacant:vacant-local@localhost:5432/vacant
```

Data lives in the named volume `pgdata`. `docker compose down` stops the container and
keeps the data; only `docker compose down -v` deletes it.

### Scraper (Task 3 onward)

```bash
cd scraper
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -e '.[dev]'

.venv/bin/python -m pytest -q                          # 29 tests, no network needed
.venv/bin/python -m vacant_scraper.probe --pages 2     # measure the live API
```

The tests run entirely against recorded fixtures, so they're fast, deterministic, and
put no load on UofT's servers.

### Everything else

```bash
# docker compose up            (after API + web are built)
```

## Architecture

```
ttb.utoronto.ca  →  scraper  →  data/*.json (committed)  →  Postgres  →  FastAPI  →  Next.js
```

The committed JSON in between the scraper and the database is deliberate: it's an audit
trail (git diffs show the timetable settling week by week), it lets the loader be tested
offline, and it stops a bad scrape from silently emptying the database.

**Your location never leaves your browser.** The API returns activity for every
building and the client does the distance sorting — which also means every user gets
the same response at a given minute, so it caches perfectly.
