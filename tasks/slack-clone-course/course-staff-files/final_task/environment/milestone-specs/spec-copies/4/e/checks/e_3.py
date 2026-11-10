import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["3.%d" % i for i in range(1, 8)]
w = mkworld(1)
if not w:
    fail_rest(R, "setup failed: could not create users, a workspace and a channel"); sys.exit()
o, m1, slug, gid = w["owner"], w["members"][0], w["slug"], w["gid"]
chan = "#%s/general" % slug
try:
    c = Irc()
    ok_reg = c.register(o["token"], "i%d" % RUN)
    c.send("JOIN " + chan); c.wait(c.numeric("366"), 3)
    sub = Sub(0, o["token"]); sub.subscribe(gid)
except Exception as e:
    fail_rest(R, "setup failed (IRC gateway or WebSocket): %s" % e); sys.exit()
if not ok_reg:
    fail_rest(R, "setup failed: IRC registration did not finish"); sys.exit()

post(2, gid, "e3-pre-%d" % RUN, m1["token"])
rule("3.1", c.wait(lambda l: "e3-pre-%d" % RUN in l, 3) is not None, "before the kill a web post on node 2 reaches the IRC client")

pid = kill_port(8001)
if pid is None:
    fail_rest(R[1:], "could not find the process listening on :8001 to kill"); sys.exit()
time.sleep(0.5)
post(0, gid, "e3-n0-%d" % RUN, m1["token"])
rule("3.2", c.wait(lambda l: "e3-n0-%d" % RUN in l, 3) is not None, "after SIGKILL of node 1 a web post on node 0 still reaches the IRC client within 3 seconds")
post(2, gid, "e3-n2-%d" % RUN, m1["token"])
rule("3.3", c.wait(lambda l: "e3-n2-%d" % RUN in l, 3) is not None, "after the kill a web post on node 2 reaches the IRC client too")
c.lines.clear(); c.send("PING :k%d" % RUN)
rule("3.4", c.wait(lambda l: "PONG" in l and "k%d" % RUN in l, 3) is not None and not c.eof, "the IRC connection itself survives the kill (PING answered)")
c.send("PRIVMSG %s :e3-irc-%d" % (chan, RUN)); time.sleep(0.8)
b0, b2 = bodies(0, gid, m1["token"]) or [], bodies(2, gid, m1["token"]) or []
rule("3.5", "e3-irc-%d" % RUN in b0 and "e3-irc-%d" % RUN in b2, "an IRC PRIVMSG sent while node 1 is down shows on nodes 0 and 2")
ids = sub.event_ids(gid)
rule("3.6", len(ids) >= 4 and dense(ids), "web and IRC writers keep one dense event_id stream across the kill (ids %s)" % ids)
try:
    c3 = Irc()
    rule("3.7", c3.register(m1["token"], "n%d" % RUN), "a new IRC client can register while node 1 is down")
except Exception as e:
    rule("3.7", False, "could not connect: %s" % e)
wait_until(lambda: health(1) == 1, 8)
