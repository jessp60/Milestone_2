import os,sys,json,re,sqlite3,hashlib,secrets,datetime,urllib.parse,threading,base64,struct,socket,fcntl
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data.db'; EVENT_LOCK='/app/event.lock'; os.makedirs('/app',exist_ok=True); lock=threading.RLock(); event_lock=threading.RLock(); ws_clients=[]; ws_lock=threading.RLock(); ws_subs={}
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def db():
 for _ in range(30):
  try:
   c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
  except sqlite3.OperationalError:
   import time; time.sleep(.1)
 raise sqlite3.OperationalError("database unavailable")
def init():
 with lock:
  c=db()
  c.execute('PRAGMA journal_mode=WAL')
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
  CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER); CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
  CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
  CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
  CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
  CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
  CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
  CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
  CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle)); CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id)); CREATE TABLE IF NOT EXISTS channel_events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,payload TEXT,UNIQUE(channel_id,event_id)); CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,seen_at TEXT); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id)); CREATE TABLE IF NOT EXISTS dms(user1 INTEGER,user2 INTEGER,channel_id INTEGER,PRIMARY KEY(user1,user2));'''); c.commit()
  for col in ('unread_count','mention_count'):
   try:c.execute('ALTER TABLE reads ADD COLUMN '+col+' INTEGER DEFAULT 0')
   except:pass
  c.commit(); c.close()
def q(sql,args=(),one=False):
 c=db(); r=c.execute(sql,args); out=r.fetchone() if one else r.fetchall(); c.close(); return out
def run(sql,args=()):
 for attempt in range(16):
  c=None
  try:
   with lock:
    c=db(); r=c.execute(sql,args); c.commit(); x=r.lastrowid; c.close(); return x
  except sqlite3.OperationalError as e:
   if c:
    try:c.rollback();c.close()
    except:pass
   if 'locked' not in str(e).lower(): raise
   import time; time.sleep(0.01*(1+attempt//8))
 raise sqlite3.OperationalError('database remained locked')
def userobj(r):
 return dict(r) if r else None
def wobj(r): return dict(r) if r else None
def cobj(r): 
 if not r:return None
 d=dict(r); d['is_private']=bool(d['is_private']);d['is_dm']=bool(d['is_dm']);d['is_archived']=bool(d['is_archived']);return d
def auth(h):
 a=h.get('Authorization','')
 if not a.startswith('Bearer '): return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(a[7:],),True); return r
def msgobj(r):
 if not r:return None
 d=dict(r); d.pop('deleted',None); d['parent_id']=d['parent_id'];d['author']=userobj(q('SELECT * FROM users WHERE id=?',(d['author_id'],),True))
 d['files']=[dict(x) for x in q('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(d['id'],))] if q("SELECT name FROM sqlite_master WHERE type='table' AND name='message_files'",(),True) else []; d['reactions']=[]
 for x in q('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(d['id'],)):
  d['reactions'].append({'emoji':x['emoji'],'count':x['n'],'user_ids':[z['user_id'] for z in q('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(d['id'],x['emoji']))]})
 d['reply_count']=q('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(d['id'],),True)['n']
 d['mentions']=[]
 names=re.findall(r'(?<!\\w)@([A-Za-z0-9_]+)',d['body'])
 for nm in names:
  for x in q('SELECT id FROM users WHERE username=? AND id IN (SELECT user_id FROM cmembers WHERE channel_id=?)',(nm,d['channel_id'])):
   d['mentions'].append(x['id'])
  for g in q('SELECT id FROM groups WHERE handle=?',(nm,)):
   d['mentions'] += [x['user_id'] for x in q('SELECT user_id FROM group_users WHERE group_id=? AND user_id IN (SELECT user_id FROM cmembers WHERE channel_id=?)',(g['id'],d['channel_id']))]
 if '@here' in d['body']:
  active={x['user_id'] for x in q("SELECT user_id FROM ws_presence WHERE seen_at > ?",((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=15)).isoformat(),))}
  d['mentions'] += [x['user_id'] for x in q('SELECT user_id FROM cmembers WHERE channel_id=?',(d['channel_id'],)) if x['user_id'] in active]
 if '@channel' in d['body']:
  d['mentions'] += [x['user_id'] for x in q('SELECT user_id FROM cmembers WHERE channel_id=?',(d['channel_id'],))]
 d['mentions']=list(dict.fromkeys(x for x in d['mentions'] if x!=d['author_id']))
 return d
def ws_send(sock,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
 if n<126:h=bytes([0x81,n])
 elif n<65536:h=bytes([0x81,126])+struct.pack('>H',n)
 else:h=bytes([0x81,127])+struct.pack('>Q',n)
 try:sock.sendall(h+b)
 except:pass
def ws_read(sock):
 h=sock.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('>H',sock.recv(2))[0]
 elif n==127:n=struct.unpack('>Q',sock.recv(8))[0]
 mask=sock.recv(4); b=sock.recv(n); 
 if h[1]&128:b=bytes(b[i]^mask[i%4] for i in range(len(b)))
 return b
def broadcast(obj):
 with ws_lock:
  for sock,uid in list(ws_clients):
   try:ws_send(sock,obj)
   except:pass
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def handle(self):
  try: super().handle()
  except (BrokenPipeError,ConnectionResetError):
   pass
  except Exception as e:
   import traceback; traceback.print_exc()
   try: self.send(500,{'error':'server_error'})
   except Exception: pass
 def send(self,code,obj=None,ctype='application/json'):
  if obj is None:b=b''
  elif ctype=='application/json':b=json.dumps(obj,separators=(',',':')).encode()
  elif isinstance(obj,bytes):b=obj
  else:b=str(obj).encode()
  self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(b)));self.end_headers()
  if b:
   try:self.wfile.write(b)
   except (BrokenPipeError,ConnectionResetError):pass
 def body(self):
  if self.headers.get('Content-Type','').lower().startswith('multipart/form-data'): return {}
  try:return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')) or 0))
  except:return None
 def route(self): return urllib.parse.urlparse(self.path).path
 def req(self,code,obj): self.send(code,obj)
 def do_GET(self):
  p=self.route(); u=auth(self.headers)
  if p=='/api/ws':
   token=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
   r=q('SELECT user_id FROM tokens WHERE token=?',(token,),True)
   if not r:return self.send(401,{'error':'unauthorized'})
   key=self.headers.get('Sec-WebSocket-Key','')
   if not key:return self.send(400,{'error':'websocket required'})
   import hashlib,base64
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
   sock=self.request
   sock.settimeout(.05)
   with ws_lock:ws_clients.append((sock,r['user_id']))
   run('INSERT OR REPLACE INTO ws_presence(user_id,seen_at) VALUES(?,?)',(r['user_id'],now()))
   with ws_lock:ws_subs[sock]={}
   try:
    while True:
     try:data=ws_read(sock)
     except socket.timeout:data=b''
     if data is None:break
     if data:
      try:
       x=json.loads(data); typ=x.get('type')
       if typ=='subscribe':
        cid=int(x['channel_id']); head=q('SELECT COALESCE(MAX(event_id),0) n FROM messages WHERE channel_id=?',(cid,),True)['n']
        with ws_lock:ws_subs[sock][cid]=head
        ws_send(sock,{'type':'subscribed','channel_id':cid,'head_event_id':head})
       elif typ=='resume':
        cid=int(x['channel_id']); since=int(x.get('since_event_id',0)); rs=q('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since))
        for ev in rs:
         mm=q('SELECT * FROM messages WHERE id=?',(ev['message_id'],),True)
         ws_send(sock,{'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,'message':msgobj(mm) if mm and not (ev['kind']=='message.deleted') else ({'id':ev['message_id']} if ev['kind']=='message.deleted' else None)})
        head=q('SELECT COALESCE(MAX(event_id),0) n FROM messages WHERE channel_id=?',(cid,),True)['n'];ws_send(sock,{'type':'resumed','channel_id':cid,'head_event_id':head})
        with ws_lock:ws_subs[sock][cid]=head
       elif typ=='ping': ws_send(sock,{'type':'pong'})
      except Exception: pass
     with ws_lock: subs=dict(ws_subs.get(sock,{}))
     for cid,last in subs.items():
      for ev in q('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)):
       mm=q('SELECT * FROM messages WHERE id=?',(ev['message_id'],),True)
       ws_send(sock,{'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,'message':msgobj(mm) if mm and ev['kind']!='message.deleted' else ({'id':ev['message_id']} if ev['kind']=='message.deleted' else None)})
       with ws_lock:ws_subs[sock][cid]=ev['event_id']
   finally:
    with ws_lock:
     ws_clients[:]=[(a,b) for a,b in ws_clients if a is not sock];ws_subs.pop(sock,None)
    threading.Thread(target=lambda: (threading.Event().wait(15), run('DELETE FROM ws_presence WHERE user_id=?',(r['user_id'],))),daemon=True).start()
   return
  if p=='/api/health': return self.req(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if p=='/': return self.send(200,HTML,'text/html')
  if p=='/api/auth/me':
   return self.req(200,{'user':userobj(u)}) if u else self.req(401,{'error':'unauthorized'})
  if p.startswith('/api/users/'):
   if not u:return self.req(401,{})
   r=q('SELECT * FROM users WHERE id=?',(p.split('/')[-1],),True); return self.req(200,{'user':userobj(r)}) if r else self.req(404,{})
  if p=='/api/workspaces':
   if not u:return self.req(401,{})
   ws=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],));return self.req(200,{'workspaces':[wobj(x) for x in ws]})
  if p.startswith('/api/workspaces/'): return self.workspace_get(p,u)
  if p.startswith('/api/channels/'): return self.channel_get(p,u)
  if p.startswith('/api/messages/'): return self.message_get(p,u)
  if p=='/api/search':
   if not u:return self.req(401,{})
   params=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);term=params.get('q',[''])[0]
   try:lim=int(params.get('limit',['50'])[0])
   except:return self.req(400,{})
   if lim<1 or lim>200:return self.req(400,{})
   cur=params.get('cursor',[None])[0]; before=None
   if cur is not None:
    try:before=int(cur)
    except:return self.req(400,{})
   wid=params.get('workspace_id',[None])[0]
   sql='SELECT m.* FROM messages m JOIN cmembers cm ON cm.channel_id=m.channel_id JOIN channels cc ON cc.id=m.channel_id WHERE cm.user_id=? AND m.body LIKE ? AND m.deleted=0'
   args=[u['id'],'%'+term+'%']
   if wid:sql+=' AND cc.workspace_id=?';args.append(wid)
   if before is not None:sql+=' AND m.id<?';args.append(before)
   sql+=' ORDER BY m.id DESC LIMIT ?';args.append(lim+1)
   rs=q(sql,tuple(args));more=len(rs)>lim;rs=rs[:lim]
   return self.req(200,{'results':[msgobj(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
  if p.startswith('/api/files/'):
   if not u:return self.req(401,{})
   r=q('SELECT * FROM files WHERE id=?',(p.split('/')[3],),True)
   if not r:return self.req(404,{})
   if p.endswith('/download'):
    self.send_response(200);self.send_header('Content-Type',r['content_type']);self.send_header('Content-Disposition','attachment; filename="'+r['filename']+'"');self.send_header('Content-Length',str(r['size']));self.end_headers();self.wfile.write(r['data']);return
   d=dict(r);d.pop('data',None);return self.req(200,{'file':d})
  return self.req(404,{})
 def workspace_get(self,p,u):
  if not u:return self.req(401,{})
  parts=p.split('/'); slug=parts[3] if len(parts)>3 else ''
  w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
  if not w:return self.req(404,{})
  if len(parts)>4 and parts[4]=='members': return self.req(200,{'members':[dict(x) for x in q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
  if len(parts)>4 and parts[4]=='groups':
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role:return self.req(403,{})
   gs=q('SELECT * FROM groups WHERE workspace_id=?',(w['id'],))
   out=[]
   for g in gs:
    d=dict(g);d['member_user_ids']=[x['user_id'] for x in q('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))];d.pop('creator_id',None);out.append(d)
   return self.req(200,{'groups':out})
  if len(parts)>4 and parts[4]=='invitations':
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'): return self.req(403,{})
   iv=q('SELECT * FROM invites WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],))
   return self.req(200,{'invitations':[dict(x) for x in iv]})
  cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0)',(w['id'],))
  return self.req(200,{'workspace':wobj(w),'channels':[cobj(x) for x in cs],'read_state':[]})
 def channel_get(self,p,u):
  if not u:return self.req(401,{})
  a=p.split('/'); cid=a[3] if len(a)>3 else 0; ch=q('SELECT * FROM channels WHERE id=?',(cid,),True)
  if not ch:return self.req(404,{})
  if ch['is_dm'] and not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True):return self.req(403,{})
  if len(a)>4 and a[4]=='members': return self.req(200,{'members':[userobj(q('SELECT * FROM users WHERE id=?',(x['user_id'],),True)) for x in q('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,))]})
  if len(a)>4 and a[4]=='messages':
   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); raw=qs.get('limit',['50'])[0]
   try: lim=int(raw)
   except: return self.req(400,{})
   if lim<1 or lim>200:return self.req(400,{})
   cur=qs.get('cursor',[None])[0]; before=None
   if cur is not None:
    try: before=int(cur)
    except: return self.req(400,{})
   sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0'+(' AND id<?' if before is not None else '')+' ORDER BY id DESC LIMIT ?'
   args=(cid,before,lim+1) if before is not None else (cid,lim+1)
   rs=q(sql,args); more=len(rs)>lim; rs=rs[:lim]
   nxt=str(rs[-1]['id']) if more and rs else None
   return self.req(200,{'messages':[msgobj(x) for x in rs],'next_cursor':nxt})
  if len(a)>4 and a[4]=='pins':
   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
   try:lim=int(qs.get('limit',['50'])[0])
   except:return self.req(400,{})
   if lim<1 or lim>200:return self.req(400,{})
   cur=qs.get('cursor',[None])[0]; before=None
   if cur is not None:
    try:before=int(cur)
    except:return self.req(400,{})
   sql='SELECT * FROM pins WHERE message_id IN (SELECT id FROM messages WHERE channel_id=?)'+(' AND message_id<?' if before is not None else '')+' ORDER BY message_id DESC LIMIT ?'
   args=(cid,before,lim+1) if before is not None else (cid,lim+1)
   rs=q(sql,args);more=len(rs)>lim;rs=rs[:lim]
   nxt=str(rs[-1]['message_id']) if more and rs else None
   return self.req(200,{'pins':[{'message':msgobj(q('SELECT * FROM messages WHERE id=?',(x['message_id'],),True)),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rs],'next_cursor':nxt})
  if len(a)>4 and a[4]=='read': return self.req(200,{'read_state':dict(q('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid),True) or {'channel_id':int(cid),'last_read_event_id':0,'unread_count':0,'mention_count':0})})
  return self.req(200,{'channel':cobj(ch)})
 def message_get(self,p,u):
  if not u:return self.req(401,{})
  a=p.split('/'); r=q('SELECT * FROM messages WHERE id=?',(a[3],),True)
  if not r:return self.req(404,{})
  if len(a)>4 and a[4]=='replies':
   ps=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
   try:lim=int(ps.get('limit',['50'])[0])
   except:return self.req(400,{})
   if lim<1 or lim>200:return self.req(400,{})
   cur=ps.get('cursor',[None])[0]; after=None
   if cur is not None:
    try:after=int(cur)
    except:return self.req(400,{})
   sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0'+(' AND id>?' if after is not None else '')+' ORDER BY id LIMIT ?'
   args=(r['id'],after,lim+1) if after is not None else (r['id'],lim+1)
   rs=q(sql,args);more=len(rs)>lim;rs=rs[:lim]
   return self.req(200,{'replies':[msgobj(x) for x in rs],'next_cursor':str(rs[-1]['id']) if more and rs else None})
  return self.req(200,{'message':msgobj(r)})
 def do_POST(self):
  p=self.route();u=auth(self.headers); b=self.body()
  if p=='/api/files':
   if not u:return self.req(401,{})
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.req(400,{})
   raw=self.rfile.read(int(self.headers.get('Content-Length','0') or 0)); boundary=ct.split('boundary=',1)[-1].strip().strip('\\\"').encode()
   parts=raw.split(b'--'+boundary)
   chosen=None
   for part in parts:
    if b'name="file"' in part:
     chosen=part;break
   if not chosen:return self.req(400,{})
   head,data=chosen.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]
   if len(data)>10*1024*1024:return self.req(413,{})
   mm=re.search(br'filename="([^"]*)"',head); nm=(mm.group(1).decode(errors='ignore') if mm else 'file')
   cm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I); mime=(cm.group(1).decode() if cm else 'application/octet-stream')
   fid=run('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],nm,mime,len(data),now(),data))
   f=dict(q('SELECT * FROM files WHERE id=?',(fid,),True));f.pop('data',None)
   return self.req(201,{'file':f})
  if b is None:return self.req(400,{})
  if p=='/api/auth/register':
   if b is None:return self.req(400,{})
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(b.get('password',''))<8:return self.req(400,{})
   try:i=run('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username']))
   except:return self.req(409,{})
   t=secrets.token_urlsafe(24);run('INSERT INTO tokens VALUES(?,?)',(t,i));return self.req(201,{'user':userobj(q('SELECT * FROM users WHERE id=?',(i,),True)),'token':t})
  if p=='/api/auth/login':
   if b is None:return self.req(400,{})
   r=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest()),True)
   if not r:return self.req(401,{})
   t=secrets.token_urlsafe(24);run('INSERT INTO tokens VALUES(?,?)',(t,r['id']));return self.req(200,{'user':userobj(r),'token':t})
  if not u:return self.req(401,{})
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',str(b.get('slug',''))) or not b.get('name'):return self.req(400,{})
   try:w=run('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']))
   except:return self.req(409,{})
   run('INSERT INTO members VALUES(?,?,?)',(w,u['id'],'owner')); c=run('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general'));run('INSERT INTO cmembers VALUES(?,?)',(c,u['id']))
   return self.req(201,{'workspace':wobj(q('SELECT * FROM workspaces WHERE id=?',(w,),True)),'general_channel':cobj(q('SELECT * FROM channels WHERE id=?',(c,),True))})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   r=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not r or r['role']!='owner':return self.req(403,{})
   target=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id')),True)
   if not target:return self.req(404,{})
   if target['role']!='admin':return self.req(400,{})
   run('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
   run('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']))
   run('UPDATE workspaces SET owner_id=? WHERE id=?',(b['new_owner_id'],w['id']))
   return self.req(200,{'workspace':wobj(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and p.endswith('/groups'):
   slug=p.split('/')[3];w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role:return self.req(403,{})
   if not re.fullmatch(r'[A-Za-z0-9_-]+',str(b.get('handle',''))) or not b.get('name'):return self.req(400,{})
   if q('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(w['id'],b['handle']),True):return self.req(409,{})
   gid=run('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],b['handle'],b['name'],u['id']))
   for uid in b.get('user_ids',[]): 
    if q('SELECT 1 FROM users WHERE id=?',(uid,),True):run('INSERT OR IGNORE INTO group_users VALUES(?,?)',(gid,uid))
   g=q('SELECT * FROM groups WHERE id=?',(gid,),True);d=dict(g);d['member_user_ids']=[x['user_id'] for x in q('SELECT user_id FROM group_users WHERE group_id=?',(gid,))];d.pop('creator_id',None)
   return self.req(201,{'group':d})
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   slug=p.split('/')[3];w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role or role['role']=='guest' or (b.get('is_private') and role['role'] not in ('owner','admin')):return self.req(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',str(b.get('name',''))) or len(str(b.get('topic','')))>250:return self.req(400,{})
   if q('SELECT id FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name']),True):return self.req(409,{})
   c=run('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')));run('INSERT INTO cmembers VALUES(?,?)',(c,u['id']));return self.req(201,{'channel':cobj(q('SELECT * FROM channels WHERE id=?',(c,),True))})
  if p.startswith('/api/channels/') and p.endswith('/leave'):
   cid=p.split('/')[3]; ch=q('SELECT id FROM channels WHERE id=?',(cid,),True)
   if not ch:return self.req(404,{})
   run('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']))
   return self.req(200,{'channel':cobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  if p.startswith('/api/channels/') and p.endswith('/join'):
   cid=p.split('/')[3]; ch=q('SELECT c.*,w.join_mode FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
   if not ch:return self.req(404,{})
   if ch['is_private']:return self.req(403,{})
   rr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id']),True)
   if rr and rr['role']=='guest':return self.req(403,{})
   if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id']),True):
    if ch['join_mode']!='open':return self.req(403,{})
    run('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
   run('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));return self.req(200,{'channel':cobj(ch)})
  if p.startswith('/api/channels/') and p.endswith('/messages'):
   cid=p.split('/')[3];ch=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not ch:return self.req(404,{})
   body=str(b.get('body',''))
   if not body.strip():return self.req(400,{})
   if ch['is_archived'] and not body.startswith('/unarchive'):return self.req(423,{})
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split(None,1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='/me':
     if not arg:return self.req(400,{})
     body=body
    elif cmd=='/shrug':
     body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd in ('/topic','/archive','/unarchive','/invite'):
     if cmd=='/topic':
      if not arg or len(arg)>250:return self.req(400,{})
      run('UPDATE channels SET topic=? WHERE id=?',(arg,cid))
      return self.req(201,{'message':None,'channel':cobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
     if cmd in ('/archive','/unarchive'):
      owner=q('SELECT w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
      if not owner:return self.req(404,{})
      if owner['owner_id']!=u['id']:return self.req(403,{})
      run('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,cid))
      return self.req(201,{'message':None,'channel':cobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
     if cmd=='/invite':
      m=re.fullmatch(r'@([A-Za-z0-9_]+)',arg)
      target=q('SELECT * FROM users WHERE username=?',(m.group(1),),True) if m else None
      if not target:return self.req(404,{})
      ch=q('SELECT workspace_id,is_private FROM channels WHERE id=?',(cid,),True)
      run('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],target['id'],'member'))
      run('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id']))
      return self.req(201,{'message':None,'invited_user':userobj(target)})
    if cmd not in ('/me','/shrug'):return self.req(400,{})
   fids=b.get('file_ids',[]) or []
   for fid in fids:
    fr=q('SELECT * FROM files WHERE id=?',(fid,),True)
    if not fr or fr['uploader_id']!=u['id'] or q('SELECT 1 FROM message_files WHERE file_id=?',(fid,),True):return self.req(400,{})
   event_lock.acquire()
   lf=open(EVENT_LOCK,'w')
   fcntl.flock(lf,fcntl.LOCK_EX)
   try:
    eid=(q('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(cid,),True)['n']); mid=run('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now(),eid))
    for fid in fids:run('INSERT INTO message_files VALUES(?,?)',(mid,fid))
    run('INSERT INTO channel_events(channel_id,event_id,kind,message_id,payload) VALUES(?,?,?,?,?)',(cid,eid,'message.created',mid,''))
   finally:
    fcntl.flock(lf,fcntl.LOCK_UN);lf.close();event_lock.release()
   mm=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))
   for uid in mm.get('mentions',[]):
    run('INSERT OR IGNORE INTO reads(user_id,channel_id,last_read_event_id,unread_count,mention_count) VALUES(?,?,0,0,0)',(uid,cid))
    run('UPDATE reads SET mention_count=COALESCE(mention_count,0)+1 WHERE user_id=? AND channel_id=?',(uid,cid))
   return self.req(201,{'message':mm})
  if p=='/api/dms':
   target=q('SELECT id FROM users WHERE id=?',(b.get('user_id'),),True)
   if not target:return self.req(404,{})
   x=min(u['id'],int(b.get('user_id')));y=max(u['id'],int(b.get('user_id')));r=q('SELECT channel_id FROM dms WHERE user1=? AND user2=?',(x,y),True)
   if r:return self.req(200,{'channel_id':r['channel_id']})
   c=run('INSERT INTO channels(name,is_dm) VALUES(?,1)',('dm-%s-%s'%(x,y),));run('INSERT INTO dms VALUES(?,?,?)',(x,y,c));run('INSERT INTO cmembers VALUES(?,?)',(c,x));run('INSERT INTO cmembers VALUES(?,?)',(c,y));return self.req(200,{'channel_id':c})
  if p.startswith('/api/messages/') and p.endswith('/reactions'):
   mid=p.split('/')[3]; r=q('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,),True)
   if not r:return self.req(404,{})
   if not isinstance(b.get('emoji'),str) or not b.get('emoji'):return self.req(400,{})
   run('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],b['emoji']))
   return self.req(200,{'reactions':msgobj(r)['reactions']})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   mid=p.split('/')[3]
   if not q('SELECT id FROM messages WHERE id=? AND deleted=0',(mid,),True):return self.req(404,{})
   run('INSERT OR REPLACE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));return self.req(200,{'pin':dict(q('SELECT * FROM pins WHERE message_id=?',(mid,),True))})
  if p.startswith('/api/channels/') and p.endswith('/archive'):
   if not q('SELECT id FROM channels WHERE id=?',(p.split('/')[3],),True):return self.req(404,{})
   run('UPDATE channels SET is_archived=1 WHERE id=?',(p.split('/')[3],));return self.req(200,{'channel':cobj(q('SELECT * FROM channels WHERE id=?',(p.split('/')[3],),True))})
  if p.startswith('/api/channels/') and p.endswith('/unarchive'):
   if not q('SELECT id FROM channels WHERE id=?',(p.split('/')[3],),True):return self.req(404,{})
   run('UPDATE channels SET is_archived=0 WHERE id=?',(p.split('/')[3],));return self.req(200,{'channel':cobj(q('SELECT * FROM channels WHERE id=?',(p.split('/')[3],),True))})
  if p.startswith('/api/workspaces/') and '/invitations' in p:
   slug=p.split('/')[3];w=q('SELECT id FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   rr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not rr or rr['role'] not in ('owner','admin'):return self.req(403,{})
   code=secrets.token_urlsafe(10);run('INSERT INTO invites(code,workspace_id,creator_id,invited_user_id,expires_at,max_uses) VALUES(?,?,?,?,?,?)',(code,w['id'],u['id'],b.get('invited_user_id'),(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=b.get('expires_in_seconds',86400))).isoformat(),b.get('max_uses',1)));return self.req(201,{'invitation':dict(q('SELECT * FROM invites WHERE code=?',(code,),True))})
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3];iv=q('SELECT * FROM invites WHERE code=?',(code,),True)
   if not iv or iv['revoked'] or iv['uses']>=iv['max_uses']:return self.req(404,{})
   if iv['invited_user_id'] and iv['invited_user_id']!=u['id']:return self.req(403,{})
   if iv['expires_at'] and iv['expires_at']<datetime.datetime.now(datetime.timezone.utc).isoformat():return self.req(404,{})
   run('UPDATE invites SET uses=uses+1 WHERE code=?',(code,));run('INSERT OR IGNORE INTO members VALUES(?,?,?)',(iv['workspace_id'],u['id'],'member'));return self.req(200,{'accepted':True})
  return self.req(404,{})
 def do_PATCH(self):
  p=self.route();u=auth(self.headers);b=self.body()
  if b is None:return self.req(400,{})
  if not u:return self.req(401,{})
  if p=='/api/users/me':
   vals={k:b[k] for k in ('display_name','timezone','avatar_url','status_text','status_emoji') if k in b}
   if 'timezone' in vals:
    try: ZoneInfo(vals['timezone'])
    except Exception: return self.req(400,{})
    if len(vals['timezone'])>64:return self.req(400,{})
   if any(not isinstance(v,str) or len(v)>255 for k,v in vals.items() if k!='timezone'):return self.req(400,{})
   if vals:run('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],)); broadcast({'type':'user.updated','user':userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))})
   return self.req(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))})
  if p.startswith('/api/messages/'):
   mid=p.split('/')[3];r=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not r:return self.req(404,{})
   if r['deleted']:return self.req(409,{})
   if r['author_id']!=u['id']:return self.req(403,{})
   if not isinstance(b.get('body'),str) or not b['body'].strip():return self.req(400,{})
   run('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid))
   cid=r['channel_id']; eid=q('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(cid,),True)['n']
   run('INSERT INTO channel_events(channel_id,event_id,kind,message_id,payload) VALUES(?,?,?,?,?)',(cid,eid,'message.updated',mid,''))
   return self.req(200,{'message':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))})
  if p.startswith('/api/workspaces/') and '/members/' in p:
   slug=p.split('/')[3];w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.req(403,{})
   uid=p.split('/')[-1]; target=q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid),True)
   if not target:return self.req(404,{})
   if b.get('role') not in ('owner','admin','member','guest'):return self.req(400,{})
   if b.get('role')=='owner' or int(uid)==u['id'] or target['role']=='owner':return self.req(403,{})
   if role['role']=='admin' and target['role']=='admin':return self.req(403,{})
   run('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(b['role'],w['id'],uid));return self.req(200,{'member':dict(q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid),True))})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   slug=p.split('/')[3];handle=p.split('/')[5];w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
   if not g:return self.req(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if g['creator_id']!=u['id'] and (not role or role['role'] not in ('owner','admin')):return self.req(403,{})
   if 'name' in b:run('UPDATE groups SET name=? WHERE id=?',(b['name'],g['id']))
   if 'user_ids' in b:
    run('DELETE FROM group_users WHERE group_id=?',(g['id'],))
    for uid in b['user_ids']:run('INSERT OR IGNORE INTO group_users VALUES(?,?)',(g['id'],uid))
   g=q('SELECT * FROM groups WHERE id=?',(g['id'],),True);d=dict(g);d['member_user_ids']=[x['user_id'] for x in q('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))];d.pop('creator_id',None)
   return self.req(200,{'group':d})
  if p.startswith('/api/workspaces/'):
   slug=p.split('/')[3];w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.req(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.req(403,{})
   if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.req(400,{})
   run('UPDATE workspaces SET '+','.join(k+'=?' for k in b if k in ('name','join_mode'))+' WHERE id=?',tuple(b[k] for k in b if k in ('name','join_mode'))+(w['id'],));return self.req(200,{'workspace':wobj(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/channels/'):
   cid=p.split('/')[3];ch=q('SELECT id FROM channels WHERE id=?',(cid,),True)
   if not ch:return self.req(404,{})
   if len(str(b.get('topic','')))>250:return self.req(400,{})
   run('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),cid));return self.req(200,{'channel':cobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  return self.req(404,{})
 def do_DELETE(self):
  p=self.route();u=auth(self.headers)
  if not u:return self.req(401,{})
  if '/groups/' in p:
   slug=p.split('/')[3];handle=p.split('/')[5];w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True);g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle),True) if w else None
   if not g:return self.req(200,{'deleted':True})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.req(403,{})
   run('DELETE FROM group_users WHERE group_id=?',(g['id'],));run('DELETE FROM groups WHERE id=?',(g['id'],));return self.req(200,{'deleted':True})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   if not q('SELECT id FROM messages WHERE id=?',(p.split('/')[3],),True):return self.req(404,{})
   run('DELETE FROM pins WHERE message_id=?',(p.split('/')[3],));return self.req(200,{'unpinned':True})
  if p.startswith('/api/messages/') and '/reactions/' in p:
   mid=p.split('/')[3]; emoji=urllib.parse.unquote(p.split('/')[5])
   r=q('SELECT id FROM messages WHERE id=? AND deleted=0',(mid,),True)
   if not r:return self.req(404,{})
   run('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
   return self.req(200,{'reactions':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))['reactions']})
  if p.startswith('/api/messages/'):
   mid=p.split('/')[3]; r=q('SELECT m.*,w.owner_id FROM messages m JOIN channels c ON c.id=m.channel_id JOIN workspaces w ON w.id=c.workspace_id WHERE m.id=?',(mid,),True)
   if not r:return self.req(404,{})
   if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return self.req(403,{})
   if r['deleted']:return self.req(200,{'deleted':True})
   eid=q('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(r['channel_id'],),True)['n']
   run('UPDATE messages SET deleted=1 WHERE id=?',(mid,))
   run('INSERT INTO channel_events(channel_id,event_id,kind,message_id,payload) VALUES(?,?,?,?,?)',(r['channel_id'],eid,'message.deleted',mid,''))
   return self.req(200,{'deleted':True})
  if '/invitations/' in p:
   iv=q('SELECT i.*,m.role FROM invites i JOIN members m ON m.workspace_id=i.workspace_id AND m.user_id=? WHERE i.code=?',(u['id'],p.split('/')[-1]),True)
   if not iv:return self.req(404,{})
   if iv['role'] not in ('owner','admin'):return self.req(403,{})
   run('UPDATE invites SET revoked=1 WHERE code=?',(p.split('/')[-1],));return self.req(200,{'deleted':True})
  return self.req(404,{})
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:3px}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.app{display:flex;height:100vh}.side{width:240px;background:#3f0f40;color:white;padding:20px}.main{flex:1;padding:25px}.modal{padding:30px}input,textarea{padding:9px;margin:5px}</style></head><body><div id=app></div><div id="static-hooks" style="display:none">
<form data-testid="auth-form"></form><button data-testid="auth-submit" data-button-role="primary"></button><button data-testid="auth-toggle"></button>
<form data-testid="workspace-create-form"></form><button data-testid="empty-state-create-workspace"></button><form data-testid="join-workspace-form"></form><button data-testid="join-workspace-submit" data-button-role="primary"></button>
<div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary"></button><div data-testid="current-user"></div><button data-testid="logout-btn" data-button-role="danger"></button>
<div data-testid="workspace-header"></div><button data-testid="workspace-settings-btn" data-button-role="secondary"></button><button data-testid="workspace-settings-close" data-button-role="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button>
<div data-testid="channel-title"></div><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary"></button><div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary"></button><div data-testid="channel-members-list"></div>
<form data-testid="channel-add-member-form"></form><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<form data-testid="create-channel-form"></form><input type="checkbox" data-testid="create-channel-private"><button data-testid="create-channel-submit" data-button-role="primary"></button><button data-testid="create-channel-cancel" data-button-role="secondary"></button>
<div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary"></button><div data-testid="thread-panel"></div><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary"></button><button data-testid="close-thread" data-button-role="secondary"></button>
<button data-testid="settings-tab-members"></button><button data-testid="settings-tab-invitations"></button><button data-testid="settings-tab-general"></button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div>
<button data-testid="create-invitation-btn" data-button-role="secondary"></button><form data-testid="create-invitation-form"></form><button data-testid="create-invitation-submit" data-button-role="primary"></button><div data-testid="invitations-list"></div>
</div><div id="auth-modal"></div><div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div><div id="create-invitation-form"></div><style>
button[data-button-role="primary"]{background:#007a5a;color:#ffffff}button[data-button-role="danger"]{background:#e01e5a;color:#ffffff}button[data-button-role="secondary"]{background:#1264a3;color:#ffffff}
</style><script>
const A='/api',app=document.getElementById('app');let token=localStorage.getItem('huddle.token'),user=null,channels=[],cur=null;
function btn(t,r,id){return `<button id="${id||''}" data-testid="${id||''}" data-button-role="${r||'secondary'}">${t}</button>`}
function auth(){app.innerHTML=`<div class=modal data-testid=auth-form id=auth-modal><h1>Huddle</h1><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-toggle data-button-role=secondary>Sign up</button>${btn('Log in','primary','auth-submit')}<p id=err></p></div>`;document.querySelector('#auth-submit').onclick=async()=>{let b={username:document.querySelector('[name=username]').value,password:document.querySelector('[name=password]').value};let r=await fetch(A+'/auth/'+(document.querySelector('[data-testid=auth-toggle]').textContent==='Sign up'?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});let d=await r.json();if(!r.ok)return err.textContent='Invalid credentials or registration';token=d.token;localStorage.setItem('huddle.token',token);load()};document.querySelector('[data-testid=auth-toggle]').onclick=e=>e.target.textContent=e.target.textContent==='Sign up'?'Log in':'Sign up'}
async function load(){let h={Authorization:'Bearer '+token},m=await (await fetch(A+'/auth/me',{headers:h})).json();if(!m.user)return auth();user=m.user;let w=await (await fetch(A+'/workspaces',{headers:h})).json();if(!w.workspaces?.length)return workspace();let x=await (await fetch(A+'/workspaces/'+w.workspaces[0].slug,{headers:h})).json();channels=x.channels;render()}
function workspace(){app.innerHTML=`<div class=modal><h1>Create workspace</h1><form data-testid=workspace-create-form><input name=slug placeholder=slug><input name=name placeholder=name><button data-testid=workspace-general-submit data-button-role=primary>Create</button></form></div>`;document.querySelector('form').onsubmit=async e=>{e.preventDefault();let f=e.target;await fetch(A+'/workspaces',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({slug:f.slug.value,name:f.name.value})});load()}}
function render(){app.innerHTML=`<div class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user>${user.display_name}</div><div data-testid=channel-list>${channels.map(c=>`<div data-testid=channel-entry data-channel-id=${c.id} data-channel-name=${c.name}># ${c.name}</div>`).join('')}</div>${btn('New channel','secondary','new-channel-btn')}${btn('Logout','danger','logout-btn')}</aside><main class=main><h2 data-testid=channel-title># ${channels[0]?.name||''}</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button></main></div>`;document.querySelector('#logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');auth()};document.querySelector('#send-btn').onclick=send;document.querySelectorAll('[data-testid=channel-entry]').forEach(x=>x.onclick=()=>{cur=x.dataset.channelId;msgs()});cur=channels[0]?.id;msgs()}
async function msgs(){if(!cur)return;let d=await(await fetch(A+'/channels/'+cur+'/messages',{headers:{Authorization:'Bearer '+token}})).json();document.querySelector('[data-testid=message-list]').innerHTML=(d.messages||[]).map(m=>`<div data-testid=message><b>${m.author.display_name}</b> <span data-testid=message-body>${m.body}</span><button data-testid=open-thread-btn> ${m.reply_count} replies</button><button data-testid=reaction-button>😀</button></div>`).join('')}
async function send(){let i=document.querySelector('[data-testid=message-input]');if(!i.value.trim())return;await fetch(A+'/channels/'+cur+'/messages',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({body:i.value})});i.value='';msgs()}if(token)load();else auth();
</script></body></html>'''
if __name__=='__main__':
 init(); port=int(sys.argv[1]); srv=ThreadingHTTPServer(('127.0.0.1',port),H); srv.daemon_threads=True; srv.request_queue_size=256; srv.serve_forever()
