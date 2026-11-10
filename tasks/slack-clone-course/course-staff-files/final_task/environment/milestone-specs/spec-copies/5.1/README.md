# Read this first: work order and rules for this run

This run builds Component A (roles and invitations), Component B (channels, message history, edit, delete and reactions), Component C (features built on messages), Component D (cluster robustness) and Component E (IRC, web UI and message edits). A and B come first and are guarded by one regression suite, `/spec/checks/regression.sh`; then C, D and E are built in 13 steps that build on each other. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen the regression suite and every check for the C, D and E steps pass, from output you saw after your last code change. Do not submit unless that is true.

The task description you were given is still the grading contract. The files in `/spec` do not change it; they tell you what to work on now and how to prove it works. You have 50 minutes in total and cannot see a clock, so run `date` at the start and again after each step.

## Files in /spec

| File | What it is | Builds on |
|---|---|---|
| `/spec/README.md` | This file: scope, work order, how to verify, when you may submit | |
| `/spec/checks/regression.sh` | The regression suite for A and B: runs all their checks at once, plus boot conditions 1 to 3 (about 15 seconds) and prints every failing rule and a final `REGRESSION:` line | the basics below |
| `/spec/c/c-1.md` | C.1: profiles (rules 1.1 to 1.10) | A and B |
| `/spec/c/c-2.md` | C.2: reactions and direct messages (rules 2.1 to 2.16) | C.1 |
| `/spec/c/c-3.md` | C.3: files (rules 3.1 to 3.16) | C.2 |
| `/spec/c/c-4.md` | C.4: slash commands (rules 4.1 to 4.13) | C.3 |
| `/spec/c/c-5.md` | C.5: user groups and group mentions (rules 5.1 to 5.19) | C.4 |
| `/spec/d/d-1.md` | D.1: a killed HTTP node does not stop the cluster (rules 1.1 to 1.8) | C.5 |
| `/spec/d/d-2.md` | D.2: the killed node comes back (rules 2.1 to 2.6) | D.1 |
| `/spec/d/d-3.md` | D.3: a killed Redis does not stop fan-out (rules 3.1 to 3.8) | D.2 |
| `/spec/d/d-4.md` | D.4: WebSocket speed under load (rules 4.1 to 4.4) | D.3 |
| `/spec/e/e-1.md` | E.1: editing and deleting messages (rules 1.1 to 1.14) | D.4 |
| `/spec/e/e-2.md` | E.2: the IRC gateway (rules 2.1 to 2.13) | E.1 |
| `/spec/e/e-3.md` | E.3: IRC while a node is killed (rules 3.1 to 3.7) | E.2 |
| `/spec/e/e-4.md` | E.4: the web UI page (rules 4.1 to 4.7) | E.3 |
| `/spec/c/checks/` `check_c_1.sh` to `check_c_5.sh` | Check the rules of one C step each | |
| `/spec/c/checks/lib.sh` | Helpers the C checks load (not run directly) | |
| `/spec/d/checks/` `check_d_1.sh` to `check_d_4.sh` | Check the rules of one D step each | |
| `/spec/d/checks/lib.sh` | Helpers the D checks load (not run directly) | |
| `/spec/d/checks/ws.py` and `d_1.py` to `d_4.py` | Scenario code the D checks run (not run directly) | |
| `/spec/e/checks/` `check_e_1.sh` to `check_e_4.sh` | Check the rules of one E step each | |
| `/spec/e/checks/lib.sh` | Helpers the E checks load (not run directly) | |
| `/spec/e/checks/ws.py` and `e_1.py` to `e_4.py` | Scenario code the E checks run (not run directly) | |

## Scope

Build only what these steps cover, plus the basics they stand on. `/app` starts empty, so first build the smallest cluster that works:

- three HTTP nodes on `127.0.0.1:8000`, `:8001` and `:8002`, each its own process, that share state and are started by
  `/app/start.sh`, with `GET /api/health` returning `200 {"status":"ok","node_id":N}` on each (node ids 0, 1, 2) and a WebSocket
  at `/api/ws?token=<bearer>` that rejects a bad token.

Components A and B are the base of the Slack clone. C, D and E build on them: registration and login, workspaces and their channels, and posting and listing messages come from A and B.

Component D also needs a local Redis server (`redis-server`) on `127.0.0.1:6379` that `/app/start.sh` starts and restarts if it dies.

Component E needs message reactions (`POST /api/messages/{id}/reactions`, `DELETE /api/messages/{id}/reactions/{emoji}`) for its edit and delete rules and its web page.

