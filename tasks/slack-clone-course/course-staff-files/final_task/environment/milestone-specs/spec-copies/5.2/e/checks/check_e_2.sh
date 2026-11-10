#!/usr/bin/env bash
# Check for E.2. Prints PASS/FAIL per rule in /spec/e/e-2.md.
# Usage: bash /spec/e/checks/check_e_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario e_2.py 2.1 2.2 2.3 2.4 2.5 2.6 2.7 2.8 2.9 2.10 2.11 2.12 2.13 ; finish
