#!/usr/bin/bash -p
# Trusted isolated bootstrap; preserve every caller argument as argv.
set -euo pipefail
exec /usr/bin/python3 -I -c '
import pathlib, sys
source = pathlib.Path(sys.argv[1]).resolve()
root = source.parent
sys.path.insert(0, str(root))
from plaud_linux.integration import entry
entry("install", source, sys.argv[2:])
' "${BASH_SOURCE[0]}" "$@"
