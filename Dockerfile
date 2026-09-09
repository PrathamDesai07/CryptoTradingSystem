FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENVIRONMENT=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    OPEN_BROWSER_ON_START=false \
    STATE_DATABASE_PATH=/app/data/trading_state.db

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir --requirement backend/requirements.txt \
    && addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY --chown=app:app backend backend
COPY --chown=app:app frontend frontend
COPY --chown=app:app config.yaml config.yaml

USER app
EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python", "backend/run.py"]
