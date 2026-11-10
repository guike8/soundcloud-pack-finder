#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -x .venv/bin/packfinder ] || ! .venv/bin/python -c 'import packfinder' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e .
fi
if [ "$#" -eq 0 ]; then
  .venv/bin/packfinder doctor
else
  .venv/bin/packfinder "$@"
fi
