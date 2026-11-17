#!/bin/sh
# Run photocull from anywhere:  ./photocull.sh /path/to/photos
PYTHONPATH="$(cd "$(dirname "$0")" && pwd)" exec python3 -m photocull "$@"
