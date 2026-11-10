# Read this first: work order and rules for this run

This run builds Component A (roles and invitations), Component B (channels, message history, edit, delete and reactions), Component C (features built on messages), Component D (cluster robustness) and Component E (IRC, web UI and message edits) in 20 steps that build on each other. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen every check for these steps pass, from output you saw after your last code change. Do not submit unless that is true.

The task description you were given is still the grading contract. The files in `/spec` do not change it; they tell you what to work on now and how to prove it works. You have 50 minutes in total and cannot see a clock, so run `date` at the start and again after each step.

| `/spec/a/a-1.md` | A.1: auth, workspaces, channels and open join (rules A1.1 to A1.28) | the basics below |
| `/spec/a/a-2.md` | A.2: roles, role-based channel permissions and ownership transfer (rules A2.1 to A2.20) | A.1 |
| `/spec/a/a-3.md` | A.3: invitations and join mode (rules A3.1 to A3.30) | A.2 |
| `/spec/b/b-1.md` | B.1: channel management (rules B1.1 to B1.22) | A.3 |
| `/spec/b/b-2.md` | B.2: messages, replies, paging, edit, delete and reactions (rules B2.1 to B2.29) | B.1 |
| `/spec/b/b-3.md` | B.3: search, pins and mentions (rules B3.1 to B3.18) | B.2 |
| `/spec/b/b-4.md` | B.4: channel events, read state and API conventions (rules B4.1 to B4.24) | B.3 |
| `/spec/c/c-1.md` | C.1: profiles (rules 1.1 to 1.10) | B.4 |
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
| `/spec/a/checks/` `check_a_1.sh` to `check_a_3.sh` | Check the rules of one A step each | |
| `/spec/a/checks/check_boot.sh` | The four baseline boot conditions, written in `/spec/a/a-1.md` (conditions 1 to 3; `--kill` adds condition 4) | |
| `/spec/a/checks/lib.sh` | Helpers the A checks load (not run directly) | |
| `/spec/b/checks/` `check_b_1.sh` to `check_b_4.sh` | Check the rules of one B step each | |
| `/spec/b/checks/lib.sh` | Helpers the B checks load (not run directly) | |
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
2. Read `/spec/a/a-1.md`, then build A.1 and run `check_a_1.sh` and `check_boot.sh` until every rule passes.
3. Read `/spec/a/a-2.md`, then build A.2 and run `check_a_2.sh` until every rule passes.
4. Read `/spec/a/a-3.md`, then build A.3 and run `check_a_3.sh` until every rule passes.
5. Read `/spec/b/b-1.md`, then build B.1 and run `check_b_1.sh` until every rule passes.
6. Read `/spec/b/b-2.md`, then build B.2 and run `check_b_2.sh` until every rule passes.
7. Read `/spec/b/b-3.md`, then build B.3 and run `check_b_3.sh` until every rule passes.
8. Read `/spec/b/b-4.md`, then build B.4 and run `check_b_4.sh` until every rule passes.
9. Read `/spec/c/c-1.md`, then build C.1 and run `check_c_1.sh` until every rule passes.
10. Read `/spec/c/c-2.md`, then build C.2 and run `check_c_2.sh` until every rule passes.
11. Read `/spec/c/c-3.md`, then build C.3 and run `check_c_3.sh` until every rule passes.
12. Read `/spec/c/c-4.md`, then build C.4 and run `check_c_4.sh` until every rule passes.
13. Read `/spec/c/c-5.md`, then build C.5 and run `check_c_5.sh` until every rule passes.
14. Read `/spec/d/d-1.md`, then build D.1 and run `check_d_1.sh` until every rule passes.
15. Read `/spec/d/d-2.md`, then build D.2 and run `check_d_2.sh` until every rule passes.
16. Read `/spec/d/d-3.md`, then build D.3 and run `check_d_3.sh` until every rule passes.
17. Read `/spec/d/d-4.md`, then build D.4 and run `check_d_4.sh` until every rule passes.
18. Read `/spec/e/e-1.md`, then build E.1 and run `check_e_1.sh` until every rule passes.
19. Read `/spec/e/e-2.md`, then build E.2 and run `check_e_2.sh` until every rule passes.
20. Read `/spec/e/e-3.md`, then build E.3 and run `check_e_3.sh` until every rule passes.
21. Read `/spec/e/e-4.md`, then build E.4 and run `check_e_4.sh` until every rule passes.
22. Run all 21 checks one last time, each as its own command.

After each change, run the check of the step you are on and the check of the step before it, so a fix in one step does not quietly break an earlier one. Run every check again when a step first passes and once more before you submit.

## How to verify

Run one check per command:

```bash
bash /spec/a/checks/check_a_1.sh
bash /spec/a/checks/check_boot.sh
bash /spec/a/checks/check_a_2.sh
bash /spec/a/checks/check_a_3.sh
bash /spec/b/checks/check_b_1.sh
bash /spec/b/checks/check_b_2.sh
bash /spec/b/checks/check_b_3.sh
bash /spec/b/checks/check_b_4.sh
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

Each check ends with a line such as `RESULT: 12 passed, 0 failed`.

- Each check finishes in under 30 seconds, and a command that runs longer is cut off with no output, so never chain two checks into one command.
- A check starts `/app/start.sh` itself if the cluster is not answering. After you change code, restart the cluster before checking again: find the process that owns each port with `ss -ltnp` and stop it by PID. Never stop processes by name (`pkill -f`, `killall` and `pgrep -f` can match your own shell).
- State must be shared across the three nodes, so a write that lands on one node is visible on the other two straight away. Every error path must finish its database work (commit or roll back) and every response needs a `Content-Length`, or the next request can hang.
- Some checks kill a node, Redis or both on purpose (by the PID that owns the port). After such a check your `/app/start.sh` must bring the missing pieces back by itself.
- Do not edit any file in `/spec`, including the checks. If a check fails, fix your code, not the check.

## Before you submit

You may submit only when all of these are true, from output you actually saw after your last code change:

- all 20 stage checks end with `0 failed`, and `check_boot.sh` ends with `RESULT: 3 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001` and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
