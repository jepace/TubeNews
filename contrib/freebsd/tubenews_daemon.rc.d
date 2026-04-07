#!/bin/sh
#
# PROVIDE: tubenews_daemon
# REQUIRE: NETWORKING
# KEYWORD: shutdown
#
# Add the following to /etc/rc.conf[.local] to enable this service:
#
# tubenews_daemon_enable="YES"

. /etc/rc.subr

name="tubenews_daemon"
rcvar=tubenews_daemon_enable
pidfile="/var/run/tubenews_daemon.pid"

: ${tubenews_daemon_enable:="NO"}
: ${tubenews_daemon_user:="www"}
: ${tubenews_daemon_dir:="/var/www/TubeNews"}

command="/usr/local/bin/python3"
command_args="${tubenews_daemon_dir}/TubeNews.py --daemon"
start_precmd="${name}_precmd"

tubenews_daemon_precmd()
{
    cd ${tubenews_daemon_dir}
}

load_rc_config $name
run_rc_command "$1"
