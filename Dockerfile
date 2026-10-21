FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app
COPY watcher.py .

# utente non privilegiato; /data e' il volume dove viene salvato l'ultimo stato
RUN useradd --system --uid 10001 watcher \
    && mkdir /data \
    && chown watcher /data
USER watcher

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=3)"]

CMD ["python", "/app/watcher.py"]
