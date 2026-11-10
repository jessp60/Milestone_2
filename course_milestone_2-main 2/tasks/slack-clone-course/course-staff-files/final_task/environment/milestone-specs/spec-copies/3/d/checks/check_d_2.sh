#!/usr/bin/env bash
# Check for D.2. Prints PASS/FAIL per rule in /spec/d/d-2.md.
# Usage: bash /spec/d/checks/check_d_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario d_2.py 2.1 2.2 2.3 2.4 2.5 2.6; finish
