import os,sys,json,re,sqlite3,secrets,hashlib,base64,datetime,threading,urllib.parse,mimetypes,fcntl,time,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
try:
 from zoneinfo import ZoneInfo
except ImportError: ZoneInfo=None
DB='/app/huddle.db'; EVENTS='/app/huddle-events.log'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
lock=threading.RLock()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,check_same_thread=False,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def init():
 with lock:
  c=db(); c.executescript('''
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
  CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
  CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
  CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
  CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER,topic TEXT,is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
  CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
  CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
  CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
  CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
  CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
  CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
  CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
  CREATE TABLE IF NOT EXISTS dms(user1 INTEGER,user2 INTEGER,channel_id INTEGER,PRIMARY KEY(user1,user2));
  ''');
  try: c.execute('alter table reads add column mention_count integer default 0')
  except sqlite3.OperationalError: pass
  c.commit(); c.close()
init()
def q(sql,args=(),one=False):
 for attempt in range(12):
  try:
   with lock:
    with open(DB+'.lock','w') as lf:
     fcntl.flock(lf,fcntl.LOCK_EX)
     c=db()
     try:
      x=c.execute(sql,args); rows=x.fetchone() if one else x.fetchall(); c.commit()
     finally: c.close(); fcntl.flock(lf,fcntl.LOCK_UN)
     return rows
  except sqlite3.OperationalError as e:
   if 'locked' not in str(e).lower() or attempt==11: raise
   time.sleep(.1*(attempt+1))

