FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# ENTRYPOINT (not CMD) so `docker compose run --rm scraper --division APSC`
# appends flags instead of replacing the whole command — `run` overrides CMD
# entirely, but only appends to ENTRYPOINT.
ENTRYPOINT ["python", "-m", "vacant_scraper"]
CMD []
