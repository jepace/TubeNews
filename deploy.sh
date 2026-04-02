#!/bin/sh
# deploy.sh — copy TubeNews runtime files into the Bastille jail
#
# Run this from the project root after git pull:
#   ./deploy.sh
#
# Preserves TubeNews.json, content/, and anything else already in the
# destination that isn't part of the codebase.

SRC="/home/jepace/dev/TubeNews"
DEST="/usr/local/bastille/jails/TubeNews/root/var/www/TubeNews"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if [ ! -d "$SRC" ]; then
    echo "ERROR: source directory not found: $SRC" >&2
    exit 1
fi

if [ ! -d "$DEST" ]; then
    echo "ERROR: destination directory not found: $DEST" >&2
    echo "       Create it first: mkdir -p $DEST" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Sync — skips data/config that must be preserved in the jail
# ---------------------------------------------------------------------------

echo "Deploying $SRC → $DEST"

rsync -av --delete \
    --exclude='TubeNews.json' \
    --exclude='content/' \
    --exclude='state/' \
    --exclude='deploy.sh' \
    --exclude='.git/' \
    --exclude='.gitignore' \
    --exclude='.claude/' \
    --exclude='.pytest_cache/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='tests/' \
    --exclude='requirements-dev.txt' \
    --exclude='README.md' \
    --exclude='CLAUDE.md' \
    --exclude='SERVING.md' \
    --exclude='TODO.md' \
    --exclude='contrib/' \
    "$SRC/" "$DEST/"

echo ""
echo "Done."

# Remind operator to create TubeNews.json if this is a first deploy
if [ ! -f "$DEST/TubeNews.json" ]; then
    echo ""
    echo "NOTE: TubeNews.json not found in destination."
    echo "      Copy and edit the sample to get started:"
    echo "        cp $DEST/TubeNews.json.sample $DEST/TubeNews.json"
fi
