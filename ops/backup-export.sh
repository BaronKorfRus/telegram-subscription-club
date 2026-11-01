#!/usr/bin/env bash
# Выгрузка данных клуба для backup-services: дамп базы и приватный каталог.
# Запуск: ops/backup-export.sh <папка> [db] — с db только db.dump (часовая копия), private не трогает.
# CLUB_PRIVATE_DIR необязателен: без него контент остаётся в репозитории и не архивируется.
# Папку создаёт вызывающий; здесь mkdir -p на случай ручного запуска. Не шифрует и не чистит старое.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRIVATE_DIR="${CLUB_PRIVATE_DIR:-}"

log() { echo "$(date '+%F %T') $*"; }

export_db() { # <папка>: дамп базы в <папка>/db.dump; пустой — ошибка
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$1/db.dump"
  if [ ! -s "$1/db.dump" ]; then
    echo "Пустой дамп базы: $1/db.dump" >&2
    return 1
  fi
}

export_private() { # <папка>: архив приватного каталога; каталог не задан или пуст — пропуск без ошибки
  if [ -z "$PRIVATE_DIR" ]; then
    echo "private не архивируется: контент в репозитории" >&2
    return 0
  fi
  if [ ! -d "$PRIVATE_DIR" ]; then
    echo "Нет каталога $PRIVATE_DIR — private.tgz пропущен" >&2
    return 0
  fi
  log "archive private"
  tar czf "$1/private.tgz" -C "$PRIVATE_DIR" .
}

main() { # <папка> [db]: выгрузить данные клуба в папку
  local dir=${1:?Укажите папку: ops/backup-export.sh <папка> [db]}
  case ${2:-} in
    ''|db) ;;
    *) echo "Режим: пусто или db, получено: $2" >&2; return 2 ;;
  esac
  case $dir in /*) ;; *) dir="$PWD/$dir" ;; esac
  cd "$ROOT_DIR"
  mkdir -p "$dir"
  log "dump database"
  export_db "$dir"
  if [ "${2:-}" != db ]; then
    export_private "$dir"
  fi
  log "export ready: $dir"
}

if [ "${BACKUP_EXPORT_LIB:-0}" != 1 ]; then
  main "$@"
fi
