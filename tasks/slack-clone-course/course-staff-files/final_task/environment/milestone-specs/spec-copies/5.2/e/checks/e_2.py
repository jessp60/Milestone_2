import sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["2.%d" % i for i in range(1, 14)]
w = mkworld(1)
if not w:
    fail_rest(R, "setup failed: could not create users, a workspace and a channel"); sys.exit()
o, m1, slug, gid = w["owner"], w["members"][0], w["slug"], w["gid"]
chan = "#%s/general" % slug
nick = "o%d" % RUN
mnick = "m%d" % RUN

try:
    c = Irc(); c.send("PASS wrong-token"); c.send("NICK x%d" % RUN); c.send("USER x 0 * :x")
    rule("2.1", c.wait(c.numeric("464"), 3) is not None, "PASS with a bad token is answered with 464")
    c.close()
except Exception as e:
    rule("2.1", False, "could not connect to 127.0.0.1:6667: %s" % e); fail_rest(R[1:], "no IRC gateway on 127.0.0.1:6667"); sys.exit()

c = Irc()
reg = c.register(o["token"], nick)
nums = c.numerics()
order = [n for n in ("001", "002", "003", "004", "005", "422") if n in nums]
pos = [nums.index(n) for n in order]
rule("2.2", reg and order == ["001", "002", "003", "004", "005", "422"] and pos == sorted(pos), "registration sends 001 to 005 then 422 in order (got %s)" % nums)
if not reg:
    fail_rest(R[2:], "registration did not finish"); sys.exit()

c.lines.clear(); c.send("JOIN %s/nochannel" % ("#" + slug))
rule("2.3", c.wait(c.numeric("403"), 3) is not None, "JOIN of an unknown channel is answered with 403")

c2 = Irc(); c2.send("PASS " + m1["token"]); c2.send("NICK " + nick)
got433 = c2.wait(c2.numeric("433"), 3)
c2.send("USER m 0 * :m"); c2.send("NICK " + mnick)
got001 = c2.wait(c2.numeric("001"), 4)
rule("2.4", got433 is not None and got001 is not None and not c2.eof, "a nick in use gets 433 without closing the connection, and a new nick then registers")

c.lines.clear(); c.send("FOOBAR now")
rule("2.5", c.wait(c.numeric("421"), 3) is not None, "an unknown command is answered with 421")

c.lines.clear(); c.send("JOIN " + chan)
j = c.wait(lambda l: " JOIN " in l and chan in l, 3)
n353 = c.wait(c.numeric("353"), 3); n366 = c.wait(c.numeric("366"), 3)
ordered = j is not None and n353 and n366 and c.lines.index(j) < c.lines.index(n353) < c.lines.index(n366)
rule("2.6", bool(ordered) and nick in (n353 or ""), "JOIN %s sends the joiner's JOIN, then 353 listing the nick, then 366 (lines: %s)" % (chan, c.lines[:4]))

c.lines.clear(); c.send("JOIN #%s-general" % slug)
rule("2.7", c.wait(c.numeric("403"), 3) is not None, "the hyphenated name #%s-general is an unknown channel (403)" % slug)

c.lines.clear(); c.send("PING :tok%d" % RUN)
rule("2.8", c.wait(lambda l: "PONG" in l and "tok%d" % RUN in l, 3) is not None, "PING is answered with PONG carrying the same token")

c2.send("JOIN " + chan); c2.wait(c2.numeric("366"), 3)
try:
    sub = Sub(1, o["token"]); sub.subscribe(gid)
except Exception as e:
    sub = None
c.send("PRIVMSG %s :hello-from-irc-%d" % (chan, RUN))
time.sleep(0.8)
bs = bodies(2, gid, o["token"]) or []
rule("2.9", "hello-from-irc-%d" % RUN in bs, "a PRIVMSG from an IRC client shows up as a message in the channel through the web API on another node")

c.lines.clear()
post(0, gid, "hello-from-web-%d" % RUN, m1["token"])
ln = c.wait(lambda l: "PRIVMSG" in l and "hello-from-web-%d" % RUN in l, 3)
rule("2.10", ln is not None and ln.startswith(":") and "!" in ln.split(" ")[0] and "@" in ln.split(" ")[0] and (" PRIVMSG %s :" % chan) in ln,
     "a web message reaches the IRC client as ':nick!user@host PRIVMSG %s :body' (got %r)" % (chan, ln))

if sub:
    post(1, gid, "mix-web1-%d" % RUN, m1["token"])
    c.send("PRIVMSG %s :mix-irc1-%d" % (chan, RUN)); time.sleep(0.5)
    post(2, gid, "mix-web2-%d" % RUN, m1["token"])
    c.send("PRIVMSG %s :mix-irc2-%d" % (chan, RUN)); time.sleep(1.0)
    ids = sub.event_ids(gid)
    mixed = [f for f in sub.events(gid) if "mix-" in str(f)]
    rule("2.11", len(mixed) == 4 and dense(ids), "web and IRC writers share one dense event_id stream (4 mixed events seen, ids %s)" % ids)
else:
    rule("2.11", False, "could not open a WebSocket")

c.lines.clear(); c.send("QUIT :bye")
c.pump(1.5)
errs = [l for l in c.lines if l.startswith("ERROR") or (len(l.split()) > 1 and l.split()[1].isdigit() and int(l.split()[1]) >= 400)]
rule("2.12", c.eof and not errs, "QUIT closes the connection without an error reply (closed=%s, error lines=%s)" % (c.eof, errs))

c2.lines.clear()
for cmd in ("NAMES " + chan, "WHO " + chan, "LIST", "TOPIC " + chan, "MODE " + chan):
    c2.send(cmd)
c2.send("PING :end%d" % RUN)
c2.wait(lambda l: "PONG" in l and "end%d" % RUN in l, 3)
bad421 = [l for l in c2.lines if len(l.split()) > 1 and l.split()[1] == "421"]
rule("2.13", not bad421 and any("PONG" in l for l in c2.lines), "NAMES, WHO, LIST, TOPIC and MODE are accepted (no 421) and the connection stays up")
