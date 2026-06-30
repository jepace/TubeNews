#!/bin/sh
# deploy.sh — copy TubeNews runtime files into the Bastille jail
#
# Run this from the project root after git pull:
#   ./deploy.sh
#
# Preserves config.json, content/, and anything else already in the
# destination that isn't part of the codebase.
#
# On FreeBSD: also installs rc.d scripts and fixes state directory ownership.

SRC="$(cd "$(dirname "$0")" && pwd)"
JAIL="tubenews"
DEST="/usr/local/bastille/jails/$JAIL/root/var/www/tubenews"

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

sudo rsync -av --delete \
    --exclude='config.json' \
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

# ---------------------------------------------------------------------------
# FreeBSD/Bastille: install rc.d scripts and fix permissions
# ---------------------------------------------------------------------------

if uname -s | grep -q FreeBSD; then
    echo "Installing FreeBSD rc.d scripts..."

    # Copy rc.d files into the jail
    sudo bastille cp "$JAIL" "$SRC/contrib/freebsd/tubenews_daemon" /etc/rc.d/tubenews_daemon
    sudo bastille cp "$JAIL" "$SRC/contrib/freebsd/tubenews_web" /etc/rc.d/tubenews_web

    # Make executable
    sudo bastille cmd "$JAIL" chmod +x /etc/rc.d/tubenews_daemon /etc/rc.d/tubenews_web

    # Fix state directory ownership (www user needs to write to state/)
    # Note: must use jail-relative path, not host path
    echo "Fixing state directory ownership to www:www..."
    DEST_IN_JAIL="/var/www/tubenews"
    sudo bastille cmd "$JAIL" chown -R www:www "$DEST_IN_JAIL/state"
    sudo bastille cmd "$JAIL" chmod 755 "$DEST_IN_JAIL/state"

    echo ""
    echo "Next steps (if first install):"
    echo "  sudo bastille console $JAIL"
    echo "  sysrc tubenews_daemon_enable=YES tubenews_daemon_dir=/var/www/tubenews"
    echo "  sysrc tubenews_web_enable=YES    tubenews_web_dir=/var/www/tubenews"
    echo "  exit"
fi

echo "Done."

# Remind operator to create config.json if this is a first deploy
if [ ! -f "$DEST/config.json" ]; then
    echo ""
    echo "NOTE: config.json not found in destination."
    echo "      Copy and edit the sample to get started:"
    echo "        cp $DEST/config.json.sample $DEST/config.json"
fi
