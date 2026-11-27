#!/bin/bash
# Double-click this file to start the quiz server on a Mac.
# If Python isn't available, it downloads a private copy into this folder
# (no admin password needed, nothing installed system-wide).
cd "$(dirname "$0")" || exit 1

PY=""
for c in "./.python/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$c" ] && "$c" -c "import sys; assert sys.version_info >= (3, 8)" 2>/dev/null; then PY="$c"; break; fi
done
# Apple's built-in python3 only works once the Command Line Tools are installed
if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c "import sys" 2>/dev/null; then
  PY=/usr/bin/python3
fi

if [ -z "$PY" ]; then
  echo "Python not found - downloading a private copy into this folder (about 20 MB)..."
  case "$(uname -m)" in
    arm64) ARCH=aarch64; SUM=4c18852bf9c1a11b56f21bcf0df1946f7e98ee43e9e4c0c5374b2b3765cf9508 ;;
    *)     ARCH=x86_64;  SUM=60c5271e7edc3c2ab47440b7abf4ed50fbc693880b474f74f05768f5b657045a ;;
  esac
  URL="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7+20241016-$ARCH-apple-darwin-install_only.tar.gz"
  TMP="$(mktemp -t quizpython)"
  if curl -fL --progress-bar -o "$TMP" "$URL" && \
     [ "$(shasum -a 256 "$TMP" | cut -d' ' -f1)" = "$SUM" ]; then
    rm -rf ./.python ./python && tar -xzf "$TMP" && mv ./python ./.python && PY="./.python/bin/python3"
  else
    echo "Download failed or file did not match the expected checksum."
  fi
  rm -f "$TMP"
fi

if [ -z "$PY" ] || ! "$PY" -c "import sys" 2>/dev/null; then
  echo
  echo "Could not set up Python automatically."
  echo "Install Python 3.12 from https://www.python.org/downloads/ (or ask IT),"
  echo "then double-click this file again."
  read -r -p "Press Enter to close."
  exit 1
fi

# caffeinate keeps the Mac awake while the quiz is running
caffeinate -i "$PY" quiz_server.py
read -r -p "Press Enter to close."
