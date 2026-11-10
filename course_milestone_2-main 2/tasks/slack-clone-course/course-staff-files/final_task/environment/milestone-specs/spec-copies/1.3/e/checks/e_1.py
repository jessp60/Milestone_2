import re, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["1.%d" % i for i in range(1, 15)]
w = mkworld(2)
if not w:
    fail_rest(R, "setup failed: could not create users, a workspace and a channel"); sys.exit()
o, m1, m2 = w["owner"], w["members"][0], w["members"][1]
ch = w["gid"]

def newmsg(text, tok, node=0):
    s, mid = post(node, ch, text, tok)
    return mid

def listing(node, tok):
    s, b = api("GET", node, "/api/channels/%d/messages?limit=200" % ch, None, tok)
    return b.get("messages", []) if s == 200 and isinstance(b, dict) else None

a = newmsg("e1-a-%d" % RUN, m1["token"])
if not a:
    fail_rest(R, "setup failed: could not post a message"); sys.exit()
s, b = api("PATCH", 0, "/api/messages/%d" % a, {"body": "e1-edited-%d" % RUN}, m1["token"])
msg = (b or {}).get("message", {}) if isinstance(b, dict) else {}
rule("1.1", s == 200 and msg.get("body") == "e1-edited-%d" % RUN and msg.get("edited_at"), "the author edits: 200 {message} with the new body and edited_at set (status %s)" % s)
rule("1.2", bool(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", str(msg.get("edited_at")))), "edited_at is an RFC 3339 UTC string (got %r)" % msg.get("edited_at"))
l2 = listing(2, m1["token"]) or []
x = [m for m in l2 if m.get("id") == a]
rule("1.3", bool(x) and x[0].get("body") == "e1-edited-%d" % RUN and x[0].get("edited_at"), "the edit is visible with edited_at in the listing on another node")
s_o, _ = api("PATCH", 1, "/api/messages/%d" % a, {"body": "hijack"}, o["token"])
s_m, _ = api("PATCH", 2, "/api/messages/%d" % a, {"body": "hijack"}, m2["token"])
rule("1.4", s_o == 403 and s_m == 403, "editing is author-only: workspace owner got %s, other member got %s (both must be 403)" % (s_o, s_m))
s, _ = api("PATCH", 0, "/api/messages/%d" % a, {"body": "x"}, None)
rule("1.5", s == 401, "edit without a token returns 401 (got %s)" % s)
s, _ = api("PATCH", 0, "/api/messages/99999999", {"body": "x"}, m1["token"])
rule("1.6", s == 404, "edit of an unknown message returns 404 (got %s)" % s)
s, _ = api("PATCH", 0, "/api/messages/%d" % a, {}, m1["token"])
rule("1.7", s == 400, "edit with no body returns 400 (got %s)" % s)

d = newmsg("e1-d-%d" % RUN, m1["token"])
api("POST", 0, "/api/messages/%d/reactions" % d, {"emoji": "👍"}, m2["token"])
s, b = api("DELETE", 1, "/api/messages/%d" % d, None, m1["token"])
rule("1.8", s == 200 and isinstance(b, dict) and b.get("deleted") is True, "the author deletes: 200 {\"deleted\":true} (status %s)" % s)
l = listing(0, m1["token"]) or []
x = [m for m in l if m.get("id") == d]
rule("1.9", (not x) or (not x[0].get("reactions")), "the deleted message is gone from the listing, or is a tombstone with no reactions counted")
s, b = api("DELETE", 2, "/api/messages/%d" % d, None, m1["token"])
rule("1.10", s == 200 and isinstance(b, dict) and b.get("deleted") is True, "deleting again is idempotent: 200 {\"deleted\":true} (status %s)" % s)
s, _ = api("PATCH", 0, "/api/messages/%d" % d, {"body": "late"}, m1["token"])
rule("1.11", s == 409, "editing a deleted message returns 409 (got %s)" % s)
c = newmsg("e1-c-%d" % RUN, m1["token"])
s, b = api("DELETE", 0, "/api/messages/%d" % c, None, o["token"])
rule("1.12", s == 200 and isinstance(b, dict) and b.get("deleted") is True, "the workspace owner deletes another user's message: 200 {\"deleted\":true} (status %s)" % s)
e = newmsg("e1-e-%d" % RUN, m1["token"])
s, _ = api("DELETE", 0, "/api/messages/%d" % e, None, m2["token"])
rule("1.13", s == 403, "a member who is neither author nor owner gets 403 on delete (got %s)" % s)

try:
    s1, s2 = Sub(1, o["token"]), Sub(2, o["token"])
    s1.subscribe(ch); s2.subscribe(ch)
    k = newmsg("e1-ev-%d" % RUN, m1["token"])
    api("PATCH", 0, "/api/messages/%d" % k, {"body": "e1-ev2-%d" % RUN}, m1["token"])
    api("DELETE", 2, "/api/messages/%d" % k, None, m1["token"])
    time.sleep(1.0)
    T = ("message.created", "message.updated", "message.deleted")
    t1 = [f["type"] for f in s1.events(ch, T)]
    i1, i2 = s1.event_ids(ch, T), s2.event_ids(ch, T)
    rule("1.14", t1 == list(T) and dense(i1) and i1 == i2, "message.created, message.updated, message.deleted arrive in order with dense event ids on both nodes (types %s, ids %s / %s)" % (t1, i1, i2))
except Exception as ex:
    rule("1.14", False, "could not open WebSockets: %s" % ex)
