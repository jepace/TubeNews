# Serving TubeNews

TubeNews is served via gunicorn. The `web/app.py` Flask app handles user
accounts, subscriptions, the admin panel, and serves the generated feeds and
stories. Set `base_url` in `TubeNews.json` to the public root URL so RSS
feed links resolve correctly.

---

## Deploying with gunicorn

`serve.sh` wraps gunicorn with the right settings and reads the port from
`TubeNews.json` automatically.

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

Copy the output into `TubeNews.json` as `tubenews_key`:

```json
{
  "tubenews_key": "paste-your-generated-key-here",
  ...
}
```

This key signs login sessions. Generate it once and leave it alone — changing it
logs everyone out.

### 3. Make yourself an admin

Add your email to `TubeNews.json`:

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
in `TubeNews.json`). Register an account — your email matches `admin_users` so
you will have admin access automatically.

To keep it running after logout:

```bash
nohup ./serve.sh > /var/log/tubenews-web.log 2>&1 &
```

For a proper service that survives reboots, see the FreeBSD rc.d section below.

### 5. Set base_url

Set `base_url` in `TubeNews.json` to the public root of your server
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
| `/dashboard` | Subscribe to channels, copy your feed and blog URLs |
| `/admin` | Manage users and channels |
| `/content/rss.xml` | Regional aggregate feed |
| `/content/<channel>/rss.xml` | Per-channel feed |
| `/feed/<token>.xml` | Your personal RSS feed (token shown on dashboard) |
| `/blog/<token>.html` | Your personal blog page (shareable, no login required) |

---

## Adding HTTPS with Certbot

Certbot handles certificates; nginx handles TLS; gunicorn handles the actual
content. The traffic flow is:

```
Browser → nginx :443 (HTTPS) → gunicorn :8000 (localhost)
```

### 1. Point your domain at the server

Make sure `feeds.example.com` resolves to your server's IP before running
certbot.

### 2. Create a basic nginx config

```nginx
server {
    listen 80;
    server_name feeds.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 3. Run certbot

```bash
certbot --nginx -d feeds.example.com
```

Certbot edits the nginx config and adds an HTTPS server block automatically.

### 4. Tell Flask it's behind HTTPS

Add to `TubeNews.json`:

```json
{
  "base_url": "https://feeds.example.com",
  ...
}
```

And set the environment variable before starting the server so session cookies
are marked `Secure`:

```bash
TUBENEWS_HTTPS=true ./serve.sh
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

## User Feeds and Blog Pages

Each registered user gets a personal RSS feed and blog page served at
token-based URLs shown on the dashboard:

| URL | What it serves |
|---|---|
| `/feed/<token>.xml` | Personal RSS feed — add to any feed reader |
| `/blog/<token>.html` | Personal blog page — shareable, no login required |

Both URLs use the same token and are safe to share. The token is shown on the
dashboard and can be reset by an admin if needed (resetting invalidates the
old URLs immediately).

**Both are generated dynamically on every request** — the web app reads the
live archive each time and returns fresh content. No static files are
pre-built; there is nothing to invalidate or manually rebuild after a new
TubeNews run.

---

## Upgrading an Existing Install

### Renaming `archive/` to `content/`

The content directory was renamed from `archive/` to `content/`. If you have an
existing install, move the directory once:

```bash
mv archive content
```

Then restart the server. If you have `"archive_dir"` set in `TubeNews.json`,
rename the key to `"content_dir"` (or leave it as `"archive_dir"` — both are
accepted for backward compatibility).

### Renaming `archive/users/` to `content/_users/`

If upgrading from a very early install that pre-dates the `_`-prefix convention
(user data was at `archive/users/`), rename in two steps:

```bash
mv archive/users archive/_users   # if not already done
mv archive content                 # rename the whole directory
```

No other changes are needed — all user data is intact at the new path.

---

## Per-Channel Focus Filtering

By default a user's feed and blog show all stories from their subscribed
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
