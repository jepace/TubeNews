#!/bin/sh
#
# PROVIDE: tubenews_web
# REQUIRE: NETWORKING
# KEYWORD: shutdown
#
# Add the following to /etc/rc.conf[.local] to enable this service:
#
# tubenews_web_enable="YES"

. /etc/rc.subr

name="tubenews_web"
rcvar=tubenews_web_enable
pidfile="/var/run/tubenews_web.pid"
logfile="/var/log/tubenews_web.log"

: ${tubenews_web_enable:="NO"}
: ${tubenews_web_user:="www"}
: ${tubenews_web_dir:="/var/www/TubeNews"}

command="/usr/sbin/daemon"
command_args="-P ${pidfile} -u ${tubenews_web_user} -o ${logfile} ${tubenews_web_dir}/serve.sh"

load_rc_config $name
run_rc_command "$1"
