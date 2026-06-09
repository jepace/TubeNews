# Serving TubeNews

TubeNews is served via gunicorn. The `web/app.py` Flask app handles user
accounts, subscriptions, the admin panel, and serves the generated feeds and
stories. Set `base_url` in `config.json` to the public root URL so RSS
feed links resolve correctly.

---

## Deploying with gunicorn

`serve.sh` wraps gunicorn with the right settings and reads the port from
`config.json` automatically.

### 1. Install dependencies

> **FreeBSD note:** `feedgen` depends on `lxml`, a C extension that requires
> libxml2/libxslt and is too large to compile inside a minimal jail.
> Install the pre-built package first, then run pip:
>
> ```bash
> pkg install py311-lxml   # adjust py311 to match your Python version
> pip install --no-cache-dir -r requirements.txt
> ```
>
> If your Python version is different, check with `python3 --version` and use
> the matching package name (e.g. `py312-lxml`).

All other platforms:

```bash
pip install -r requirements.txt
```

All packages install globally — no virtual environment needed.

### 2. Generate a secret key

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Copy the output into `config.json` as `tubenews_key`:

```json
{
  "tubenews_key": "paste-your-generated-key-here",
  ...
}
```

This key signs login sessions. Generate it once and leave it alone — changing it
logs everyone out.

### 3. Make yourself an admin

Add your email to `config.json`:

```json
{
  "admin_users": ["you@example.com"],
  ...
}
```

### 4. Start the server

```bash
./serve.sh
```

Open `http://your-server:8000` in a browser (default port; change with `"port"`
in `config.json`). Register an account — your email matches `admin_users` so
you will have admin access automatically.

To keep it running after logout:

```bash
nohup ./serve.sh > /var/log/tubenews-web.log 2>&1 &
```

For a proper service that survives reboots, see the FreeBSD rc.d section below.

### 5. Set base_url

Set `base_url` in `config.json` to the public root of your server
(no trailing slash):

```json
{
  "base_url": "http://your-server:8000",
  ...
}
```

### URL layout

| URL | What you get |
|---|---|
| `/` | Login / dashboard |
| `/dashboard` | Subscribe to channels, copy your feed URLs |
| `/admin` | Manage users and channels |
| `/content/rss.xml` | Regional aggregate feed |
| `/content/<channel>/rss.xml` | Per-channel feed |
| `/feed/<token>.xml` | Your personal RSS feed (token shown on dashboard) |
| `/feed/<token>.html` | Your personal feed page (shareable, no login required) |

---

## Adding HTTPS with nginx + Certbot

Certbot handles certificates; nginx handles TLS termination on the host;
gunicorn handles requests inside the jail. Traffic flow:

```
Browser → nginx :443 (host) → gunicorn :8000 (jail at 10.0.0.1)
```

nginx proxies all requests to gunicorn — Flask's `serve_content` route already
handles `/content/` with the appropriate security checks, so no direct
filesystem access from nginx is needed.

A ready-to-use config is included at `contrib/nginx/tubenews.org.conf`.

### 1. Install nginx and certbot on the host (not in the jail)

```sh
pkg install nginx py311-certbot-nginx
```

### 2. Install the nginx config

```sh
cp contrib/nginx/tubenews.org.conf /usr/local/etc/nginx/conf.d/tubenews.org.conf
```

If nginx uses the default single-file config, add an include to
`/usr/local/etc/nginx/nginx.conf` inside the `http {}` block:

```nginx
include /usr/local/etc/nginx/conf.d/*.conf;
```

### 3. Enable and start nginx

```sh
sysrc nginx_enable=YES
service nginx start
```

Verify: `curl -I http://tubenews.org` should return a 200 proxied from gunicorn
(or a connection-refused error if gunicorn isn't running yet — that's fine,
nginx is working).

### 4. Obtain a TLS certificate

```sh
certbot --nginx -d tubenews.org -d www.tubenews.org
```

Certbot rewrites the nginx config to add HTTPS server blocks and redirects
HTTP → HTTPS automatically. After this, both `http://` and `https://` work,
and `www.tubenews.org` redirects to `https://tubenews.org`.

