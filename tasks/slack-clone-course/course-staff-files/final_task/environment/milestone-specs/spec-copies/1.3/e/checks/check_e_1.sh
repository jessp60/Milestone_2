#!/usr/bin/env bash
# Check for E.1. Prints PASS/FAIL per rule in /spec/e/e-1.md.
# Usage: bash /spec/e/checks/check_e_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

boot; scenario e_1.py 1.1 1.2 1.3 1.4 1.5 1.6 1.7 1.8 1.9 1.10 1.11 1.12 1.13 1.14 ; finish
