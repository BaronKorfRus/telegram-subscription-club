#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
if [ -f .env ]; then
    # shellcheck disable=SC1091
    . ./.env
fi
set +a

BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"
file="$BACKUP_DIR/$(date +%Y-%m-%d_%H-%M-%S).sql.gz"

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$file"
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.sql.gz' -mtime +14 -delete

if [ -n "${BACKUP_REMOTE:-}" ]; then
    scp "$file" "$BACKUP_REMOTE"
fi

echo "backup: $file"
