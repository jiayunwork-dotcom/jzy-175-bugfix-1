FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SITING_DATA_DIR=/data

WORKDIR /app

# Runtime dependencies first (better layer caching).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# A single curl-free healthcheck using the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import json,urllib.request,sys; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'))['status']=='ok' else 1)"

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Single container serves the whole HTTP API; SQLite lives under /data.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
