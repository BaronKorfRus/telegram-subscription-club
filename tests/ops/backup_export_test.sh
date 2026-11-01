#!/usr/bin/env bash
# Тесты ops/backup-export.sh и ops/backup-import.sh: без docker и сети.
# docker подменяется заглушкой, tar — обёрткой над настоящим tar; проверяются аргументы и порядок вызовов.
# Запуск: bash tests/ops/backup_export_test.sh
# shellcheck disable=SC1091  # скрипты подключаются по пути рядом с тестом
# shellcheck disable=SC2030,SC2031  # *_LIB выставляется в подоболочке намеренно: main при source не запускается
# shellcheck disable=SC2329  # docker/tar/log — заглушки, их вызывают скрипты после source
# shellcheck disable=SC2016  # $POSTGRES_* в ожидаемых строках буквальны: их раскрывает контейнер

set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd)

TMP=$(mktemp -d "${TMPDIR:-/tmp}/backup-export-test.XXXXXX") || exit 1
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); }

bad() { # <сообщение>
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1"
}

assert_eq() { # <имя> <ожидалось> <получено>
  if [ "$2" = "$3" ]; then ok; else bad "$1: ожидалось [$2], получено [$3]"; fi
}

assert_contains() { # <имя> <фрагмент> <текст>
  case "$3" in
    *"$2"*) ok ;;
    *) bad "$1: нет [$2] в [$3]" ;;
  esac
}

setup() { # чистая папка копии, приватный каталог и журнал вызовов для одной проверки
  R=$(mktemp -d "$TMP/case.XXXXXX") || exit 1
  DEST=$R/copy
  PRIV=$R/private
  CALLS=$R/calls.log
  ERR=$R/stderr
  : > "$CALLS"
  DUMP_BYTES=1
}

stubs() { # заглушки вместо docker и tar: вызовы в журнал, дамп и stdin дампа — в файлы
  docker() { # <аргументы docker>: pg_dump отдаёт дамп, pg_restore читает stdin
    printf 'docker %s\n' "$*" >> "$CALLS"
    case " $* " in
      *" pg_dump "*) [ "$DUMP_BYTES" = 0 ] || printf 'dump content' ;;
      *" pg_restore "*) cat > "$R/stdin.dump" ;;
    esac
    return 0
  }

  tar() { # <аргументы tar>: журнал и настоящий tar
    printf 'tar %s\n' "$*" >> "$CALLS"
    command tar "$@"
  }

  log() { :; }
}

run_export() { # <аргументы backup-export.sh>: main в подоболочке с заглушками
  (
    export BACKUP_EXPORT_LIB=1 CLUB_PRIVATE_DIR="$PRIV"
    # shellcheck disable=SC1091
    source "$HERE/../../ops/backup-export.sh"
    stubs
    main "$@"
  )
}

run_export_no_private() { # <аргументы backup-export.sh>: то же без CLUB_PRIVATE_DIR
  (
    export BACKUP_EXPORT_LIB=1
    unset CLUB_PRIVATE_DIR
    # shellcheck disable=SC1091
    source "$HERE/../../ops/backup-export.sh"
    stubs
    main "$@"
  )
}

run_import() { # <аргументы backup-import.sh>: main в подоболочке с заглушками
  (
    export BACKUP_IMPORT_LIB=1 CLUB_PRIVATE_DIR="$PRIV"
    # shellcheck disable=SC1091
    source "$HERE/../../ops/backup-import.sh"
    stubs
    main "$@"
  )
}

run_import_no_private() { # <аргументы backup-import.sh>: то же без CLUB_PRIVATE_DIR
  (
    export BACKUP_IMPORT_LIB=1
    unset CLUB_PRIVATE_DIR
    # shellcheck disable=SC1091
    source "$HERE/../../ops/backup-import.sh"
    stubs
    main "$@"
  )
}

dump_call='docker compose exec -T db sh -c pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'
restore_call='docker compose exec -T db sh -c pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner'

# --- Подключение ---------------------------------------------------------

printf '== подключение\n'

assert_eq "BACKUP_EXPORT_LIB=1: main не запускается при source" "function" \
  "$( ( export BACKUP_EXPORT_LIB=1; source "$HERE/../../ops/backup-export.sh"; type -t main ) 2>/dev/null )"
