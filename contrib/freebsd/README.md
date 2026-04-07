# FreeBSD/Bastille Setup

Scripts and instructions for running TubeNews in a FreeBSD Bastille jail with auto-start on reboot.

## Installation

### 1. Copy rc.d scripts into the jail

```bash
sudo bastille cp TubeNews contrib/freebsd/tubenews_daemon.rc.d /etc/rc.d/tubenews_daemon
sudo bastille cp TubeNews contrib/freebsd/tubenews_web.rc.d /etc/rc.d/tubenews_web
sudo bastille console TubeNews -c "chmod +x /etc/rc.d/tubenews_daemon /etc/rc.d/tubenews_web"
```

### 2. Enable services in jail

```bash
sudo bastille console TubeNews
echo 'tubenews_daemon_enable="YES"' >> /etc/rc.conf.local
echo 'tubenews_web_enable="YES"' >> /etc/rc.conf.local
exit
```

### 3. Enable jail auto-start on boot

Configure your Bastille jail to auto-start on system reboot:

```bash
sudo bastille config TubeNews
```

Look for the `enable` or `boot` setting and set it to `1` or `YES`.

### 4. Verify

Test that services start:

```bash
sudo bastille console TubeNews
service tubenews_daemon status
service tubenews_web status
exit
```

After reboot, verify both services are running:

```bash
sudo bastille console TubeNews
ps aux | grep -i tubenews
```

## Services

- **tubenews_daemon**: Runs `python3 TubeNews.py --daemon` for WebSub push notifications
- **tubenews_web**: Runs `./serve.sh` for the Flask web UI (gunicorn)

Both services will auto-start when the jail boots.
