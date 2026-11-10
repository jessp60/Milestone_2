import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6"]
w = mkworld(0)
if not w:
    fail_rest(R, "setup failed: could not create a user, a workspace and a channel"); sys.exit()
tok, ch = w["owner"]["token"], w["gid"]
try:
    s0 = Sub(0, tok); s0.subscribe(ch)
except Exception as e:
    fail_rest(R, "setup failed: WebSocket: %s" % e); sys.exit()
post(0, ch, "d2-first-%d" % RUN, tok)

pid = kill_port(8002)
t0 = time.time()
if pid is None:
    fail_rest(R, "could not find the process listening on :8002 to kill"); sys.exit()
survivors_ok, posted = True, False
back = None
while time.time() - t0 < 15:
    if health(0) != 0 or health(1) != 1:
        survivors_ok = False
    if not posted and time.time() - t0 > 1:
        posted = post(0, ch, "d2-outage-%d" % RUN, tok)[0] == 201
    if health(2, 0.8) == 2:
        back = time.time() - t0
        break
    time.sleep(0.3)
rule("2.1", back is not None, "the killed node on :8002 answers /api/health with node_id 2 again within 15 seconds without anyone starting it (took %s)" % ("%.1fs" % back if back else "more than 15s"))
rule("2.2", survivors_ok, "nodes 0 and 1 kept answering the whole time")
if back is None:
    fail_rest(R[2:], "node 2 did not come back"); sys.exit()
if not posted:
    post(0, ch, "d2-outage-%d" % RUN, tok)
time.sleep(0.5)
b = bodies(2, ch, tok)
rule("2.3", b is not None and "d2-outage-%d" % RUN in b and "d2-first-%d" % RUN in b, "the respawned node returns the messages posted before and during its outage")
st, _ = post(2, ch, "d2-after-%d" % RUN, tok)
b0 = bodies(0, ch, tok) or []
rule("2.4", st == 201 and "d2-after-%d" % RUN in b0, "a write on the respawned node returns 201 and shows on node 0 (status %s)" % st)
try:
    s2 = Sub(2, tok); s2.subscribe(ch)
    post(0, ch, "d2-live-%d" % RUN, tok)
    g = s2.wait(lambda f: f.get("type") == "message.created" and "d2-live-%d" % RUN in str(f), 3)
    time.sleep(0.4)
    i0 = s0.event_ids(ch)
    rule("2.5", g is not None and dense(i0), "a subscriber on the respawned node gets new events, and event ids stay dense on node 0 (%s)" % i0)
except Exception as e:
    rule("2.5", False, "could not open a WebSocket on node 2: %s" % e)
ids = [health(0), health(1), health(2)]
rule("2.6", ids == [0, 1, 2], "all three nodes answer with their own node_id (got %s)" % ids)
