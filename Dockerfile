# ---------------------------------------------------------------------------
# Autonomous SOP Generation Engine - offline container image
# The image contains no API keys and makes no outbound calls at runtime;
# it talks only to an Ollama daemon reachable on the local network.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_DIR=/tmp/sop_engine

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# app/static carries the self-contained web application served at "/".
COPY tests ./tests

# From inside a container, localhost is the container itself. Point at the
# host daemon by default; override with -e OLLAMA_HOST=... when needed.
ENV OLLAMA_HOST=http://host.docker.internal:11434 \
    MODEL_NAME=deepseek-r1:8b

RUN mkdir -p /tmp/sop_engine && \
    useradd --create-home --uid 10001 sop && \
    chown -R sop:sop /tmp/sop_engine /app
USER sop

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