assert_eq "BACKUP_IMPORT_LIB=1: main не запускается при source" "function" \
  "$( ( export BACKUP_IMPORT_LIB=1; source "$HERE/../../ops/backup-import.sh"; type -t main ) 2>/dev/null )"

# --- export --------------------------------------------------------------

printf '\n== export\n'

setup
mkdir -p "$PRIV"
printf 'secret' > "$PRIV/data.txt"
run_export "$DEST" 2> "$ERR"; rc=$?
assert_eq "export: код 0" 0 "$rc"
assert_eq "export: вызовы и порядок" "$(printf '%s\n' "$dump_call" "tar czf $DEST/private.tgz -C $PRIV .")" \
  "$(cat "$CALLS")"
assert_eq "export: дамп непустой" "dump content" "$(cat "$DEST/db.dump")"
if [ -s "$DEST/private.tgz" ]; then ok; else bad "export: нет файла private.tgz"; fi
assert_contains "export: archive содержит данные" "data.txt" \
  "$(command tar tzf "$DEST/private.tgz" 2>/dev/null)"
assert_eq "export: файлов два" 2 "$(find "$DEST" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"

setup
mkdir -p "$PRIV"
run_export "$DEST" db 2> "$ERR"; rc=$?
assert_eq "export db: код 0" 0 "$rc"
assert_eq "export db: только дамп" "$dump_call" "$(cat "$CALLS")"
assert_eq "export db: файл один" "db.dump" "$(ls -1A "$DEST")"
assert_eq "export db: tar не вызывался" 0 "$(grep -c '^tar' "$CALLS")"

setup
run_export "$DEST" 2> "$ERR"; rc=$?
assert_eq "export без private: код 0" 0 "$rc"
assert_eq "export без private: только дамп" "$dump_call" "$(cat "$CALLS")"
assert_eq "export без private: файл один" "db.dump" "$(ls -1A "$DEST")"
assert_contains "export без private: сообщение в stderr" "Нет каталога $PRIV" "$(cat "$ERR")"

setup
run_export_no_private "$DEST" 2> "$ERR"; rc=$?
assert_eq "export без CLUB_PRIVATE_DIR: код 0" 0 "$rc"
assert_eq "export без CLUB_PRIVATE_DIR: только дамп" "$dump_call" "$(cat "$CALLS")"
assert_eq "export без CLUB_PRIVATE_DIR: файл один" "db.dump" "$(ls -1A "$DEST")"
assert_contains "export без CLUB_PRIVATE_DIR: сообщение в stderr" "private не архивируется" "$(cat "$ERR")"
assert_eq "export без CLUB_PRIVATE_DIR: tar не вызывался" 0 "$(grep -c '^tar' "$CALLS")"

setup
DUMP_BYTES=0
mkdir -p "$PRIV"
run_export "$DEST" 2> "$ERR"; rc=$?
assert_eq "export пустой дамп: код 1" 1 "$rc"
assert_contains "export пустой дамп: сообщение в stderr" "Пустой дамп базы" "$(cat "$ERR")"
assert_eq "export пустой дамп: private не архивировался" 0 "$(grep -c '^tar' "$CALLS")"

setup
run_export "$DEST" hourly 2> "$ERR"; rc=$?
assert_eq "export неизвестный режим: код 2" 2 "$rc"
assert_eq "export неизвестный режим: вызовов нет" "" "$(cat "$CALLS")"
assert_contains "export неизвестный режим: сообщение" "Режим" "$(cat "$ERR")"

setup
run_export 2> "$ERR"; rc=$?
assert_eq "export без аргумента: код 1" 1 "$rc"
assert_eq "export без аргумента: вызовов нет" "" "$(cat "$CALLS")"

# --- import --------------------------------------------------------------

printf '\n== import\n'

setup
run_import "$DEST" 2> "$ERR"; rc=$?
assert_eq "import без db.dump: код 1" 1 "$rc"
assert_contains "import без db.dump: сообщение" "Нет $DEST/db.dump" "$(cat "$ERR")"
assert_eq "import без db.dump: сервисы не останавливались" "" "$(cat "$CALLS")"

