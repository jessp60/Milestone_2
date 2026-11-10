"""Helpers shared by the Component D and E checks (HTTP, WebSocket, IRC, killing a process by its port).
Imported by d_*.py / e_*.py. Not run directly."""
import base64, json, os, random, signal, socket, struct, threading, time, urllib.error, urllib.request

PORTS = (8000, 8001, 8002)
RUN = random.randint(10000, 99999)
PW = "correct-horse-1"


def base(n):
    return "http://127.0.0.1:%d" % PORTS[n]


def api(method, node, path, body=None, token=None, timeout=5):
    """Returns (status, json-or-None). Status 0 means no answer."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base(node) + path, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def health(node, timeout=1.5):
    s, b = api("GET", node, "/api/health", timeout=timeout)
    return b.get("node_id") if s == 200 and isinstance(b, dict) and b.get("status") == "ok" else None


def mkuser(tag):
    name = "u%d%s" % (RUN, tag)
    s, b = api("POST", 0, "/api/auth/register", {"username": name, "password": PW})
    if s != 201 or not b:
        return None
    return {"token": b["token"], "id": b["user"]["id"], "name": name}


def mkworld(extra=1):
    """One workspace with an owner and `extra` more members, all in #general. Returns a dict or None."""
    owner = mkuser("o")
    if not owner:
        return None
    slug = "w%d%d" % (RUN, random.randint(100, 999))
    s, b = api("POST", 0, "/api/workspaces", {"slug": slug, "name": "Check Workspace"}, owner["token"])
    if s != 201:
        return None
    gid = b["general_channel"]["id"]
    members = []
    for i in range(extra):
        m = mkuser("m%d" % i)
        if not m:
            return None
        s, _ = api("POST", 0, "/api/channels/%d/join" % gid, {}, m["token"])
        if s != 200:
            return None
        members.append(m)
    return {"owner": owner, "members": members, "slug": slug, "gid": gid}


def post(node, ch, text, token):
    """Returns (status, message-id-or-None)."""
    s, b = api("POST", node, "/api/channels/%d/messages" % ch, {"body": text}, token)
    return s, (b["message"]["id"] if s == 201 and isinstance(b, dict) and b.get("message") else None)


def bodies(node, ch, token):
    s, b = api("GET", node, "/api/channels/%d/messages?limit=200" % ch, None, token)
    if s != 200 or not isinstance(b, dict):
        return None
    return [m["body"] for m in b.get("messages", [])]


def rule(rid, ok, detail=""):
    print("%s %s %s" % ("ok" if ok else "bad", rid, detail), flush=True)


def fail_rest(ids, why):
    for r in ids:
        rule(r, False, why)


