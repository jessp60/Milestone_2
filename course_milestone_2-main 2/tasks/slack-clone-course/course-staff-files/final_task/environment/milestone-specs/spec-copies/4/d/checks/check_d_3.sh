#!/usr/bin/env bash
# Check for D.3. Prints PASS/FAIL per rule in /spec/d/d-3.md.
# Usage: bash /spec/d/checks/check_d_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario d_3.py 3.1 3.2 3.3 3.4 3.5 3.6 3.7 3.8; finish
