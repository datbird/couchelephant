FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COUCHELEPHANT_DB=/data/couchelephant.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
EXPOSE 8710

# One process. The sync loop runs as a FastAPI background task rather than a
# second container, because it and the web UI share one SQLite file and
# splitting them buys nothing but a locking problem.
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8710", "--log-level", "info"]
