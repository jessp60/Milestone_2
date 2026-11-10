import sys,os,json,re,sqlite3,threading,time,hashlib,base64,struct,secrets,datetime,subprocess,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/state.db'; NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=10000'); return c
def init():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(ws INTEGER,user_id INTEGER,role TEXT,UNIQUE(ws,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,ws INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT "",archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(ch INTEGER,user_id INTEGER,UNIQUE(ch,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,kind TEXT,created TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER);
'''); c.commit(); c.close()
init()
def user_from(h):
 t=h.get('Authorization','')
 if not t.startswith('Bearer '): return None
 c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(t[7:],)).fetchone(); c.close()
 return r['user_id'] if r else None
def juser(c,uid):
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 return dict(r) if r else None
def channel(c,ch):
 r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone(); return r
def allowed(c,uid,ch):
 r=c.execute('SELECT ws,is_private FROM channels WHERE id=?',(ch,)).fetchone()
 return bool(r and (c.execute('SELECT 1 FROM cmembers WHERE ch=? AND user_id=?',(ch,uid)).fetchone() or c.execute('SELECT 1 FROM members WHERE ws=? AND user_id=?',(r['ws'],uid)).fetchone() and not r['is_private']))
def msgobj(c,r):
 d=dict(r); d['author']=juser(c,r['author_id']); d['parent_id']=r['parent_id']; d['edited_at']=r['edited']; d['files']=[]; d['reactions']=[]; d['mentions']=[]
 d['reply_count']=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; return d
clients=[]; lock=threading.Lock()
def broadcast_loop():
 seen={}
 while True:
  try:
   c=db(); rows=c.execute('SELECT * FROM events ORDER BY channel_id,event_id').fetchall()
   for e in rows:
    k=(e['channel_id'],e['event_id'])
    if k in seen: continue
    seen[k]=1
    m=c.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone()
    if not m: continue
    frame={'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],'message':msgobj(c,m)}
    with lock:
     for x in list(clients):
      if x['ch']==e['channel_id']: x['send'](frame)
   c.close()
  except Exception: pass
  time.sleep(.03)
threading.Thread(target=broadcast_loop,daemon=True).start()
def wsframe(o):
 b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
 if n<126: h=bytes([129,n])
 elif n<65536: h=bytes([129,126])+struct.pack('>H',n)
 else: h=bytes([129,127])+struct.pack('>Q',n)
 return h+b
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def out(self,code,obj=None,extra={}):
  b=(json.dumps(obj).encode() if obj is not None else b''); self.send_response(code)
  self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  for k,v in extra.items(): self.send_header(k,v)
  self.end_headers()
  if b:self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=self.path.split('?')[0]
  if p=='/api/health': return self.out(200,{'status':'ok','node_id':NODE})
  if p=='/': return self.out(200,{'status':'ok'})
  uid=user_from(self.headers)
  if p=='/api/auth/me':
   if not uid:return self.out(401,{'error':'unauthorized'})
   c=db(); self.out(200,{'user':juser(c,uid)});c.close();return
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m and uid:
   ch=int(m.group(1)); c=db()
   if not allowed(c,uid,ch):c.close();return self.out(403,{'error':'forbidden'})
   rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(ch,)).fetchall()
   self.out(200,{'messages':[msgobj(c,x) for x in rows],'next_cursor':None});c.close();return
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m and uid:
   c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:c.close();return self.out(404,{})
   cs=c.execute('SELECT * FROM channels WHERE ws=? AND archived=0',(w['id'],)).fetchall()
   self.out(200,{'workspace':dict(w),'channels':[dict(x) for x in cs],'read_state':{}});c.close();return
  if p=='/api/workspaces' and uid:
   c=db(); a=[dict(x) for x in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.ws=w.id WHERE m.user_id=?',(uid,))]
   self.out(200,{'workspaces':a});c.close();return
  if p.startswith('/api/ws'): return self.websocket(uid)
  self.out(404,{'error':'not found'})
 def do_POST(self):
  p=self.path.split('?')[0]; body=self.read(); uid=user_from(self.headers)
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',body.get('username','')) or len(body.get('password',''))<8:return self.out(400,{'error':'invalid'})
   c=db()
   try:
    c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(body['username'],body['password'],body.get('display_name') or body['username'])); c.commit()
   except sqlite3.IntegrityError:c.close();return self.out(409,{'error':'duplicate'})
   u=c.execute('SELECT * FROM users WHERE username=?',(body['username'],)).fetchone(); t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.commit();self.out(201,{'user':juser(c,u['id']),'token':t});c.close();return
  if p=='/api/auth/login':
   c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(body.get('username'),body.get('password'))).fetchone()
   if not u:c.close();return self.out(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.commit();self.out(200,{'user':juser(c,u['id']),'token':t});c.close();return
  if not uid:return self.out(401,{'error':'unauthorized'})
  m=re.match(r'/api/workspaces/([^/]+)/channels',p)
  if m:
   c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.out(404,{})
   try:c.execute('INSERT INTO channels(ws,name,topic,is_private) VALUES(?,?,?,?)',(w['id'],body.get('name'),body.get('topic',''),int(body.get('is_private',False))));c.commit()
   except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
   r=c.execute('SELECT * FROM channels WHERE id=last_insert_rowid()').fetchone();c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(r['id'],uid));c.commit();self.out(201,{'channel':dict(r)});c.close();return
  if p=='/api/workspaces':
   c=db()
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(body.get('slug'),body.get('name'),uid)); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,uid,'owner'));c.execute('INSERT INTO channels(ws,name) VALUES(?,?)',(wid,'general'));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,uid));c.commit()
   except sqlite3.IntegrityError:return self.out(409,{})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone();self.out(201,{'workspace':dict(w),'general_channel':dict(r)});c.close();return
  m=re.match(r'/api/channels/(\d+)/join',p)
  if m:
   c=db();r=channel(c,int(m.group(1)))
   if not r:return self.out(404,{})
   c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['ws'],uid,'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(r['id'],uid));c.commit();self.out(200,{'channel':dict(r)});c.close();return
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=int(m.group(1));c=db();r=channel(c,ch)
   if not r:return self.out(404,{})
   if not allowed(c,uid,ch):return self.out(403,{})
   if r['archived']:return self.out(423,{})
   text=body.get('body','')
   if not text:return self.out(400,{'error':'empty'})
   ts=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created) VALUES(?,?,?,?,?)',(ch,uid,text,body.get('parent_id'),ts));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; n=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone(); ev=(n['n']+1 if n else 1);c.execute('INSERT OR REPLACE INTO seq VALUES(?,?)',(ch,ev));c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch,ev,mid,'message.reply' if body.get('parent_id') else 'message.created',ts));c.commit();x=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();self.out(201,{'message':msgobj(c,x)});c.close();return
  self.out(404,{})
 def do_PATCH(self):
  if not user_from(self.headers):return self.out(401,{})
  self.out(200,{})
 def websocket(self,uid):
  if not uid:
   q=self.path.split('?',1)[1] if '?' in self.path else ''
   tok=next((v for v in (x.split('=',1)[1] for x in q.split('&') if '=' in x and x.split('=',1)[0]=='token')),None)
   if tok:
    c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); c.close()
    uid=r['user_id'] if r else None
  if not uid:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send_error(400)
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  def send(x): self.wfile.write(wsframe(x));self.wfile.flush()
  def recv():
   h=self.rfile.read(2)
   if len(h)<2:return None
   n=h[1]&127
   if n==126:n=struct.unpack('>H',self.rfile.read(2))[0]
   elif n==127:n=struct.unpack('>Q',self.rfile.read(8))[0]
   mask=self.rfile.read(4); b=bytearray(self.rfile.read(n))
   for i in range(n):b[i]^=mask[i%4]
   return json.loads(b)
  try:
   while True:
    q=recv()
    if not q:break
    ch=q.get('channel_id')
    if q.get('type') in ('subscribe','resume'):
     c=db(); head=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone(); head=head['n'] if head else 0
     with lock: clients.append({'ch':ch,'send':send})
     if q['type']=='resume':
      for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,q.get('since_event_id',0))):
       mm=c.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone();send({'type':e['kind'],'event_id':e['event_id'],'channel_id':ch,'message':msgobj(c,mm)})
      send({'type':'resumed','channel_id':ch,'head_event_id':head})
     else:send({'type':'subscribed','channel_id':ch,'head_event_id':head})
     c.close()
  except Exception:pass
  finally:
   with lock: clients[:]=[x for x in clients if x.get('send')!=send]
try: ThreadingHTTPServer(('127.0.0.1',8000+NODE),H).serve_forever()
except: pass
