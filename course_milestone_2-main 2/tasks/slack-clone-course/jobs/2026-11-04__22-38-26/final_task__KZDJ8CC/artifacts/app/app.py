import os, sys, json, time, uuid, base64, hashlib, socket, struct, threading, sqlite3, re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB='/app/data/chat.db'
os.makedirs('/app/data',exist_ok=True)
db_lock=threading.RLock()
def conn():
    c=sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory=sqlite3.Row
    return c
def init():
    with db_lock:
        c=conn()
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0,event_seq INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
        '''); c.commit(); c.close()
init()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+('%03d'%(int(time.time()*1000)%1000))+'Z'
def userobj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def msgobj(c,r):
    u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
    replies=c.execute('select count(*) n from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
def emit(c,ch,kind,payload):
    seq=c.execute('select event_seq from channels where id=?',(ch,)).fetchone()['event_seq']+1
    c.execute('update channels set event_seq=? where id=?',(seq,ch))
    p=dict(payload); p.update(type=kind,event_id=seq,channel_id=ch)
    c.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(ch,seq,kind,json.dumps(p)))
    return p
def token_user(c,h):
    a=h.get('Authorization','')
    if not a.startswith('Bearer '): return None
    r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(a[7:],)).fetchone()
    return r
class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*x): pass
    def sendj(self,status,obj):
        b=json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        p=urlparse(self.path); path=p.path
        if path=='/api/health':
            return self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
        if path=='/': return self.sendj(200,{'status':'ok'})
        c=conn(); u=token_user(c,self.headers)
        if path.startswith('/api/ws') and not u:
            qt=parse_qs(p.query).get('token',[''])[0]
            if qt: u=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(qt,)).fetchone()
        if path=='/api/auth/me':
            if not u:return self.sendj(401,{'error':'unauthorized'})
            return self.sendj(200,{'user':userobj(u)})
        if path.startswith('/api/ws'): return self.websocket(c,u)
        if not u:return self.sendj(401,{'error':'unauthorized'})
        m=re.match(r'^/api/workspaces/([^/]+)$',path)
        if m:
            w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
            if not w:return self.sendj(404,{'error':'not found'})
            chans=[dict(x) for x in c.execute('select * from channels where workspace_id=? and archived=0',(w['id'],))]
            for x in chans:x['is_private']=bool(x['is_private']);x['is_dm']=False;x['is_archived']=bool(x.pop('archived'));x['workspace_id']=w['id']
            return self.sendj(200,{'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'channels':chans,'read_state':{}})
        m=re.match(r'^/api/channels/(\d+)/messages$',path)
        if m:
            ch=int(m.group(1)); rows=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc limit 200',(ch,)).fetchall()
            return self.sendj(200,{'messages':[msgobj(c,x) for x in rows],'next_cursor':None})
        self.sendj(404,{'error':'not found'})
    def do_POST(self):
        p=urlparse(self.path); path=p.path; c=conn()
        if path=='/api/auth/register':
            b=self.body(); name=b.get('username',''); pw=b.get('password','')
            if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8:return self.sendj(400,{'error':'invalid'})
            try:
                c.execute('insert into users(username,password,display_name) values(?,?,?)',(name,pw,b.get('display_name') or name)); uid=c.execute('select last_insert_rowid()').fetchone()[0]; tok=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('insert into tokens values(?,?)',(tok,uid));c.commit()
            except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
            return self.sendj(201,{'user':userobj(c.execute('select * from users where id=?',(uid,)).fetchone()),'token':tok})
        if path=='/api/auth/login':
            b=self.body();u=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
            if not u:return self.sendj(401,{'error':'invalid credentials'})
            tok=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('insert into tokens values(?,?)',(tok,u['id']));c.commit();return self.sendj(200,{'user':userobj(u),'token':tok})
        u=token_user(c,self.headers)
        if not u:return self.sendj(401,{'error':'unauthorized'})
        b=self.body()
        if path=='/api/workspaces':
            slug=b.get('slug',''); name=b.get('name','')
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return self.sendj(400,{'error':'invalid slug'})
            try:
                c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,name,u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));gid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(gid,u['id']));c.commit()
            except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
            return self.sendj(201,{'workspace':{'id':wid,'slug':slug,'name':name,'owner_id':u['id'],'join_mode':'open'},'general_channel':{'id':gid,'workspace_id':wid,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False}})
        m=re.match(r'^/api/channels/(\d+)/join$',path)
        if m:
            ch=int(m.group(1)); q=c.execute('select * from channels where id=?',(ch,)).fetchone()
            if not q:return self.sendj(404,{'error':'not found'})
            c.execute('insert or ignore into channel_members values(?,?)',(ch,u['id']));c.execute('insert or ignore into members values(?,?,?)',(q['workspace_id'],u['id'],'member'));c.commit();return self.sendj(200,{'channel':dict(q)})
        m=re.match(r'^/api/channels/(\d+)/messages$',path)
        if m:
            ch=int(m.group(1)); q=c.execute('select * from channels where id=?',(ch,)).fetchone()
            if not q:return self.sendj(404,{'error':'not found'})
            if q['archived']:return self.sendj(423,{'error':'archived'})
            body=b.get('body','')
            if not isinstance(body,str) or not body.strip():return self.sendj(400,{'error':'empty'})
            with db_lock:
                c.execute('begin immediate')
                t=now();c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,u['id'],body,b.get('parent_id'),t));mid=c.execute('select last_insert_rowid()').fetchone()[0];r=c.execute('select * from messages where id=?',(mid,)).fetchone(); ev=emit(c,ch,'message.created',{'message':msgobj(c,r)});c.commit()
            return self.sendj(201,{'message':msgobj(c,r)})
        self.sendj(404,{'error':'not found'})
    def websocket(self,c,u):
        if not u:self.send_error(401);return
        key=self.headers.get('Sec-WebSocket-Key')
        if not key:self.send_error(400);return
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
        self.connection.settimeout(.2); subscribed={}; alive=True
        def frame(data):
            b=data.encode(); n=len(b); h=bytes([0x81])+((bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n)))
            try:self.connection.sendall(h+b)
            except: pass
        def send(o): frame(json.dumps(o,separators=(',',':')))
        while alive:
            try:
                first=self.connection.recv(2)
                if not first:break
                ln=first[1]&127
                if ln==126:ln=struct.unpack('>H',self.connection.recv(2))[0]
                mask=self.connection.recv(4); data=self.connection.recv(ln); data=bytes(x^mask[i%4] for i,x in enumerate(data))
                x=json.loads(data)
                if x.get('type')=='subscribe':
                    ch=int(x['channel_id']);subscribed[ch]=0
                    head=c.execute('select event_seq from channels where id=?',(ch,)).fetchone()
                    send({'type':'subscribed','channel_id':ch,'head_event_id':head['event_seq'] if head else 0})
                elif x.get('type')=='resume':
                    ch=int(x['channel_id']);since=int(x.get('since_event_id',0)); rows=c.execute('select payload from events where channel_id=? and event_id>? order by event_id',(ch,since)).fetchall()
                    for r in rows:send(json.loads(r['payload']))
                    head=c.execute('select event_seq from channels where id=?',(ch,)).fetchone();send({'type':'resumed','channel_id':ch,'head_event_id':head['event_seq'] if head else since})
            except socket.timeout: pass
            except Exception: break
            for ch in list(subscribed):
                rows=c.execute('select payload from events where channel_id=? and event_id>? order by event_id',(ch,subscribed[ch])).fetchall()
                for r in rows:
                    x=json.loads(r['payload']);send(x);subscribed[ch]=x['event_id']
        try:self.connection.close()
        except:pass
def run(port):
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
if __name__=='__main__':run(int(sys.argv[1]))
