#!/usr/bin/env bash
# PostgreSQL local de developpement SANS Docker (Mission 004.1).
#
# Cluster jetable dans .pgdata/ (ignore par Git), sur un port dedie (55432 par
# defaut), accessible uniquement en local via socket et 127.0.0.1. Il ne touche
# a aucune autre base de la machine. Equivalent Docker: docker-compose.yml.
#
#   scripts/dev_postgres.sh start    # initialise si besoin, puis demarre
#   scripts/dev_postgres.sh stop
#   scripts/dev_postgres.sh status
#   scripts/dev_postgres.sh destroy  # arrete et supprime .pgdata/
#
# Binaires: PG_BIN, sinon Homebrew postgresql@17, sinon le PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${MERVIO_PGDATA:-$ROOT/.pgdata}"
PORT="${MERVIO_PGPORT:-55432}"
SUPERUSER="${MERVIO_PGSUPERUSER:-mervio_admin}"
# macOS: sans locale valide, postmaster refuse de demarrer ("became multithreaded")
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

if [[ -z "${PG_BIN:-}" ]]; then
  if [[ -x /opt/homebrew/opt/postgresql@17/bin/pg_ctl ]]; then
    PG_BIN=/opt/homebrew/opt/postgresql@17/bin
  else
    PG_BIN="$(dirname "$(command -v pg_ctl)")"
  fi
fi

case "${1:-}" in
  start)
    if [[ ! -f "$DATA/PG_VERSION" ]]; then
      # trust uniquement en local: cluster de developpement jetable, jamais expose
      "$PG_BIN/initdb" -D "$DATA" -U "$SUPERUSER" --auth-local=trust --auth-host=trust \
        --encoding=UTF8 --locale=C >/dev/null
    fi
    "$PG_BIN/pg_ctl" -D "$DATA" -l "$DATA/server.log" -w \
      -o "-p $PORT -k $DATA -c listen_addresses=127.0.0.1" start
    echo "MERVIO_ADMIN_DATABASE_URL=postgresql://$SUPERUSER@127.0.0.1:$PORT/postgres"
    ;;
  stop)
    "$PG_BIN/pg_ctl" -D "$DATA" -w stop
    ;;
  status)
    "$PG_BIN/pg_ctl" -D "$DATA" status
    ;;
  destroy)
    if [[ -f "$DATA/postmaster.pid" ]]; then "$PG_BIN/pg_ctl" -D "$DATA" -w stop; fi
    rm -rf "$DATA"
    ;;
  *)
    echo "usage: $0 {start|stop|status|destroy}" >&2
    exit 2
    ;;
esac
