# One image for laptop, on-prem and cloud. Build from the src/ directory:
#   docker build -t agentboard src/
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/agentboard.db \
    AGENT_MANIFEST_DIR=/app/catalog

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agentboard ./agentboard
COPY catalog ./catalog
COPY demo.py ./

RUN useradd -r -u 10001 board && mkdir -p /data && chown board /data
USER board
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["uvicorn", "agentboard.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
