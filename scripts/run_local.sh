#!/usr/bin/env bash
# Local dev runner: sets up venv, installs deps, runs migrations, starts the bot.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -U pip
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "No .env found — copy .env.example to .env and fill BOT_TOKEN first." >&2
  exit 1
fi

# Apply migrations (Postgres). For a quick SQLite dev run the app auto-creates tables.
alembic upgrade head || echo "alembic skipped (using auto create_all in dev)"

python -m src.app
