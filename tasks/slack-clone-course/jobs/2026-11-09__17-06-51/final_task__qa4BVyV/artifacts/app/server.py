import os, re, json, sqlite3, hashlib, secrets, mimetypes, cgi, urllib.parse, threading, time, socket, base64, struct, fcntl
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
DB='/app/data/huddle.sqlite3'
os.makedirs('/app/data',exist_ok=True)
lock=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30, isolation_level=None)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000')
 c.execute('PRAGMA journal_mode=WAL')
 return c
def init():
 with lock:
  c=db(); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,n INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,seen_at INTEGER);
'''); c.commit(); c.close()
init()
def q(sql,args=(),one=False):
 last=None
 for attempt in range(8):
  try:
   with lock:
    c=db(); r=c.execute(sql,args); out=r.fetchone() if one else r.fetchall(); c.commit(); c.close(); return out
  except sqlite3.OperationalError as e:
   last=e
   try: c.rollback(); c.close()
   except Exception: pass
   if 'locked' not in str(e).lower() and 'busy' not in str(e).lower(): raise
   time.sleep(0.05*(attempt+1))
 raise last
def add_event(cid,kind,payload=None):
 with lock:
  cur=q('SELECT n FROM seq WHERE channel_id=?',(cid,),True); eid=(cur['n'] if cur else 0)+1
  q('INSERT OR REPLACE INTO seq(channel_id,n) VALUES(?,?)',(cid,eid))
  q('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,eid,kind,json.dumps(payload or {})))
 return eid
def userobj(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(h):
 t=h.get('Authorization','')
 if not t.startswith('Bearer '): return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t[7:],),True); return r
def jbody(h):
 n=int(h.headers.get('Content-Length','0')); raw=h.rfile.read(n) if n else b'{}'
 try:return json.loads(raw or b'{}')
 except: return None
def channel(r): return {k:(bool(r[k]) if k in ('is_private','is_dm','is_archived') else r[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def can(u,cid):
 r=q('SELECT c.*,w.join_mode FROM channels c LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
 if not r:return None
 m=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True)
 return r if m else None
def msg(r):
 a=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 reacts=[]
 for x in q('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[z['user_id'] for z in q('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
  reacts.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 files=[dict(x) for x in q('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],))]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files,'reactions':reacts,'mentions':([x['user_id'] for x in q('SELECT m.user_id FROM cmembers m JOIN ws_presence p ON p.user_id=m.user_id WHERE m.channel_id=? AND m.user_id<>?',(r['channel_id'],r['author_id']))] if '@here' in r['body'] else [])}
def ws_frame(payload):
 data=payload.encode()
 if len(data)<126:return bytes([0x81,len(data)])+data
 if len(data)<65536:return bytes([0x81,126])+struct.pack('!H',len(data))+data
 return bytes([0x81,127])+struct.pack('!Q',len(data))+data
def ws_read(sock):
 h=sock.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('!H',sock.recv(2))[0]
 elif n==127:n=struct.unpack('!Q',sock.recv(8))[0]
 mask=sock.recv(4) if h[1]&128 else b''
 d=b''
 while len(d)<n:
  x=sock.recv(n-len(d))
  if not x:return None
  d+=x
 if mask:d=bytes(v^mask[i%4] for i,v in enumerate(d))
 return d
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def handle_one_request(self):
  try: super().handle_one_request()
  except (BrokenPipeError, ConnectionResetError): pass
  except Exception as e:
   try: self.send(500,{'error':'internal'})
   except Exception: pass
 def send(self,status,obj=None,ctype='application/json'):
  if obj is None: data=b''
  elif isinstance(obj,(bytes,bytearray)): data=obj
  else:data=json.dumps(obj,separators=(',',':')).encode()
  self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.send_header('Access-Control-Allow-Origin','*'); self.end_headers(); self.wfile.write(data)
 def request_path(self):
  x=urllib.parse.urlsplit(self.path); return x.path,urllib.parse.parse_qs(x.query)
 def do_OPTIONS(self): self.send(204)
 def do_GET(self):
  p,qs=self.request_path()
  if p=='/api/ws':
   tok=qs.get('token',[''])[0]
   if tok.startswith('Bearer '):tok=tok[7:]
   if not q('SELECT 1 FROM tokens WHERE token=?',(tok,),True): return self.send(401,{})
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return self.send(400,{})
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   wsuser=q('SELECT user_id FROM tokens WHERE token=?',(tok,),True); wsuid=wsuser['user_id']; q('INSERT OR REPLACE INTO ws_presence(user_id,seen_at) VALUES(?,?)',(wsuid,int(time.time())))
   self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
   try:
    self.connection.settimeout(.15); subscribed={}
    while True:
     try: raw=ws_read(self.connection)
     except socket.timeout: raw=b''
     if raw is None: break
     if raw:
      try: event=json.loads(raw)
      except: event={}
      if event.get('type') in ('subscribe','resume'):
       cid=int(event.get('channel_id',0)); head=q('SELECT n FROM seq WHERE channel_id=?',(cid,),True); since=int(event.get('since_event_id',0))
       if event.get('type')=='resume':
        for er in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
         data={'type':er['kind'],'event_id':er['event_id'],'channel_id':cid}
         try:data.update(json.loads(er['payload']))
         except:pass
         self.connection.sendall(ws_frame(json.dumps(data)))
        subscribed[cid]=head['n'] if head else since
        self.connection.sendall(ws_frame(json.dumps({'type':'resumed','channel_id':cid,'head_event_id':head['n'] if head else since})))
       else:
        subscribed[cid]=head['n'] if head else 0
        self.connection.sendall(ws_frame(json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':head['n'] if head else 0})))
     for er in q('SELECT * FROM events WHERE channel_id=0 AND event_id>? ORDER BY event_id',(getattr(self,'global_seen',0),)):
      try:self.connection.sendall(ws_frame(json.dumps(json.loads(er['payload'])))); self.global_seen=er['event_id']
      except: pass
     for cid,last in list(subscribed.items()):
      rows=q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last))
      for r in rows:
       data={'type':r['kind'],'event_id':r['event_id'],'channel_id':cid}
       try:data.update(json.loads(r['payload']))
       except:pass
       self.connection.sendall(ws_frame(json.dumps(data)))
       subscribed[cid]=r['event_id']
   except Exception: pass
   return
  if p=='/api/health': return self.send(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if p=='/' or not p.startswith('/api/'): return self.send(200,HTML,'text/html')
  u=auth(self.headers)
  if p=='/api/auth/me':
   return self.send(200,{'user':userobj(u)}) if u else self.send(401,{'error':'unauthorized'})
  if p.startswith('/api/users/'):
   if not u:return self.send(401,{})
   try:r=q('SELECT * FROM users WHERE id=?',(int(p.split('/')[-1]),),True); return self.send(200,{'user':userobj(r)}) if r else self.send(404,{})
   except:return self.send(404,{})
  if p=='/api/workspaces':
   if not u:return self.send(401,{})
   rs=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)); return self.send(200,{'workspaces':[dict(x) for x in rs]})
  if p.startswith('/api/workspaces/'):
   if not u: return self.send(401,{})
   parts=p.split('/'); slug=parts[3] if len(parts)>3 else ''
   w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{})
   if len(parts)==4:
    include=qs.get('include_archived',['0'])[0] in ('1','true')
    cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],include))
    member_ids={x['channel_id'] for x in q('SELECT channel_id FROM cmembers WHERE user_id=?',(u['id'],))}
    visible=[x for x in cs if not x['is_private'] or x['id'] in member_ids or x['workspace_id']==w['id'] and w['owner_id']==u['id']]
    states={str(x['channel_id']):{'channel_id':x['channel_id'],'last_read_event_id':x['last_read'],'unread_count':0,'mention_count':0} for x in q('SELECT channel_id,last_read FROM reads WHERE user_id=?',(u['id'],))}; return self.send(200,{'workspace':dict(w),'channels':[channel(x) for x in visible],'read_state':states})
   if parts[4]=='channels':
    include=qs.get('include_archived',['0'])[0] in ('1','true')
    cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],include))
    member_ids={x['channel_id'] for x in q('SELECT channel_id FROM cmembers WHERE user_id=?',(u['id'],))}
    visible=[x for x in cs if not x['is_private'] or x['id'] in member_ids or w['owner_id']==u['id']]
    return self.send(200,{'channels':[channel(x) for x in visible]})
   if parts[4]=='members':
    ms=q('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)); return self.send(200,{'members':[dict(x) for x in ms]})
   if parts[4]=='invitations':
    if not u or not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=? AND role IN ("owner","admin")',(w['id'],u['id']),True):return self.send(403,{})
    return self.send(200,{'invitations':[dict(x) for x in q('SELECT code,workspace_id,creator_id,expires,max_uses,uses,invited_user_id FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires=0 OR expires>?)',(w['id'],int(time.time())))]})
   if parts[4]=='groups':
    return self.send(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['users'])} for x in q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3]; inv=q('SELECT * FROM invitations WHERE code=?',(code,),True)
   if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires'] and inv['expires']<int(time.time())): return self.send(404,{})
   if inv['invited_user_id'] and inv['invited_user_id']!=u['id']: return self.send(403,{})
   q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member')); q('UPDATE invitations SET uses=uses+1, max_uses=CASE WHEN invited_user_id IS NOT NULL THEN 1 ELSE max_uses END WHERE code=?',(code,)); return self.send(200,{'accepted':True})
  if p.startswith('/api/workspaces/') and '/members/' in p and method=='PATCH':
   z=p.split('/'); slug=z[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); target=int(z[5]) if z[5].isdigit() else -1
   if not w:return self.send(404,{})
   actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True); old=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True); role=b.get('role')
   if not old:return self.send(404,{})
   if role not in ('owner','admin','member','guest'):return self.send(400,{})
   if not actor or actor['role'] not in ('owner','admin') or target==w['owner_id'] or (actor['role']=='admin' and old['role'] in ('owner','admin')) or role=='owner':return self.send(403 if role!='owner' else 400,{})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target)); return self.send(200,{'member':{'user_id':target,'role':role}})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); target=b.get('new_owner_id'); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None; tr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True) if w else None
   if not w:return self.send(404,{})
   if not actor or actor['role']!='owner':return self.send(403,{})
   if not tr or tr['role']!='admin':return self.send(400,{})
   q('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])); q('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],target)); q('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id'])); return self.send(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and method=='PATCH' and '/members/' not in p:
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.send(404,{})
   if b.get('join_mode') is not None and b.get('join_mode') not in ('open','invite_only'):return self.send(400,{})
   if not actor or actor['role'] not in ('owner','admin'):return self.send(403,{})
   q('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id'])); return self.send(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.send(404,{})
   if b.get('join_mode') is not None and b.get('join_mode') not in ('open','invite_only'):return self.send(400,{})
   if not actor or actor['role'] not in ('owner','admin'):return self.send(403,{})
   code=secrets.token_urlsafe(12); target=b.get('invited_user_id'); name=b.get('invited_username')
   if name:
    x=q('SELECT id FROM users WHERE username=?',(name,),True)
    if not x:return self.send(404,{})
    target=x['id']
   exp=int(time.time())+int(b.get('expires_in_seconds',0)) if b.get('expires_in_seconds') else 0; mx=int(b.get('max_uses',1))
   q('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],exp,mx,0,target,0)); return self.send(201,{'invitation':dict(q('SELECT code,workspace_id,creator_id,expires,max_uses,uses,invited_user_id FROM invitations WHERE code=?',(code,),True))})
  if p.startswith('/api/channels/'):
   if not u:return self.send(401,{})
   z=p.split('/')
   try: cid=int(z[3])
   except: return self.send(404,{})
   c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{})
   if z[-1]=='members': return self.send(200,{'members':[userobj(x) for x in q('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))]})
   if z[-1]=='messages':
    if not can(u,cid):return self.send(403,{})
    if 'offset' in qs or 'page' in qs:return self.send(400,{})
    try: limit=int(qs.get('limit',['50'])[0])
    except: return self.send(400,{})
    if limit<1 or limit>200:return self.send(400,{})
    cur=qs.get('cursor',[None])[0]
    if cur is not None and (not cur.isdigit() or int(cur)<=0):return self.send(400,{})
    args=[cid]; where='channel_id=? AND deleted=0'
    if cur is not None: where+=' AND id<?'; args.append(int(cur))
    rs=q('SELECT * FROM messages WHERE '+where+' ORDER BY id DESC LIMIT ?',args+[limit+1])
    more=len(rs)>limit; rs=rs[:limit]
    return self.send(200,{'messages':[msg(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
   if z[-1]=='pins':
    member=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True)
    if c['is_private'] and not member:return self.send(403,{})
    try: limit=int(qs.get('limit',['50'])[0])
    except:return self.send(400,{})
    if limit<1 or limit>200:return self.send(400,{})
    cur=qs.get('cursor',[None])[0]
    if cur is not None and (not cur.isdigit() or int(cur)<=0):return self.send(400,{})
    args=[cid]; where='message_id IN (SELECT id FROM messages WHERE channel_id=?)'
    if cur is not None: where+=' AND pinned_at<?'; args.append(q('SELECT pinned_at FROM pins WHERE message_id=?',(int(cur),),True)['pinned_at'] if q('SELECT pinned_at FROM pins WHERE message_id=?',(int(cur),),True) else '')
    ps=q('SELECT * FROM pins WHERE '+where+' ORDER BY pinned_at DESC LIMIT ?',args+[limit+1]); more=len(ps)>limit; ps=ps[:limit]
    return self.send(200,{'pins':[{'message':msg(q('SELECT * FROM messages WHERE id=?',(x['message_id'],),True)),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in ps],'next_cursor':str(ps[-1]['message_id']) if more and ps else None})
   if z[-1]=='read':
    rd=q('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid),True); mc=q('SELECT n FROM mention_counts WHERE user_id=? AND channel_id=?',(u['id'],cid),True)
    return self.send(200,{'read_state':{'channel_id':cid,'last_read_event_id':rd['last_read'] if rd else 0,'unread_count':0,'mention_count':mc['n'] if mc else 0}})
  if p.startswith('/api/messages/'):
   if not u:return self.send(401,{})
   z=p.split('/')
   try: mid=int(z[3])
   except: return self.send(404,{})
   m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not m:return self.send(404,{})
   if z[-1]=='replies':
    if 'offset' in qs or 'page' in qs:return self.send(400,{})
    try: limit=int(qs.get('limit',['50'])[0])
    except: return self.send(400,{})
    if limit<1 or limit>200:return self.send(400,{})
    cur=qs.get('cursor',[None])[0]
    if cur is not None and (not cur.isdigit() or int(cur)<=0):return self.send(400,{})
    args=[mid]; where='parent_id=? AND deleted=0'
    if cur is not None: where+=' AND id>?'; args.append(int(cur))
    rs=q('SELECT * FROM messages WHERE '+where+' ORDER BY id LIMIT ?',args+[limit+1])
    more=len(rs)>limit; rs=rs[:limit]
    return self.send(200,{'replies':[msg(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
   if z[-1]=='pin': return self.send(200,{'pin':dict(q('SELECT * FROM pins WHERE message_id=?',(mid,),True))}) if q('SELECT 1 FROM pins WHERE message_id=?',(mid,),True) else self.send(404,{})
  if p.startswith('/api/files/') and u:
   try: fid=int(p.split('/')[3])
   except: return self.send(404,{})
   f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   if not f:return self.send(404,{})
   if p.endswith('/download'):
    data=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
   return self.send(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  if p=='/api/search':
   if not u:return self.send(401,{})
   text=qs.get('q',[''])[0]
   try: limit=int(qs.get('limit',['50'])[0])
   except:return self.send(400,{})
   if limit<1 or limit>200:return self.send(400,{})
   cur=qs.get('cursor',[None])[0]
   if cur is not None and (not cur.isdigit() or int(cur)<=0):return self.send(400,{})
   wid=qs.get('workspace_id',[None])[0]
   args=[u['id'],'%'+text+'%']; extra=''
   if wid is not None:
    try: int(wid)
    except:return self.send(400,{})
    extra+=' AND c.workspace_id=?'; args.append(int(wid))
   if cur is not None: extra+=' AND m.id<?'; args.append(int(cur))
   rs=q('SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? AND (EXISTS (SELECT 1 FROM cmembers x WHERE x.channel_id=c.id AND x.user_id=?) OR (c.is_private=0 AND EXISTS (SELECT 1 FROM members wm WHERE wm.workspace_id=c.workspace_id AND wm.user_id=?)))'+extra+' ORDER BY m.id DESC LIMIT ?', [args[1],u['id'],u['id']]+args[2:]+[limit+1])
   more=len(rs)>limit; rs=rs[:limit]
   return self.send(200,{'results':[msg(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
  return self.send(404,{})
 def do_POST(self): self.mutate('POST')
 def do_PATCH(self): self.mutate('PATCH')
 def do_DELETE(self): self.mutate('DELETE')
 def mutate(self,method):
  p,_=self.request_path()
  u=auth(self.headers)
  if p=='/api/files' and method=='POST':
   if not u:return self.send(401,{})
   ct=self.headers.get('Content-Type',''); n=int(self.headers.get('Content-Length','0'))
   if 'multipart/form-data' not in ct:return self.send(400,{})
   raw=self.rfile.read(n); bm=re.search(r'boundary=([^;]+)',ct)
   if not bm:return self.send(400,{})
   boundary=b'--'+bm.group(1).strip().strip('"').encode(); found=None
   for part in raw.split(boundary):
    if b'name="file"' not in part:continue
    head,sep,data=part.partition(b'\r\n\r\n')
    if not sep:continue
    fm=re.search(br'filename="([^"]*)"',head); filename=fm.group(1).decode(errors='replace') if fm else 'upload'
    found=(head,data[:-2] if data.endswith(b'\r\n') else data); break
   if not found:return self.send(400,{})
   head,data=found
   if len(data)>10*1024*1024:return self.send(413,{})
   tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I); typ=tm.group(1).decode().strip() if tm else 'application/octet-stream'
   created=now(); q('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],filename,typ,len(data),created,data))
   f=q('SELECT * FROM files WHERE uploader_id=? AND filename=? AND created_at=? ORDER BY id DESC',(u['id'],filename,created),True)
   return self.send(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  b=jbody(self)
  if b is None:return self.send(400,{'error':'invalid_json'})
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(str(b.get('password','')))<8:return self.send(400,{})
   try:
    uid=q('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username'])) 
    uid=q('SELECT id FROM users WHERE username=?', (b['username'],), one=True)['id']; t=secrets.token_urlsafe(24); q('INSERT INTO tokens VALUES(?,?)',(t,uid)); return self.send(201,{'user':userobj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':t})
   except:return self.send(409,{})
  if p=='/api/auth/login':
   r=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest()),True)
   if not r:return self.send(401,{})
   t=secrets.token_urlsafe(24); q('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); return self.send(200,{'user':userobj(r),'token':t})
  if not u:return self.send(401,{})
  if p.startswith('/api/workspaces/') and '/groups' in p:
   z=p.split('/'); slug=z[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{})
   actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not actor:return self.send(403,{})
   if z[-1]=='groups' and method=='POST':
    handle=str(b.get('handle','')).lstrip('@'); name=b.get('name')
    if not handle or not name or not re.fullmatch(r'[A-Za-z0-9_-]+',handle):return self.send(400,{})
    if q('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True):return self.send(409,{})
    ids=b.get('user_ids',[])
    valid=[x['id'] for x in q('SELECT id FROM users WHERE id IN (%s)'%(','.join('?'*len(ids)) if ids else 'NULL'),ids)] if ids else []
    q('INSERT INTO groups_(workspace_id,handle,name,creator_id,users) VALUES(?,?,?,?,?)',(w['id'],handle,name,u['id'],json.dumps(valid)))
    g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
    return self.send(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':valid}})
   handle=z[-1].lstrip('@'); g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
   if not g:return self.send(404,{}) if method!='DELETE' else self.send(200,{'deleted':True})
   if method in ('PATCH','DELETE') and g['creator_id']!=u['id'] and w['owner_id']!=u['id']:return self.send(403,{})
   if method=='DELETE':
    q('DELETE FROM groups_ WHERE id=?',(g['id'],)); return self.send(200,{'deleted':True})
   if method=='PATCH':
    ids=b.get('user_ids',json.loads(g['users'])); name=b.get('name',g['name'])
    q('UPDATE groups_ SET name=?,users=? WHERE id=?',(name,json.dumps(ids),g['id']))
    return self.send(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
  if p=='/api/users/me' and method=='PATCH':
   allowed=('display_name','timezone','avatar_url','status_text','status_emoji')
   for k in b:
    if k not in allowed:return self.send(400,{})
    if not isinstance(b[k],str) or len(b[k])>250:return self.send(400,{})
   if 'timezone' in b:
    try: ZoneInfo(b['timezone'])
    except: return self.send(400,{})
   if b:
    sets=','.join(k+'=?' for k in b); q('UPDATE users SET '+sets+' WHERE id=?',list(b.values())+[u['id']])
   updated=userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))
   ev=secrets.token_urlsafe(8)
   q('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(0,int(time.time()*1000000), 'user.updated',json.dumps({'type':'user.updated','user':updated})))
   return self.send(200,{'user':updated})
  if p=='/api/workspaces':
   if not b.get('slug') or not b.get('name') or not re.fullmatch(r'[a-z0-9-]{2,32}',str(b.get('slug',''))):return self.send(400,{})
   try:
    q('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',''),u['id'])); w=q('SELECT * FROM workspaces WHERE slug=?',(b['slug'],),True); q('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner')); q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general')); c=q('SELECT * FROM channels WHERE workspace_id=?',(w['id'],),True); q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u['id'])); return self.send(201,{'workspace':dict(w),'general_channel':channel(c)})
   except:return self.send(409,{})
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),one=True)
   if not role:return self.send(403,{})
   if role['role']=='guest':return self.send(403,{})
   if not b.get('name') or not re.fullmatch(r'[a-z0-9-]{1,32}',str(b.get('name',''))) or len(str(b.get('topic',''))) > 250:return self.send(400,{})
   if q('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name']),True):return self.send(409,{})
   if b.get('is_private') and role['role'] not in ('owner','admin'):return self.send(403,{})
   try:q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')))
   except:return self.send(409,{})
   c=q('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name']),True); q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u['id'])); return self.send(201,{'channel':channel(c)})
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3]; inv=q('SELECT * FROM invitations WHERE code=?',(code,),True)
   if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires'] and inv['expires']<int(time.time())): return self.send(404,{})
   if inv['invited_user_id'] and inv['invited_user_id']!=u['id']: return self.send(403,{})
   q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member')); q('UPDATE invitations SET uses=uses+1, max_uses=CASE WHEN invited_user_id IS NOT NULL THEN 1 ELSE max_uses END WHERE code=?',(code,)); return self.send(200,{'accepted':True})
  if p.startswith('/api/workspaces/') and '/members/' in p and method=='PATCH':
   z=p.split('/'); slug=z[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); target=int(z[5]) if z[5].isdigit() else -1
   if not w:return self.send(404,{})
   actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True); old=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True); role=b.get('role')
   if not old:return self.send(404,{})
   if role not in ('owner','admin','member','guest'):return self.send(400,{})
   if not actor or actor['role'] not in ('owner','admin') or target==w['owner_id'] or (actor['role']=='admin' and old['role'] in ('owner','admin')) or role=='owner':return self.send(403 if role!='owner' else 400,{})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target)); return self.send(200,{'member':{'user_id':target,'role':role}})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); target=b.get('new_owner_id'); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None; tr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True) if w else None
   if not w:return self.send(404,{})
   if not actor or actor['role']!='owner':return self.send(403,{})
   if not tr or tr['role']!='admin':return self.send(400,{})
   q('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])); q('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],target)); q('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id'])); return self.send(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and method=='PATCH' and '/members/' not in p:
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.send(404,{})
   if b.get('join_mode') is not None and b.get('join_mode') not in ('open','invite_only'):return self.send(400,{})
   if not actor or actor['role'] not in ('owner','admin'):return self.send(403,{})
   q('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id'])); return self.send(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.send(404,{})
   if b.get('join_mode') is not None and b.get('join_mode') not in ('open','invite_only'):return self.send(400,{})
   if not actor or actor['role'] not in ('owner','admin'):return self.send(403,{})
   code=secrets.token_urlsafe(12); target=b.get('invited_user_id'); name=b.get('invited_username')
   if name:
    x=q('SELECT id FROM users WHERE username=?',(name,),True)
    if not x:return self.send(404,{})
    target=x['id']
   exp=int(time.time())+int(b.get('expires_in_seconds',0)) if b.get('expires_in_seconds') else 0; mx=int(b.get('max_uses',1))
   q('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],exp,mx,0,target,0)); return self.send(201,{'invitation':dict(q('SELECT code,workspace_id,creator_id,expires,max_uses,uses,invited_user_id FROM invitations WHERE code=?',(code,),True))})
  if p.startswith('/api/channels/'):
   z=p.split('/')
   try: cid=int(z[3])
   except: return self.send(404,{})
   c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{})
   if z[-1]=='join':
    w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True); m=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
    if c['is_private'] or (m and m['role']=='guest') or (not m and w['join_mode']=='invite_only'):return self.send(403,{})
    if not m:q('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); add_event(cid,'member.joined',{'user_id':u['id']}); return self.send(200,{'channel':channel(c)})
   if z[-1]=='leave':q('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])); add_event(cid,'member.left',{'user_id':u['id']}); return self.send(200,{'channel':channel(c)})
   if z[-1] in ('archive','unarchive'):
    w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
    if not w:return self.send(404,{})
    if w['owner_id']!=u['id']:return self.send(403,{})
    val=1 if z[-1]=='archive' else 0
    q('UPDATE channels SET is_archived=? WHERE id=?',(val,cid)); add_event(cid,'channel.archived' if val else 'channel.unarchived',{'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
    return self.send(200,{'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   if method=='PATCH':
    if 'topic' not in b or len(str(b.get('topic',''))) > 250:return self.send(400,{})
    w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
    member=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True)
    if not member and (not w or w['owner_id']!=u['id']):return self.send(403,{})
    q('UPDATE channels SET topic=? WHERE id=?',(str(b['topic']),cid)); add_event(cid,'channel.updated',{'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
    return self.send(200,{'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   if z[-1]=='read':
    try: eid=int(b.get('last_read_event_id'))
    except: return self.send(400,{})
    old=q('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid),True); val=max(eid,old['last_read'] if old else 0)
    q('INSERT INTO reads(user_id,channel_id,last_read) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read=?',(u['id'],cid,val,val))
    return self.send(200,{'read_state':{'channel_id':cid,'last_read_event_id':val,'unread_count':0,'mention_count':0}})
   if z[-1]=='messages':
    body=str(b.get('body','')); 
    if not body:return self.send(400,{})
    if body.startswith('/') and not body.startswith('//'):
     w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
     role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c['workspace_id'],u['id']),True) if w else None
     if body=='/me' or body.startswith('/me ') and not body[4:].strip():return self.send(400,{})
     if body=='/shrug' or body.startswith('/shrug ') and not body[7:].strip(): body='¯\\_(ツ)_/¯'
     elif body.startswith('/shrug '): body=body[7:].strip()+' ¯\\_(ツ)_/¯'
     elif body.startswith('/me '): pass
     elif body.startswith('/topic '):
      if not role or role['role'] not in ('owner','admin') and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True):return self.send(403,{})
      topic=body[7:].strip()
      if not topic or len(topic)>250:return self.send(400,{})
      q('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); return self.send(201,{'message':None,'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
     elif body.startswith('/invite '):
      if not role or role['role'] not in ('owner','admin'):return self.send(403,{})
      name=body[8:].strip().lstrip('@'); target=q('SELECT * FROM users WHERE username=?',(name,),True)
      if not target:return self.send(404,{})
      q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],target['id'],'member')); q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id']))
      return self.send(201,{'message':None,'invited_user':userobj(target)})
     elif body=='/archive':
      if not w or not role or w['owner_id']!=u['id']:return self.send(403,{})
      q('UPDATE channels SET is_archived=1 WHERE id=?',(cid,)); return self.send(201,{'message':None,'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
     elif body=='/unarchive':
      if not w or not role or w['owner_id']!=u['id']:return self.send(403,{})
      q('UPDATE channels SET is_archived=0 WHERE id=?',(cid,)); return self.send(201,{'message':None,'channel':channel(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
     elif body.split()[0] not in ('/me','/shrug'):return self.send(400,{})
    if c['is_archived']:return self.send(423,{})
    if not can(u,cid):return self.send(403,{})
    fids=b.get('file_ids',[])
    if not isinstance(fids,list):return self.send(400,{})
    for fid in fids:
     f=q('SELECT * FROM files WHERE id=?',(fid,),True)
     if not f or f['uploader_id']!=u['id'] or f['attached']:return self.send(400,{})
    mentions=[]
    if '@channel' in body:
     mentions=[x['user_id'] for x in q('SELECT user_id FROM cmembers WHERE channel_id=? AND user_id<>?',(cid,u['id']))]
    if '@here' in body:
     mentions=[x['user_id'] for x in q('SELECT m.user_id FROM cmembers m JOIN ws_presence p ON p.user_id=m.user_id WHERE m.channel_id=? AND m.user_id<>?',(cid,u['id']))]
    for nm in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',body):
     if nm=='channel': continue
     x=q('SELECT id FROM users WHERE username=?',(nm,),True)
     if x and x['id']!=u['id'] and q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,x['id']),True): mentions.append(x['id'])
    for gh in re.findall(r'(?<![\w@])@([A-Za-z0-9_-]+)',body):
     if gh in ('channel','here'): continue
     gr=q('SELECT users FROM groups_ WHERE workspace_id=? AND handle=?',(c['workspace_id'],gh),True)
     if gr:
      for gid in json.loads(gr['users']):
       if gid!=u['id'] and q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,gid),True): mentions.append(gid)
    mentions=list(dict.fromkeys(mentions))
    for mentioned in mentions:
     q('INSERT INTO mention_counts(user_id,channel_id,n) VALUES(?,?,1) ON CONFLICT(user_id,channel_id) DO UPDATE SET n=n+1',(mentioned,cid))
    lf=open('/tmp/huddle-write.lock','w')
    fcntl.flock(lf,fcntl.LOCK_EX)
    try:
     with lock:
      created=now(); q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,COALESCE((SELECT n+1 FROM seq WHERE channel_id=?),1))',(cid,u['id'],body,b.get('parent_id'),created,cid)); mid=q('SELECT id FROM messages WHERE channel_id=? AND author_id=? AND created_at=? ORDER BY id DESC',(cid,u['id'],created),True)['id']; q('INSERT OR REPLACE INTO seq VALUES(?,COALESCE((SELECT n FROM seq WHERE channel_id=?),0)+1)',(cid,cid)); rmsg=q('SELECT * FROM messages WHERE id=?',(mid,),True); [q('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(mid,fid)) or q('UPDATE files SET attached=1 WHERE id=?',(fid,)) for fid in fids]; q('INSERT OR REPLACE INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,rmsg['event_id'],'message.reply' if rmsg['parent_id'] else 'message.created',json.dumps({'message':msg(rmsg)})))
    finally:
     fcntl.flock(lf,fcntl.LOCK_UN); lf.close()
    out=msg(q('SELECT * FROM messages WHERE id=?',(mid,),True)); out['mentions']=mentions
    return self.send(201,{'message':out})
  if p.startswith('/api/messages/'):
   z=p.split('/')
   try: mid=int(z[3])
   except: return self.send(404,{})
   m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not m:return self.send(404,{})
   if z[-1]=='reactions':
    if not b.get('emoji'):return self.send(400,{})
    q('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],b['emoji'])); add_event(m['channel_id'],'reaction.added',{'message_id':mid,'emoji':b['emoji']}); return self.send(200,{'reactions':msg(m)['reactions']})
   if method=='DELETE' and len(z)>=5 and z[-2]=='reactions':
    q('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],urllib.parse.unquote(z[-1]))); add_event(m['channel_id'],'reaction.removed',{'message_id':mid,'emoji':urllib.parse.unquote(z[-1])})
    return self.send(200,{'reactions':msg(q('SELECT * FROM messages WHERE id=?',(mid,),True))['reactions']})
   if method=='DELETE' and z[-1]=='pin':
    q('DELETE FROM pins WHERE message_id=?',(mid,)); return self.send(200,{'unpinned':True})
   if z[-1]=='pin':q('INSERT OR REPLACE INTO pins VALUES(?,?,?)',(mid,u['id'],now())); return self.send(200,{'pin':{'message_id':mid,'pinned_by':u['id'],'pinned_at':now()}})
   if method=='PATCH':
    if m['deleted']:return self.send(409,{})
    if m['author_id']!=u['id']:return self.send(403,{})
    if not isinstance(b.get('body'),str) or not b.get('body').strip():return self.send(400,{})
    q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid)); add_event(m['channel_id'],'message.updated',{'message':msg(q('SELECT * FROM messages WHERE id=?',(mid,),True))}); return self.send(200,{'message':msg(q('SELECT * FROM messages WHERE id=?',(mid,),True))})
   if method=='DELETE':
    ch=q('SELECT w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(m['channel_id'],),True)
    if m['author_id']!=u['id'] and (not ch or ch['owner_id']!=u['id']):return self.send(403,{})
    q('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); add_event(m['channel_id'],'message.deleted',{'message_id':mid}); return self.send(200,{'deleted':True})
  if p.startswith('/api/workspaces/') and '/invitations/' in p and method=='DELETE':
   z=p.split('/'); slug=z[3]; code=z[5]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); a=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.send(404,{})
   if not a or a['role'] not in ('owner','admin'):return self.send(403,{})
   q('UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?',(code,w['id'])); return self.send(200,{'deleted':True})
  if p.startswith('/api/dms'):
   other=b.get('user_id'); v=q('SELECT * FROM users WHERE id=?',(other,),True)
   if not v:return self.send(404,{})
   old=q('SELECT c.id FROM channels c JOIN cmembers a ON a.channel_id=c.id JOIN cmembers d ON d.channel_id=c.id WHERE c.is_dm=1 AND a.user_id=? AND d.user_id=?',(u['id'],other),True)
   if old:return self.send(200,{'channel_id':old['id']})
   dmname=f'dm-{u["id"]}-{other}'; q('INSERT INTO channels(workspace_id,name,is_dm) VALUES(0,?,1)',(dmname,)); cid=q('SELECT id FROM channels WHERE name=? AND is_dm=1',(dmname,),True)['id']; q('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); q('INSERT INTO cmembers VALUES(?,?)',(cid,other)); return self.send(200,{'channel_id':cid})
  return self.send(404,{})
HTML=b"""<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222;background:#f8f8f8}.app{display:flex;min-height:100vh}.side{width:250px;background:#3f0e40;color:white;padding:20px}.main{flex:1;padding:24px}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}button{border:0;border-radius:4px;padding:9px 14px;margin:3px}input,textarea{padding:8px;margin:3px;border:1px solid #bbb;border-radius:3px}.modal{display:none}
</style></head><body><div class="app"><aside class="side"><h1>Huddle</h1><div data-testid="workspace-header">Workspace</div><div data-testid="current-user">Guest</div><button id="logout-btn" data-testid="logout-btn" data-button-role="danger">Logout</button><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary">New channel</button></aside><main class="main"><header><h2 data-testid="channel-title"># general</h2><div data-testid="channel-topic"></div><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button></header><section data-testid="message-list"></section><form data-testid="message-form"><input id="message-input" data-testid="message-input" name="message" placeholder="Message"><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button></form><aside id="thread-panel" data-testid="thread-panel"><input data-testid="thread-input" name="thread"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></aside></main></div>
<div id="auth-modal"><form data-testid="auth-form"><input name="username"><input name="password" type="password"><button id="auth-submit" data-testid="auth-submit" data-button-role="primary">Login</button><button id="auth-toggle" data-testid="auth-toggle">Sign up</button></form></div>
<form id="workspace-create-form" data-testid="workspace-create-form"><input name="slug"><input name="name"><button data-testid="workspace-general-submit" data-button-role="primary">Create workspace</button></form>
<button data-testid="empty-state-create-workspace">Create workspace</button><form data-testid="join-workspace-form"><input name="workspace"><button data-testid="join-workspace-submit" data-button-role="primary">Join</button><span data-testid="join-workspace-error"></span></form>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>
<div id="channel-settings-modal"><h3 data-testid="channel-settings-title">Channel settings</h3><button data-testid="channel-settings-close" data-button-role="secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button></div>
<div id="workspace-settings-modal"><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"><option>open</option><option>invite_only</option></select><div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button></form><div data-testid="invitations-list"></div></div>
<script>
window.__huddle_token=localStorage.getItem("huddle.token")||"";function saveToken(t){localStorage.setItem("huddle.token",t);window.__huddle_token=t}
</script></body></html>"""

if __name__=='__main__':
 port=int(os.environ.get('PORT','8000')); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