### 5. Tell Flask it's behind HTTPS

In the jail's `config.json`:

```json
{
  "base_url": "https://tubenews.org"
}
```

And start gunicorn with the HTTPS flag so session cookies are marked `Secure`:

```sh
TUBENEWS_HTTPS=true ./serve.sh
```

### 6. Verify end-to-end

```sh
curl -I http://tubenews.org        # → 301 https://tubenews.org
curl -I http://www.tubenews.org    # → 301 https://tubenews.org
curl -I https://tubenews.org       # → 200
curl -I https://www.tubenews.org   # → 301 https://tubenews.org
```

---

## Running on a Schedule (cron)

Add a crontab entry to run TubeNews automatically. Every 30 minutes is
reasonable; YouTube channels typically publish a few videos per week.

```cron
*/30 * * * * cd /path/to/TubeNews && python3 TubeNews.py >> /var/log/tubenews.log 2>&1
```

Edit your crontab with `crontab -e`.

> **Tip:** Run `helpers/catchup.py` once before the first scheduled run on any
> channel that already has videos, or TubeNews will process the entire backlog.

---

## Keeping the Server Running: System Service

### FreeBSD (rc.d)

Create `/usr/local/etc/rc.d/tubenews` with mode `0555`:

```sh
#!/bin/sh
# PROVIDE: tubenews
# REQUIRE: NETWORKING
# KEYWORD: shutdown

. /etc/rc.subr

name="tubenews"
rcvar="tubenews_enable"
tubenews_user="${tubenews_user:-www}"
tubenews_dir="${tubenews_dir:-/var/www/TubeNews}"
pidfile="/var/run/${name}.pid"
command="/usr/sbin/daemon"
command_args="-P ${pidfile} -r -f ${tubenews_dir}/serve.sh"

load_rc_config $name
run_rc_command "$1"
```

Enable and start:

```sh
sysrc tubenews_enable=YES
sysrc tubenews_dir=/var/www/TubeNews   # adjust to your install path
service tubenews start
```

### Linux (systemd)

Create `/etc/systemd/system/tubenews.service`:

