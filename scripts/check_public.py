#!/usr/bin/env python3
"""Ищет общие признаки утечек в файлах, которые попадут в публичный репозиторий.

Проверяются только универсальные признаки, без бренда и реальных ID:
- токены Telegram-бота вида ``bot<digits>:<token>``;
- блоки приватных ключей;
- файлы ``.env`` (кроме ``.env.example``);
- абсолютные пути к домашним и серверным каталогам.

Запуск: ``python scripts/check_public.py [путь]``.
Без пути сканируются файлы из git-индекса, приватные материалы проекта исключены.
С путём сканируется всё дерево (кроме ``.git``, кэшей, симлинков и бинарников) —
это режим проверки собранного публичного экспорта.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Приватные материалы, которых нет в публичном экспорте.
PRIVATE_PATHS = (
    ".superpowers/",
    "docs/runbooks/",
    "docs/superpowers/",
    "docs/ai-workflow.md",
)

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}

# Шаблоны собираются из частей, чтобы скрипт не находил сам себя.
TELEGRAM_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")
PRIVATE_KEY = re.compile(
    "-----BEGIN "
    + r"(?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?"
    + "PRIVATE KEY-----"
)
ABSOLUTE_PATHS = (
    ("абсолютный путь: Users", re.compile("/" + "Users/")),
    ("абсолютный путь: home", re.compile("/" + "home/")),
    ("абсолютный путь: srv", re.compile("/" + "srv/")),
)


def _git_files() -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    names = [name for name in result.stdout.decode("utf-8", "replace").split("\0") if name]
    return [ROOT / name for name in names]


def _walk_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _is_private(relative: str) -> bool:
    return any(
        relative == prefix.rstrip("/") or relative.startswith(prefix) for prefix in PRIVATE_PATHS
    )


def _is_env_file(path: Path) -> bool:
    name = path.name
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan(path: Path, relative: str) -> list[str]:
    problems: list[str] = []
    if _is_env_file(path):
        problems.append(f"{relative}: файл .env")

    text = _read_text(path)
    if text is None:
        return problems

    for number, line in enumerate(text.splitlines(), 1):
        if TELEGRAM_TOKEN.search(line):
            problems.append(f"{relative}:{number}: токен Telegram-бота")
        if PRIVATE_KEY.search(line):
            problems.append(f"{relative}:{number}: приватный ключ")
        for label, pattern in ABSOLUTE_PATHS:
            if pattern.search(line):
                problems.append(f"{relative}:{number}: {label}")
    return problems


def _select_files(target: Path) -> list[Path] | None:
    if target.is_file():
        return [target]
    if not target.is_dir():
        return None
    if target == ROOT:
        files = _git_files()
        if files is not None:
            return files
    return list(_walk_files(target))


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    files = _select_files(target)
    if files is None:
        print(f"Нет такого пути: {target}", file=sys.stderr)
        return 2

    root_mode = target == ROOT
    single_file = target.is_file()
    problems: list[str] = []
    checked = 0
    for path in files:
        if single_file:
            relative = path.name
        else:
            try:
                relative = path.relative_to(target).as_posix()
            except ValueError:
                if root_mode:
                    continue
                relative = path.name
        if root_mode and _is_private(relative):
            continue
        checked += 1
        problems.extend(_scan(path, relative))

    if problems:
        print("Найдены признаки утечек:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"OK: признаков утечек нет (проверено {checked} файлов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
