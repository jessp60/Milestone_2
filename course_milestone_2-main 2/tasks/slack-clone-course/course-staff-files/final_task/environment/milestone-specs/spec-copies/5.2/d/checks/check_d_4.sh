#!/usr/bin/env bash
# Check for D.4. Prints PASS/FAIL per rule in /spec/d/d-4.md.
# Usage: bash /spec/d/checks/check_d_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario d_4.py 4.1 4.2 4.3 4.4; finish
