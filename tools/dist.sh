#!/bin/sh
# Builds the release tarball dist/venus-os-victron-ble-<version>.tar.gz
# (unpacks to victron-ble/; install with victron-ble/install.sh on the device).
set -e
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import victronble; print(victronble.VERSION)")
OUT=dist/venus-os-victron-ble-$VERSION.tar.gz
STAGE=$(mktemp -d)
mkdir -p $STAGE/victron-ble/ext/velib_python $STAGE/victron-ble/victronble $STAGE/victron-ble/tests dist
cp dbus-victron-ble.py victron-ble boot.sh install.sh uninstall.sh README.md LICENSE $STAGE/victron-ble/
cp victronble/*.py $STAGE/victron-ble/victronble/
cp ext/velib_python/vedbus.py ext/velib_python/ve_utils.py ext/velib_python/LICENSE $STAGE/victron-ble/ext/velib_python/
cp tests/*.py $STAGE/victron-ble/tests/
chmod 755 $STAGE/victron-ble/dbus-victron-ble.py $STAGE/victron-ble/victron-ble $STAGE/victron-ble/*.sh
tar -C $STAGE --owner=0 --group=0 -czf $OUT victron-ble
rm -rf $STAGE
echo "==> $OUT"
