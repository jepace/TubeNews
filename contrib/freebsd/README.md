# FreeBSD/Bastille Setup

Scripts and instructions for running TubeNews in a FreeBSD Bastille jail with auto-start on reboot.

## Prerequisites

### State Directory Permissions

Both services run as the `www` user. Ensure the `state/` directory is owned by `www`:

```sh
sudo bastille console tubenews
chown -R www:www /var/www/tubenews/state
chmod 755 /var/www/tubenews/state
exit
```

## Installation

### 1. Deploy files and install rc.d scripts

Run `deploy.sh` from the project root on the host — it syncs the codebase into the
jail and installs both rc.d scripts automatically on FreeBSD:

```sh
./deploy.sh
```

Or install manually:

```sh
sudo bastille cp tubenews contrib/freebsd/tubenews_daemon /etc/rc.d/tubenews_daemon
sudo bastille cp tubenews contrib/freebsd/tubenews_web    /etc/rc.d/tubenews_web
sudo bastille cmd tubenews chmod +x /etc/rc.d/tubenews_daemon /etc/rc.d/tubenews_web
```

### 2. Enable services in the jail

```sh
sudo bastille console tubenews
sysrc tubenews_daemon_enable=YES tubenews_daemon_dir=/var/www/tubenews
sysrc tubenews_web_enable=YES    tubenews_web_dir=/var/www/tubenews
exit
```

Optional settings:

```sh
# Run services as a different user (default: www)
sysrc tubenews_daemon_user=www
sysrc tubenews_web_user=www

# Enable Secure cookie flag when behind an HTTPS proxy
sysrc tubenews_web_https=YES

# Override log file destinations
sysrc tubenews_daemon_logfile=/var/log/tubenews_daemon.log
sysrc tubenews_web_logfile=/var/log/tubenews_web.log
```

### 3. Start services

```sh
sudo bastille console tubenews
service tubenews_daemon start
service tubenews_web start
exit
```

### 4. Enable jail auto-start on host boot

```sh
sudo bastille config tubenews set boot 1
```

## Managing services

```sh
sudo bastille console tubenews

service tubenews_daemon status
service tubenews_daemon restart
service tubenews_web status
service tubenews_web restart
```

Check logs:

```sh
tail -f /var/log/tubenews_daemon.log
tail -f /var/log/tubenews_web.log
```

## Services

- **tubenews_daemon** — runs `python3 TubeNews.py` in WebSub daemon mode
- **tubenews_web** — runs `serve.sh` (gunicorn) for the Flask web UI
