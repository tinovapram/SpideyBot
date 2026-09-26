#!/usr/bin/env bash
# Backup PostgreSQL from the spideybot-db container.
#
# Usage:
#   ./scripts/backup_db.sh                  # → backups/spideybot_YYYYMMDD_HHMMSS.sql.gz
#   ./scripts/backup_db.sh /custom/path.sql # → /custom/path.sql.gz
#
# Requires: docker
set -euo pipefail

CONTAINER="spideybot-db"
USER="spidey"
DB="spideybot"

DEST="${1:-backups/spideybot_$(date +%Y%m%d_%H%M%S).sql.gz}"
mkdir -p "$(dirname "$DEST")"

echo "⏳ Dumping $DB from container $CONTAINER → $DEST"
docker exec "$CONTAINER" pg_dump -U "$USER" -d "$DB" --no-owner --no-privileges \
  | gzip > "$DEST"

echo "✅ Backup complete: $(du -h "$DEST" | cut -f1)"
