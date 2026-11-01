#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose build

docker compose up -d db
docker compose run --rm web alembic upgrade head

docker compose up -d
docker compose ps
