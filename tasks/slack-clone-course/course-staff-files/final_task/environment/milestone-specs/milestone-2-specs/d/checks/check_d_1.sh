#!/usr/bin/env bash
# Check for D.1. Prints PASS/FAIL per rule in /spec/d/d-1.md.
# Usage: bash /spec/d/checks/check_d_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario d_1.py 1.1 1.2 1.3 1.4 1.5 1.6 1.7 1.8; finish
