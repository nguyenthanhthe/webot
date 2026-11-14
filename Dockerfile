# Wolvesville Bot Farm Enterprise Container
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    STATE_DIR=/data

RUN groupadd --system wvbot \
    && useradd --system --gid wvbot --create-home --home-dir /home/wvbot wvbot \
    && mkdir -p /app /data \
    && chown -R wvbot:wvbot /app /data

WORKDIR /app

# Zero external pip dependencies - pure standard library runtime
COPY --chown=wvbot:wvbot wvbot /app/wvbot
COPY --chown=wvbot:wvbot run.py /app/run.py
COPY --chown=wvbot:wvbot setcreds.py /app/setcreds.py

USER wvbot

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5)"

ENTRYPOINT ["python", "run.py"]
CMD ["--mode", "farm"]
