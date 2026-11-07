# Wolvesville Bot Farm Enterprise Container
FROM python:3.12-slim

WORKDIR /app

# Zero external pip dependencies - pure standard library runtime
COPY wvbot /app/wvbot
COPY run.py /app/run.py
COPY tools /app/tools

ENV PYTHONUNBUFFERED=1
ENV STATE_DIR=/data

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/status', timeout=5)"

ENTRYPOINT ["python", "run.py", "--mode", "farm"]
