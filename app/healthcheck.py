"""Проверка живости сервиса для Docker healthcheck.

Использование: python -m app.healthcheck bot|billing
Код возврата 0 — сервис живой, 1 — отметка устарела или отсутствует.
"""

from __future__ import annotations

import argparse
import sys

from app.db import SessionLocal
from app.services import health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка живости фонового сервиса")
    parser.add_argument("name", choices=health.SERVICES)
    args = parser.parse_args(argv)
    try:
        with SessionLocal() as session:
            services = health.status(session)
    except Exception as exc:  # база недоступна — тоже нездоров
        print(f"healthcheck {args.name}: {type(exc).__name__}: {exc}")
        return 1
    item = services.get(args.name, {"ok": False, "age_sec": None})
    print(f"healthcheck {args.name}: ok={item['ok']} age_sec={item['age_sec']}")
    return 0 if item["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
