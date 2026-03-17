# Serving TubeNews Feeds

TubeNews writes its output to the `archive/` directory. To make the RSS feeds
subscribable over the network, you need to:

1. Serve `archive/` over HTTP
2. Set `base_url` in `TubeNews.json` to the public root URL of that directory
3. Run `TubeNews.py` on a schedule

---

## Quick Test (Python built-in server)

```bash
cd archive
python3 -m http.server 8080
```

Feeds are then available at:
- `http://localhost:8080/rss.xml` — regional meta-feed
- `http://localhost:8080/<channel_slug>/rss.xml` — per-channel feed
- `http://localhost:8080/users/<user_slug>/rss.xml` — per-user feed

---

## nginx

Serve the `archive/` directory as a static site. Replace `/path/to/TubeNews` and
`feeds.example.com` with your actual paths.

```nginx
server {
    listen 80;
    server_name feeds.example.com;

    root /path/to/TubeNews/archive;
    autoindex on;

    location / {
        try_files $uri $uri/ =404;
        add_header Content-Type application/rss+xml;
    }
}
```

For HTTPS, run `certbot --nginx -d feeds.example.com` after the above is in place.

---

## Apache

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

## Setting base_url

Once the server is running, set `base_url` in `TubeNews.json` to the public root
URL (no trailing slash):

```json
{
  "base_url": "https://feeds.example.com",
  ...
}
```

This value is embedded in the meta-feed `<link>` and per-user feed `<link>` elements
so feed readers can find the self-link. It is not required for the feeds to work, but
RSS validators and some readers expect it.

---

## Running on a Schedule (cron)

Add a crontab entry to run TubeNews automatically. Every 30 minutes is reasonable;
YouTube channels typically publish a few videos per week.

```cron
# Run TubeNews every 30 minutes
*/30 * * * * cd /path/to/TubeNews && /path/to/TubeNews/venv/bin/python TubeNews.py >> /var/log/tubenews.log 2>&1
```

Edit your crontab with `crontab -e`.

Tip: Run `helpers/catchup.py` once before the first scheduled run on any channel that
already has videos, or TubeNews will process the entire backlog on the first run.

---

## User Feeds

Personal per-user feeds are generated automatically when TubeNews runs, provided users
have been created with `helpers/manage_users.py`.

```
archive/users/<user_slug>/rss.xml
```

Share the URL `https://feeds.example.com/users/<user_slug>/rss.xml` with each user so
they can subscribe to only the channels they care about.
