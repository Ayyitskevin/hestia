#!/usr/bin/env bash
# Push the durable copy OFF the box: the DB backups AND (for local storage) the media
# directory — the client galleries that ARE the product. The one machine holding your
# data is zero backups; this is the off-site half of the story.
#
#   HESTIA_OFFSITE_REMOTE="s3:my-bucket/hestia" bash scripts/offsite-sync.sh
#
# HESTIA_OFFSITE_REMOTE is an rclone "remote:path". rclone speaks S3, Backblaze B2,
# Cloudflare R2, Google Drive, SFTP, and more — configure it once with `rclone config`.
# Run this on a cron a few minutes AFTER the daily backup (see docs/operations.md).
# rclone copy is non-deleting, not append-only: changed same-path objects can replace
# prior remote bytes. D5 requires destination versioning/object lock (or equivalent)
# plus remote verification and a fresh receipt before this is launch evidence.
set -euo pipefail

REMOTE="${HESTIA_OFFSITE_REMOTE:-}"
[ -n "$REMOTE" ] || { echo "ERROR: set HESTIA_OFFSITE_REMOTE (an rclone remote:path, e.g. s3:bucket/hestia)" >&2; exit 2; }
command -v rclone >/dev/null 2>&1 || { echo "ERROR: rclone not found — install it: https://rclone.org/install/" >&2; exit 1; }

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
BACKUP_DIR="${HESTIA_BACKUP_DIR:-$DATA_DIR/backups}"
MEDIA_DIR="${HESTIA_MEDIA_DIR:-$DATA_DIR/media}"
STORAGE="${HESTIA_STORAGE_BACKEND:-local}"

# DB backups: copy (never delete off-site) so the remote keeps a longer history than
# the box's 14-day local rotation — gzipped DBs are tiny, so unbounded is cheap safety.
echo "→ DB backups → $REMOTE/backups"
rclone copy "$BACKUP_DIR" "$REMOTE/backups" --stats-one-line

# Media originals + thumbnails: only for local storage (S3/R2 already lives off-box).
# copy, not sync, preserves destination-only objects when a local gallery is removed.
# A changed object at the same key can still overwrite remote bytes; provider-side
# retention is required for immutable history.
if [ "$STORAGE" = "local" ]; then
  if [ -d "$MEDIA_DIR" ]; then
    echo "→ media → $REMOTE/media"
    rclone copy "$MEDIA_DIR" "$REMOTE/media" --stats-one-line
  else
    echo "→ no media dir at $MEDIA_DIR yet — nothing to sync"
  fi
else
  echo "→ storage=$STORAGE: media already lives off-box in the object store, skipping"
fi

echo "off-site copy commands completed (UNVERIFIED; not D5 evidence) → $REMOTE"
