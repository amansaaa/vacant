# Vacant

Find classrooms at UofT St. George with no class currently scheduled in them.

> **"No class scheduled"** — not "empty." We know what the timetable knows, nothing more.

## Status

🚧 Under construction.

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

### Everything else

```bash
# docker compose up            (after API + web are built)
```

## Architecture

See [CLAUDE.md](CLAUDE.md) for the full project charter, decisions, and task list.
