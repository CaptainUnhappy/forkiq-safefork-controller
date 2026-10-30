FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home forkiq \
    && mkdir /var/lib/forkiq && chown 10001:10001 /var/lib/forkiq
WORKDIR /opt/forkiq
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY LICENSE ./LICENSE
ENV FORKIQ_STATE_DIR=/var/lib/forkiq PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8792
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8792/healthz',timeout=3)); assert r['status']=='ok'"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8792", "--workers", "1"]
