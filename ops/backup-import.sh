#!/usr/bin/env bash
# Восстановление данных клуба из папки ops/backup-export.sh: база, приватный каталог, сервисы.
# Запуск: ops/backup-import.sh <папка> [db] — с db только база, приватный каталог не трогает.
# CLUB_PRIVATE_DIR необязателен: без него или если каталога нет на диске — восстанавливается только база.
# Файлы проверяются до остановки сервисов; подтверждение спрашивает вызывающий (backup-services).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRIVATE_DIR="${CLUB_PRIVATE_DIR:-}"

log() { echo "$(date '+%F %T') $*"; }

import_check() { # <папка> [db]: нужные файлы на месте, иначе ошибка до остановки сервисов
  local dir=$1
  local -a files=(db.dump)
  # private.tgz обязателен, только если приватный каталог задан, существует и режим не db
  if [ "${2:-}" != db ] && [ -n "$PRIVATE_DIR" ] && [ -d "$PRIVATE_DIR" ]; then
    files+=(private.tgz)
  fi
  local f
  for f in "${files[@]}"; do
    if [ ! -s "$dir/$f" ]; then
      echo "Нет $dir/$f" >&2
      return 1
    fi
  done
}

import_db() { # <папка>: база из <папка>/db.dump поверх текущей
  docker compose exec -T db \
    sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$1/db.dump"
}

import_private() { # <папка>: очистить приватный каталог и распаковать в него private.tgz
  mkdir -p "$PRIVATE_DIR"
  rm -rf "${PRIVATE_DIR:?}"/* "${PRIVATE_DIR:?}"/.[!.]*
  tar xzf "$1/private.tgz" -C "$PRIVATE_DIR"
}

main() { # <папка> [db]: восстановить данные клуба
  local dir=${1:?Укажите папку: ops/backup-import.sh <папка> [db]}
  case ${2:-} in
    ''|db) ;;
    *) echo "Режим: пусто или db, получено: $2" >&2; return 2 ;;
  esac
  case $dir in /*) ;; *) dir="$PWD/$dir" ;; esac
  import_check "$dir" "${2:-}"
  # rm -rf внутри приватного каталога: корень файловой системы не принимаем
  if [ "${2:-}" != db ] && [ -n "$PRIVATE_DIR" ] && [ -d "$PRIVATE_DIR" ] && [ "$PRIVATE_DIR" = / ]; then
    echo "CLUB_PRIVATE_DIR не должен быть /" >&2
    return 1
  fi
  cd "$ROOT_DIR"
  log "stop app"
  docker compose stop web bot billing heartbeat
  log "restore database"
  import_db "$dir"
  # приватный каталог не задан или не существует — восстанавливаем только базу.
  # private.tgz гарантирован import_check при заданном и существующем каталоге.
  if [ "${2:-}" != db ] && [ -n "$PRIVATE_DIR" ] && [ -d "$PRIVATE_DIR" ]; then
    log "restore private"
    import_private "$dir"
  fi
  log "start app"
  docker compose up -d
  log "restored: $dir"
}

if [ "${BACKUP_IMPORT_LIB:-0}" != 1 ]; then
  main "$@"
fi