setup
mkdir -p "$DEST" "$PRIV"
printf 'dump content' > "$DEST/db.dump"
run_import "$DEST" 2> "$ERR"; rc=$?
assert_eq "import без private.tgz (каталог есть): код 1" 1 "$rc"
assert_contains "import без private.tgz: сообщение" "Нет $DEST/private.tgz" "$(cat "$ERR")"
assert_eq "import без private.tgz: сервисы не останавливались" "" "$(cat "$CALLS")"

setup
mkdir -p "$DEST"
printf 'dump content' > "$DEST/db.dump"
run_import "$DEST" 2> "$ERR"; rc=$?
assert_eq "import с заданным, но отсутствующим private каталогом: код 0" 0 "$rc"
assert_eq "import с отсутствующим private каталогом: вызовы и порядок" "$(printf '%s\n' \
  'docker compose stop web bot billing heartbeat' "$restore_call" 'docker compose up -d')" \
  "$(cat "$CALLS")"
assert_eq "import с отсутствующим private каталогом: дамп передан на stdin" "dump content" "$(cat "$R/stdin.dump")"
if [ ! -e "$PRIV" ]; then ok; else bad "import с отсутствующим private каталогом: приватный путь тронут"; fi

setup
mkdir -p "$DEST"
printf 'dump content' > "$DEST/db.dump"
run_import "$DEST" db 2> "$ERR"; rc=$?
assert_eq "import db: код 0" 0 "$rc"
assert_eq "import db: вызовы и порядок" "$(printf '%s\n' \
  'docker compose stop web bot billing heartbeat' "$restore_call" 'docker compose up -d')" \
  "$(cat "$CALLS")"
assert_eq "import db: дамп передан на stdin" "dump content" "$(cat "$R/stdin.dump")"
if [ ! -e "$PRIV" ]; then ok; else bad "import db: приватный каталог тронут"; fi

setup
mkdir -p "$DEST"
printf 'dump content' > "$DEST/db.dump"
run_import_no_private "$DEST" 2> "$ERR"; rc=$?
assert_eq "import без CLUB_PRIVATE_DIR: код 0" 0 "$rc"
assert_eq "import без CLUB_PRIVATE_DIR: вызовы и порядок" "$(printf '%s\n' \
  'docker compose stop web bot billing heartbeat' "$restore_call" 'docker compose up -d')" \
  "$(cat "$CALLS")"
assert_eq "import без CLUB_PRIVATE_DIR: дамп передан на stdin" "dump content" "$(cat "$R/stdin.dump")"
assert_eq "import без CLUB_PRIVATE_DIR: tar не вызывался" 0 "$(grep -c '^tar' "$CALLS")"

setup
mkdir -p "$DEST" "$PRIV"
printf 'dump content' > "$DEST/db.dump"
printf 'fresh' > "$PRIV/new.txt"
command tar czf "$DEST/private.tgz" -C "$PRIV" .
printf 'stale' > "$PRIV/stale.txt"
printf 'hidden' > "$PRIV/.hidden"
run_import "$DEST" 2> "$ERR"; rc=$?
assert_eq "import: код 0" 0 "$rc"
assert_eq "import: вызовы и порядок" "$(printf '%s\n' \
  'docker compose stop web bot billing heartbeat' "$restore_call" \
  "tar xzf $DEST/private.tgz -C $PRIV" 'docker compose up -d')" \
  "$(cat "$CALLS")"
assert_eq "import: дамп передан на stdin" "dump content" "$(cat "$R/stdin.dump")"
assert_eq "import: приватный каталог очищен и распакован" "new.txt" "$(ls -1A "$PRIV")"
assert_eq "import: данные распакованы" "fresh" "$(cat "$PRIV/new.txt")"

setup
mkdir -p "$DEST"
printf 'dump content' > "$DEST/db.dump"
run_import "$DEST" hourly 2> "$ERR"; rc=$?
assert_eq "import неизвестный режим: код 2" 2 "$rc"
assert_eq "import неизвестный режим: вызовов нет" "" "$(cat "$CALLS")"

setup
run_import 2> "$ERR"; rc=$?
assert_eq "import без аргумента: код 1" 1 "$rc"
assert_eq "import без аргумента: вызовов нет" "" "$(cat "$CALLS")"

# --- Итог ----------------------------------------------------------------

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
