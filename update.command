#!/bin/sh
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
export PYTHONUTF8=1
if [ -x .venv/bin/python ]; then
  .venv/bin/python kb.py update "$@"
else
  python3 kb.py update "$@"
fi
kb_exit=$?
printf '\nPress Enter to close... '
read -r kb_reply
exit "$kb_exit"
