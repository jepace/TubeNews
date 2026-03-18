# Serving TubeNews

TubeNews writes its output to the `archive/` directory. To make feeds
subscribable over the network you need to serve them over HTTP and set
`base_url` in `TubeNews.json` to the public root URL.

There are two ways to serve TubeNews:

| Approach | Good for |
|---|---|
| **Flask web UI** (recommended) | User accounts, subscriptions, admin panel, and feeds — all in one process |
| **Static file server** (nginx/Apache) | Feeds only, no UI |

---

## Option A: Flask Web UI (Recommended)

The `web/app.py` Flask app serves both the web interface and the archive files,
so you only need one process.

### 1. Install web dependencies

```bash
pip install -r requirements.txt
```

### 2. Generate a secret key

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
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
  "admin_emails": ["you@example.com"],
  ...
}
```

### 4. Start the app

```bash
python web/app.py
```

Open `http://your-server:8000` in a browser (default port; change with `"port"` in `TubeNews.json`). Register an account — your email
matches `admin_emails` so you will have admin access automatically.

### 5. Set base_url

Set `base_url` in `TubeNews.json` to the public root of your Flask app
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
| `/dashboard` | Subscribe to channels, copy your feed URL |
| `/admin` | Manage users and channels |
| `/archive/rss.xml` | Regional meta-feed |
| `/archive/<channel>/rss.xml` | Per-channel feed |
| `/feed/<token>` | Your personal RSS feed (token shown on dashboard) |

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

Certbot handles certificates; nginx handles TLS; Flask (or your static server)
handles the actual content. The traffic flow is:

```
Browser → nginx :443 (HTTPS) → Flask :5000 (localhost)
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
        proxy_pass http://127.0.0.1:5000;
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

And set the environment variable before starting Flask so session cookies are
marked `Secure`:

```bash
export TUBENEWS_HTTPS=true
python web/app.py
```

---

## Running on a Schedule (cron)

Add a crontab entry to run TubeNews automatically. Every 30 minutes is
reasonable; YouTube channels typically publish a few videos per week.

```cron
*/30 * * * * cd /path/to/TubeNews && /path/to/TubeNews/venv/bin/python TubeNews.py >> /var/log/tubenews.log 2>&1
```

Edit your crontab with `crontab -e`.

> **Tip:** Run `helpers/catchup.py` once before the first scheduled run on any
> channel that already has videos, or TubeNews will process the entire backlog.

---

## Running Flask in Production (gunicorn)

For a production server, use gunicorn instead of the built-in Flask dev server:

```bash
pip install gunicorn
export TUBENEWS_HTTPS=true   # if behind HTTPS nginx
gunicorn -w 2 'web.app:app'
```

gunicorn listens on port 8000 by default; adjust your nginx `proxy_pass`
accordingly, or pass `-b 127.0.0.1:5000` to keep port 5000.

---

## User Feeds and Blog Pages

Each registered user gets a personal RSS feed and blog page generated
automatically when TubeNews runs:

```
archive/users/<user_slug>/rss.xml      ← RSS feed for their subscribed channels
archive/users/<user_slug>/index.html   ← readable blog page
```

These are accessible at:
- `/archive/users/<user_slug>/rss.xml`
- `/archive/users/<user_slug>/index.html`

Or use the token-based URL shown on the dashboard (`/feed/<token>`) — it works
without knowing the user's slug and is safe to share with a feed reader.