def wait_until(fn, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def dense(ids):
    return len(ids) > 0 and all(b - a == 1 for a, b in zip(ids, ids[1:]))


# ---------- killing a process by the port it listens on (never by name) ----------
def port_pid(port):
    inodes = set()
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(fn).read().splitlines()[1:]
        except Exception:
            continue
        for ln in lines:
            p = ln.split()
            if int(p[1].split(":")[1], 16) == port and p[3] == "0A":
                inodes.add(p[9])
    if not inodes:
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                try:
                    link = os.readlink("/proc/%s/fd/%s" % (pid, fd))
                except Exception:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    return int(pid)
        except Exception:
            continue
    return None


def kill_port(port):
    pid = port_pid(port)
    if pid is None or pid == os.getpid():
        return None
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return None
    return pid


def redis_ping(timeout=1.0):
    try:
        s = socket.create_connection(("127.0.0.1", 6379), timeout=timeout)
        s.sendall(b"PING\r\n")
        s.settimeout(timeout)
        d = s.recv(64)
        s.close()
        return d.startswith(b"+PONG")
    except Exception:
        return False


# ---------- WebSocket client ----------
class Sub:
    def __init__(self, node, token):
        self.frames = []
        self.closed = False
        self.s = socket.create_connection(("127.0.0.1", PORTS[node]), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET /api/ws?token=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                        % (token, PORTS[node], key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            d = self.s.recv(4096)
            if not d:
                raise OSError("closed during handshake")
            head += d
        first, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in first.split(b"\r\n")[0] + b" ":
            raise OSError("not upgraded: " + first.split(b"\r\n")[0].decode("latin1"))
        self.buf = rest
        self.s.settimeout(None)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        buf = self.buf
        while True:
            while len(buf) >= 2:
                op = buf[0] & 0x0F
                ln = buf[1] & 0x7F
                i = 2
                if ln == 126:
                    if len(buf) < 4:
                        break
                    ln = struct.unpack(">H", buf[2:4])[0]
                    i = 4
                elif ln == 127:
                    if len(buf) < 10:
                        break
                    ln = struct.unpack(">Q", buf[2:10])[0]
                    i = 10
                if len(buf) < i + ln:
                    break
                payload, buf = buf[i:i + ln], buf[i + ln:]
                if op == 1:
                    try:
                        f = json.loads(payload.decode())
                        if isinstance(f, dict):
                            f["_t"] = time.time()
                            self.frames.append(f)
                    except Exception:
                        pass
                elif op == 8:
                    self.closed = True
                    return
                elif op == 9:
                    self._raw(0xA, payload)
            try:
                d = self.s.recv(65536)
            except Exception:
                self.closed = True
                return
            if not d:
                self.closed = True
                return
            buf += d

    def _raw(self, op, payload):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | op])
        hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        try:
            self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        except Exception:
            self.closed = True

    def send(self, obj):
        self._raw(1, json.dumps(obj).encode())

    def wait(self, pred, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            for f in list(self.frames):
                if pred(f):
                    return f
            time.sleep(0.05)
        return None

    def subscribe(self, ch):
        self.send({"type": "subscribe", "channel_id": ch})
        f = self.wait(lambda x: x.get("type") == "subscribed" and x.get("channel_id") == ch)
        return f.get("head_event_id") if f else None

    def events(self, ch, types=("message.created",)):
        return [f for f in list(self.frames) if f.get("type") in types and f.get("channel_id") == ch]

    def event_ids(self, ch, types=("message.created",)):
        return [f["event_id"] for f in self.events(ch, types) if isinstance(f.get("event_id"), int)]

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------- IRC client ----------
class Irc:
    def __init__(self, timeout=3.0):
        self.s = socket.create_connection(("127.0.0.1", 6667), timeout=timeout)
        self.s.settimeout(0.3)
        self.buf = b""
        self.lines = []
        self.eof = False

    def send(self, line):
        self.s.sendall((line + "\r\n").encode())

    def pump(self, secs=0.0):
        end = time.time() + secs
        while True:
            try:
                d = self.s.recv(65536)
                if not d:
                    self.eof = True
                    break
                self.buf += d
            except socket.timeout:
                if time.time() >= end:
                    break
            except OSError:
                self.eof = True
                break
            if time.time() >= end and secs == 0:
                break
        while b"\r\n" in self.buf or b"\n" in self.buf:
            ln, _, self.buf = self.buf.partition(b"\n")
            ln = ln.decode("utf-8", "replace").rstrip("\r")
            if ln:
                self.lines.append(ln)

    def wait(self, pred, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.2)
            for ln in self.lines:
                if pred(ln):
                    return ln
            if self.eof:
                break
        for ln in self.lines:
            if pred(ln):
                return ln
        return None

    def numeric(self, code):
        return lambda ln: len(ln.split()) > 1 and ln.split()[1] == code

    def register(self, token, nick, timeout=4.0):
        self.send("PASS " + token)
        self.send("NICK " + nick)
        self.send("USER %s 0 * :%s" % (nick, nick))
        return self.wait(self.numeric("422"), timeout) is not None or any(
            len(l.split()) > 1 and l.split()[1] == "001" for l in self.lines)

    def numerics(self):
        out = []
        for ln in self.lines:
            p = ln.split()
            if len(p) > 1 and p[1].isdigit():
                out.append(p[1])
        return out

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass
