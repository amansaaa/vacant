FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# Task 4 will define the entrypoint
CMD ["python", "-m", "vacant_scraper"]
