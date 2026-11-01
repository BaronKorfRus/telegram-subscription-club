FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install --no-cache-dir .

# Т-Банк использует Russian Trusted Root CA; добавляем его и в certifi (httpx).
COPY ops/russian_trusted_root_ca.pem /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
RUN update-ca-certificates \
    && cat /usr/local/share/ca-certificates/russian_trusted_root_ca.crt >> "$(python -c 'import certifi; print(certifi.where())')"

USER app

EXPOSE 8000

CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
