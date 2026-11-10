# Milestone 2 report

- Name: Jessica Pinto
- UNI: jp4908

All results are from my own checks.

## Runs

One row per repeat of each run. "Run" is the run in the run plan in README Setup. The rows in this table are my official runs, and the Experiments section below uses only these. I also kept extra sample runs (earlier and repeated attempts) in the job portfolio so that I could analyze performance; they are not counted as official. Runs that cost more than $3 ran with the per-run cost cap raised to $5, as staff advised.

| Run | Component | Repeat | Job folder | Check results | Cost | Ran checks (times) | Submitted with failures? | Problems |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 | C | 1 | `jobs/2026-11-04__20-47-21/` (session `final_task__WfaBRbs__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0 (passed/failed) | about $0.35 (45 steps, about 3 minutes) | 24 (3 of them on a `verbose` copy of a check) | No | Built the cluster and C.1 to C.5 from an empty `/app`. C.2 failed 6/10 and 9/4 at first and C.3 failed rules at first; all fixed. After a late change C.5 fell to 12 passed, 7 failed (the `@here` rule 5.17 and its follow-ups), then 18/1 and 19/0; the agent reran all five checks before submitting. In this `score_only` run the agent made `verbose` copies of the checks (`sed` on `FEEDBACK`) to see why rules failed. |
| 1.1 | C | 2 | `jobs/2026-11-04__20-59-33/` (session `final_task__TgGgZt8__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0 (passed/failed) | about $0.61 (67 steps) | 35 (several on a `verbose` copy of a check) | No | Built the cluster and C.1 to C.5 from an empty `/app`. Early failures in C.1 (12/4), C.3 (14/2), C.4 (8/5) and C.5 (9/10), all fixed. After the C.5 work C.1 regressed to 9 passed, 1 failed (rule 1.10, `user.updated` not reaching a WebSocket on another node, because the cross-node event watcher had been disabled) and stayed at 9/1 across about 15 check runs and many debugging steps before the agent re-enabled the watcher (10/0). It then reran all five checks (10, 16, 16, 13, 19) before submitting. In this `score_only` run the agent again ran `verbose` copies of the checks to see why rules failed. |
| 1.2 | D | 1 | `jobs/2026-11-04__21-19-53/` (session `final_task__jHWFwsp__agent`) | D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0 (passed/failed) | about $0.17 (29 steps, about 2 minutes) | 11 | No | Built the cluster, Redis supervision and D.1 to D.4 from an empty `/app`. The first three runs of `check_d_1.sh` printed only `RESULT: 0 passed, 8 failed`: the check stopped during its setup, and `score_only` gave no reason, so the agent spent about 15 steps probing by hand. It found that its server read the WebSocket token only from the Authorization header and not from the `?token=` query string the checks use (handshake 401), and after fixing that, nodes 1 and 2 were still running the old code until their processes were restarted. After that D.1 to D.4 passed, and it ran all four checks once more (8/6/8/4) before submitting. No `verbose` copies of the checks this time. |
| 1.2 | D | 2 | `jobs/2026-11-04__21-26-06/` (session `final_task__ZAGb6Fo__agent`) | D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0 (passed/failed) | about $0.08 (18 steps, about 1 minute) | 8 | No | Built the cluster, Redis supervision and D.1 to D.4 from an empty `/app`. Every check passed on its first run (8, 6, 8, 4), and the agent then reran all four once more before submitting, so there were no failures to fix. It read the check scripts (`ws.py`, `d_1.py` to `d_4.py`) before writing any code and built exactly what they exercise, using a shared store, polling-based cross-node WebSocket fan-out and a supervisor that respawns killed nodes and Redis. No `verbose` copies of the checks. |
| 1.3 | E | 1 | `jobs/2026-11-04__21-33-08/` (session `final_task__ybqeVwo__agent`) | E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $0.45 (48 steps, about 7.5 minutes) | 8 (each of E.1 to E.4 once, then each again after the last code change) | No | Built the cluster and E.1 to E.4 from an empty `/app`. `score_only` gave only a summary line, so the agent ran the scenario scripts directly to find the failing rules. First results were E.1 13/1, E.2 10/3, E.3 5/2, E.4 6/1. Fixed: a WebSocket subscribe/auth failure (rule 1.14), an undefined `broadcast_web` in the IRC bridge, node 1's WebSocket not delivering cross-node messages, the IRC gateway working only on node 0, and a missing `create-invitation-form` id in the web UI. No `verbose` copies of the checks. |
| 1.3 | E | 2 | `jobs/2026-11-04__21-47-37/` (session `final_task__uBZ4qnM__agent`) | E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $0.35 (45 steps) | 13 (E.1 x3, E.2 x5, E.3 x2, E.4 x3) | No | Built the cluster and E.1 to E.4 from an empty `/app`. `score_only` gave only a summary line, so the agent ran the scenario scripts directly to find the failing rules. First results were E.1 13/1 (cross-node WebSocket delivery), E.2 5/8, E.3 7/0 and E.4 6/1 (modal ids missing from the HTML). E.2 took the most work (5/8, 11/2, 12/1, 13/0). A Python patch it wrote through the shell left literal `\n` sequences in `app.py`, so the file stopped compiling and the HTTP workers went down. It also had a stale supervisor PID and an IRC gateway that did not bind until it was started by hand. Fixes after that: nick-collision tracking and web-to-IRC bridging across processes. It reran all four checks after the last change (14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. No `verbose` copies of the checks. |
| 2.1 | C | 1 | `jobs/2026-11-04__22-05-19/` (session `final_task__9B67VzJ__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0 (passed/failed) | about $0.21 (30 steps, about 3 minutes) | 15 (C.1 x2, C.2 x3, C.3 x3, C.4 x4, C.5 x2, with one extra C.2 run) | No | `verbose` run. The agent read the per-rule `PASS`/`FAIL` lines and fixed rules directly, with no need to build its own debug copies of the checks. Built the cluster and C.1 to C.5 from an empty `/app`. C.1 and C.2 passed on the first run. C.3 failed 6/10 first (multipart parsing and file metadata, file-attach rules 3.11 to 3.16), fixed in two steps. C.4 first printed `0 passed, 13 failed` ("setup failed, rule not run"), because its setup uses `POST /api/workspaces/{slug}/channels`, which was not built yet. After adding the channel routes it got 11/2, 12/1 (`/me` with only spaces) and then 13/0. C.5 first got 5/14 (no user-group routes), then 19/0 after adding group CRUD, `@handle` and `@here` expansion. It reran all five checks after the last code change (10/16/16/13/19) and confirmed health on ports 8000 to 8002 before submitting. |
| 2.1 | C | 2 | `jobs/2026-11-04__22-28-26/` (session `final_task__ksatYZJ__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0 (passed/failed) | about $0.43 (49 steps, about 4.5 minutes) | 18 (C.1 x2, C.2 x5, C.3 x2, C.4 x6, C.5 x3) | No | `verbose` run. Built the cluster and C.1 to C.5 from an empty `/app`. The agent copied the checks to `/tmp` only to read their rule bodies before coding, and did not edit them. The first cluster start failed because the container's `/bin/sh` did not support the launcher's wait syntax, fixed in a few steps. C.1 passed first time. C.2 failed 13/3 three times (message-list GET route missing, then 405s) because restarts left stale node processes running old code, until a clean restart gave 16/0. C.3 passed 16/0 first time. C.4 failed 10/3 four times (SQLite 0/1 integers, an inverted guard that rejected `/unarchive` with 423) for the same stale-process reason, and passed 13/0 after a clean restart. C.5 went 14/5, then 15/4, then 19/0. A shell-written migration patch left literal `\n` in the Python source and broke startup, and a global `handle` uniqueness constraint in the schema blocked group rules. It reran C.1 to C.4 after the last code change (10/16/16/13) and confirmed health on ports 8000 to 8002 before submitting. |
| 2.2 | D | 1 | `jobs/2026-11-04__22-38-26/` (session `final_task__KZDJ8CC__agent`) | D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0 (passed/failed) | about $0.09 (17 steps, about 1.5 minutes) | 9 (D.1 x3, D.2 x2, D.3 x2, D.4 x2) | No | `verbose` run. Built the cluster, Redis supervision and D.1 to D.4 from an empty `/app`. The only failure was on the first D.1 run: 0/8, with every rule printing `setup failed: WebSocket: not upgraded: HTTP/1.1 401 Unauthorized`. The reason was on screen, so the agent fixed it in one step: its server read the WebSocket token only from the Authorization header and not from the `?token=` query string the checks use. D.1 then passed 8/0, and D.2, D.3 and D.4 passed on their first run (6/8/4). It reran all four checks after the last code change (8/6/8/4) and confirmed health on ports 8000 to 8002 before submitting. No debug copies of the checks. |
| 2.2 | D | 2 | `jobs/2026-11-04__22-44-08/` (session `final_task__qca3NgH__agent`) | D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0 (passed/failed) | about $0.12 (22 steps, about 1.5 minutes) | 9 (D.1 x3, D.2 x2, D.3 x2, D.4 x2) | No | `verbose` run. Built the cluster, Redis supervision and D.1 to D.4 from an empty `/app`. It read the check scripts (`ws.py`, `d_1.py` to `d_4.py`) before writing any code. One syntax error (an extra closing parenthesis in the WebSocket handler) stopped the first start, fixed in one step. The only check failure was on the first D.1 run, 7/1: rule 1.8, resume on node 2 after the kill replayed duplicate events instead of only the missed ones `[2, 3]`. The `verbose` line named the rule and the events, and the agent fixed the replay cursor in one step. After restarting the nodes D.1 passed 8/0, and D.2, D.3 and D.4 passed on their first run (6/8/4). It reran all four checks after the last code change (8/6/8/4) and confirmed health on ports 8000 to 8002 before submitting. No debug copies of the checks. |
| 2.3 | E | 1 | `jobs/2026-11-04__22-49-55/` (session `final_task__VZB8XiT__agent`) | E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $0.34 (40 steps, about 4.5 minutes) | 17 (E.1 x6, E.2 x6, E.3 x2, E.4 x3) | No | `verbose` run. Built the cluster and E.1 to E.4 from an empty `/app`. The first start failed on a Python indentation/syntax error in the WebSocket loop (an inner `try` without its `except`, then an outer one with an extra parenthesis), which took about 6 steps to repair with shell patches. E.1 then stayed at 13/1 for four runs: a fresh subscription replayed all old events, and the server was blocked in a buffered `rfile.read(2)` after a socket timeout. The agent fixed it by changing the WebSocket read loop, and E.1 passed 14/0. E.2 went 10/3, then 0/13 twice because the IRC gateway process stopped serving after an edit (the socket timeout escaped the handler), then 12/1 and 13/0 after a nick-collision fix. E.3 passed 7/0 on its first run. E.4 failed 6/1 (the initial SPA HTML was missing a pinned id) and passed 7/0 after the fix. It reran all four checks after the last code change (14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. No debug copies of the checks. |
| 2.3 | E | 2 | `jobs/2026-11-04__23-16-40/` (session `final_task__esxA6jB__agent`) | E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $0.28 (37 steps, about 4 minutes) | 15 (E.1 x5, E.2 x5, E.3 x2, E.4 x3) | No | `verbose` run. Built the cluster and E.1 to E.4 from an empty `/app`. It read the check scripts (`ws.py`, `e_1.py` to `e_4.py`) before writing any code. The first start failed because no HTTP ports came up (a code error and a stale supervisor), fixed in about 3 steps. E.1 stayed at 13/1 for three runs: WebSocket subscription timed out (the handler used the wrong upgrade key), then rule 1.14 failed because `subscribe` replayed old history instead of starting at the current head. It then passed 14/0. E.2 went 8/5, then 12/1 twice, then 13/0. A shell patch to the IRC gateway introduced an indentation error and a bare `try:` without `except`, and the last failure was nick collision (the gateway ignored `USER` when the first nick had been rejected). E.3 passed 7/0 on its first run. E.4 failed 5/2 (four pinned selectors missing from the initial HTML) and passed 7/0 after they were added. It reran all four checks after the last code change (14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. No debug copies of the checks. |
| 3 | C, D, E | 1 | `jobs/2026-11-05__09-59-51/` (session `final_task__wTp8Nby__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $1.45 (97 steps, about 14 minutes) | 54 (C 27, D 15, E 12; the final pass reran all 13 checks once each) | No | Built C.1 to E.4 as one cluster from an empty `/app`, in order. C.1 and C.2 passed on the first run. C.3 failed 5/11 and 6/10 (multipart uploads timing out), then 0/16 after an edit broke setup, then 13/3 and 16/0. C.4 went 8/5, 10/3, 12/1, 13/0. C.5 was the longest stretch: 5/14, 4/15, 5/14 and 5/14 (an SQLite write lock), then 18/1 for six check runs, all on rule 5.17 (`@here` must mention a channel member with an open WebSocket) because presence was not registered reliably, until it reached 19/0. D.1 went 4/4, 4/4, 5/3, 5/3 and then 8/0 (poller starting after the first event, and replay). D.2 went 1/5 and 6/0. D.3 went 6/2 twice and then 8/0 (duplicate events after the outage). D.4 passed 4/0 on its first run. E.1 failed 13/1 once (rule 1.14, event order for `message.created`, `message.updated`, `message.deleted`) and then 14/0. E.2 stayed at 5/8 twice until the agent replaced a placeholder IRC gateway that lacked token validation, then 13/0. E.3 passed 7/0 on its first run. E.4 went 4/3 and 7/0 once the static test IDs and two modal DOM ids were added. After the last code change it reran all 13 checks in order (10/16/16/13/19, 8/6/8/4, 14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. No debug copies of the checks. |
| 3 | C, D, E | 2 | `jobs/2026-11-05__10-26-52/` (session `final_task__5S6mkMK__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $1.92 (109 steps, about 22 minutes) | 69 (C 26, D 29, E 14; the final pass reran all 13 checks once each) | No | Built C.1 to E.4 as one cluster from an empty `/app`, in order. C.1 first printed 0/10 (the cluster was not up: a launcher bug and a handler that defined a method wrongly), then passed. Later changes broke C.1 rule 1.10 (`user.updated` not reaching a WebSocket on another node) several times; it went 9/1 in eight check runs across the run and was fixed for good at the end, when the agent corrected the broadcast record format. C.3 went 3/13, 7/9 (multipart parser) and 16/0. C.4 went 5/8, 10/3, 13/0. C.5 went 5/14, 2/17, 8/11, 11/8 and 19/0. D.1 stayed at 7/1 for 12 check runs on rule 1.8 (resume on node 2 after the kill must replay the missed events). The agent first suspected a timing race in the check, but the cause was in its own code: a missing import and a wrong resume cursor. After fixing both, D.1 passed 8/0. D.2 went 1/5, 5/1 and 6/0. D.3 stayed at 5/3 for five runs (rules 3.2, 3.4 and 3.5: fan-out to node 1 and dense event ids) and then passed 8/0. E.1 went 6/8, 7/7 and 14/0. E.2 went 0/13 (the IRC gateway was not up; stale child processes still owned the ports), then 4/9, 12/1, 13/0. E.4 went 4/3 and 7/0. Four check runs (C.5 once, D.1 twice, D.2 once) hit the 30-second command limit while the server was in a broken state; the agent reran them and carried on. After the last code change it reran all 13 checks (10/16/16/13/19, 8/6/8/4, 14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. No debug copies of the checks. |
| 4 | C, D, E | 1 | `jobs/2026-11-05__11-11-27/` (session `final_task__BEzZ47e__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $2.23 (136 steps, about 14.5 minutes) | 49 (C 25, D 10, E 14) | No | Checks only, no written specs; the agent read the check scripts to learn what to build. C.1 passed on its first run. C.2 printed 0/16 three times (setup failed) for about 20 steps: the agent first thought the check or registration was at fault, and the cause was in its own code, a message request that hung because the handler held an SQLite write lock. After a transaction fix it got 12/4 and 16/0. C.3 failed 5/11 (no multipart route) and then passed. C.4 went 3/10, 12/1 (a private-channel invite failing because of an over-escaped regex) and 13/0. C.5 took 9 runs (5/14, 6/13, 7/12, 14/5, 14/5, 15/4, 15/4, 19/0, 19/0); the group rules 5.7, 5.9, 5.11, 5.12, 5.15 and 5.16, and `@here` rule 5.17, were the ones that kept failing. D.1 failed 5/3 three times, then 6/2 and 8/0 after the agent switched WebSocket delivery to a single durable event-log poller. D.2 passed 6/0 on its first run. D.3 went 7/1 (Redis was not restarted by the supervisor) and 8/0. D.4 passed 4/0 (median latency 32 ms). E.1 failed 13/1 four times on rule 1.14 (event order for `message.created`, `message.updated`, `message.deleted`) before passing 14/0. E.2 went 11/2, 11/2, 12/1 and 13/0. E.3 passed 7/0 on its first run. E.4 went 3/4, 6/1 (workspace settings modal id) and 7/0. The last code change was in the E.4 markup, and afterwards the agent reran E.4, E.1, C.1 to C.5 and D.4, all passing, and then submitted. It did not rerun D.1, D.2, D.3, E.2 or E.3 after that last change, although the README requires every check to pass after the last code change. Their last results were all passing, from before the E.4 change. No debug copies of the checks and no 30-second timeouts. |
| 4 | C, D, E | 2 | `jobs/2026-11-09__14-58-36-4/` (session `final_task__z8UKARv__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $2.13 (121 steps, about 12 minutes by the agent's own `date` calls) | 51 | No | Built the cluster and C.1 to E.4 from an empty `/app` with only the checks (no written specs), running each check and reading its output to find what was missing. C.1 sat at 9/1 for three runs before reaching 10/0. C.2 started at 0/16 (11/5 twice, then 16/0). C.5 took the most runs: 10 runs from 5/14 up through 14/5 and 15/4 (each held for several runs) to 19/0. D.1 started at 4/4 and reached 8/0. E.2 started at 0/13 twice (the IRC gateway did not exist yet) and reached 13/0 after several fixes (10/3, 11/2, 12/1). E.4 went 4/3, 6/1, 7/0. Two commands hit the 30 s limit and lost their output. After the last code change it reran all the checks (C 10/16/16/13/19, D 8/6/8/4, E 14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. |
| 5.1 | A, B, C, D, E | 1 | `jobs/2026-11-09__15-24-59/` (session `final_task__NCdSzWn__agent`) | C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed); A and B: 0 broken (174 rules pass) | about $4.89 (190 steps; cost cap raised to $5 for this run) | 100 (72 stage checks: C 34, D 16, E 22; plus 28 runs of `regression.sh`) | No | `verbose` run, regression suite only for A and B. The agent built A and B first from `regression.sh`, which went 125, 160, 94, 89, 76, 156, 104, 261, 158, 106, 75, 72, 103, 119, 75, 50, 257, 69, 56, 15, 57, 251, 60, 88, 258, 5, 4 and then 0 broken (174 rules). It jumped up four times, to 261, 257, 251 and 258 broken, after changes to the shared server code; each time the agent found and fixed the cause before moving on. C.1 stayed at 9/1 for three runs (rule 1.10) before 10/0. C.2 and C.4 each needed 3 runs to reach full marks. C.3 went 5/11 twice, 10/6, 14/2 and 16/0. C.5 went 5/14, 0/1, 18/1 and 19/0. D.1 went 5/3 twice and then 8/0. D.2 went 1/5 and 6/0. D.4 went 3/1 and then 4/0. E.1 went 7/7, 0/14, 8/6 and 13/1 before 14/0, E.2 went 5/8 and 12/1 before 13/0, and E.4 went 0/7 and 3/4 before 7/0. After the last code change it reran the regression suite and all 13 checks (10/16/16/13/19, 8/6/8/4, 14/13/7/7) and confirmed health on ports 8000 to 8002 before submitting. |
| 5.1 | A to E | 2 | `jobs/2026-11-09__16-40-07/` (session `final_task__GuWCA2t__agent`) | A and B regression: 0 broken (174 rules), C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $3.77 (163 steps, about 22 minutes by the agent's own `date` calls) | 74 (50 stage checks, 24 regression suite runs) | No | Built the cluster and C, D, E from an empty `/app` with the C, D, E specs and checks plus `regression.sh` for A and B. The regression suite started at 276 broken and came down over 24 runs (114, 112, 111, 95, 61, 44, 26, 18, 10, 7, 1) to 0 broken. The last two runs were clean, so the agent confirmed A and B after its final code change. C.3 took 6 runs (5/11, 3/13, 10/6, 12/4, then 16/0) and C.5 took 9 (5/14, 15/4, 16/3, then 18/1 for four runs, then 19/0). D.1 started at 4/4 and D.3 at 1/7, both fixed. E.2 started at 0/13 (the IRC gateway did not exist yet) and reached 13/0 after 7/6 and 12/1. E.4 went 2/5, 5/2, 7/0. Four commands hit the 30 s limit and lost their output. It reran all of C, D and E and the regression suite after the last change, confirmed health on ports 8000 to 8002, and then submitted. |
| 5.2 | A to E | 1 | `jobs/2026-11-09__17-06-51/` (session `final_task__qa4BVyV__agent`) | A and B regression: 0 broken (174 rules), C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $3.47 (154 steps, about 17 minutes by the agent's own `date` calls) | 78 (62 stage checks, 16 regression suite runs) | No | Built the cluster and C, D, E from an empty `/app` with Specs A and B written out, the C, D, E specs and checks, and `regression.sh`. The regression suite started at 152 broken, spiked back to 174 once, and came down over 16 runs (56, 49, 31, 25, 21, 16, 12, 47, 1) to 0 broken, and it stayed at 0 for the last three runs. At one point the count went back up from 12 to 47 after a change. C.3 took 7 runs (5/11, 13/3, 14/2 twice, then 16/0). C.5 took 10 runs (4/15, 14/5, 4/15 again after a regression, 18/1 for four runs, then 19/0). D.3 started at 1/7 (Redis supervision) and D.4 at 3/1, both fixed. E.2 started at 0/13 (no IRC gateway yet), reached 12/1, fell back to 0/13 after a change, then reached 13/0. E.4 went 2/5 then 7/0. After the last code change it reran all the C, D, E checks and the regression suite (0 broken, three times in a row) before submitting. |
| 5.2 | A to E | 2 | `jobs/2026-11-09__19-24-33/` (session `final_task__3uoRiHx__agent`) | A and B regression: 104 broken at the last run, C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0 (passed/failed) | about $4.33 (194 steps, about 22 minutes by the agent's own `date` calls) | 88 (70 stage checks, 18 regression suite runs) | Yes, A and B regression (104 rules broken) | Built the cluster and C, D, E from an empty `/app` with Specs A and B written out, the C, D, E specs and checks, and `regression.sh`. All C, D and E checks ended green: C.1 took 7 runs, C.5 7, D.1 5 (4/4 twice), D.4 6 (it dropped to 0/4 once), E.1 7, E.2 6 (0/13 at first) and E.4 9 (4/3 three times, one 0/13). The A/B regression suite never recovered: 116, 114, 108, 98, 90, 92, 95, 85, 71, 80, 93, 166, 72, 109, 76, 95, 68, 104 broken over 18 runs. Most of the failures were status-000 setup cascades after one early request failed. The agent noted this on the last run, looked at the logs (no runtime exceptions) and restarted a fresh cluster, but did not rerun the suite before submitting. 3 commands hit the 30 s limit. |
| 5.3 | A to E | 1 | `jobs/2026-11-09__20-16-27/` (session `final_task__BunezZn__agent`) | A.1 28/0, A.2 20/0, A.3 30/0, B.1 22/0, B.2 29/0, B.3 18/0, B.4 24/0, C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0, boot check 4/0 (passed/failed) | about $2.82 (132 steps, about 17 minutes by the agent's own `date` calls) | 67 (including 2 boot check runs) | No | Built the cluster and A to E from an empty `/app` with Specs A to E and plain checks, and no regression suite. A.1 started at 9/22 and stayed there for five runs (the app did not start cleanly) before reaching 28/0. B.1 went 0/1, 22/0, then 19/3 for three runs after a later change, and was fixed again to 22/0. B.2 started at 8/21, B.3 at 2/16 (then 6/12, 16/2, 18/0) and B.4 at 11/13. C.4 took 5 runs and C.5 6 (3/7, 10/0, 18/1 twice, then 19/0). D.1 went 6/2 twice, then 7/0 and 8/0. E.2 started at 0/13 (no IRC gateway yet) and reached 13/0 via 12/1. 2 commands hit the 30 s limit. After its last code change (a B.1 fix) it reran B.1 to B.4, C.1 to C.5 and the boot check (4 passed, 0 failed, node 1 came back on its own). It did not rerun A.1 to A.3, D or E after that change, so those results are from just before it. |
| 5.3 | A to E | 2 | `jobs/2026-11-09__19-56-14/` (session `final_task__bLpcErz__agent`) | A.1 28/0, A.2 20/0, A.3 30/0, B.1 22/0, B.2 29/0, B.3 18/0, B.4 24/0, C.1 10/0, C.2 16/0, C.3 16/0, C.4 13/0, C.5 19/0, D.1 8/0, D.2 6/0, D.3 8/0, D.4 4/0, E.1 14/0, E.2 13/0, E.3 7/0, E.4 7/0, boot check 4/0 (passed/failed) | about $1.66 (95 steps, about 11 minutes by the agent's own `date` calls) | 71 (each check at least twice; the boot check was run both without and with the node kill) | No | Built the cluster and A to E from an empty `/app` with Specs A to E and plain checks, and no regression suite. A and B were built first: A.2 started at 4/18 (twice) and B.4 took 7 runs (7/18 twice, then 14/10 three times, then 24/0), and B.2 started at 17/12. C.1 started at 5/5, C.3 at 6/10 and D.3 at 1/7 (twice, until Redis supervision was added). E.2 started at 0/13 (twice, no IRC gateway yet) and reached 13/0 via 12/1, and E.4 started at 4/3. After the last code change it reran all the checks and the four-condition boot check (4 passed, 0 failed, node 1 came back on its own) and confirmed everything was running before submitting. |

## Experiments

### 1) Feedback

> Do you find that adding detailed output to the checks helps the agent?

#### Runs compared and why

`score_only` runs 1.1, 1.2 and 1.3 against `verbose` runs 2.1, 2.2 and 2.3. Each pair builds the same component (C, D, E) from the same specs and checks, and only the `FEEDBACK` setting in `lib.sh` differs. Each run was done twice, so there are 12 runs.

#### Supporting results

| Component | Mode | Cost (repeat 1 / 2) | Steps (1 / 2) | Check runs (1 / 2) |
|---|---|---|---|---|
| C | `score_only` (1.1) | $0.35 / $0.61 | 45 / 67 | 24 / 35 |
| C | `verbose` (2.1) | $0.21 / $0.43 | 30 / 49 | 15 / 18 |
| D | `score_only` (1.2) | $0.17 / $0.08 | 29 / 18 | 11 / 8 |
| D | `verbose` (2.2) | $0.09 / $0.12 | 17 / 22 | 9 / 9 |
| E | `score_only` (1.3) | $0.45 / $0.35 | 48 / 45 | 8 / 13 |
| E | `verbose` (2.3) | $0.34 / $0.28 | 40 / 37 | 17 / 15 |

All 12 runs passed every check and none submitted with failures. Summed over C, D and E, the `score_only` runs cost $2.01 and took 252 steps, and the `verbose` runs cost $1.47 and took 195 steps (27% less money, 23% fewer steps).

#### Answer

Yes, a little. `verbose` was cheaper in 5 of the 6 repeat pairs and cheaper on average for every component. It did not change whether the agent finished, because every run finished with everything passing.

#### Consistency across repeats

The direction was the same in 5 of 6 pairs, but the size of the effect varied a lot, and the ranges of the two modes overlap (for example C: `score_only` $0.35 to $0.61 against `verbose` $0.21 to $0.43). The one reversal was D repeat 2, where `score_only` ($0.08) was cheaper than `verbose` ($0.12). The number of check runs did not follow the mode: `verbose` used fewer in 3 of 6 pairs.

#### Other evidence

- Both `score_only` C runs (1.1) made their own `verbose` copies of the checks (a `sed` on `FEEDBACK`) to see which rules failed, so the agent works around a missing detail when it can.
- The clearest case is the same bug seen in both modes. In D, an agent whose server read the WebSocket token only from the Authorization header got `0 passed, 8 failed` with no reason under `score_only` (1.2 repeat 1) and spent about 15 steps probing by hand. Under `verbose` (2.2 repeat 1) the line `HTTP/1.1 401 Unauthorized` was printed, and the fix took one step.
- Detail did not help when the cause was a crashed or stale process (stale node processes serving old code, a dead IRC gateway). A check cannot explain those in either mode, and they took many runs in both.
- Agents that read the scenario scripts before coding (1.2 repeat 2, 2.2 repeat 2) avoided the problem altogether, so what the agent reads matters about as much as the mode.
- Limits: two repeats per cell, one agent, and run-to-run variation as large as the mode effect.

### 2) Tests as the spec

> Can the checks alone replace the written rules?

#### Runs compared and why

Run 3 (README, specs C, D, E and checks) against Run 4 (README and checks only, no written specs). Both are `verbose`, build C, D and E together (13 stages), and have two repeats. The 13 checks have 141 rules in total (C 74, D 26, E 41).

#### Supporting results

| Run | Cost | Steps | Check runs | Result |
|---|---|---|---|---|
| 3 repeat 1 | $1.45 | 97 | 54 | all 141 rules pass, submitted |
| 3 repeat 2 | $1.92 | 109 | 69 | all 141 rules pass, submitted |
| 4 repeat 1 | $2.23 | 136 | 49 | all 141 rules pass, submitted (but did not rerun 5 checks after its last code change) |
| 4 repeat 2 | $2.13 | 121 | 51 | all 141 rules pass, submitted |

Averages: Run 3 cost $1.69, 103 steps and 61.5 check runs; Run 4 cost $2.18, 128.5 steps and 50 check runs. Run 4 cost about 1.3 times the money and 1.25 times the steps.

#### Answer

Yes, mostly. With only the checks, the agent built everything and passed all 141 rules in both repeats, so the rules in the checks are complete enough to be learned from them. The price is about 30% more money and 25% more steps than with the written specs, and one repeat skipped some final reruns.

#### Consistency across repeats

Both setups were consistent: both Run 3 repeats cost $1.45 to $1.92 and both Run 4 repeats cost $2.13 to $2.23, and the ranges do not overlap. The gap between the two setups is small, but it points the same way in both repeats.

#### Other evidence

- Without specs, the agents spent steps working out status codes and field names from the check scripts. Run 4 repeat 2 started C.2 at 0/16 and E.2 at 0/13 twice, and C.5 took 10 check runs.
- Run 4 repeat 1 ended without rerunning D.1 to D.3, E.2 and E.3 after its last change, although the README requires it. Without the written rules to re-read, the agent had less to go on when verifying.
- Both Run 4 repeats made fewer check runs than the Run 3 average but used more steps, which fits more reading and diagnosis per run.
- Limits: two repeats per setup and run-to-run variation of the same size as the difference.

### 3) Scale

> Do you find that a component passes fewer of its checks when the agent builds it together with
> other components, rather than on its own?

#### Runs compared and why

Each component built alone (2.1, 2.2, 2.3, all `verbose`) against all three built together (Run 3, `verbose`). Using the `verbose` runs keeps the testing mode the same.

#### Supporting results

| | Cost | Steps | Check runs | Rules passing at submit |
|---|---|---|---|---|
| C + D + E alone (sum of the means of 2.1, 2.2, 2.3) | $0.74 | 97.5 | 41.5 | all |
| C, D, E together (mean of Run 3) | $1.69 | 103 | 61.5 | all (141 of 141 rules, both repeats) |

Together cost about 2.3 times as much and needed about 1.5 times as many check runs. The number of steps was close (about 1.06 times).

#### Answer

No, not at submit: every component passed every check in both settings. The difference shows up in cost and effort. Together cost more than twice as much, and earlier components fell back along the way.

#### Consistency across repeats

Both Run 3 repeats passed everything, at $1.45 and $1.92. Both were above every single-component pair.

#### Other evidence

- In Run 3 repeat 2, C.1 fell back to 9/1 several times as later stages changed the server, and D.1 sat at 7/1 for 12 check runs.
- Costs grow faster than steps because the context gets longer when more is built, so each step is more expensive.
- The comparison is not perfectly clean: each single-component run also had to build the basics (accounts, workspaces, channels, messages) that C, D and E need.

### 4) Regression

> a) Do you find that building components C, D, and E breaks features from A and B that were
> already working?

> b) Does it make a difference whether the agent has Specs A and B or only their regression
> tests?

> c) Does it make a difference whether the A and B checks are given as a regression suite or as
> plain checks?

#### Runs compared and why

5.1 (C, D, E specs and checks, plus the A and B regression suite only), 5.2 (the same plus the written Specs A and B), and 5.3 (specs and plain checks for all five), all `verbose`. The official runs are two repeats of 5.1, two of 5.2 and one of 5.3. For (a) I used the `regression.sh` counts inside 5.1 and 5.2, plus the A and B checks inside 5.3. For (b) I compared 5.1 with 5.2, and for (c) I compared 5.2 with 5.3.

#### Supporting results

| Run | A and B at the end | C, D, E checks green at the end (of 13) | Cost | Steps | Outcome |
|---|---|---|---|---|---|
| 5.1 repeat 1 | 0 broken | 13 | $4.89 | 190 | passed, submitted |
| 5.1 repeat 2 | 0 broken | 13 | $3.77 | 163 | passed, submitted |
| 5.2 repeat 1 | 0 broken | 13 | $3.47 | 154 | passed, submitted |
| 5.2 repeat 2 | 104 broken | 13 | $4.33 | 194 | submitted with A and B failing |
| 5.3 repeat 2 | all green | 13 | $2.82 | 132 | passed, submitted |

Four of the five official Run 5 runs passed everything and submitted. The fifth (5.2 repeat 2) submitted with all of C, D and E green but A and B still at 104 broken. All five ran with the cost cap raised to $5.

#### Answer

a) Yes, in the middle of a run. `regression.sh` counts jumped up several times after changes to shared server code: 5.1 repeat 1 jumped to 261, 257, 251 and 258 broken at four points, 5.2 repeat 1 from 12 to 47, and 5.2 repeat 2 swung between 68 and 166 broken for 18 runs and never recovered. In 5.3, B.1 fell from 22/0 to 19/3 after a later change. But most of these happened while A and B were still being built, so I cannot attribute them to C, D or E alone. In four of the five runs the agent got A and B back to full marks before submitting.

b) Not clearly. With only the regression suite (5.1), both repeats finished with everything passing, at $4.89 and $3.77. With the written Specs A and B (5.2), one repeat finished for $3.47 (cheaper than both 5.1 runs) and the other submitted with A and B still at 104 broken, even though C, D and E were all green. All five ran with the same $5 cap, so the cap does not explain it. So in this data the written specs did not make A and B safer, and 5.2 was the less reliable setup, but with two runs each I cannot separate the effect of the specs from run-to-run variation.

c) No clear difference, with one 5.3 run. 5.3 passed for $2.82 and 132 steps, cheaper than any 5.1 or 5.2 run, and it had no regression suite to run (5.2 repeat 1 ran `regression.sh` 16 times). It still needed A.1 five times at 9/22 before the app started cleanly. One run cannot rule out luck.

#### Consistency across repeats

The two 5.1 repeats both passed but differed a lot in cost ($3.77 and $4.89). The two 5.2 repeats were not consistent: one passed with A and B at 0 broken, the other submitted with 104 broken. There is only one 5.3 run, so there is no repeat to compare.

#### Other evidence

- The extra sample runs I kept in the job portfolio (not official) point the same way for 5.2: three more 5.2 attempts hit the cost cap with A and B still broken, with the regression count swinging between about 15 and 120 broken. Several of those swings, and most of the 104 broken in 5.2 repeat 2, were status 000 setup failures after one bad request, so part of the "broken" counts is noise from the server being overloaded or restarted.
- The regression suite runs its checks in parallel to fit the 30-second command limit, which makes load-related failures more likely.
- In 5.3, the agent did not rerun A.1 to A.3, D or E after its last code change (a B.1 fix), although the README requires it. Its last results for those checks are from just before the change.
- D.4's performance limits and C.5's group and `@here` rules were the checks that took the most runs across Run 5.
- Limits: five official runs, one agent, and one failed 5.2 run was partly noise from setup failures. Building all five components is close to the budget whatever the setup.