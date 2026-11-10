#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -x .venv/bin/packfinder ] || ! .venv/bin/python -c 'import telethon, packfinder' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e '.[telegram]'
fi
exec .venv/bin/packfinder telegram-setup --session data/telegram
