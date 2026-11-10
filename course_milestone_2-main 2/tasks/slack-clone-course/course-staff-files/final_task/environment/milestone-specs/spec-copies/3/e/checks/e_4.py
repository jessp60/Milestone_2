import re, sys, urllib.request
from html.parser import HTMLParser
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ws import *

R = ["4.%d" % i for i in range(1, 8)]
TESTIDS = """auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form
join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn workspace-header workspace-settings-btn
workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit channel-title channel-topic
channel-settings-btn channel-settings-title channel-settings-close channel-members-list channel-add-member-form
channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit archive-channel-btn
unarchive-channel-btn create-channel-form create-channel-private create-channel-submit create-channel-cancel message-list
message-input send-btn thread-panel thread-input thread-send close-thread settings-tab-members settings-tab-invitations
settings-tab-general settings-pane-members settings-pane-invitations settings-pane-general create-invitation-btn
create-invitation-form create-invitation-submit invitations-list""".split()
IDS = ["auth-modal", "create-channel-modal", "channel-settings-modal", "workspace-settings-modal", "create-invitation-form"]
ROLES = {"primary": ["auth-submit", "send-btn", "thread-send", "create-channel-submit", "join-workspace-submit", "workspace-general-submit",
                     "create-invitation-submit", "channel-topic-submit", "channel-add-member-submit"],
         "danger": ["archive-channel-btn", "logout-btn"],
         "secondary": ["new-channel-btn", "create-invitation-btn", "unarchive-channel-btn", "create-channel-cancel", "close-thread",
                       "channel-settings-close", "workspace-settings-close", "channel-settings-btn", "workspace-settings-btn"]}
COLORS = {"primary": ["#007a5a", "rgb(0, 122, 90)"], "danger": ["#e01e5a", "rgb(224, 30, 90)"], "secondary": ["#1264a3", "rgb(18, 100, 163)"]}

class P(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags = []; self.assets = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs); self.tags.append((tag, a))
        if tag == "link" and "stylesheet" in (a.get("rel") or "") and a.get("href"): self.assets.append(a["href"])
        if tag == "script" and a.get("src"): self.assets.append(a["src"])

def get(node, path):
    try:
        r = urllib.request.urlopen(base(node) + path, timeout=5)
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "", ""

pages = {}
for n in (0, 1, 2):
    pages[n] = get(n, "/")
rule("4.1", all(p[0] == 200 and "html" in p[1].lower() and "<" in p[2] for p in pages.values()), "GET / returns 200 HTML on all three nodes (statuses %s)" % [p[0] for p in pages.values()])
if pages[0][0] != 200:
    fail_rest(R[1:], "GET / on node 0 did not return the page"); sys.exit()
doc = pages[0][2]
par = P(); par.feed(doc)
tid = {a.get("data-testid"): a for t, a in par.tags if a.get("data-testid")}
dom = {a.get("id") for t, a in par.tags if a.get("id")}
miss = [t for t in TESTIDS if t not in tid]
rule("4.2", not miss, "every pinned data-testid that is not data-dependent is in the initial HTML (missing: %s)" % miss)
missid = [i for i in IDS if i not in dom]
rule("4.3", not missid, "the pinned DOM ids are in the initial HTML (missing: %s)" % missid)
wrong = [t for role, ts in ROLES.items() for t in ts if t in tid and tid[t].get("data-button-role") != role]
rule("4.4", not wrong and not [t for ts in ROLES.values() for t in ts if t not in tid], "every pinned button carries the right data-button-role (wrong or missing: %s)" % wrong)
text = doc
for a in par.assets[:8]:
    path = a if a.startswith("/") else "/" + a
    if a.startswith("http"): continue
    text += "\n" + get(0, path)[2]
low = text.lower()
nocol = [r for r, cs in COLORS.items() if not any(c in low for c in cs)]
rule("4.5", not nocol and ("#ffffff" in low or "#fff" in low or "rgb(255, 255, 255)" in low or "white" in low), "the page styles use the pinned button colours #007a5a, #e01e5a and #1264a3 with white text (no colour found for: %s)" % nocol)
rule("4.6", "huddle.token" in text, "the page code stores the bearer token under the localStorage key huddle.token")
same = []
for n in (1, 2):
    p2 = P(); p2.feed(pages[n][2]); t2 = {a.get("data-testid") for t, a in p2.tags if a.get("data-testid")}
    same.append(t2 == set(tid))
rule("4.7", all(same), "nodes 1 and 2 serve the same pinned elements as node 0")
