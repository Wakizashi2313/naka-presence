#!/bin/sh
# Construit dist/naka-presence_<version>_all.deb et dist/naka-presence-<version>-1.noarch.rpm.
# Prérequis : python3 + pip, nfpm (https://nfpm.goreleaser.com).
set -eu
cd "$(dirname "$0")/../.."

VERSION=$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' naka_presence.py)
[ -n "$VERSION" ] || { echo "APP_VERSION introuvable" >&2; exit 1; }
export VERSION

# pystray et pypresence, purs Python, aux versions de requirements.txt (licences comprises)
rm -rf build/linux && mkdir -p build/linux/vendor dist
python3 -m pip install --quiet --no-deps --no-compile --target build/linux/vendor \
    "$(grep -i '^pystray==' requirements.txt)" "$(grep -i '^pypresence==' requirements.txt)"
find build/linux/vendor -name '__pycache__' -prune -exec rm -rf {} +
rm -rf build/linux/vendor/bin

python3 -m py_compile naka_presence.py
nfpm package --config packaging/linux/nfpm.yaml --packager deb --target dist/
nfpm package --config packaging/linux/nfpm.yaml --packager rpm --target dist/
ls -l dist/*.deb dist/*.rpm
