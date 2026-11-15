#!/bin/sh
# dbus-victron-ble boot hook, called from /data/rcS.local at every boot.
#
# Venus OS rebuilds /service (a tmpfs) at boot, so the daemontools service is
# created here again each time. Nothing on the read-only root filesystem is
# changed, which is why this survives firmware updates.

DIR=$(dirname "$(readlink -f "$0")")
NAME=dbus-victron-ble
SVC=/run/service/$NAME

mkdir -p $SVC/log
cat > $SVC/run <<EOF
#!/bin/sh
echo "*** starting $NAME ***"
exec 2>&1
. /etc/profile.d/profile.sh
exec softlimit -d 100000000 -s 1000000 -a 100000000 python3 -u $DIR/dbus-victron-ble.py
EOF
cat > $SVC/log/run <<EOF
#!/bin/sh
exec 2>&1
exec multilog t s25000 n4 /var/log/$NAME
EOF
chmod 755 $SVC/run $SVC/log/run

# svscan picks the service up within 5 seconds
[ -e /service/$NAME ] || ln -s $SVC /service/$NAME