Do not build parts of the Slack clone these steps do not cover. If `/app` already has code, improve it and do not start over.

Some features appear in more than one step (for example reactions in B.2 and C.2, and message edit and delete in B.2 and E.1): build each one once and make every step's check pass. Killed HTTP nodes and a killed Redis are checked in D; IRC bridging while an HTTP node is down is checked in E.3 (the IRC gateway itself is E.2).

All routes are under `/api/...`, return JSON, and take `Authorization: Bearer <token>`. Status codes: `200` on read or update success, `201` on create, `400` on validation, `401` on missing or invalid auth, `403` on auth-but-forbidden, `404` on unknown id, `409` on conflict, `413` on upload over the limit, `423` on writes to an archived channel.

## Order of work

1. Build the cluster above. Run `date`.
2. Build Components A and B. There are no written specs for them: read `/spec/checks/regression.sh`, which holds all their checks, to see what it asks for. Run `bash /spec/checks/regression.sh` until it ends with `REGRESSION: A and B: 0 broken`.
3. Read `/spec/c/c-1.md`, then build C.1 and run `check_c_1.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
4. Read `/spec/c/c-2.md`, then build C.2 and run `check_c_2.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
5. Read `/spec/c/c-3.md`, then build C.3 and run `check_c_3.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
6. Read `/spec/c/c-4.md`, then build C.4 and run `check_c_4.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
7. Read `/spec/c/c-5.md`, then build C.5 and run `check_c_5.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
8. Read `/spec/d/d-1.md`, then build D.1 and run `check_d_1.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
9. Read `/spec/d/d-2.md`, then build D.2 and run `check_d_2.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
10. Read `/spec/d/d-3.md`, then build D.3 and run `check_d_3.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
11. Read `/spec/d/d-4.md`, then build D.4 and run `check_d_4.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
12. Read `/spec/e/e-1.md`, then build E.1 and run `check_e_1.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
13. Read `/spec/e/e-2.md`, then build E.2 and run `check_e_2.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
14. Read `/spec/e/e-3.md`, then build E.3 and run `check_e_3.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
15. Read `/spec/e/e-4.md`, then build E.4 and run `check_e_4.sh` until every rule passes. Then run `regression.sh` and fix anything it reports as broken before you go to the next step.
16. Run `regression.sh` and all 13 checks one last time, each as its own command.

After every step, run `bash /spec/checks/regression.sh` and fix any breakage before you move on, because work on C, D and E can break the A and B features that already pass. After each change, also run the check of the step you are on and the check of the step before it. Run every check again when a step first passes and once more before you submit.

## How to verify

Run one check per command:

```bash
bash /spec/checks/regression.sh
bash /spec/c/checks/check_c_1.sh
bash /spec/c/checks/check_c_2.sh
bash /spec/c/checks/check_c_3.sh
bash /spec/c/checks/check_c_4.sh
bash /spec/c/checks/check_c_5.sh
bash /spec/d/checks/check_d_1.sh
bash /spec/d/checks/check_d_2.sh
bash /spec/d/checks/check_d_3.sh
bash /spec/d/checks/check_d_4.sh
bash /spec/e/checks/check_e_1.sh
bash /spec/e/checks/check_e_2.sh
bash /spec/e/checks/check_e_3.sh
bash /spec/e/checks/check_e_4.sh
```

Each check ends with a line such as `RESULT: 12 passed, 0 failed`. The regression suite ends with a line such as `REGRESSION: A and B: 0 broken (132 rules pass)`.

- Each check finishes in under 30 seconds, and a command that runs longer is cut off with no output, so never chain two checks into one command.
- A check starts `/app/start.sh` itself if the cluster is not answering. After you change code, restart the cluster before checking again: find the process that owns each port with `ss -ltnp` and stop it by PID. Never stop processes by name (`pkill -f`, `killall` and `pgrep -f` can match your own shell).
- State must be shared across the three nodes, so a write that lands on one node is visible on the other two straight away. Every error path must finish its database work (commit or roll back) and every response needs a `Content-Length`, or the next request can hang.
- Some checks kill a node, Redis or both on purpose (by the PID that owns the port). After such a check your `/app/start.sh` must bring the missing pieces back by itself.
- Do not edit any file in `/spec`, including the checks. If a check fails, fix your code, not the check.

## Before you submit

You may submit only when all of these are true, from output you actually saw after your last code change:

- `bash /spec/checks/regression.sh` ends with `REGRESSION: A and B: 0 broken`;
- all 13 checks for C, D and E end with `0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001` and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
