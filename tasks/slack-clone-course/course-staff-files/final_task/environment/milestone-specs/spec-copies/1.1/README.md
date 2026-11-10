# Read this first: work order and rules for this run

This run builds Component C (features built on messages) in 5 steps that build on each other. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen every check for these steps pass, from output you saw after your last code change. Do not submit unless that is true.

The task description you were given is still the grading contract. The files in `/spec` do not change it; they tell you what to work on now and how to prove it works. You have 50 minutes in total and cannot see a clock, so run `date` at the start and again after each step.

## Files in /spec

| File | What it is | Builds on |
|---|---|---|
| `/spec/README.md` | This file: scope, work order, how to verify, when you may submit | |
| `/spec/c/c-1.md` | C.1: profiles (rules 1.1 to 1.10) | the basics below |
| `/spec/c/c-2.md` | C.2: reactions and direct messages (rules 2.1 to 2.16) | C.1 |
| `/spec/c/c-3.md` | C.3: files (rules 3.1 to 3.16) | C.2 |
| `/spec/c/c-4.md` | C.4: slash commands (rules 4.1 to 4.13) | C.3 |
| `/spec/c/c-5.md` | C.5: user groups and group mentions (rules 5.1 to 5.19) | C.4 |
| `/spec/c/checks/` `check_c_1.sh` to `check_c_5.sh` | Check the rules of one C step each | |
| `/spec/c/checks/lib.sh` | Helpers the C checks load (not run directly) | |

## Scope

Build only what these steps cover, plus the basics they stand on. `/app` starts empty, so first build the smallest cluster that works:

- three HTTP nodes on `127.0.0.1:8000`, `:8001` and `:8002`, each its own process, that share state and are started by
  `/app/start.sh`, with `GET /api/health` returning `200 {"status":"ok","node_id":N}` on each (node ids 0, 1, 2) and a WebSocket
  at `/api/ws?token=<bearer>` that rejects a bad token;
- register and login (`POST /api/auth/register`, `POST /api/auth/login`) with bearer tokens;
- workspaces with a `#general` channel (`POST /api/workspaces`), channels in a workspace (create, list, join, private channels,
  topic, archive and unarchive), and `GET /api/channels/{id}/read` returning `{read_state:{unread_count, mention_count}}`;
- posting and listing messages (`POST` and `GET /api/channels/{id}/messages`, with `201 {message}` on a post and `mentions`,
  `files` and `reactions` on every message);
- the WebSocket events `subscribed`, `message.created` (with a dense per-channel `event_id`) and `resumed`.

Do not build parts of the Slack clone these steps do not cover. If `/app` already has code, improve it and do not start over.

All routes are under `/api/...`, return JSON, and take `Authorization: Bearer <token>`. Status codes: `200` on read or update success, `201` on create, `400` on validation, `401` on missing or invalid auth, `403` on auth-but-forbidden, `404` on unknown id, `409` on conflict, `413` on upload over the limit, `423` on writes to an archived channel.

## Order of work

1. Build the basics above. Run `date`.
2. Read `/spec/c/c-1.md`, then build C.1 and run `check_c_1.sh` until every rule passes.
3. Read `/spec/c/c-2.md`, then build C.2 and run `check_c_2.sh` until every rule passes.
4. Read `/spec/c/c-3.md`, then build C.3 and run `check_c_3.sh` until every rule passes.
5. Read `/spec/c/c-4.md`, then build C.4 and run `check_c_4.sh` until every rule passes.
6. Read `/spec/c/c-5.md`, then build C.5 and run `check_c_5.sh` until every rule passes.
7. Run all 5 checks one last time, each as its own command.

After each change, run the check of the step you are on and the check of the step before it, so a fix in one step does not quietly break an earlier one. Run every check again when a step first passes and once more before you submit.

## How to verify

Run one check per command:

```bash
bash /spec/c/checks/check_c_1.sh
bash /spec/c/checks/check_c_2.sh
bash /spec/c/checks/check_c_3.sh
bash /spec/c/checks/check_c_4.sh
bash /spec/c/checks/check_c_5.sh
```

Each check ends with a line such as `RESULT: 12 passed, 0 failed`.

- Each check finishes in under 30 seconds, and a command that runs longer is cut off with no output, so never chain two checks into one command.
- A check starts `/app/start.sh` itself if the cluster is not answering. After you change code, restart the cluster before checking again: find the process that owns each port with `ss -ltnp` and stop it by PID. Never stop processes by name (`pkill -f`, `killall` and `pgrep -f` can match your own shell).
- State must be shared across the three nodes, so a write that lands on one node is visible on the other two straight away. Every error path must finish its database work (commit or roll back) and every response needs a `Content-Length`, or the next request can hang.
- Some checks kill a node, Redis or both on purpose (by the PID that owns the port). After such a check your `/app/start.sh` must bring the missing pieces back by itself.
- Do not edit any file in `/spec`, including the checks. If a check fails, fix your code, not the check.

## Before you submit

You may submit only when all of these are true, from output you actually saw after your last code change:

- all 5 checks end with `0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001` and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
