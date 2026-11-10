import json, re, sys, threading, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["4.1", "4.2", "4.3", "4.4"]
w = mkworld(2)
if not w:
    fail_rest(R, "setup failed: could not create users, a workspace and a channel"); sys.exit()
ch = w["gid"]
users = [w["owner"]] + w["members"]
try:
    subs = [Sub(n, users[n]["token"]) for n in (0, 1, 2)]
    for s in subs:
        s.subscribe(ch)
except Exception as e:
    fail_rest(R, "setup failed: WebSocket: %s" % e); sys.exit()

PER = 30
sent, statuses = {}, []
lock = threading.Lock()

def writer(n):
    for i in range(PER):
        tag = "perf%d-%d-%d" % (RUN, n, i)
        t = time.time()
        with lock:
            sent[tag] = t
        s, _ = post(n, ch, tag, users[n]["token"])
        with lock:
            statuses.append(s)

th = [threading.Thread(target=writer, args=(n,)) for n in (0, 1, 2)]
t_start = time.time()
for t in th:
    t.start()
for t in th:
    t.join(20)
time.sleep(1.5)

lat_by_sub, missing = [], 0
for s in subs:
    seen = {}
    for f in s.events(ch):
        mt = re.search(r"perf%d-\d-\d+" % RUN, json.dumps(f, ensure_ascii=False))
        m = mt.group(0) if mt else ""
        if m in sent and m not in seen:
            seen[m] = f["_t"] - sent[m]
    missing += len(sent) - len(seen)
    lat_by_sub.append(sorted(seen.values()))

def pct(v, p):
    return v[min(len(v) - 1, int(len(v) * p))] if v else 9.99

med = max(pct(v, 0.5) for v in lat_by_sub)
p95 = max(pct(v, 0.95) for v in lat_by_sub)
expected = len(sent) * 3
rule("4.1", med <= 0.3, "median delivery latency (worst subscriber) is %.0f ms, limit 300 ms" % (med * 1000))
rule("4.2", p95 <= 0.8, "p95 delivery latency (worst subscriber) is %.0f ms, limit 800 ms" % (p95 * 1000))
rule("4.3", expected > 0 and missing <= 0.01 * expected, "%d of %d expected message.created events are missing, limit 1%%" % (missing, expected))
rule("4.4", len(statuses) == 3 * PER and all(s == 201 for s in statuses), "all %d concurrent posts returned 201 (statuses seen: %s)" % (3 * PER, sorted(set(statuses))))
