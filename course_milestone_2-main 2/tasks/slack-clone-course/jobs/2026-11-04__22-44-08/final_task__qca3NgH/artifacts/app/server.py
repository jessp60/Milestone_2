#!/usr/bin/env python3
import os,sys,json,time,sqlite3,secrets,hashlib,re,base64,struct,socket,threading,urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
DB='/app/data/state.db'; os.makedirs('/app/data',exist_ok=True)
NODE=int(os.environ.get('NODE_ID','0')); PORT=8000+NODE
SCHEMA='''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT "",archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,created REAL,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,kind TEXT,created REAL,PRIMARY KEY(channel_id,event_id));'''
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.executescript(SCHEMA); return c
def now(): return time.time()
def outmsg(c,r):
 u=c.execute('SELECT id,username,display_name FROM users WHERE id=?',(r['author_id'],)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':dict(u) if u else None,'body':r['body'],'parent_id':None,'reply_count':0,'created_at':time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime(r['created']))+('%03d'%int((r['created']%1)*1000))+'Z','edited_at':None,'files':[],'reactions':[],'mentions':[]}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,obj):
  b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  x=self.headers.get('Authorization','')
  if not x.startswith('Bearer '): return None
  c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close(); return r
 def user_or_401(self):
  u=self.auth()
  if not u: self.sendj(401,{'error':'unauthorized'})
  return u
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/': return self.page()
  if path=='/api/ws': return self.websocket(urllib.parse.parse_qs(p.query).get('token',[''])[0])
  u=self.user_or_401()
  if not u:return
  c=db()
  if path=='/api/auth/me': self.sendj(200,{'user':dict(u)}); c.close(); return
  m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
  if m:
   rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(int(m.group(1)),)).fetchall()
   self.sendj(200,{'messages':[outmsg(c,r) for r in rows],'next_cursor':None}); c.close(); return
  m=re.fullmatch(r'/api/channels/(\d+)/read',path)
  if m:self.sendj(200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':0,'unread_count':0,'mention_count':0}});c.close();return
  m=re.fullmatch(r'/api/workspaces/([^/]+)',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{'error':'not found'})
   ch=c.execute('SELECT * FROM channels WHERE workspace_id=?',(w['id'],)).fetchall()
   self.sendj(200,{'workspace':dict(w),'channels':[{'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'topic':x['topic'],'is_archived':bool(x['archived'])} for x in ch],'read_state':[]});c.close();return
  if path=='/api/workspaces':
   ws=c.execute('SELECT * FROM workspaces').fetchall();self.sendj(200,{'workspaces':[dict(x) for x in ws]});c.close();return
  c.close(); self.sendj(404,{'error':'not found'})
 def do_POST(self):
  path=urllib.parse.urlparse(self.path).path
  if path=='/api/auth/register':
   try:
    b=self.body(); name=b['username']; pw=b['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8: raise ValueError()
    c=db(); cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(name,pw,b.get('display_name',name))); uid=cur.lastrowid; tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid));c.commit();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();self.sendj(201,{'user':dict(u),'token':tok});c.close()
   except sqlite3.IntegrityError:self.sendj(409,{'error':'duplicate'})
   except Exception:self.sendj(400,{'error':'invalid'})
   return
  if path=='/api/auth/login':
   b=self.body();c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
   if not u:self.sendj(401,{'error':'invalid credentials'});c.close();return
   tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,u['id']));c.commit();self.sendj(200,{'user':dict(u),'token':tok});c.close();return
  u=self.user_or_401()
  if not u:return
  b=self.body();c=db()
  if path=='/api/workspaces':
   try:
    slug=b['slug']; cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',slug),u['id']));wid=cur.lastrowid;c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));cc=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=cc.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();self.sendj(201,{'workspace':dict(w),'general_channel':{'id':cid,'workspace_id':wid,'name':'general','is_private':False,'topic':'','is_archived':False}})
   except sqlite3.IntegrityError:self.sendj(409,{'error':'duplicate'})
   c.close();return
  m=re.fullmatch(r'/api/channels/(\d+)/join',path)
  if m:
   cid=int(m.group(1));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:self.sendj(404,{'error':'not found'})
   else:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();self.sendj(200,{'joined':True})
   c.close();return
  m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
  if m:
   cid=int(m.group(1));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:self.sendj(404,{'error':'not found'});c.close();return
   text=str(b.get('body',''))
   if not text:self.sendj(400,{'error':'empty'});c.close();return
   try:
    c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created) VALUES(?,?,?,?)',(cid,u['id'],text,now()));mid=cur.lastrowid; eid=(c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]);c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,eid,mid,'message.created',now()));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();self.sendj(201,{'message':outmsg(c,r)})
   except Exception:c.rollback();self.sendj(400,{'error':'write failed'})
   c.close();return
  self.sendj(404,{'error':'not found'});c.close()
 def page(self):
  b=b'''<!doctype html><html><body><h1>Huddle</h1><div id="app"></div><script>localStorage.setItem('huddle.ready','1')</script></body></html>'''
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def websocket(self,token):
  c=db();u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
  if not u:self.send_error(401);return
  key=self.headers.get('Sec-WebSocket-Key',''); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  self.connection.settimeout(.2); subs=set(); sent={}; buf=b''
  def frame(x):
   z=json.dumps(x,separators=(',',':')).encode(); n=len(z); h=bytes([129])+((bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n))); 
   try:self.connection.sendall(h+z)
   except: raise
  def readframe():
   nonlocal buf
   while len(buf)<2: buf+=self.connection.recv(4096)
   n=buf[1]&127;i=2
   if n==126:
    while len(buf)<4:buf+=self.connection.recv(4096)
    n=struct.unpack('>H',buf[2:4])[0];i=4
   while len(buf)<i+4+n:buf+=self.connection.recv(4096)
   mask=buf[i:i+4]; z=buf[i+4:i+4+n];buf=buf[i+4+n:];return json.loads(bytes(v^mask[j%4] for j,v in enumerate(z)))
  try:
   while True:
    try:
     q=readframe()
     if q.get('type') in ('subscribe','resume'):
      ch=int(q['channel_id']);subs.add(ch); since=int(q.get('since_event_id',0));c=db(); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]; 
      for r in rows:
       mm=c.execute('SELECT * FROM messages WHERE id=?',(r['message_id'],)).fetchone();frame({'type':'message.created','event_id':r['event_id'],'channel_id':ch,'message':outmsg(c,mm)})
      frame({'type':'resumed' if q.get('type')=='resume' else 'subscribed','channel_id':ch,'head_event_id':head});sent[ch]=head;c.close()
    except socket.timeout: pass
    c=db()
    for ch in list(subs):
     last=sent.get(ch,0); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)).fetchall()
     for r in rows:
      mm=c.execute('SELECT * FROM messages WHERE id=?',(r['message_id'],)).fetchone();frame({'type':'message.created','event_id':r['event_id'],'channel_id':ch,'message':outmsg(c,mm)});sent[ch]=r['event_id']
    c.close()
  except Exception: return
if __name__=='__main__':
 db().close(); ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
