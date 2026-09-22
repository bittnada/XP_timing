#!/bin/sh
set -eu
# Run from the installed bin directory. Never rebuild a DB implicitly.
exec python3 dreamplace/Placer.py "${TC_CONFIG:-dreamplace/examples/two_db_superblue1.json}" "$@"
