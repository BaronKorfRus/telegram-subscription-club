import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CADDY = ROOT / "ops" / "club.caddy"
EXCLUDED = {APP / "cohort.py"}

# Общие признаки, а не боевые значения: этот файл едет в публичный репозиторий.
FORBIDDEN = (
    (re.compile(r"(?<![\w-])-100\d{9,}"), "Telegram ID канала/чата"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "токен бота"),
)


def test_no_telegram_ids_or_tokens_in_app():
    problems = []
    for path in APP.rglob("*.py"):
        if path in EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, label in FORBIDDEN:
            if pattern.search(text):
                problems.append(f"{path.relative_to(ROOT)}: {label}")

    assert not problems, "\n".join(problems)


def test_caddy_site_address_uses_domain_from_env():
    lines = CADDY.read_text(encoding="utf-8").splitlines()
    first_line = next(line for line in lines if line.strip())

    assert first_line.strip() == "{$DOMAIN} {"
