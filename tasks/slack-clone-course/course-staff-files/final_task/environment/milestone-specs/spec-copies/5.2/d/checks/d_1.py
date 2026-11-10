import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8"]
w = mkworld(1)
if not w:
    fail_rest(R, "setup failed: could not create users, a workspace and a channel"); sys.exit()
tok, ch = w["owner"]["token"], w["gid"]
try:
    s0, s1, s2 = Sub(0, tok), Sub(1, tok), Sub(2, tok)
    heads = [s.subscribe(ch) for s in (s0, s1, s2)]
except Exception as e:
    fail_rest(R, "setup failed: WebSocket: %s" % e); sys.exit()

st, _ = post(0, ch, "d1-before-%d" % RUN, tok)
got = s2.wait(lambda f: f.get("type") == "message.created" and f.get("channel_id") == ch and "d1-before" in str(f.get("message", f)), 3)
rule("1.1", st == 201 and got is not None, "post on node 0 before the kill reaches the subscriber on node 2 (status %s)" % st)
last_b = max(s1.event_ids(ch) or [0])

pid = kill_port(8001)
t0 = time.time()
if pid is None:
    fail_rest(R[1:], "could not find the process listening on :8001 to kill"); sys.exit()
up = wait_until(lambda: health(0) == 0 and health(2) == 2, 3)
rule("1.2", bool(up), "after SIGKILL of the process on :8001, nodes 0 and 2 still answer /api/health")

b0, b2 = bodies(0, ch, tok), bodies(2, ch, tok)
rule("1.3", b0 is not None and b2 is not None and "d1-before-%d" % RUN in b0 and "d1-before-%d" % RUN in b2,
     "reads work on both surviving nodes and show the earlier message")

st_a, _ = post(0, ch, "d1-w0-%d" % RUN, tok)
st_b, _ = post(2, ch, "d1-w2-%d" % RUN, tok)
x2, x0 = bodies(2, ch, tok) or [], bodies(0, ch, tok) or []
rule("1.4", st_a == 201 and st_b == 201 and "d1-w0-%d" % RUN in x2 and "d1-w2-%d" % RUN in x0,
     "writes succeed on both surviving nodes (%s, %s) and each shows on the other" % (st_a, st_b))

g0 = s0.wait(lambda f: f.get("type") == "message.created" and "d1-w2-%d" % RUN in str(f), 3)
g2 = s2.wait(lambda f: f.get("type") == "message.created" and "d1-w0-%d" % RUN in str(f), 3)
rule("1.5", g0 is not None and g2 is not None, "WebSocket subscribers on nodes 0 and 2 get message.created for posts made on the other node")

time.sleep(0.5)
i0, i2 = s0.event_ids(ch), s2.event_ids(ch)
rule("1.6", len(i0) >= 3 and dense(i0) and i0 == i2, "event ids are dense and identical on both survivors across the kill (node 0: %s, node 2: %s)" % (i0, i2))

closed = wait_until(lambda: s1.closed, 3)
rule("1.7", bool(closed), "the WebSocket that was connected to the killed node closes within 3 seconds")

try:
    r = Sub(2, tok)
    r.send({"type": "resume", "channel_id": ch, "since_event_id": last_b})
    done = r.wait(lambda f: f.get("type") == "resumed", 3)
    ev = [f["event_id"] for f in r.frames if f.get("type") == "message.created" and f.get("channel_id") == ch]
    want = [i for i in i2 if i > last_b]
    rule("1.8", done is not None and ev == want and done.get("head_event_id") == (want[-1] if want else last_b),
         "resume on node 2 after the kill replays the missed events %s in order, then resumed (got %s)" % (want, ev))
except Exception as e:
    rule("1.8", False, "could not reconnect to node 2: %s" % e)

# give the cluster a moment to bring node 1 back so the next check starts clean
wait_until(lambda: health(1) == 1, 8)
