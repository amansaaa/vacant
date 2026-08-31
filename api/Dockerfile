FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

EXPOSE 8000

# Task 10 will finalize this
CMD ["uvicorn", "vacant_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
