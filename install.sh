#!/bin/sh
# Installs dbus-victron-ble on a Venus OS device:
#   - copies it to /data/victron-ble (survives firmware updates),
#   - adds its boot hook to /data/rcS.local,
#   - starts it.
# Run as root from the unpacked release directory. Running it again updates
# an existing installation; the yield counters (state.json) are kept.

set -e

SRC=$(dirname "$(readlink -f "$0")")
DEST=/data/victron-ble
HOOK=$DEST/boot.sh
RCS=/data/rcS.local

if [ ! -f /opt/victronenergy/version ]; then
	echo "This is not a Venus OS device." >&2
	exit 1
fi
if ! python3 -c "import dbus, gi" 2>/dev/null; then
	echo "python3 with dbus and gi is required (Venus OS ships both)." >&2
	exit 1
fi

if [ "$SRC" != "$DEST" ]; then
	mkdir -p $DEST
	rm -rf $DEST/victronble $DEST/ext $DEST/tests
	cp -r "$SRC/victronble" "$SRC/ext" $DEST/
	if [ -d "$SRC/tests" ]; then cp -r "$SRC/tests" $DEST/; fi
	for f in dbus-victron-ble.py victron-ble boot.sh install.sh uninstall.sh README.md LICENSE; do
		if [ -f "$SRC/$f" ]; then cp "$SRC/$f" $DEST/; fi
	done
fi
rm -rf $DEST/victronble/__pycache__ $DEST/ext/velib_python/__pycache__ $DEST/tests/__pycache__
chmod 755 $DEST/dbus-victron-ble.py $DEST/victron-ble $DEST/boot.sh $DEST/install.sh $DEST/uninstall.sh

if [ ! -f $RCS ]; then
	if [ -f $RCS.disabled ]; then
		echo "Note: $RCS.disabled exists: Venus 'Modification checks' disabled the custom boot script."
	fi
	printf '#!/bin/sh\n' > $RCS
	chmod 755 $RCS
fi
if ! grep -qF "$HOOK" $RCS; then
	printf '# Victron instant readout (Bluetooth) devices on D-Bus\n%s\n' "$HOOK" >> $RCS
	echo "boot hook added to $RCS"
fi

if [ -e /service/dbus-victron-ble ]; then
	svc -t /service/dbus-victron-ble
	echo "dbus-victron-ble restarted"
else
	$HOOK
	echo "dbus-victron-ble started"
fi

cat <<EOF

Next: enable "Instant readout via Bluetooth" on each device in VictronConnect,
show its encryption data there, and store the key here:

    $DEST/victron-ble key <MAC address> <encryption key>
    $DEST/victron-ble            (status)
EOF