def user(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def ch(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def fileobj(r): return {k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}
def mention_ids(text, channel_id, author_id):
 ids=set()
 members=[x['user_id'] for x in q('select user_id from channel_members where channel_id=?',(channel_id,))]
 for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',text):
  if name in ('channel','here'):
   candidates=members
   if name=='here':
    candidates=[x for x in members if os.path.exists('/app/ws-online-'+str(x))]
  else:
   g=q('select user_ids from groups_ where handle=? and workspace_id=(select workspace_id from channels where id=?)',(name,channel_id),True)
   if g: candidates=json.loads(g['user_ids'])
   else:
    z=q('select id from users where username=?',(name,),True); candidates=[z['id']] if z else []
   candidates=[x for x in candidates if x in members]
  for x in candidates:
   if x!=author_id: ids.add(x)
 return sorted(ids)
def msg(r):
 a=q('select * from users where id=?',(r['author_id'],),True); rs=q('select emoji,user_id from reactions where message_id=?',(r['id'],)); fs=q('select f.* from files f join attachments a on a.file_id=f.id where a.message_id=?',(r['id'],))
 em={}
 for x in rs: em.setdefault(x['emoji'],[]).append(x['user_id'])
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('select count(*) n from messages where parent_id=? and deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fileobj(f) for f in fs],'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in em.items()],'mentions':mention_ids(r['body'],r['channel_id'],r['author_id'])}
def wsclient(sock):
 try:
  h=sock.recv(2)
  if len(h)<2:return None
  n=h[1]&127; masked=bool(h[1]&128)
  if n==126:n=struct.unpack('>H',sock.recv(2))[0]
  elif n==127:n=struct.unpack('>Q',sock.recv(8))[0]
  mask=sock.recv(4) if masked else b''; data=sock.recv(n)
  if masked:data=bytes(data[i]^mask[i%4] for i in range(len(data)))
  return json.loads(data.decode()) if data else None
 except (socket.timeout,BlockingIOError): return None
 except: return {'type':'__close__'}
def wsframe(obj):
 b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
 if n<126: h=bytes([0x81,n])
 elif n<65536: h=bytes([0x81,126])+struct.pack('>H',n)
 else: h=bytes([0x81,127])+struct.pack('>Q',n)
 return h+b
def event_append(obj):
 with open(EVENTS,'a') as f: f.write(json.dumps(obj)+'\n'); f.flush()
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x): pass
 def sendj(self,status,obj):
  b=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length',0))
  try: return json.loads(self.rfile.read(n) or b'{}')
  except Exception: raise ValueError('bad json')
 def auth(self):
  t=self.headers.get('Authorization','').replace('Bearer ',''); r=q('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,),True)
  return r
 def reqpath(self): return urllib.parse.urlparse(self.path).path
 def do_GET(self):
  if self.reqpath()=='/api/ws':
   tok=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
   authrow=q('select user_id from tokens where token=?',(tok,),True)
   if not authrow: return self.sendj(401,{})
   online='/app/ws-online-'+str(authrow['user_id']); open(online,'w').write(str(os.getpid()))
   key=self.headers.get('Sec-WebSocket-Key','')
   if not key: return self.sendj(400,{})
   import base64,hashlib
   acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',acc); self.end_headers(); self.wfile.flush()
   self.connection.settimeout(0.2); pos=os.path.getsize(EVENTS) if os.path.exists(EVENTS) else 0
   try:
    while True:
     cmd=wsclient(self.connection)
     if cmd and cmd.get('type')=='subscribe':
      cid=int(cmd.get('channel_id',0)); head=q('select coalesce(max(event_id),0) n from messages where channel_id=?',(cid,),True)['n']
      self.connection.sendall(wsframe({'type':'subscribed','channel_id':cid,'head_event_id':head}))
     elif cmd and cmd.get('type')=='resume':
      cid=int(cmd.get('channel_id',0)); since=int(cmd.get('since_event_id',0))
      rows=q('select * from messages where channel_id=? and event_id>? order by event_id',(cid,since))
      for rr in rows:self.connection.sendall(wsframe({'type':'message.created','event_id':rr['event_id'],'channel_id':cid,'message':msg(rr)}))
      head=q('select coalesce(max(event_id),0) n from messages where channel_id=?',(cid,),True)['n']; self.connection.sendall(wsframe({'type':'resumed','channel_id':cid,'head_event_id':head}))
     try:
      with open(EVENTS,'r') as f:
       f.seek(pos); lines=f.readlines(); pos=f.tell()
      for line in lines:
       self.connection.sendall(wsframe(json.loads(line)))
     except FileNotFoundError:
      pass
     except Exception:
      return
     time.sleep(0.2)
   finally:
    try: os.unlink(online)
    except: pass
    return
  p=self.reqpath(); u=self.auth()
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if p=='/': return self.html()
  if p.startswith('/api/') and p!='/api/health' and not u:return self.sendj(401,{'error':'unauthorized'})
  if p=='/api/auth/me': return self.sendj(200,{'user':user(u)})
  if p=='/api/workspaces':
   ws=q('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],))
   return self.sendj(200,{'workspaces':[dict(x) for x in ws]})
  m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
   a=q('select * from invitations where workspace_id=? and (expires is null or expires>?) and uses<max_uses',(w['id'],int(datetime.datetime.now().timestamp())))
   return self.sendj(200,{'invitations':[dict(x) for x in a]})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   if not q('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id']),True):return self.sendj(403,{})
   gs=q('select * from groups_ where workspace_id=?',(w['id'],),)
   return self.sendj(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])} for g in gs]})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(members|channels))?$',p)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   if p.endswith('/members'):
    a=q('select m.*,u.username,u.display_name from members m join users u on u.id=m.user_id where workspace_id=?',(w['id'],)); return self.sendj(200,{'members':[dict(x) for x in a]})
   inc=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('include_archived',['0'])[0] in ('1','true')
   cs=q('select * from channels where workspace_id=?'+('' if inc else ' and is_archived=0'),(w['id'],)); cs=[x for x in cs if not x['is_private'] or q('select 1 from channel_members where channel_id=? and user_id=?',(x['id'],u['id']),True) or w['owner_id']==u['id']]; return self.sendj(200,{'workspace':dict(w),'channels':[ch(x) for x in cs],'read_state':{}})
  m=re.match(r'/api/channels/(\d+)/read$',p)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   z=q('select * from reads where user_id=? and channel_id=?',(u['id'],c['id']),True)
   return self.sendj(200,{'read_state':{'channel_id':c['id'],'last_read_event_id':z['last_read_event_id'] if z else 0,'unread_count':0,'mention_count':z['mention_count'] if z and z['mention_count'] else 0}})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   if (c['is_dm'] or c['is_private']) and not q('select 1 from channel_members where channel_id=? and user_id=?',(c['id'],u['id']),True):return self.sendj(403,{})
   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); lim=int(qs.get('limit',['50'])[0]); cur=qs.get('cursor',[None])[0]
   if lim<1 or lim>200:return self.sendj(400,{})
   try: off=int(cur) if cur is not None else 0
   except: return self.sendj(400,{})
   if off<0:return self.sendj(400,{})
   boundary=int(cur) if cur is not None else 9223372036854775807
   a=q('select * from messages where channel_id=? and deleted=0 and parent_id is null and id<? order by id desc limit ?',(m.group(1),boundary,lim+1))
   nxt=str(a[lim-1]['id']) if len(a)>lim else None; return self.sendj(200,{'messages':[msg(x) for x in a[:lim]],'next_cursor':nxt})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   if not q('select id from messages where id=?',(m.group(1),),True):return self.sendj(404,{})
   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); lim=int(qs.get('limit',['50'])[0]);   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); lim=int(qs.get('limit',['50'])[0]); off=int(qs.get('cursor',['0'])[0] or 0) if (qs.get('cursor',['0'])[0] or '0').isdigit() else -1
   if lim<1 or lim>200 or off<0:return self.sendj(400,{})
   a=q('select * from messages where parent_id=? and deleted=0 order by id limit ? offset ?',(m.group(1),lim+1,off)); return self.sendj(200,{'replies':[msg(x) for x in a[:lim]],'next_cursor':str(off+lim) if len(a)>lim else None})
  m=re.match(r'/api/channels/(\d+)/members$',p)
  if m:
   if not q('select id from channels where id=?',(m.group(1),),True):return self.sendj(404,{})
   a=q('select u.* from channel_members cm join users u on u.id=cm.user_id where cm.channel_id=?',(m.group(1),)); return self.sendj(200,{'members':[user(x) for x in a]})
  if p.startswith('/api/search'):
   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); z=qs.get('q',[''])[0]; lim=int(qs.get('limit',['50'])[0]); cur=qs.get('cursor',[None])[0]
   if lim<1 or lim>200:return self.sendj(400,{})
   try: boundary=int(cur) if cur else 9223372036854775807
   except: return self.sendj(400,{})
   wid=qs.get('workspace_id',[None])[0]
   if wid is not None:
    try: int(wid)
    except: return self.sendj(400,{})
   sql='select m.* from messages m join channels c on c.id=m.channel_id where m.body like ? and m.deleted=0 and m.id<? and exists(select 1 from channel_members cm where cm.channel_id=c.id and cm.user_id=?)'
   args=['%'+z+'%',boundary,u['id']]
   if wid is not None: sql+=' and c.workspace_id=?'; args.append(int(wid))
   sql+=' order by m.id desc limit ?'; args.append(lim+1)
   a=q(sql,tuple(args))
   return self.sendj(200,{'results':[msg(x) for x in a[:lim]],'next_cursor':str(a[lim-1]['id']) if len(a)>lim else None})
  m=re.match(r'/api/channels/(\d+)/pins$',p)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   if c['is_private'] and not q('select 1 from channel_members where channel_id=? and user_id=?',(c['id'],u['id']),True):return self.sendj(403,{})
   a=q('select p.*,m.*,pinned.username as pn from pins p join messages m on m.id=p.message_id join users pinned on pinned.id=p.pinned_by where m.channel_id=? order by p.pinned_at desc',(c['id'],))
   qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); lim=int(qs.get('limit',['50'])[0]); off=int(qs.get('cursor',['0'])[0] or 0) if (qs.get('cursor',['0'])[0] or '0').isdigit() else -1
   if lim<1 or lim>200 or off<0:return self.sendj(400,{})
   a=a[off:off+lim+1]; return self.sendj(200,{'pins':[{'message':msg(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in a[:lim]],'next_cursor':str(off+lim) if len(a)>lim else None})
  m=re.match(r'/api/files/(\d+)$',p)
  if m:
   x=q('select * from files where id=?',(m.group(1),),True); return self.sendj(200,{'file':fileobj(x)} if x else {},) if x else self.sendj(404,{})
  m=re.match(r'/api/files/(\d+)/download$',p)
  if m:
   x=q('select * from files where id=?',(m.group(1),),True)
   if not x:return self.sendj(404,{})
   self.send_response(200); self.send_header('Content-Type',x['content_type']); self.send_header('Content-Disposition','attachment; filename="'+x['filename']+'"'); self.send_header('Content-Length',str(x['size'])); self.end_headers(); self.wfile.write(x['data']); return
  return self.sendj(404,{})
 def do_POST(self):
  p=self.reqpath(); u=self.auth()
  if p=='/api/auth/register' or p=='/api/auth/login':
   try: b=self.body()
   except ValueError: return self.sendj(400,{})
   if p.endswith('register'):
    if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.sendj(400,{})
    try: q('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(b['username'],b['password'],b.get('display_name',b['username']),'UTC','','',''))
    except: return self.sendj(409,{})
   x=q('select * from users where username=? and password=?',(b.get('username'),b.get('password')),True)
   if not x:return self.sendj(401,{})
   t=secrets.token_urlsafe(24); q('insert into tokens values(?,?)',(t,x['id'])); return self.sendj(201 if p.endswith('register') else 200,{'user':user(x),'token':t})
  if not u:return self.sendj(401,{})
  if p=='/api/files':
   ct=self.headers.get('Content-Type',''); mtype=re.search(r'boundary=([^;]+)',ct)
   if not mtype:return self.sendj(400,{})
   boundary=('--'+mtype.group(1).strip('"')).encode(); raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
   part=None
   for seg in raw.split(boundary):
    if b'Content-Disposition:' in seg and b'name="file"' in seg:
     part=seg.split(b'\r\n\r\n',1)[1].split(b'\r\n--',1)[0]; part=part[:-2] if part.endswith(b'\r\n') else part; hdr=seg.split(b'\r\n\r\n',1)[0].decode('latin1'); break
   if part is None:return self.sendj(400,{})
   fn=re.search(r'filename="([^"]*)"',hdr); filename=fn.group(1) if fn else 'upload'
   cm=re.search(r'Content-Type:\s*([^\r\n]+)',hdr,re.I); typ=cm.group(1).strip() if cm else 'application/octet-stream'
   if len(part)>10485760:return self.sendj(413,{})
   t=now(); q('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],filename,typ,len(part),t,part))
   f=q('select * from files order by id desc limit 1',(),True); return self.sendj(201,{'file':fileobj(f)})
  try: b=self.body()
  except ValueError: return self.sendj(400,{})
  im=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
  if im:
   w=q('select * from workspaces where slug=?',(im.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
   target=b.get('invited_user_id')
   if b.get('invited_username'):
    z=q('select id from users where username=?',(b['invited_username'],),True); target=z['id'] if z else None
   exp=int(datetime.datetime.now().timestamp())+int(b['expires_in_seconds']) if b.get('expires_in_seconds') else None
   code=secrets.token_urlsafe(18); maxu=int(b.get('max_uses',1))
   q('insert into invitations(code,workspace_id,invited_user_id,expires,max_uses,uses) values(?,?,?,?,?,0)',(code,w['id'],target,exp,maxu))
   x=q('select * from invitations where code=?',(code,),True); return self.sendj(201,{'invitation':dict(x)})
  am=re.match(r'/api/invitations/([^/]+)/accept$',p)
  if am:
   x=q('select * from invitations where code=?',(am.group(1),),True)
   if not x:return self.sendj(404,{})
   if x['expires'] and x['expires']<=int(datetime.datetime.now().timestamp()):return self.sendj(404,{})
   if x['uses']>=x['max_uses']:return self.sendj(404,{})
   if x['invited_user_id'] and x['invited_user_id']!=u['id']:return self.sendj(403,{})
   q('insert or ignore into members values(?,?,?)',(x['workspace_id'],u['id'],'member'))
   q('delete from invitations where code=?',(x['code'],)) if x['invited_user_id'] else q('update invitations set uses=uses+1 where code=?',(x['code'],))
   w=q('select * from workspaces where id=?',(x['workspace_id'],),True); g=q('select id from channels where workspace_id=? and name="general"',(w['id'],),True)
   if g:q('insert or ignore into channel_members values(?,?)',(g['id'],u['id']))
   return self.sendj(200,{'workspace':dict(w)})
  tm=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
  if tm:
   w=q('select * from workspaces where slug=?',(tm.group(1),),True)
   if not w:return self.sendj(404,{})
   cur=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   target=q('select role from members where workspace_id=? and user_id=?',(w['id'],b.get('new_owner_id')),True)
   if not cur or cur['role']!='owner':return self.sendj(403,{})
   if not target or target['role']!='admin':return self.sendj(400,{})
   q('update members set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id']))
   q('update members set role="owner" where workspace_id=? and user_id=?',(w['id'],b['new_owner_id']))
   q('update workspaces set owner_id=? where id=?',(b['new_owner_id'],w['id']))
   return self.sendj(200,{'workspace':dict(q('select * from workspaces where id=?',(w['id'],),True))})
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')) or not b.get('name'):return self.sendj(400,{})
   try:q('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(b['slug'],b['name'],u['id'],'open'))
   except:return self.sendj(409,{})
   w=q('select * from workspaces where slug=?',(b['slug'],),True)
   q('insert into members values(?,?,?)',(w['id'],u['id'],'owner'))
   q('insert into channels(workspace_id,name,is_private,is_dm,topic) values(?,?,0,0,"")',(w['id'],'general'))
   c=q('select * from channels where workspace_id=? and name="general"',(w['id'],),True)
   q('insert into channel_members values(?,?)',(c['id'],u['id']))
   return self.sendj(201,{'workspace':dict(w),'general_channel':ch(c)})
  if p=='/api/workspaces':
   ws=q('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],))
   return self.sendj(200,{'workspaces':[dict(x) for x in ws]})
  m=re.match(r'/api/users/(\d+)$',p)
  if m:
   x=q('select * from users where id=?',(m.group(1),),True); return self.sendj(200,{'user':user(x)} if x else {'error':'not found'},) if x else self.sendj(404,{'error':'not found'})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   w=q('select * from workspaces where slug=?',(gm.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role:return self.sendj(403,{})
   try:q('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],b.get('handle'),b.get('name'),u['id'],json.dumps(b.get('user_ids',[]))))
   except:return self.sendj(409,{})
   g=q('select * from groups_ where workspace_id=? and handle=?',(w['id'],b.get('handle')),True)
   return self.sendj(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return self.sendj(400,{})
   try:q('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(b['slug'],b['name'],u['id'],'open'))
   except:return self.sendj(409,{})
   w=q('select * from workspaces where slug=?',(b['slug'],),True); q('insert into members values(?,?,?)',(w['id'],u['id'],'owner')); q('insert into channels(workspace_id,name,is_private,is_dm,topic) values(?,?,0,0,"")',(w['id'],'general')); c=q('select * from channels where workspace_id=?',(w['id'],),True); q('insert into channel_members values(?,?)',(c['id'],u['id'])); return self.sendj(201,{'workspace':dict(w),'general_channel':ch(c)})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role or role['role']=='guest':return self.sendj(403,{})
   if b.get('is_private') and role['role'] not in ('owner','admin'):return self.sendj(403,{})
   if not isinstance(b.get('name'),str) or not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.sendj(400,{})
   try:q('insert into channels(workspace_id,name,is_private,is_dm,topic) values(?,?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),0,b.get('topic','')))
   except:return self.sendj(409,{})
   c=q('select * from channels where workspace_id=? and name=?',(w['id'],b['name']),True); q('insert into channel_members values(?,?)',(c['id'],u['id'])); return self.sendj(201,{'channel':ch(c)})
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   wsrole=q('select role from members where workspace_id=? and user_id=?',(c['workspace_id'],u['id']),True)
   already=q('select 1 from channel_members where channel_id=? and user_id=?',(c['id'],u['id']),True)
   if c['is_private'] and not already:return self.sendj(403,{})
   if wsrole and wsrole['role']=='guest':return self.sendj(403,{})
   if not wsrole and (q('select join_mode from workspaces where id=?',(c['workspace_id'],),True)['join_mode']=='invite_only' or c['is_private']):return self.sendj(403,{})
   q('insert or ignore into members values(?,?,?)',(c['workspace_id'],u['id'],'member')); q('insert or ignore into channel_members values(?,?)',(c['id'],u['id'])); return self.sendj(200,{'channel':ch(c)})
  rm=re.match(r'/api/channels/(\d+)/read$',p)
  if rm:
   c=q('select * from channels where id=?',(rm.group(1),),True)
   if not c:return self.sendj(404,{})
   z=q('select * from reads where user_id=? and channel_id=?',(u['id'],c['id']),True); old=z['last_read_event_id'] if z else 0; val=max(old,int(b.get('last_read_event_id',0)))
   q('insert or replace into reads(user_id,channel_id,last_read_event_id,mention_count) values(?,?,?,?)',(u['id'],c['id'],val,z['mention_count'] if z else 0)); z=q('select * from reads where user_id=? and channel_id=?',(u['id'],c['id']),True); return self.sendj(200,{'read_state':{'channel_id':c['id'],'last_read_event_id':z['last_read_event_id'],'unread_count':0,'mention_count':z['mention_count'] or 0}})
  m=re.match(r'/api/channels/(\d+)/(leave|archive|unarchive)$',p)
  if m:
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   action=m.group(2)
   if action=='leave':
    q('delete from channel_members where channel_id=? and user_id=?',(c['id'],u['id'])); return self.sendj(200,{'channel':ch(c)})
   role=q('select role from members where workspace_id=? and user_id=?',(c['workspace_id'],u['id']),True)
   if not role or role['role']!='owner':return self.sendj(403,{})
   q('update channels set is_archived=? where id=?',(1 if action=='archive' else 0,c['id']))
   c=q('select * from channels where id=?',(c['id'],),True); return self.sendj(200,{'channel':ch(c)})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   if not b.get('body',''):return self.sendj(400,{})
   c=q('select * from channels where id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   if (c['is_dm'] or c['is_private']) and not q('select 1 from channel_members where channel_id=? and user_id=?',(c['id'],u['id']),True):return self.sendj(403,{})
   text=b.get('body','')
   if c['is_archived'] and not text.startswith('/unarchive'):return self.sendj(423,{})
   if text.startswith('/') and not text.startswith('//'):
    parts=text.split(None,1); cmd=parts[0][1:]; arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='me':
     if not arg:return self.sendj(400,{})
     text='/me '+arg
    elif cmd=='shrug':
     text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if not arg:return self.sendj(400,{})
     q('update channels set topic=? where id=?',(arg,c['id'])); c=q('select * from channels where id=?',(c['id'],),True); return self.sendj(201,{'message':None,'channel':ch(c)})
    elif cmd=='invite':
     target=q('select * from users where username=?',(arg.lstrip('@'),),True)
     if not target:return self.sendj(404,{})
     q('insert or ignore into channel_members values(?,?)',(c['id'],target['id'])); return self.sendj(201,{'message':None,'invited_user':user(target)})
    elif cmd in ('archive','unarchive'):
     role=q('select role from members where workspace_id=? and user_id=?',(c['workspace_id'],u['id']),True)
     if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
     q('update channels set is_archived=? where id=?',(1 if cmd=='archive' else 0,c['id'])); c=q('select * from channels where id=?',(c['id'],),True); return self.sendj(201,{'message':None,'channel':ch(c)})
    else:return self.sendj(400,{})
   b['body']=text
   ids=b.get('file_ids',[])
   for fid in ids:
    f=q('select * from files where id=?',(fid,),True)
    if not f or f['uploader_id']!=u['id'] or q('select 1 from attachments where file_id=?',(fid,),True): return self.sendj(400,{})
   eid=q('select coalesce(max(event_id),0)+1 n from messages where channel_id=?',(c['id'],),True)['n']; q('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id) values(?,?,?,?,?,?)',(c['id'],u['id'],b['body'],b.get('parent_id'),now(),eid)); x=q('select * from messages where channel_id=? order by id desc limit 1',(c['id'],),True)
   for fid in ids:q('insert into attachments values(?,?)',(x['id'],fid))
   for mid in mention_ids(b['body'],c['id'],u['id']):
    q('insert or ignore into reads(user_id,channel_id,last_read_event_id,mention_count) values(?,?,0,0)',(mid,c['id']))
    q('update reads set mention_count=coalesce(mention_count,0)+1 where user_id=? and channel_id=?',(mid,c['id']))
   event_append({'type':'message.reply' if b.get('parent_id') else 'message.created','event_id':x['event_id'],'channel_id':c['id'],'message':msg(x)})
   return self.sendj(201,{'message':msg(x)})
  m=re.match(r'/api/messages/(\d+)/reactions$',p)
  if m:
   x=q('select * from messages where id=? and deleted=0',(m.group(1),),True)
   if not x:return self.sendj(404,{})
   if not b.get('emoji'):return self.sendj(400,{})
   q('insert or ignore into reactions values(?,?,?)',(m.group(1),u['id'],b['emoji']))
   x=q('select * from messages where id=?',(m.group(1),),True); return self.sendj(200,{'reactions':msg(x)['reactions']})
  m=re.match(r'/api/messages/(\d+)/pin$',p)
  if m:
   if not q('select id from messages where id=?',(m.group(1),),True):return self.sendj(404,{})
   q('insert or ignore into pins values(?,?,?)',(m.group(1),u['id'],now())); return self.sendj(200,{'pin':{'message_id':int(m.group(1)),'pinned_by':u['id'],'pinned_at':now()}})
  if p=='/api/dms':
   other=q('select id from users where id=?',(b.get('user_id'),),True)
   if not other:return self.sendj(404,{})
   a,bid=sorted((u['id'],other['id']))
   old=q('select channel_id from dms where user1=? and user2=?',(a,bid),True)
   if old:return self.sendj(200,{'channel_id':old['channel_id']})
   q('insert into channels(workspace_id,name,is_private,is_dm,topic) values(0,?,?,1,"")',(f'dm-{a}-{bid}',1))
   c=q('select * from channels where is_dm=1 and name=?',(f'dm-{a}-{bid}',),True); q('insert into dms values(?,?,?)',(a,bid,c['id'])); q('insert into channel_members values(?,?)',(c['id'],a)); q('insert into channel_members values(?,?)',(c['id'],bid)); return self.sendj(200,{'channel_id':c['id']})
  return self.sendj(404,{})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.sendj(401,{})
  p=self.reqpath()
  if p=='/api/files':
   ct=self.headers.get('Content-Type',''); mtype=re.search(r'boundary=([^;]+)',ct)
   if not mtype:return self.sendj(400,{})
   boundary=('--'+mtype.group(1).strip('"')).encode(); raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
   part=None
   for seg in raw.split(boundary):
    if b'Content-Disposition:' in seg and b'name="file"' in seg:
     part=seg.split(b'\r\n\r\n',1)[1].split(b'\r\n--',1)[0]; part=part[:-2] if part.endswith(b'\r\n') else part; hdr=seg.split(b'\r\n\r\n',1)[0].decode('latin1'); break
   if part is None:return self.sendj(400,{})
   fn=re.search(r'filename="([^"]*)"',hdr); filename=fn.group(1) if fn else 'upload'
   cm=re.search(r'Content-Type:\s*([^\r\n]+)',hdr,re.I); typ=cm.group(1).strip() if cm else 'application/octet-stream'
   if len(part)>10485760:return self.sendj(413,{})
   t=now(); q('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],filename,typ,len(part),t,part))
   f=q('select * from files order by id desc limit 1',(),True); return self.sendj(201,{'file':fileobj(f)})
  b=self.body(); p=self.reqpath()
  im=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if im:
   w=q('select * from workspaces where slug=?',(im.group(1),),True); role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.sendj(404,{})
   if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
   q('delete from invitations where workspace_id=? and code=?',(w['id'],im.group(2))); return self.sendj(200,{'deleted':True})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if gm:
   w=q('select * from workspaces where slug=?',(gm.group(1),),True); g=q('select * from groups_ where workspace_id=? and handle=?',(w['id'],gm.group(2)),True) if w else None
   if not g:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if u['id']!=g['creator_id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   name=b.get('name',g['name']); ids=b.get('user_ids',json.loads(g['user_ids']))
   q('update groups_ set name=?,user_ids=? where id=?',(name,json.dumps(ids),g['id']))
   return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
  mm=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
  if mm:
   w=q('select * from workspaces where slug=?',(mm.group(1),),True)
   if not w:return self.sendj(404,{})
   actor=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True); target=q('select role from members where workspace_id=? and user_id=?',(w['id'],mm.group(2)),True)
   if not target:return self.sendj(404,{})
   role=b.get('role')
   if role not in ('owner','admin','member','guest'):return self.sendj(400,{})
   if int(mm.group(2))==u['id'] or target['role']=='owner':return self.sendj(403,{})
   if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{})
   if actor['role']=='admin' and target['role']=='admin':return self.sendj(403,{})
   if role=='owner':return self.sendj(400,{})
   q('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],mm.group(2)))
   x=q('select role from members where workspace_id=? and user_id=?',(w['id'],mm.group(2)),True); return self.sendj(200,{'member':{'user_id':int(mm.group(2)),'role':x['role']}})
  tm=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
  if tm:
   w=q('select * from workspaces where slug=?',(tm.group(1),),True)
   if not w:return self.sendj(404,{})
   cur=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   target=q('select role from members where workspace_id=? and user_id=?',(w['id'],b.get('new_owner_id')),True)
   if not cur or cur['role']!='owner':return self.sendj(403,{})
   if not target or target['role']!='admin':return self.sendj(400,{})
   q('update members set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id']))
   q('update members set role="owner" where workspace_id=? and user_id=?',(w['id'],b['new_owner_id']))
   q('update workspaces set owner_id=? where id=?',(b['new_owner_id'],w['id']))
   return self.sendj(200,{'workspace':dict(q('select * from workspaces where id=?',(w['id'],),True))})
  wm=re.match(r'/api/workspaces/([^/]+)$',p)
  if wm:
   w=q('select * from workspaces where slug=?',(wm.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
   if 'join_mode' in b and (b['join_mode'] not in ('open','invite_only') or role['role']!='owner'):return self.sendj(400 if b.get('join_mode') not in ('open','invite_only') else 403,{})
   q('update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where id=?',(b.get('name'),b.get('join_mode'),w['id']))
   return self.sendj(200,{'workspace':dict(q('select * from workspaces where id=?',(w['id'],),True))})
  cm=re.match(r'/api/channels/(\d+)$',p)
  if cm:
   c=q('select * from channels where id=?',(cm.group(1),),True)
   if not c:return self.sendj(404,{})
   if len(b.get('topic',''))>250:return self.sendj(400,{})
   if not q('select 1 from channel_members where channel_id=? and user_id=?',(c['id'],u['id']),True) and not q('select 1 from workspaces where id=? and owner_id=?',(c['workspace_id'],u['id']),True):return self.sendj(403,{})
   q('update channels set topic=? where id=?',(b.get('topic',c['topic']),c['id'])); c=q('select * from channels where id=?',(c['id'],),True); return self.sendj(200,{'channel':ch(c)})
  if p=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
   if 'display_name' in b and len(b['display_name'])>200:return self.sendj(400,{})
   if 'timezone' in b:
    try: ZoneInfo(b['timezone'])
    except Exception: return self.sendj(400,{})
   for k,v in b.items():
    if k in allowed:q('update users set '+k+'=? where id=?',(v,u['id']))
   x=q('select * from users where id=?',(u['id'],),True); event_append({'type':'user.updated','user':user(x)}); return self.sendj(200,{'user':user(x)})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   x=q('select * from messages where id=?',(m.group(1),),True)
   if not x:return self.sendj(404,{})
   if x['deleted']:return self.sendj(409,{})
   if x['author_id']!=u['id']:return self.sendj(403,{})
   q('update messages set body=?,edited_at=? where id=?',(b.get('body',''),now(),m.group(1))); x=q('select * from messages where id=?',(m.group(1),),True); return self.sendj(200,{'message':msg(x)})
  return self.sendj(404,{})
 def do_DELETE(self):
  u=self.auth(); p=self.reqpath()
  if not u:return self.sendj(401,{})
  im=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if im:
   w=q('select * from workspaces where slug=?',(im.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
   q('delete from invitations where workspace_id=? and code=?',(w['id'],im.group(2)))
   return self.sendj(200,{'deleted':True})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if gm:
   w=q('select * from workspaces where slug=?',(gm.group(1),),True); g=q('select * from groups_ where workspace_id=? and handle=?',(w['id'],gm.group(2)),True) if w else None
   if not g:return self.sendj(200,{'deleted':True})
   role=q('select role from members where workspace_id=? and user_id=?',(w['id'],u['id']),True)
   if u['id']!=g['creator_id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   q('delete from groups_ where id=?',(g['id'],)); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   x=q('select m.*,c.workspace_id,w.owner_id from messages m join channels c on c.id=m.channel_id join workspaces w on w.id=c.workspace_id where m.id=?',(m.group(1),),True)
   if not x:return self.sendj(200,{'deleted':True})
   if x['author_id']!=u['id'] and x['owner_id']!=u['id']:return self.sendj(403,{})
   q('update messages set deleted=1 where id=?',(m.group(1),)); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/pin$',p)
  if m:
   if not q('select id from messages where id=?',(m.group(1),),True):return self.sendj(404,{})
   q('delete from pins where message_id=?',(m.group(1),)); return self.sendj(200,{'unpinned':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.*)$',p)
  if m:
   x=q('select * from messages where id=?', (m.group(1),),True)
   if not x:return self.sendj(404,{})
   q('delete from reactions where message_id=? and user_id=? and emoji=?',(m.group(1),u['id'],urllib.parse.unquote(m.group(2))))
   x=q('select * from messages where id=?',(m.group(1),),True); return self.sendj(200,{'reactions':msg(x)['reactions']})
  return self.sendj(404,{})
 def html(self):
  b='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:15px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;background:#007a5a}.app{display:flex;height:100vh}.side{width:240px;background:#3f0f3f;color:white;padding:20px}.main{flex:1;padding:25px}input,textarea{padding:10px;margin:5px}#auth-modal{display:block} </style></head><body><div class=app><aside class=side><h2>Huddle</h2><div data-testid=workspace-header>Workspace</div><div data-testid=channel-list></div></aside><main class=main><section id=auth-modal><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary>Login / Sign up</button></form></section><h1 data-testid=channel-title>Welcome</h1><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button></main></div></body></html>'''
  x=b.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(x))); self.end_headers(); self.wfile.write(x)
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