```ini
[Unit]
Description=TubeNews web server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/var/www/TubeNews
ExecStart=/var/www/TubeNews/serve.sh
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable and start:

```sh
systemctl daemon-reload
systemctl enable --now tubenews
```

For the scraper on a schedule, add a systemd timer or use cron (see above).

---

## User Feeds

Each registered user gets a personal RSS feed and feed page served at
token-based URLs shown on the dashboard:

| URL | What it serves |
|---|---|
| `/feed/<token>.xml` | Personal RSS feed — add to any feed reader |
| `/feed/<token>.html` | Personal feed page — shareable, no login required |

Both URLs use the same token and are safe to share. The token is shown on the
dashboard and can be reset by an admin if needed (resetting invalidates the
old URLs immediately).

**Both are generated dynamically on every request** — the web app reads the
live archive each time and returns fresh content. No static files are
pre-built; there is nothing to invalidate or manually rebuild after a new
TubeNews run.

---

## state_dir

By default TubeNews creates a `state/` directory next to `TubeNews.py` to hold
all internal state (user accounts, run logs, channel config, lock file, Supadata
balance cache, WebSub queue and subscription data). This directory is **never
web-served** — it should live outside your web server's document root.

To override the location, set `state_dir` in `config.json` (absolute path or
relative to `TubeNews.py`):

```json
{
  "content_dir": "/var/www/html/tubenews",
  "state_dir": "/var/lib/tubenews/state"
}
```

The `content_dir` and `state_dir` keys are resolved by `resolve_roots()` in
`tubenews_utils.py` and used by both `TubeNews.py` and `web/app.py`.

---

## WebSub Integration

TubeNews supports YouTube's PubSubHubbub (WebSub) push feed so new videos
trigger processing within minutes instead of waiting for the next cron run.

### Enable

Add these keys to `config.json`:

```json
{
  "websub_callback_url": "https://yourdomain.com/youtube/push",
  "websub_secret":       "generate-with-token-hex-32",
  "websub_daemon_port":  8675
}
```

Generate a secret:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

### Run

Start the daemon instead of a one-shot run:

```bash
python3 TubeNews.py --daemon
```

The daemon runs indefinitely. It starts two threads:

- **`_wsb_receiver_thread`** — HTTP server on `websub_daemon_port`; receives
  and verifies push notifications from YouTube's hub, writes them to
  `state/queue/push_queue.json`.
- **`_wsb_processor_thread`** — wakes every `websub_check_interval_minutes`
  (default 10) and processes queued notifications that have aged past
  `websub_min_age_minutes` (default 360, i.e. 6 hours — avoids processing
  livestreams before they end).

Keep the daemon alive with the same systemd unit or rc.d service used for
`serve.sh` (run each as a separate service), or use a process supervisor.

### Reverse proxy

Expose `websub_daemon_port` via nginx so YouTube's hub can reach it over HTTPS.
Add a location block to your existing TubeNews nginx config:

```nginx
location /youtube/push {
    proxy_pass         http://10.0.0.1:8675;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}
```

Replace `10.0.0.1` with the jail IP if yours differs. The path in the
`location` block must match the path component of `websub_callback_url` in
`config.json`. The block is already included in
`contrib/nginx/tubenews.org.conf`.

### Subscriptions

- **Subscribe** happens automatically when a channel is added via the web UI
  admin panel (`/admin/feeds/add`).
- **Unsubscribe** happens automatically when a channel is removed
  (`/admin/feeds/<idx>/delete`).
- **Renewal** is handled internally — the daemon re-subscribes all channels on
  startup and re-subscribes any subscription expiring within 24 hours on each
  processor cycle. No cron job is needed.

Subscription state is stored in `state/subscriptions.json` (keyed by
`channel_id`). The WebSub lease is 604 800 s (7 days); the hub is
`https://pubsubhubbub.appspot.com/subscribe`.

---

## Migration from feeds[] to channels.json

Channel configuration has moved from the `feeds[]` array in `config.json` to
`state/channels.json`. The old location is still read as a fallback so existing
installs continue to work without any manual step.

### Automatic migration

TubeNews reads `state/channels.json` on startup. If that file does not exist, it
falls back to `feeds[]` in `config.json`. No action is required for existing
deployments — channels appear correctly in the web UI and the scraper runs
normally.

### Manual migration

The easiest path is to use the admin panel: visit `/admin/feeds`, verify the
channel list, make any edit (or add/remove a channel), and save. The web UI
writes to `state/channels.json`; after the first save the fallback is no longer
needed.

Alternatively, copy the array from `config.json`:

```bash
# extract feeds[] and write to state/channels.json
python3 -c "
import json, pathlib
cfg = json.loads(pathlib.Path('config.json').read_text())
pathlib.Path('state').mkdir(exist_ok=True)
pathlib.Path('state/channels.json').write_text(json.dumps(cfg.get('feeds', []), indent=2))
"
```

Once `state/channels.json` exists, the `feeds[]` key in `config.json` is
ignored. It can be removed from the file but does not need to be.

---

## Upgrading an Existing Install

### Renaming `archive/` to `content/`

The content directory was renamed from `archive/` to `content/`. If you have an
existing install, move the directory once:

```bash
mv archive content
```

Then restart the server. If you have `"archive_dir"` set in `config.json`,
rename the key to `"content_dir"`.

---

## Per-Channel Focus Filtering

By default a user's feed and feed page show all stories from their subscribed
channels. Each user can optionally narrow what they see on a per-channel basis:

1. Go to `/dashboard`
2. For each subscribed channel, type focus keywords into the **Your focus**
   field (e.g. `housing, zoning, permits`)
3. Click **Subscribe** to save

Stories whose AI-assigned topics overlap with the user's focus keywords are
shown; the rest are filtered out. Two people subscribing to the same channel
can have completely different feeds.

**Notes:**
- Leaving the focus field blank shows all stories from that channel (the default).
- Stories written before topic tagging was introduced always appear regardless
  of focus — there is no need to re-process old content.
- Topic matching is substring-tolerant: focus keyword `housing` matches a story
  tagged `affordable housing`.
