import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8"]
rule("3.1", redis_ping(), "a Redis server answers PING on 127.0.0.1:6379")
w = mkworld(0)
if not w:
    fail_rest(R[1:], "setup failed: could not create a user, a workspace and a channel"); sys.exit()
tok, ch = w["owner"]["token"], w["gid"]
try:
    subs = [Sub(n, tok) for n in (0, 1, 2)]
    for s in subs:
        s.subscribe(ch)
except Exception as e:
    fail_rest(R[1:], "setup failed: WebSocket: %s" % e); sys.exit()
post(0, ch, "d3-pre-%d" % RUN, tok)
g = subs[1].wait(lambda f: f.get("type") == "message.created" and "d3-pre-%d" % RUN in str(f), 3)
rule("3.2", g is not None, "before the outage a post on node 0 reaches the subscriber on node 1")

pid = kill_port(6379)
t0 = time.time()
if pid is None:
    fail_rest(R[2:], "could not find the process listening on :6379 to kill"); sys.exit()
codes = []
for n in (0, 1, 2):
    codes.append(post(n, ch, "d3-out%d-%d" % (n, RUN), tok)[0])
took = time.time() - t0
rule("3.3", codes == [201, 201, 201] and took < 5, "within 5 seconds of killing Redis, writes succeed on every node (statuses %s, %.1fs)" % (codes, took))
got = [subs[1].wait(lambda f, n=n: f.get("type") == "message.created" and "d3-out%d-%d" % (n, RUN) in str(f), max(0.2, 5 - (time.time() - t0))) for n in (0, 2)]
rule("3.4", all(got), "the subscriber on node 1 still receives events from posts made on nodes 0 and 2 during the outage")
time.sleep(0.5)
ids = [s.event_ids(ch) for s in subs]
rule("3.5", len(ids[1]) >= 4 and dense(ids[1]) and ids[0] == ids[1] == ids[2], "event ids stay dense and identical on all three nodes across the outage (%s)" % ids[1])
back = wait_until(redis_ping, 15)
rule("3.6", bool(back), "/app/start.sh respawns Redis: PING answers again within 15 seconds of the kill")
post(2, ch, "d3-post-%d" % RUN, tok)
g0 = subs[0].wait(lambda f: f.get("type") == "message.created" and "d3-post-%d" % RUN in str(f), 4)
time.sleep(0.4)
ids = subs[0].event_ids(ch)
rule("3.7", g0 is not None and dense(ids), "after Redis is back, a post on node 2 reaches the subscriber on node 0 and event ids are still dense (%s)" % ids)
rule("3.8", [health(0), health(1), health(2)] == [0, 1, 2], "all three HTTP nodes still answer /api/health")
