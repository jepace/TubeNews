# Serving TubeNews

TubeNews writes its output to the `archive/` directory. To make feeds
subscribable over the network you need to serve them over HTTP and set
`base_url` in `TubeNews.json` to the public root URL.

There are two ways to serve TubeNews:

| Approach | Good for |
|---|---|
| **gunicorn** (recommended) | User accounts, subscriptions, admin panel, and feeds — all in one process |
| **Static file server** (nginx/Apache) | Feeds only, no UI |

---

## Option A: gunicorn Web UI (Recommended)

The `web/app.py` Flask app serves both the web interface and the archive files.
`serve.sh` wraps gunicorn with the right settings and reads the port from
`TubeNews.json` automatically.

### 1. Install dependencies

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
| `/archive/rss.xml` | Regional aggregate feed |
| `/archive/<channel>/rss.xml` | Per-channel feed |
| `/feed/<token>.xml` | Your personal RSS feed (token shown on dashboard) |
| `/blog/<token>.html` | Your personal blog page (shareable, no login required) |

---

## Option B: Static File Server (Feeds Only)

Use this if you don't need user accounts and just want to publish feeds.

### Quick test (Python built-in server)

```bash
cd archive
python3 -m http.server 8080
```

Feeds are then at `http://localhost:8080/rss.xml`, etc.

### nginx

```nginx
server {
    listen 80;
    server_name feeds.example.com;

    root /path/to/TubeNews/archive;
    autoindex on;

    location / {
        try_files $uri $uri/ =404;
    }

    location ~* \.xml$ {
        try_files $uri =404;
        add_header Content-Type application/rss+xml;
    }
}
```

### Apache

```apache
<VirtualHost *:80>
    ServerName feeds.example.com
    DocumentRoot /path/to/TubeNews/archive
    Options Indexes FollowSymLinks
    AllowOverride None
    Require all granted
</VirtualHost>
```

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

Use the ready-to-use service files in `contrib/` to run TubeNews as a
managed system service that starts at boot and restarts on crash.

| OS | System | Files |
|---|---|---|
| FreeBSD | rc.d | `contrib/freebsd/tubenews` |
| Linux | systemd | `contrib/linux/tubenews-web.service` |
| macOS | launchd | `contrib/macos/com.tubenews.web.plist` |

See `contrib/README.md` for step-by-step installation instructions for
each platform. The Linux files also include a systemd timer
(`tubenews-run.timer`) as an alternative to cron for the scraper.

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
