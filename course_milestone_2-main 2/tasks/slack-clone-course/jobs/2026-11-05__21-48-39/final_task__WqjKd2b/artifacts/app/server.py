import os
import re
import json
import time
import uuid
import sqlite3
import fcntl
import hashlib
import secrets
import mimetypes
import base64
import struct
import threading
import urllib.parse
import datetime
import socket
import cgi
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
DB='/app/data.db'; os.makedirs('/app',exist_ok=True)
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def jdump(x): return json.dumps(x,separators=(',',':')).encode()
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000'); c.execute('PRAGMA synchronous=NORMAL'); return c
def init():
 for attempt in range(20):
  try:
   c=db()
   c.execute('PRAGMA journal_mode=WAL')
   c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,UNIQUE(message_id,file_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,target_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER,mentions INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS presence(user_id INTEGER,channel_id INTEGER,last_seen REAL,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id))'''); c.commit(); return c
  except sqlite3.OperationalError:
   try: c.close()
   except: pass
   time.sleep(.25)
 raise
init()
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(c,h):
 t=h.headers.get('Authorization','')
 if not t.startswith('Bearer '): return None
 r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t[7:],)).fetchone()
 return r
def channel(c,cid):
 return c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
def role(c,w,u):
 r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); return r['role'] if r else None
def accessible(c,ch,u):
 if not ch:return False
 return c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u)).fetchone() is not None
def mention_ids(c,r):
 text=r['body'] or ''; author=r['author_id']; ch=channel(c,r['channel_id']); out=[]
 members=[x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(r['channel_id'],))]
 for z in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',text):
  ids=[]
  if z=='channel': ids=members
  elif z=='here':
   with lock: online={c.execute('SELECT user_id FROM tokens WHERE token=?',('',)).fetchone()} if False else set()
   ids=[x['user_id'] for x in c.execute('SELECT user_id FROM presence WHERE channel_id=? AND last_seen>?',(r['channel_id'],time.time()-30))]
  else:
   u=c.execute('SELECT id FROM users WHERE username=?',(z,)).fetchone()
   g=c.execute('SELECT users FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],z)).fetchone()
   if u: ids=[u['id']]
   elif g: ids=json.loads(g['users'])
  for i in ids:
   if i!=author and i in members and i not in out: out.append(i)
 return out
def msgobj(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rx=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
 replies=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()],'reactions':rx,'mentions':mention_ids(c,r)}
def ws_send(sock,obj):
 b=jdump(obj); n=len(b); head=bytes([0x81,n]) if n<126 else bytes([0x81,126])+struct.pack('>H',n)
 try:sock.sendall(head+b)
 except: pass
clients=[]; online_users=set(); lock=threading.Lock()
def broadcast(ch,obj):
 with lock:
  for s,cid,*_ in list(clients):
   if cid==ch: ws_send(s,obj)
def broadcast_global(obj):
 with lock:
  for s,cid,*_ in list(clients): ws_send(s,obj)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def send(self,status,obj=None,headers=None):
  b=b'' if obj is None else (obj if isinstance(obj,bytes) else jdump(obj))
  self.send_response(status); self.send_header('Content-Length',str(len(b)))
  if obj is not None and not isinstance(obj,bytes):self.send_header('Content-Type','application/json')
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers()
  if b:self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'))
  try: return json.loads(self.rfile.read(n) or b'{}')
  except Exception:
   self.send(400,{'error':'invalid_json'}); return None
 def route(self):
  return urllib.parse.urlparse(self.path)
 def do_GET(self):
  p=self.route(); path=p.path; c=db()
  if path=='/api/health': return self.send(200,{'status':'ok','node_id':int(os.getenv('NODE_ID','0'))})
  if path=='/' or path.startswith('/static'): return self.send(200,HTML.encode(),{'Content-Type':'text/html'})
  if path=='/api/ws': return self.websocket(c)
  u=auth(c,self)
  if path=='/api/auth/me': return self.send(200,{'user':userobj(u)}) if u else self.send(401,{'error':'unauthorized'})
  if path.startswith('/api/auth/') : return self.send(404,{})
  if not u:return self.send(401,{'error':'unauthorized'})
  seg=path.split('/')
  if path=='/api/workspaces':
   rows=c.execute('SELECT * FROM workspaces WHERE id IN (SELECT workspace_id FROM members WHERE user_id=?)',(u['id'],)).fetchall()
   return self.send(200,{'workspaces':[dict(x) for x in rows]})
  if len(seg)==4 and seg[2]=='users':
   r=c.execute('SELECT * FROM users WHERE id=?',(seg[3],)).fetchone(); return self.send(200,{'user':userobj(r)}) if r else self.send(404,{})
  if len(seg)==5 and seg[2]=='files' and seg[4]=='download':
   r=c.execute('SELECT * FROM files WHERE id=?',(seg[3],)).fetchone()
   if not r:return self.send(404,{})
   return self.send(200,bytes(r['data']),{'Content-Type':r['content_type'],'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
  if path=='/api/files/'+seg[-1] if len(seg)>3 else False:
   r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(seg[-1],)).fetchone(); return self.send(200,{'file':dict(r)}) if r else self.send(404,{})
  if len(seg)>=4 and seg[2]=='workspaces':
   slug=seg[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return self.send(404,{})
   if len(seg)==5 and seg[4]=='members':
    rows=c.execute('SELECT u.id,m.role,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall()
    return self.send(200,{'members':[dict(x, user_id=x['id']) for x in rows]})
   if len(seg)==5 and seg[4]=='invitations':
    if role(c,w['id'],u['id']) not in ('owner','admin'):return self.send(403,{})
    rs=c.execute('SELECT * FROM invites WHERE workspace_id=? AND expires>=? AND uses<max_uses',(w['id'],int(time.time()))).fetchall()
    return self.send(200,{'invitations':[{'code':x['code'],'workspace_id':x['workspace_id'],'invited_user_id':x['target_id'],'max_uses':x['max_uses'],'uses':x['uses'],'expires_at':x['expires']} for x in rs]})
   if len(seg)==5 and seg[4]=='groups':
    gs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall()
    return self.send(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['users'])} for g in gs]})
   ch=c.execute('SELECT * FROM channels WHERE workspace_id=? '+('' if urllib.parse.parse_qs(p.query).get('include_archived',[''])[0] in ('1','true') else 'AND is_archived=0'),(w['id'],)).fetchall()
   states=[]
   for x in ch:
    rr=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone(); states.append({'channel_id':x['id'],'last_read_event_id':rr['last_read'] if rr else 0,'unread_count':0,'mention_count':rr['mentions'] if rr else 0})
   return self.send(200,{'workspace':dict(w),'channels':[dict(x,is_private=bool(x['is_private']),is_dm=bool(x['is_dm']),is_archived=bool(x['is_archived'])) for x in ch],'read_state':states})
  if len(seg)>=4 and seg[2]=='channels':
   ch=channel(c,seg[3])
   if not ch:return self.send(404,{})
   if not accessible(c,ch,u['id']):return self.send(403,{})
   if len(seg)==5 and seg[4]=='members':
    rows=c.execute('SELECT u.* FROM cmembers m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?',(ch['id'],)).fetchall()
    return self.send(200,{'members':[userobj(x) for x in rows]})
   if len(seg)==5 and seg[4]=='messages':
    q=urllib.parse.parse_qs(p.query); lim=int(q.get('limit',['50'])[0])
    if lim<1 or lim>200:return self.send(400,{})
    cur=q.get('cursor',[None])[0]
    try: rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 '+('AND id<? ' if cur else '')+'ORDER BY id DESC LIMIT ?',((ch['id'],int(cur),lim+1) if cur else (ch['id'],lim+1))).fetchall()
    except: return self.send(400,{})
    nxt=str(rows[lim-1]['id']) if len(rows)>lim else None
    return self.send(200,{'messages':[msgobj(c,x) for x in rows[:lim]],'next_cursor':nxt})
   if len(seg)==5 and seg[4]=='read':
    r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
    return self.send(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':r['last_read'] if r else 0,'unread_count':0,'mention_count':r['mentions'] if r else 0}})
   if len(seg)==5 and seg[4]=='replies':
    q=urllib.parse.parse_qs(p.query); lim=int(q.get('limit',['50'])[0]); cur=q.get('cursor',[None])[0]
    if lim<1 or lim>200:return self.send(400,{})
    try:
     rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 '+('AND id>? ' if cur else '')+'ORDER BY id ASC LIMIT ?',((int(seg[3]),int(cur),lim+1) if cur else (int(seg[3]),lim+1))).fetchall()
    except: return self.send(404,{})
    nxt=str(rows[lim-1]['id']) if len(rows)>lim else None
    return self.send(200,{'replies':[msgobj(c,x) for x in rows[:lim]],'next_cursor':nxt})
   if len(seg)==5 and seg[4]=='pins':
    q=urllib.parse.parse_qs(p.query); lim=int(q.get('limit',['50'])[0]); cur=q.get('cursor',[None])[0]
    if lim<1 or lim>200:return self.send(400,{})
    try: rs=c.execute('SELECT * FROM pins WHERE message_id IN (SELECT id FROM messages WHERE channel_id=?) '+('AND message_id<? ' if cur else '')+'ORDER BY message_id DESC LIMIT ?',((ch['id'],int(cur),lim+1) if cur else (ch['id'],lim+1))).fetchall()
    except: return self.send(400,{})
    nxt=str(rs[lim-1]['message_id']) if len(rs)>lim else None
    return self.send(200,{'pins':[{'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(x['message_id'],)).fetchone()),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rs[:lim]],'next_cursor':nxt})
  if path.startswith('/api/search'):
   z=urllib.parse.parse_qs(p.query); text=z.get('q',[''])[0]; lim=int(z.get('limit',['50'])[0]); cur=z.get('cursor',[None])[0]
   if lim<1 or lim>200:return self.send(400,{})
   try: rs=c.execute("SELECT * FROM messages WHERE deleted=0 AND body LIKE ? "+('AND id<? ' if cur else '')+"ORDER BY id DESC LIMIT ?",(('%'+text+'%',int(cur),lim+1) if cur else ('%'+text+'%',lim+1))).fetchall()
   except: return self.send(400,{})
   nxt=str(rs[lim-1]['id']) if len(rs)>lim else None
   return self.send(200,{'results':[msgobj(c,x) for x in rs[:lim]],'next_cursor':nxt})
  return self.send(404,{})
 def do_POST(self): return self._do_POST()
 def do_PATCH(self): return self._do_PATCH()
 def do_DELETE(self): return self._do_DELETE()
 def _do_POST(self):
  p=self.route(); path=p.path; c=db()
  if path=='/api/auth/register':
   b=self.body()
   if b is None:return
   un=b.get('username',''); pw=b.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.send(400,{})
   try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); c.commit()
   except:return self.send(409,{})
   r=c.execute('SELECT * FROM users WHERE username=?',(un,)).fetchone(); t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit()
   return self.send(201,{'user':userobj(r),'token':t})
  if path=='/api/auth/login':
   b=self.body()
   if b is None:return
   r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
   if not r:return self.send(401,{})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();return self.send(200,{'user':userobj(r),'token':t})
  u=auth(c,self)
  if not u:return self.send(401,{})
  if path=='/api/files':
   if not self.headers.get('Content-Type','').startswith('multipart/form-data'):return self.send(400,{})
   form=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers.get('Content-Type',''),'CONTENT_LENGTH':self.headers.get('Content-Length','0')})
   if 'file' not in form:return self.send(400,{})
   part=form['file']; data=part.file.read() if getattr(part,'file',None) else b''
   if len(data)>10*1024*1024:return self.send(413,{})
   fn=part.filename or 'file'; ct=part.type or 'application/octet-stream'
   c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,ct,len(data),now(),data));c.commit()
   r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=last_insert_rowid()').fetchone()
   return self.send(201,{'file':dict(r)})
  b=self.body()
  if b is None:return
  m=re.match(r'/api/channels/([0-9]+)/read$',path)
  if m:
   ch=channel(c,m.group(1))
   if not ch:return self.send(404,{})
   if not accessible(c,ch,u['id']):return self.send(403,{})
   val=b.get('last_read_event_id')
   if not isinstance(val,int):return self.send(400,{})
   r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
   oldv=r['last_read'] if r else 0
   val=max(oldv,val)
   c.execute('INSERT OR REPLACE INTO reads(user_id,channel_id,last_read,mentions) VALUES(?,?,?,?)',(u['id'],ch['id'],val,r['mentions'] if r else 0));c.commit()
   return self.send(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':val,'unread_count':0,'mention_count':r['mentions'] if r else 0}})
  if path=='/api/workspaces':
   slug=b.get('slug','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return self.send(400,{})
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),u['id'])); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general'));ch=c.execute('SELECT * FROM channels WHERE workspace_id=?',(w['id'],)).fetchone();c.execute('INSERT INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.commit()
   except:return self.send(409,{})
   return self.send(201,{'workspace':dict(w),'general_channel':dict(ch)})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); r=role(c,w['id'],u['id']) if w else None
   if not w:return self.send(404,{})
   if r not in ('owner','admin','member') or (b.get('is_private') and r=='member'):return self.send(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.send(400,{})
   if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name'])).fetchone():return self.send(409,{})
   c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')));ch=c.execute('SELECT * FROM channels WHERE id=last_insert_rowid()').fetchone();c.execute('INSERT INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.send(201,{'channel':dict(ch, is_private=bool(ch['is_private']), is_dm=bool(ch['is_dm']), is_archived=bool(ch['is_archived']))})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=channel(c,m.group(1))
   if not ch:return self.send(404,{})
   r=role(c,ch['workspace_id'],u['id']); w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if ch['is_private'] and not accessible(c,ch,u['id']) or r=='guest':return self.send(403,{})
   if not r and w['join_mode']=='invite_only':return self.send(403,{})
   if not r:c.execute('INSERT INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
   c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.commit();return self.send(200,{'channel':dict(ch, is_private=bool(ch['is_private']), is_dm=bool(ch['is_dm']), is_archived=bool(ch['is_archived']))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=channel(c,m.group(1))
   if not ch:return self.send(404,{})
   if not accessible(c,ch,u['id']):return self.send(403,{})
   if ch['is_archived'] and not (b.get('body','').startswith('/unarchive') or b.get('body','').startswith('/archive')):return self.send(423,{})
   body=b.get('body','')
   if not body:return self.send(400,{})
   if body.startswith('/') and not body.startswith('//'):
    z=body.split(' ',1); cmd=z[0]; arg=z[1].strip() if len(z)>1 else ''
    if cmd=='/shrug':body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/me' and not arg:return self.send(400,{})
    elif cmd in ('/topic','/archive','/unarchive') or cmd=='/invite':
     if cmd=='/topic':
      c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));c.commit();cc=channel(c,ch['id']);return self.send(201,{'message':None,'channel':dict(cc,is_private=bool(cc['is_private']),is_dm=bool(cc['is_dm']),is_archived=bool(cc['is_archived']))})
     if cmd in ('/archive','/unarchive'):
      if role(c,ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
      c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,ch['id']));c.commit()
      cc=channel(c,ch['id'])
      return self.send(201,{'message':None,'channel':dict(cc,is_private=bool(cc['is_private']),is_dm=bool(cc['is_dm']),is_archived=bool(cc['is_archived']))})
     if cmd=='/invite':
      if not arg.startswith('@'):return self.send(400,{})
      z=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
      if not z:return self.send(404,{})
      if role(c,ch['workspace_id'],u['id']) not in ('owner','admin','member'):return self.send(403,{})
      c.execute('INSERT OR IGNORE INTO cmembers(channel_id,user_id) VALUES(?,?)',(ch['id'],z['id']));c.commit()
      return self.send(201,{'message':None,'invited_user':userobj(z)})
     return self.send(400,{})
    elif cmd not in ('/me','/shrug'):return self.send(400,{})
   fids=b.get('file_ids',[]) or []
   for fid in fids:
    fr=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not fr or fr['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():
     c.rollback(); return self.send(400,{})
   evlock=open('/app/event.lock','w'); fcntl.flock(evlock,fcntl.LOCK_EX)
   try:
    c.execute('INSERT OR IGNORE INTO seq VALUES(?,0)',(ch['id'],));c.execute('UPDATE seq SET n=n+1 WHERE channel_id=?',(ch['id'],)); ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone()['n']
   except:
    fcntl.flock(evlock,fcntl.LOCK_UN); evlock.close(); c.rollback(); raise
   c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],body,b.get('parent_id'),now(),ev));r=c.execute('SELECT * FROM messages WHERE id=last_insert_rowid()').fetchone()
   for fid in fids:c.execute('INSERT OR IGNORE INTO message_files(message_id,file_id) VALUES(?,?)',(r['id'],fid))
   c.commit()
   o0=msgobj(c,r); c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],ev,'message.reply' if r['parent_id'] else 'message.created',json.dumps({'message':o0}))); c.commit()
   mids=mention_ids(c,r)
   for mid in mids:
    c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id,last_read,mentions) VALUES(?,?,0,0)',(mid,ch['id']))
    c.execute('UPDATE reads SET mentions=mentions+1 WHERE user_id=? AND channel_id=?',(mid,ch['id']))
   c.commit(); fcntl.flock(evlock,fcntl.LOCK_UN); evlock.close(); o=msgobj(c,r);return self.send(201,{'message':o})
  if path=='/api/dms':
   other=b.get('user_id')
   if not c.execute('SELECT 1 FROM users WHERE id=?',(other,)).fetchone():return self.send(404,{})
   rows=c.execute('SELECT c.* FROM channels c JOIN cmembers a ON a.channel_id=c.id JOIN cmembers d ON d.channel_id=c.id WHERE c.is_dm=1 AND a.user_id=? AND d.user_id=?',(u['id'],other)).fetchone()
   if rows:return self.send(200,{'channel_id':rows['id']})
   c.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',('dm-'+str(u['id'])+'-'+str(other),));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,u['id'],cid,other));c.commit();return self.send(200,{'channel_id':cid})
  m=re.match(r'/api/messages/([0-9]+)/reactions$',path)
  if m:
   r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
   if not r:return self.send(404,{})
   emoji=b.get('emoji','')
   if not isinstance(emoji,str) or not emoji:return self.send(400,{})
   c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(r['id'],u['id'],emoji));c.commit()
   xs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)).fetchall()
   return self.send(200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in xs]})
  m=re.match(r'/api/messages/([0-9]+)/pin$',path)
  if m:
   r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not r:return self.send(404,{})
   c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],now()));c.commit();x=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone()
   return self.send(200,{'pin':{'message_id':r['id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}})
  m=re.match(r'/api/channels/([0-9]+)/(leave|archive|unarchive)$',path)
  if m:
   ch=channel(c,m.group(1))
   if not ch:return self.send(404,{})
   if m.group(2)=='leave':
    c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));c.commit();return self.send(200,{'channel':dict(ch, is_private=bool(ch['is_private']), is_dm=bool(ch['is_dm']), is_archived=bool(ch['is_archived']))})
   if role(c,ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
   c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if m.group(2)=='archive' else 0,ch['id']));c.commit();return self.send(200,{'channel':dict(channel(c,ch['id']))})
  m=re.match(r'/api/workspaces/([^/]+)/members/([0-9]+)$',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   nr=b.get('role'); rr=role(c,w['id'],u['id']); old=role(c,w['id'],int(m.group(2)))
   if nr not in ('owner','admin','member','guest'):return self.send(400,{})
   if nr=='owner':return self.send(400,{})
   if rr not in ('owner','admin') or int(m.group(2))==u['id'] or (old in ('owner','admin') and rr!='owner'):return self.send(403,{})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],int(m.group(2))));c.commit();return self.send(200,{'member':{'user_id':int(m.group(2)),'role':nr}})
  m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   if w['owner_id']!=u['id']:return self.send(403,{})
   target=b.get('new_owner_id'); tr=role(c,w['id'],target) if target else None
   if tr!='admin':return self.send(400,{})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target))
   c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit()
   return self.send(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   if role(c,w['id'],u['id']) not in ('owner','admin'):return self.send(403,{})
   target=b.get('invited_user_id')
   if not target and b.get('invited_username'):
    z=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone(); target=z['id'] if z else None
   code=secrets.token_urlsafe(12); exp=int(time.time())+int(b.get('expires_in_seconds',86400)); mx=1 if target else int(b.get('max_uses',1))
   c.execute('INSERT INTO invites VALUES(?,?,?,?,?,?,?)',(code,w['id'],u['id'],target,exp,mx,0));c.commit()
   return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':mx,'expires_at':exp}})
  m=re.match(r'/api/invitations/([^/]+)/accept$',path)
  if m:
   inv=c.execute('SELECT * FROM invites WHERE code=?',(m.group(1),)).fetchone()
   if not inv or inv['expires']<int(time.time()) or inv['uses']>=inv['max_uses']:return self.send(404,{})
   if inv['target_id'] and inv['target_id']!=u['id']:return self.send(403,{})
   c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'))
   c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(inv['code'],));c.commit()
   return self.send(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone())})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   if not role(c,w['id'],u['id']):return self.send(403,{})
   h=b.get('handle',''); name=b.get('name',''); ids=b.get('user_ids',[])
   if not h or not name:return self.send(400,{})
   if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():return self.send(409,{})
   c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,users) VALUES(?,?,?,?,?)',(w['id'],h,name,u['id'],json.dumps(ids)));c.commit()
   g=c.execute('SELECT * FROM groups_ WHERE id=last_insert_rowid()').fetchone()
   return self.send(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}})
  return self.send(404,{})
 def _do_PATCH(self):
  p=self.route();c=db();u=auth(c,self)
  if not u:return self.send(401,{})
  b=self.body()
  if b is None:return
  if p.path=='/api/users/me':
   allowed=('display_name','timezone','avatar_url','status_text','status_emoji')
   vals={k:b[k] for k in allowed if k in b}
   if 'timezone' in vals:
    try: ZoneInfo(str(vals['timezone']))
    except Exception:return self.send(400,{'error':'invalid_timezone'})
   if any(len(str(v))>250 for k,v in vals.items() if k!='timezone') or len(str(vals.get('display_name','')))>200:return self.send(400,{'error':'field_too_long'})
   if vals:
    c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],));c.commit()
   fresh=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); obj=userobj(fresh); broadcast_global({'type':'user.updated','user':obj})
   return self.send(200,{'user':obj})
  m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   target=int(m.group(2)); nr=b.get('role'); rr=role(c,w['id'],u['id']); old=role(c,w['id'],target)
   if nr not in ('owner','admin','member','guest'):return self.send(400,{})
   if nr=='owner':return self.send(400,{})
   if rr not in ('owner','admin') or target==u['id'] or (old in ('owner','admin') and rr!='owner'):return self.send(403,{})
   if not old:return self.send(404,{})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],target));c.commit()
   return self.send(200,{'member':{'user_id':target,'role':nr}})
  m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   target=int(m.group(2)); nr=b.get('role'); rr=role(c,w['id'],u['id']); old=role(c,w['id'],target)
   if nr not in ('owner','admin','member','guest'):return self.send(400,{})
   if nr=='owner':return self.send(400,{})
   if not old:return self.send(404,{})
   if rr not in ('owner','admin') or target==u['id'] or (old in ('owner','admin') and rr!='owner'):return self.send(403,{})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],target));c.commit()
   return self.send(200,{'member':{'user_id':target,'role':nr}})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:return self.send(404,{})
   if g['creator_id']!=u['id'] and role(c,w['id'],u['id'])!='owner':return self.send(403,{})
   c.execute('UPDATE groups_ SET name=?,users=? WHERE id=?',(b.get('name',g['name']),json.dumps(b.get('user_ids',json.loads(g['users']))),g['id']));c.commit()
   g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone()
   return self.send(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['users'])}})
  m=re.match(r'/api/workspaces/([^/]+)$',p.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   if role(c,w['id'],u['id']) not in ('owner','admin'):return self.send(403,{})
   if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.send(400,{})
   c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id']));c.commit()
   return self.send(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/channels/(\d+)$',p.path)
  if m:
   ch=channel(c,m.group(1))
   if not ch:return self.send(404,{})
   if len(b.get('topic',ch['topic']))>250:return self.send(400,{})
   if not accessible(c,ch,u['id']) and role(c,ch['workspace_id'],u['id'])!='owner':return self.send(403,{})
   c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',ch['topic']),ch['id']));c.commit();return self.send(200,{'channel':dict(channel(c,ch['id']))})
  m=re.match(r'/api/messages/(\d+)$',p.path)
  if m:
   r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not r:return self.send(404,{})
   if r['deleted']:return self.send(409,{})
   if r['author_id']!=u['id']:return self.send(403,{})
   if not isinstance(b.get('body'),str) or not b.get('body').strip():return self.send(400,{})
   c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),r['id']))
   ch=channel(c,r['channel_id']); c.execute('INSERT OR IGNORE INTO seq VALUES(?,0)',(ch['id'],)); c.execute('UPDATE seq SET n=n+1 WHERE channel_id=?',(ch['id'],)); ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone()['n']; c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],ev,'message.updated',json.dumps({'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone())}))); c.commit()
   return self.send(200,{'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone())})
  return self.send(404,{})
 def _do_DELETE(self):
  c=db();u=auth(c,self)
  if not u:return self.send(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if g and g['creator_id']!=u['id'] and role(c,w['id'],u['id'])!='owner':return self.send(403,{})
   if g:c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit()
   return self.send(200,{'deleted':True})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',self.path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.send(404,{})
   if role(c,w['id'],u['id']) not in ('owner','admin'):return self.send(403,{})
   c.execute('DELETE FROM invites WHERE code=?',(m.group(2),));c.commit();return self.send(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/pin$',self.path)
  if m:c.execute('DELETE FROM pins WHERE message_id=?',(m.group(1),));c.commit();return self.send(200,{'unpinned':True})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not r:return self.send(200,{'deleted':True})
   ch=channel(c,r['channel_id']); w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if r['author_id']!=u['id'] and w['owner_id']!=u['id']:return self.send(403,{})
   if not r['deleted']:
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],)); c.execute('INSERT OR IGNORE INTO seq VALUES(?,0)',(ch['id'],)); c.execute('UPDATE seq SET n=n+1 WHERE channel_id=?',(ch['id'],)); ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone()['n']; c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],ev,'message.deleted',json.dumps({'message_id':r['id']}))); c.commit()
   return self.send(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',self.path)
  if m:
   c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m.group(1),u['id'],urllib.parse.unquote(m.group(2))));c.commit()
   xs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(m.group(1),)).fetchall()
   return self.send(200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in xs]})
  return self.send(404,{})
 def websocket(self,c):
  t=urllib.parse.parse_qs(self.route().query).get('token',[''])[0]
  r=c.execute('SELECT user_id FROM tokens WHERE token=?',(t,)).fetchone()
  if not r:return self.send(401,{})
  key=self.headers.get('Sec-WebSocket-Key')
  acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
  sock=self.connection; sock.settimeout(.25); cid=None; last=0
  c.execute('INSERT OR REPLACE INTO presence(user_id,channel_id,last_seen) SELECT ?,channel_id,? FROM cmembers WHERE user_id=?',(r['user_id'],time.time(),r['user_id']));c.commit()
  with lock: clients.append((sock,cid,r['user_id'])); online_users.add(r['user_id'])
  def exact(k):
   z=b''
   while len(z)<k:
    q=sock.recv(k-len(z))
    if not q: raise OSError()
    z+=q
   return z
  def emit_new():
   nonlocal last
   if cid is None:return
   cc=db()
   rows=cc.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id ASC',(cid,last)).fetchall()
   for er in rows:
    ws_send(sock,{'type':er['type'],'event_id':er['event_id'],'channel_id':cid,**json.loads(er['payload'])})
    last=er['event_id']
   cc.close()
  try:
   while True:
    emit_new()
    try:
     h=exact(2)
    except socket.timeout:
     continue
    if not h:break
    n=h[1]&127
    if n==126:n=struct.unpack('>H',exact(2))[0]
    elif n==127:n=struct.unpack('>Q',exact(8))[0]
    mask=exact(4); data=exact(n); data=bytes(data[i]^mask[i%4] for i in range(n))
    if h[0]&15==9:
     ws_send(sock,json.loads(data.decode()) if data else {'type':'pong'}); continue
    x=json.loads(data)
    if x.get('type') in ('subscribe','resume'):
     cid=int(x['channel_id']); since=int(x.get('since_event_id',0))
     c.execute('INSERT OR REPLACE INTO presence(user_id,channel_id,last_seen) VALUES(?,?,?)',(r['user_id'],cid,time.time()));c.commit()
     c.execute('INSERT OR IGNORE INTO seq(channel_id,n) VALUES(?,0)',(cid,)); c.commit(); head=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone()['n']
     if x['type']=='resume':
      last=since; emit_new()
      ws_send(sock,{'type':'resumed','channel_id':cid,'head_event_id':head})
     else:
      last=head
      ws_send(sock,{'type':'subscribed','channel_id':cid,'head_event_id':head})
     with lock:
      for i,item in enumerate(clients):
       if item[0] is sock:clients[i]=(item[0],cid,item[2])
  except (socket.timeout,OSError,ConnectionError,ValueError):
   pass
  finally:
   with lock:
    removed=[item[2] for item in clients if item[0] is sock]
    clients[:]=[item for item in clients if item[0] is not sock]
    for uid in removed:
     if not any(item[2]==uid for item in clients): online_users.discard(uid)
   try:sock.close()
   except:pass
 def do_PUT(self):return self.send(404,{})
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222;background:#f5f5f5}.layout{display:flex;height:100vh}.sidebar{width:250px;background:#3f0e40;color:#fff;padding:16px}.main{flex:1;padding:20px}.thread{width:320px;background:#fff;padding:16px}.box{background:#fff;padding:16px;margin:8px 0;border-radius:5px}input,select,textarea{padding:8px;margin:4px;border:1px solid #bbb;border-radius:3px}button{padding:9px 14px;border:0;border-radius:4px;color:#fff}button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}.hidden{display:none}</style></head><body>
<div id="app"><div class="layout"><aside class="sidebar"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button id="logout-btn" data-testid="logout-btn" data-button-role="danger">Logout</button></aside>
<main class="main"><h1 data-testid="channel-title">#general</h1><div data-testid="channel-topic"></div><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div id="message-list" data-testid="message-list"></div><input id="message-input" data-testid="message-input" name="message" placeholder="Message"><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button></main>
<aside class="thread hidden" data-testid="thread-panel"><button data-testid="close-thread" data-button-role="secondary">Close</button><div data-testid="thread-input"></div><input><button data-testid="thread-send" data-button-role="primary">Reply</button></aside></div>
<div id="auth-modal"><form data-testid="auth-form"><input name="username" placeholder="Username"><input name="password" type="password" placeholder="Password"><button id="auth-submit" data-testid="auth-submit" data-button-role="primary">Sign in</button></form><button data-testid="auth-toggle">Sign up</button></div>
<form id="workspace-create-form" data-testid="workspace-create-form"><input name="workspace"><button data-testid="empty-state-create-workspace">Create workspace</button></form><form data-testid="join-workspace-form"><input name="join"><button id="join-workspace-submit" data-testid="join-workspace-submit" data-button-role="primary">Join workspace</button><span data-testid="join-workspace-error"></span></form>
<div id="workspace-settings-modal"><button data-testid="workspace-settings-close" data-button-role="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button></div>
<div id="channel-settings-modal"><button data-testid="channel-settings-close" data-button-role="secondary"></button><h3 data-testid="channel-settings-title"></h3><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>
<form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><button data-testid="create-invitation-submit" data-button-role="primary">Create invite</button></form><div data-testid="invitations-list"></div><div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div></div>
<script>document.querySelector('[data-testid=auth-form]').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(f))});let d=await r.json();if(d.token){localStorage.setItem('huddle.token',d.token);window.__huddle_token=d.token;document.querySelector('[data-testid=current-user]').textContent=d.user.username}else document.querySelector('[data-testid=auth-form]').setAttribute('data-error','Invalid credentials')}</script></body></html>'''

if __name__=='__main__':
 init(); port=int(os.getenv('PORT','8000'));ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
