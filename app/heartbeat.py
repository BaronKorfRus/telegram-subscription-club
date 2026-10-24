import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

POLL_SECONDS = 60
DEFAULT_HEALTHCHECK_URL = "http://web:8000/healthz"


def _heartbeat_payload() -> dict:
    return {"service": "club"}


def send_heartbeat() -> bool:
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        logger.warning("HEARTBEAT_URL не задан, хартбит пропущен")
        return False
    token = os.environ.get("HEARTBEAT_TOKEN", "").strip()
    headers = {}
    if token:
        headers["X-Heartbeat-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.post(url, headers=headers, json=_heartbeat_payload(), timeout=10)
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Не удалось отправить хартбит")
        return False


def check_health() -> bool:
    url = os.environ.get("HEALTHCHECK_URL", DEFAULT_HEALTHCHECK_URL)
    try:
        response = httpx.get(url, timeout=5)
    except httpx.HTTPError:
        logger.exception("Локальный healthz недоступен")
        return False
    if response.status_code != 200:
        logger.error("Локальный healthz вернул %s", response.status_code)
        return False
    return True


def run_once() -> bool:
    if not check_health():
        return False
    return send_heartbeat()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("heartbeat started, every %ss", POLL_SECONDS)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Ошибка цикла хартбита")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
