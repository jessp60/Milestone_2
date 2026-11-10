#!/usr/bin/env bash
# Check for E.4. Prints PASS/FAIL per rule in /spec/e/e-4.md.
# Usage: bash /spec/e/checks/check_e_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario e_4.py 4.1 4.2 4.3 4.4 4.5 4.6 4.7 ; finish
