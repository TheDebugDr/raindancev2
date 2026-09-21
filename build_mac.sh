#!/usr/bin/env bash
# Build RainDance into a double-clickable Mac app (.app) and a .dmg.
#
# RUN THIS IN TERMINAL ON YOUR MAC, from the project folder:
#     bash build_mac.sh
#
# It cannot be run from the Claude session: that shell is a Linux VM, and a Mac
# app bundle can only be produced on macOS itself.
set -euo pipefail

APP_NAME="RainDance"
ENTRY="app.py"
BUILD_VENV=".venv-build"

echo "==> checking python"
PY="$(command -v python3.12 || command -v python3.11 || command -v python3)"
PYV="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    using $PY (python $PYV)"
case "$PYV" in
  3.9|3.10)
    echo "    !! pywebview/pyobjc will very likely fail to build on $PYV."
    echo "       Install 3.11+ first:  brew install python@3.12"
    echo "       then re-run this script. Stopping now so the failure is clear."
    exit 1 ;;
esac

echo "==> build venv"
"$PY" -m venv "$BUILD_VENV"
# shellcheck disable=SC1090
source "$BUILD_VENV/bin/activate"
python -m pip install --quiet --upgrade pip

echo "==> dependencies"
pip install --quiet -r requirements.txt
pip install --quiet "pywebview>=4.4"        # the native window
pip install --quiet pyinstaller

echo "==> chromium for playwright"
python -m playwright install chromium

echo "==> packing $APP_NAME.app"
# nicegui-pack wraps PyInstaller and bundles NiceGUI's static assets.
nicegui-pack --onefile --windowed --name "$APP_NAME" "$ENTRY"

if [ ! -d "dist/$APP_NAME.app" ]; then
  echo "!! dist/$APP_NAME.app was not produced — check the PyInstaller output above."
  exit 1
fi

echo "==> disk image"
rm -f "$APP_NAME.dmg"
hdiutil create -volname "$APP_NAME" \
               -srcfolder "dist/$APP_NAME.app" \
               -ov -format UDZO "$APP_NAME.dmg"

cat <<EOF

Done.
  dist/$APP_NAME.app   double-click to run
  $APP_NAME.dmg        drag-to-install image

Two things to know before handing the .dmg to anyone else:

1. Chromium is NOT inside the bundle. Playwright keeps it in
   ~/Library/Caches/ms-playwright, so the app works on THIS Mac but a fresh
   machine needs 'python -m playwright install chromium' first. Fine for a
   demo on your own laptop; a real installer needs the browser bundled.

2. The app is unsigned. macOS Gatekeeper will refuse the first launch — open
   it once with right-click > Open, or run:
       xattr -dr com.apple.quarantine "dist/$APP_NAME.app"
   Shipping it properly needs an Apple Developer ID and notarisation.
EOF
