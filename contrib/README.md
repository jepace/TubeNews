# Service Integration Files

Ready-to-use service definitions for running TubeNews as a managed system
service. Choose the directory that matches your OS.

Before installing any file, **replace every `/path/to/TubeNews` placeholder**
with the actual path to your TubeNews installation.

---

## FreeBSD — rc.d (`contrib/freebsd/`)

| File | Purpose |
|---|---|
| `tubenews` | rc.d service for the web server (gunicorn via `serve.sh`) |

The scraper (`TubeNews.py`) is run by cron on FreeBSD — see SERVING.md.

**Install:**
```sh
cp contrib/freebsd/tubenews /usr/local/etc/rc.d/tubenews
chmod +x /usr/local/etc/rc.d/tubenews
```

Add to `/etc/rc.conf`:
```sh
tubenews_enable="YES"
tubenews_dir="/path/to/TubeNews"   # required
tubenews_user="www"                # user to run as
# tubenews_https="YES"             # uncomment if behind an HTTPS proxy
# tubenews_logfile="/var/log/tubenews.log"  # default
```

```sh
service tubenews start
service tubenews status
service tubenews restart
```

**Auto-restart on crash:** Edit the rc script and uncomment the `-r -R 5`
daemon flags (see the comment inside the file). This uses `daemon(8)`'s
built-in restart support, available on FreeBSD 12+.

---

## Linux — systemd (`contrib/linux/`)

| File | Purpose |
|---|---|
| `tubenews-web.service` | Persistent web server (gunicorn) |
| `tubenews-run.service` | One-shot scraper run |
| `tubenews-run.timer` | Fires `tubenews-run.service` every 30 minutes |

**Edit both `.service` files** — set `WorkingDirectory` and `User` to match
your installation, then:

```sh
cp contrib/linux/tubenews-web.service /etc/systemd/system/
cp contrib/linux/tubenews-run.service /etc/systemd/system/
cp contrib/linux/tubenews-run.timer   /etc/systemd/system/
systemctl daemon-reload

# Web server
systemctl enable --now tubenews-web

# Scraper timer (replaces cron)
systemctl enable --now tubenews-run.timer
```

**Useful commands:**
```sh
systemctl status tubenews-web
journalctl -u tubenews-web -f

systemctl list-timers tubenews-run
journalctl -u tubenews-run
```

The timer uses `Persistent=true`, so if the machine is off when a run was
due, it will catch up once on the next boot.

---

## macOS — launchd (`contrib/macos/`)

| File | Purpose |
|---|---|
| `com.tubenews.web.plist` | Persistent web server (gunicorn via `serve.sh`) |
| `com.tubenews.run.plist` | Periodic scraper run every 30 minutes |

**Edit both plist files** — replace the `/path/to/TubeNews` placeholder.

```sh
# Install for the current user (no root required):
cp contrib/macos/com.tubenews.web.plist ~/Library/LaunchAgents/
cp contrib/macos/com.tubenews.run.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tubenews.web.plist
launchctl load ~/Library/LaunchAgents/com.tubenews.run.plist

# Trigger the scraper immediately (for testing):
launchctl start com.tubenews.run

# Unload (stop and disable):
launchctl unload ~/Library/LaunchAgents/com.tubenews.web.plist
```

For a system-wide install (runs at boot, requires root), copy to
`/Library/LaunchDaemons/` instead of `~/Library/LaunchAgents/` and use
`sudo launchctl`.

Logs go to `/usr/local/var/log/tubenews-*.log` by default — create that
directory first (`mkdir -p /usr/local/var/log`), or edit the plist paths.
