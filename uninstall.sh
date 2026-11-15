#!/bin/sh
# Stops dbus-victron-ble and removes its boot hook.
#   --purge   also delete /data/victron-ble (including the yield counters)
# The keys, names and VRM instances stay in the Venus settings
# (/Settings/Devices/victron_*), so a reinstall picks them up again.

NAME=dbus-victron-ble
RCS=/data/rcS.local

if [ -e /service/$NAME ]; then
	rm -f /service/$NAME			# first, so that svscan does not restart it
	svc -dx /run/service/$NAME /run/service/$NAME/log 2>/dev/null
	sleep 1
fi
rm -rf /run/service/$NAME

if [ -f $RCS ]; then
	sed -i -e '\#/data/victron-ble/boot.sh#d' -e '/Victron instant readout (Bluetooth) devices on D-Bus/d' $RCS
fi

if [ "$1" = "--purge" ]; then
	rm -rf /data/victron-ble
	echo "dbus-victron-ble removed, including /data/victron-ble"
else
	echo "dbus-victron-ble stopped and removed from the boot; files remain in /data/victron-ble"
fi
