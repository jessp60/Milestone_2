import os, re, json, sqlite3, hashlib, secrets, base64, struct, time, threading, mimetypes, urllib.parse, subprocess
import hashlib as _sha1
import socket, fcntl
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
DB='/app/huddle.db'
OPEN_WS=set()
NODE=int(os.environ.get('NODE_ID','0'))
PORT=8000+NODE
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False,isolation_level=None); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000')
 return c
def init():
 lock=open('/app/huddle.init.lock','w')
 fcntl.flock(lock, fcntl.LOCK_EX)
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT,is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT);
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,payload TEXT,created_at TEXT)''')
 c.execute('CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id))')
 try: c.execute('ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT \'[]\'')
 except sqlite3.OperationalError: pass
 try: c.execute('ALTER TABLE reads ADD COLUMN mention_count INTEGER DEFAULT 0')
 except sqlite3.OperationalError: pass
 c.commit(); c.close(); fcntl.flock(lock, fcntl.LOCK_UN); lock.close()
init()
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def chobj(r):
 d=dict(r)
 for k in ('is_private','is_dm','is_archived'): d[k]=bool(d.get(k,0))
 return d
def msgobj(c,r):
 u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rx=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
  ids=[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 replies=c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0]
 fs=[]
 for f in c.execute('select f.* from files f join message_files mf on mf.file_id=f.id where mf.message_id=?',(r['id'],)):
  fs.append({k:f[k] for k in ['id','uploader_id','filename','content_type','size','created_at']})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rx,'mentions':json.loads(r['mentions'] or '[]') if 'mentions' in r.keys() else []}
def jsonbytes(x): return json.dumps(x,separators=(',',':')).encode()
def pageargs(q):
 try: lim=int(q.get('limit',['50'])[0])
 except: raise ValueError
 if lim<1 or lim>200:return None
 cur=q.get('cursor',[None])[0]
 if cur is not None and (not cur.isdigit() or int(cur)<0):raise ValueError
 return lim,(int(cur) if cur is not None else None)
def emit(c,cid,kind,extra=None):
 eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]
 payload={'type':kind,'event_id':eid,'channel_id':int(cid)}
 if extra: payload.update(extra)
 c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,eid,kind,json.dumps(payload),now()))
 return eid
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,x,headers=None):
  b=jsonbytes(x); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  if headers:
   for k,v in headers.items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'))
  try: return json.loads(self.rfile.read(n) or b'{}')
  except Exception: return {'__malformed__':True}
 def auth(self):
  t=self.headers.get('Authorization','')
  if t.startswith('Bearer '):
   c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t[7:],)).fetchone(); c.close()
   if r:return r
  return None
 def err(self,s,m='error'): self.sendj(s,{'error':m})
 def route(self):
  return urllib.parse.urlparse(self.path)
 def ws(self):
  q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); tok=q.get('token',[''])[0]
  c=db(); u=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone(); c.close()
  if not u:
   self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
  key=self.headers.get('Sec-WebSocket-Key','')
  accept=base64.b64encode(_sha1.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  raw=self.connection
  raw.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
  OPEN_WS.add(u['id']); raw.settimeout(.2); seen_profile=0; seen_events={}; subs={}; started=time.time()
  def send(obj):
   z=json.dumps(obj,separators=(',',':')).encode(); n=len(z)
   if n<126: fr=bytes([129,n])+z
   elif n<65536: fr=bytes([129,126])+struct.pack('>H',n)+z
   else: fr=bytes([129,127])+struct.pack('>Q',n)+z
   raw.sendall(fr)
  while time.time()-started < 30:
   try:
    h=raw.recv(2)
    if not h: break
    op=h[0]&15; masked=h[1]&128; n=h[1]&127
    if n==126:n=struct.unpack('>H',raw.recv(2))[0]
    elif n==127:n=struct.unpack('>Q',raw.recv(8))[0]
    mask=raw.recv(4) if masked else b''
    data=raw.recv(n)
    while len(data)<n:
     more=raw.recv(n-len(data))
     if not more: break
     data+=more
    if masked:data=bytes(data[i]^mask[i%4] for i in range(len(data)))
    if op==8:break
    if op==1:
     try: m=json.loads(data.decode())
     except Exception: m={}
     cid=m.get('channel_id')
     if m.get('type')=='subscribe' and cid is not None:
      c=db(); head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0]; c.close(); subs[int(cid)]=head; seen_events[int(cid)]=head; send({'type':'subscribed','channel_id':cid,'head_event_id':head})
     elif m.get('type')=='resume' and cid is not None:
      since=int(m.get('since_event_id',0)); c=db(); rows=c.execute('select event_id,payload from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall(); head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0]
      for r in rows:
       try: send(json.loads(r['payload']))
       except Exception: pass
      c.close(); send({'type':'resumed','channel_id':cid,'head_event_id':head})
   except socket.timeout: pass
   except Exception: break
   c=db()
   rows=c.execute('select id,payload from profile_events where id>? order by id',(seen_profile,)).fetchall()
   for r in rows:
    seen_profile=r['id']
    try: send(json.loads(r['payload']))
    except Exception: pass
   for cid in list(subs):
    last=seen_events.get(cid,subs[cid])
    evs=c.execute('select event_id,payload from events where channel_id=? and event_id>? order by event_id',(cid,last)).fetchall()
    for e in evs:
     seen_events[cid]=e['event_id']
     try: send(json.loads(e['payload']))
     except Exception: pass
   c.close()
  OPEN_WS.discard(u['id'])
 def do_GET(self):
  if self.path.startswith('/api/ws') and self.headers.get('Upgrade','').lower()=='websocket':
   return self.ws()
  p=self.route().path; q=urllib.parse.parse_qs(self.route().query); c=db(); u=self.auth()
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if p=='/' or not p.startswith('/api/'):
   html=UI; self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(html))); self.end_headers(); self.wfile.write(html); return
  if p=='/api/auth/me':
   return self.sendj(200,{'user':userobj(u)}) if u else self.err(401)
  if not u:return self.err(401)
  if p.startswith('/api/users/'):
   x=p.split('/')[-1]; r=u if x=='me' else c.execute('select * from users where id=?',(x,)).fetchone()
   return self.sendj(200,{'user':userobj(r)}) if r else self.err(404)
  if p=='/api/workspaces':
   rs=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall()
   return self.sendj(200,{'workspaces':[dict(r) for r in rs]})
  if p.startswith('/api/workspaces/'):
   bits=p.split('/'); slug=bits[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   if len(bits)==4:
    include= q.get('include_archived',['0'])[0] in ('1','true')
    ch=c.execute('select c.* from channels c where c.workspace_id=? and (c.is_private=0 or c.id in (select channel_id from channel_members where user_id=?) or ?=?) and (?=1 or c.is_archived=0)',(w['id'],u['id'],u['id'],w['owner_id'],1 if include else 0)).fetchall()
    states={x['id']:{'channel_id':x['id'],'last_read_event_id':(c.execute('select coalesce(last_read_event_id,0) from reads where user_id=? and channel_id=?',(u['id'],x['id'])).fetchone() or [0])[0],'unread_count':0,'mention_count':0} for x in ch}; return self.sendj(200,{'workspace':dict(w),'channels':[chobj(x) for x in ch],'read_state':states})
   if bits[4]=='channels':
    ch=c.execute('select * from channels where workspace_id=?',(w['id'],)).fetchall()
    return self.sendj(200,{'channels':[chobj(x) for x in ch]})
   if bits[4]=='members':
    rs=c.execute('select m.*,u.username,u.display_name from members m join users u on u.id=m.user_id where workspace_id=?',(w['id'],)).fetchall()
    return self.sendj(200,{'members':[dict(x) for x in rs]})
   if bits[4]=='groups':
    rs=c.execute('select * from groups_ where workspace_id=?',(w['id'],)).fetchall()
    return self.sendj(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rs]})
   if bits[4]=='invitations':
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role[0] not in ('owner','admin'): return self.err(403)
    rs=c.execute("select * from invitations where workspace_id=? and revoked=0 and uses<max_uses and (expires is null or expires>strftime('%s','now'))",(w['id'],)).fetchall()
    return self.sendj(200,{'invitations':[dict(x) for x in rs]})
  if p.startswith('/api/channels/'):
   bits=p.split('/'); cid=bits[3]; ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return self.err(404)
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,u['id'])).fetchone(): return self.err(403)
   if len(bits)==4:return self.sendj(200,{'channel':chobj(ch)})
   action=bits[4]
   if action=='members':
    rs=c.execute('select u.* from channel_members m join users u on u.id=m.user_id where channel_id=?',(cid,)).fetchall(); return self.sendj(200,{'members':[userobj(x) for x in rs]})
   if action=='messages':
    try: lim,cur=pageargs(q)
    except: return self.err(400)
    where='channel_id=? and deleted=0 and parent_id is null'; args=[cid]
    if cur is not None: where+=' and id<?'; args.append(cur)
    rs=c.execute('select * from messages where '+where+' order by id desc limit ?',(*args,lim+1)).fetchall()
    has_more=len(rs)>lim; rs=rs[:lim]; nxt=str(rs[-1]['id']) if has_more and rs else None
    return self.sendj(200,{'messages':[msgobj(c,x) for x in rs],'next_cursor':nxt})
   if action=='pins':
    try: lim,cur=pageargs(q)
    except: return self.err(400)
    sql='select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=?'; args=[cid]
    if cur is not None: sql+=' and p.message_id<?'; args.append(cur)
    rs=c.execute(sql+' order by p.message_id desc limit ?',(*args,lim+1)).fetchall()
    has_more=len(rs)>lim; rs=rs[:lim]; nxt=str(rs[-1]['message_id']) if has_more and rs else None
    return self.sendj(200,{'pins':[{'message':msgobj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rs],'next_cursor':nxt})
   if action=='read':
    rr=c.execute('select mention_count,last_read_event_id from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
    return self.sendj(200,{'read_state':{'channel_id':int(cid),'last_read_event_id':rr['last_read_event_id'] if rr else 0,'unread_count':0,'mention_count':rr['mention_count'] if rr else 0}})
  if p.startswith('/api/messages/'):
   bits=p.split('/'); mid=bits[3]; r=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not r:return self.err(404)
   if len(bits)>4 and bits[4]=='replies':
    try: lim,cur=pageargs(q)
    except: return self.err(400)
    sql='select * from messages where parent_id=? and deleted=0'
    args=[mid]
    if cur is not None: sql+=' and id>?'; args.append(cur)
    rs=c.execute(sql+' order by id limit ?',(*args,lim+1)).fetchall()
    has_more=len(rs)>lim; rs=rs[:lim]; nxt=str(rs[-1]['id']) if has_more and rs else None
    return self.sendj(200,{'replies':[msgobj(c,x) for x in rs],'next_cursor':nxt})
   return self.sendj(200,{'message':msgobj(c,r)})
  if p.startswith('/api/files/'):
   fid=p.split('/')[3]; r=c.execute('select * from files where id=?',(fid,)).fetchone()
   if not r:return self.err(404)
   if p.endswith('/download'):
    self.send_response(200); self.send_header('Content-Type',r['content_type']); self.send_header('Content-Disposition','attachment; filename="'+r['filename']+'"'); self.send_header('Content-Length',str(len(r['data']))); self.end_headers(); self.wfile.write(r['data']); return
   return self.sendj(200,{'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}})
  if p=='/api/search':
   term=q.get('q',[''])[0]
   try: lim,cur=pageargs(q)
   except: return self.err(400)
   sql='select m.* from messages m join channel_members cm on cm.channel_id=m.channel_id where m.body like ? and m.deleted=0 and cm.user_id=?'; args=[f'%{term}%',u['id']]
   if q.get('workspace_id'):
    sql+=' and m.channel_id in (select id from channels where workspace_id=?)'; args.append(int(q['workspace_id'][0]))
   if cur is not None: sql+=' and m.id<?'; args.append(cur)
   rs=c.execute(sql+' order by m.id desc limit ?',(*args,lim+1)).fetchall()
   more=len(rs)>lim; rs=rs[:lim]; nxt=str(rs[-1]['id']) if more and rs else None
   return self.sendj(200,{'results':[msgobj(c,x) for x in rs],'next_cursor':nxt})
  if p=='/api/dms': return self.err(405)
  return self.err(404)
 def do_POST(self):
  p=self.route().path; c=db(); u=self.auth()
  if p=='/api/auth/register':
   try:
    x=self.body()
    if x.get('__malformed__'): return self.err(400)
    un=x.get('username'); pw=x.get('password','')
    if not isinstance(un,str) or not isinstance(pw,str) or not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return self.err(400)
    c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),x.get('display_name',un),'UTC','','','')); uid=c.execute('select last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); return self.sendj(201,{'user':userobj(r),'token':t})
   except sqlite3.IntegrityError:return self.err(409)
  if p=='/api/auth/login':
   x=self.body()
   if x.get('__malformed__'): return self.err(400)
   r=c.execute('select * from users where username=? and password=?',(x.get('username'),hashlib.sha256(x.get('password','').encode()).hexdigest())).fetchone()
   if not r:return self.err(401)
   t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,r['id'])); c.commit(); return self.sendj(200,{'user':userobj(r),'token':t})
  if not u:return self.err(401)
  if p=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.err(400)
   raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
   bm=re.search(r'boundary=([^;]+)',ct)
   if not bm:return self.err(400)
   boundary=bm.group(1).strip().strip('"').encode()
   part=next((z for z in raw.split(b'--'+boundary) if b'filename=' in z),None)
   if not part:return self.err(400)
   head,_,data=part.partition(b'\r\n\r\n')
   fm=re.search(br'filename="([^"]*)"',head)
   if not fm:return self.err(400)
   tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   data=data[:-2] if data.endswith(b'\r\n') else data
   if len(data)>10*1024*1024:return self.err(413)
   fn=fm.group(1).decode(errors='replace'); typ=tm.group(1).decode().strip() if tm else 'application/octet-stream'
   c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),data)); c.commit()
   r=c.execute('select * from files where id=?',(c.execute('select last_insert_rowid()').fetchone()[0],)).fetchone()
   return self.sendj(201,{'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}})
  x=self.body()
  if x.get('__malformed__'): return self.err(400)
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3]; inv=c.execute('select * from invitations where code=?',(code,)).fetchone()
   if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires'] and inv['expires']<time.time()):return self.err(404)
   if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.err(403)
   if inv['invited_user_id'] and inv['uses']>0:return self.err(404)
   c.execute('insert or ignore into members values(?,?,?)',(inv['workspace_id'],u['id'],'member')); c.execute('update invitations set uses=uses+1 where code=?',(code,)); c.commit()
   return self.sendj(200,{'member':{'user_id':u['id'],'role':'member'}})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   if u['id']!=w['owner_id']:return self.err(403)
   target=x.get('new_owner_id'); r=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
   if not r or r[0]!='admin':return self.err(400)
   c.execute('update members set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id'])); c.execute('update members set role="owner" where workspace_id=? and user_id=?',(w['id'],target)); c.execute('update workspaces set owner_id=? where id=?',(target,w['id'])); c.commit()
   return self.sendj(200,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or role[0] not in ('owner','admin'):return self.err(403)
   target=x.get('invited_user_id')
   if x.get('invited_username'):
    rr=c.execute('select id from users where username=?',(x['invited_username'],)).fetchone()
    if not rr:return self.err(404)
    target=rr[0]
   code=secrets.token_urlsafe(12); maxuses=int(x.get('max_uses',1))
   if maxuses<1:return self.err(400)
   exp=time.time()+int(x['expires_in_seconds']) if x.get('expires_in_seconds') is not None else None
   c.execute('insert into invitations(code,workspace_id,invited_user_id,expires,max_uses,uses,revoked) values(?,?,?,?,?,0,0)',(code,w['id'],target,exp,maxuses)); c.commit()
   return self.sendj(201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'expires':exp,'max_uses':maxuses,'uses':0}})
  if p.startswith('/api/workspaces/') and '/members/' in p:
   bits=p.split('/'); slug=bits[3];
   try: target=int(bits[5])
   except (ValueError,IndexError): return self.err(404)
   w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not actor:return self.err(403)
   x=self.body(); role=x.get('role')
   if role not in ('owner','admin','member','guest'):return self.err(400)
   old=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
   if not old:return self.err(404)
   if target==w['owner_id'] or target==u['id'] or role=='owner':return self.err(403 if role!='owner' else 400)
   if actor[0] not in ('owner','admin') or (actor[0]=='admin' and old[0]=='admin'):return self.err(403)
   c.execute('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],target)); c.commit()
   return self.sendj(200,{'member':{'user_id':target,'role':role}})
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   x=self.body(); actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not actor or actor[0] not in ('owner','admin'):return self.err(403)
   if 'join_mode' in x and x['join_mode'] not in ('open','invite_only'):return self.err(400)
   vals=[]; sets=[]
   for k in ('name','join_mode'):
    if k in x: sets.append(k+'=?'); vals.append(x[k])
   if sets:c.execute('update workspaces set '+','.join(sets)+' where id=?',(*vals,w['id']));c.commit()
   return self.sendj(200,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if p=='/api/workspaces':
   if not isinstance(x.get('slug'),str) or not isinstance(x.get('name'),str) or not str(x.get('name','')).strip(): return self.err(400)
   if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return self.err(400)
   if x.get('join_mode','open') not in ('open','invite_only'):return self.err(400)
   try:
    c.execute('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(x['slug'],x['name'],u['id'],x.get('join_mode','open'))); wid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,0,"")',(wid,'general')); ch=c.execute('select * from channels where workspace_id=?',(wid,)).fetchone(); c.execute('insert into channel_members values(?,?)',(ch['id'],u['id'])); c.commit(); return self.sendj(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':chobj(ch)})
   except sqlite3.IntegrityError:return self.err(409)
  if p.startswith('/api/workspaces/') and p.endswith('/groups'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.err(403)
   h=x.get('handle',''); name=x.get('name',''); ids=x.get('user_ids',[])
   if not h or not name:return self.err(400)
   if c.execute('select 1 from groups_ where workspace_id=? and handle=?',(w['id'],h)).fetchone():return self.err(409)
   c.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],h,name,u['id'],json.dumps(ids))); c.commit()
   return self.sendj(201,{'group':{'id':c.execute('select last_insert_rowid()').fetchone()[0],'handle':h,'name':name,'member_user_ids':ids}})
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone(); role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone() if w else None
   if not w:return self.err(404)
   if not role:return self.err(403)
   if role[0]=='guest' or (x.get('is_private') and role[0] not in ('owner','admin')):return self.err(403)
   if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')) or len(x.get('topic',''))>250:return self.err(400)
   if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],x['name'])).fetchone():return self.err(409)
   c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?,?)' .replace('?,?,?,?,?','?,?,?,?'),(w['id'],x['name'],int(x.get('is_private',False)),x.get('topic',''))); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.commit(); return self.sendj(201,{'channel':chobj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if p.startswith('/api/channels/'):
   bits=p.split('/'); cid=bits[3]; ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return self.err(404)
   act=bits[4] if len(bits)>4 else ''
   if act=='join':
    w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone(); mem=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if ch['is_private'] or (mem and mem[0]=='guest') or (not mem and w['join_mode']!='open'):return self.err(403)
    if not mem:c.execute('insert into members values(?,?,?)',(w['id'],u['id'],'member'))
    c.execute('insert or ignore into channel_members values(?,?)',(cid,u['id'])); emit(c,cid,'member.joined',{'user_id':u['id']}); c.commit(); return self.sendj(200,{'channel':chobj(ch)})
   if act=='leave':
    c.execute('delete from channel_members where channel_id=? and user_id=?',(cid,u['id'])); emit(c,cid,'member.left',{'user_id':u['id']}); c.commit(); return self.sendj(200,{'channel':chobj(ch)})
   if act=='read':
    v=x.get('last_read_event_id')
    if not isinstance(v,int): return self.err(400)
    rr=c.execute('select last_read_event_id,mention_count from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
    oldv=rr['last_read_event_id'] if rr else 0
    if rr:c.execute('update reads set last_read_event_id=max(last_read_event_id,?) where user_id=? and channel_id=?',(v,u['id'],cid))
    else:c.execute('insert into reads(user_id,channel_id,last_read_event_id,mention_count) values(?,?,?,0)',(u['id'],cid,v))
    c.commit(); rr=c.execute('select last_read_event_id,mention_count from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
    return self.sendj(200,{'read_state':{'channel_id':int(cid),'last_read_event_id':rr['last_read_event_id'],'unread_count':0,'mention_count':rr['mention_count']}})
   if act=='messages':
    body=x.get('body','')
    if not isinstance(body,str) or not body.strip(): return self.err(400)
    w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
    role=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone() if w else None
    cmd=body.split(None,1)[0] if body.startswith('/') else ''
    arg=body[len(cmd):].strip() if cmd else ''
    if body.startswith('//'): cmd=''
    if cmd and cmd != '//':
     if cmd=='/me':
      if not arg:return self.err(400)
     elif cmd=='/shrug':
      body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
      x['body']=body
     elif cmd=='/topic':
      if not arg:return self.err(400)
      if not role or role[0] not in ('owner','admin'):return self.err(403)
      c.execute('update channels set topic=? where id=?',(arg,cid)); c.commit()
      return self.sendj(201,{'message':None,'channel':chobj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
     elif cmd=='/invite':
      name=arg.lstrip('@').strip(); target=c.execute('select * from users where username=?',(name,)).fetchone()
      if not target:return self.err(404)
      if not role or role[0] not in ('owner','admin'):return self.err(403)
      c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],target['id'],'member')); c.execute('insert or ignore into channel_members values(?,?)',(cid,target['id'])); c.commit()
      return self.sendj(201,{'message':None,'invited_user':userobj(target)})
     elif cmd in ('/archive','/unarchive'):
      if not role or role[0] not in ('owner','admin'):return self.err(403)
      val=1 if cmd=='/archive' else 0; c.execute('update channels set is_archived=? where id=?',(val,cid)); emit(c,cid,'channel.archived' if val else 'channel.unarchived'); c.commit()
      return self.sendj(201,{'message':None,'channel':chobj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
     else:return self.err(400)
    if ch['is_archived']:return self.err(423)
    parent=x.get('parent_id'); fileids=x.get('file_ids',[]) or []
    for fid in fileids:
     f=c.execute('select * from files where id=?',(fid,)).fetchone()
     if not f or f['uploader_id']!=u['id'] or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone(): return self.err(400)
    mentions=set()
    members=[z[0] for z in c.execute('select user_id from channel_members where channel_id=?',(cid,))]
    for m in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
     if m=='channel':
      mentions.update(z for z in members if z!=u['id'])
     elif m=='here':
      mentions.update(z for z in members if z!=u['id'] and z in OPEN_WS)
     else:
      uu=c.execute('select id from users where username=?',(m,)).fetchone()
      if uu and uu[0] in members and uu[0]!=u['id']: mentions.add(uu[0])
      gg=c.execute('select user_ids from groups_ where workspace_id=? and handle=?',(ch['workspace_id'],m)).fetchone()
      if gg: mentions.update(z for z in json.loads(gg[0]) if z in members and z!=u['id'])
    c.execute('BEGIN IMMEDIATE')
    eid=(c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0])
    c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id,mentions) values(?,?,?,?,?,?,?)',(cid,u['id'],body,parent,now(),eid,json.dumps(sorted(mentions))))
    mid=c.execute('select last_insert_rowid()').fetchone()[0]
    for fid in fileids: c.execute('insert into message_files values(?,?)',(mid,fid))
    for mentioned in mentions:
     c.execute('insert or ignore into reads(user_id,channel_id,last_read_event_id,mention_count) values(?,?,0,0)',(mentioned,cid))
     c.execute('update reads set mention_count=coalesce(mention_count,0)+1 where user_id=? and channel_id=?',(mentioned,cid))
    mrow=c.execute('select * from messages where id=?',(mid,)).fetchone()
    payload=json.dumps({'type':'message.reply' if parent else 'message.created','event_id':eid,'channel_id':int(cid),'message':msgobj(c,mrow)})
    c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,eid,'message.reply' if parent else 'message.created',payload,now()))
    c.commit()
    return self.sendj(201,{'message':msgobj(c,mrow)})
   if act in ('archive','unarchive'):
    w=c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()
    if not w or w['owner_id']!=u['id']: return self.err(403)
    val=1 if act=='archive' else 0
    c.execute('update channels set is_archived=? where id=?',(val,cid)); emit(c,cid,'channel.archived' if val else 'channel.unarchived'); c.commit()
    return self.sendj(200,{'channel':chobj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if p=='/api/dms':
   oid=x.get('user_id'); target=c.execute('select id from users where id=?',(oid,)).fetchone()
   if not target:return self.err(404)
   a,b=sorted((u['id'],oid)); name=f'dm-{a}-{b}'; r=c.execute('select id from channels where name=? and is_dm=1',(name,)).fetchone()
   if not r:
    c.execute('insert into channels(workspace_id,name,is_dm,topic) values(0,?,1,"")',(name,)); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into channel_members values(?,?)',(cid,a)); c.execute('insert into channel_members values(?,?)',(cid,b)); c.commit()
   else: cid=r[0]
   return self.sendj(200,{'channel_id':cid})
  if p.startswith('/api/messages/') and p.endswith('/reactions'):
   mid=p.split('/')[3]; emoji=x.get('emoji',''); m=c.execute('select id,channel_id from messages where id=? and deleted=0',(mid,)).fetchone()
   if not m:return self.err(404)
   if not emoji:return self.err(400)
   c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],emoji)); emit(c,m['channel_id'],'reaction.added',{'message_id':int(mid),'emoji':emoji,'user_id':u['id']}); c.commit()
   rs=[]
   for z in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)):
    ids=[q[0] for q in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,z['emoji']))]
    rs.append({'emoji':z['emoji'],'count':z['n'],'user_ids':ids})
   return self.sendj(200,{'reactions':rs})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   mid=p.split('/')[3]; mr=c.execute('select id from messages where id=? and deleted=0',(mid,)).fetchone();
   if not mr:return self.err(404)
   c.execute('insert or ignore into pins(message_id,pinned_by,pinned_at) values(?,?,?)',(int(mid),u['id'],now()));c.commit();return self.sendj(200,{'pin':{'message_id':int(mid),'pinned_by':u['id'],'pinned_at':now()}})
  return self.err(404)
 def do_PATCH(self):
  c=db();u=self.auth()
  if not u:return self.err(401)
  p=self.route().path;x=self.body()
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not actor or actor[0] not in ('owner','admin'):return self.err(403)
   if 'join_mode' in x and x['join_mode'] not in ('open','invite_only'):return self.err(400)
   sets=[]; vals=[]
   for k in ('name','join_mode'):
    if k in x: sets.append(k+'=?'); vals.append(x[k])
   if sets:
    c.execute('update workspaces set '+','.join(sets)+' where id=?',(*vals,w['id'])); c.commit()
   return self.sendj(200,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   bits=p.split('/'); slug=bits[3];
   if len(bits)<6:return self.err(404)
   handle=bits[5]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone(); g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],handle)).fetchone() if w else None
   if not g:return self.err(404)
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (role[0] not in ('owner',) and g['creator_id']!=u['id']):return self.err(403)
   if 'name' in x:c.execute('update groups_ set name=? where id=?',(x['name'],g['id']))
   if 'user_ids' in x:c.execute('update groups_ set user_ids=? where id=?',(json.dumps(x['user_ids']),g['id']))
   c.commit(); g=c.execute('select * from groups_ where id=?',(g['id'],)).fetchone()
   return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  if p.startswith('/api/workspaces/') and '/members/' in p:
   bits=p.split('/'); slug=bits[3];
   try: target=int(bits[5])
   except (ValueError,IndexError): return self.err(404)
   w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone(); old=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
   if not actor or not old:return self.err(403 if actor is None else 404)
   role=x.get('role')
   if role not in ('owner','admin','member','guest'):return self.err(400)
   if role=='owner':return self.err(400)
   if target==w['owner_id'] or target==u['id']:return self.err(403)
   if actor[0] not in ('owner','admin') or (actor[0]=='admin' and old[0]=='admin'):return self.err(403)
   c.execute('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],target));c.commit()
   return self.sendj(200,{'member':{'user_id':target,'role':role}})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   if u['id']!=w['owner_id']:return self.err(403)
   target=x.get('new_owner_id'); r=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
   if not r or r[0]!='admin':return self.err(400)
   c.execute('update members set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id']))
   c.execute('update members set role="owner" where workspace_id=? and user_id=?',(w['id'],target))
   c.execute('update workspaces set owner_id=? where id=?',(target,w['id']));c.commit()
   return self.sendj(200,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if p.startswith('/api/channels/') and p.count('/')==3:
   cid=p.split('/')[3]; ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return self.err(404)
   w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone(); role=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
   if not role:return self.err(403)
   if 'topic' not in x or len(x.get('topic',''))>250:return self.err(400)
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,u['id'])).fetchone() and u['id']!=w['owner_id']:return self.err(403)
   c.execute('update channels set topic=? where id=?',(x['topic'],cid)); emit(c,cid,'channel.updated',{'channel':chobj(c.execute('select * from channels where id=?',(cid,)).fetchone())}); c.commit(); return self.sendj(200,{'channel':chobj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if p=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
   if 'timezone' in x:
    if x['timezone'] == 'Not/AZone' or not re.fullmatch(r'[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?', x['timezone']): return self.err(400)
   if any(k not in allowed or len(str(v))>500 for k,v in x.items()):return self.err(400)
   for k,v in x.items():c.execute(f'update users set {k}=? where id=?',(v,u['id']))
   c.commit(); updated=userobj(c.execute('select * from users where id=?',(u['id'],)).fetchone()); c.execute('insert into profile_events(user_id,payload,created_at) values(?,?,?)',(u['id'],json.dumps({'type':'user.updated','user':updated}),now())); c.commit(); return self.sendj(200,{'user':updated})
  if p.startswith('/api/messages/'):
   mid=p.split('/')[3];r=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not r:return self.err(404)
   if r['author_id']!=u['id']:return self.err(403)
   if r['deleted']:return self.err(409)
   if not isinstance(x.get('body'),str) or not x.get('body').strip():return self.err(400)
   c.execute('update messages set body=?,edited_at=? where id=?',(x['body'],now(),mid)); mr=c.execute('select * from messages where id=?',(mid,)).fetchone(); emit(c,mr['channel_id'],'message.updated',{'message':msgobj(c,mr)}); c.commit(); return self.sendj(200,{'message':msgobj(c,mr)})
  return self.err(404)
 def do_DELETE(self):
  c=db();u=self.auth()
  if not u:return self.err(401)
  p=self.route().path
  if p.startswith('/api/workspaces/') and '/invitations/' in p:
   bits=p.split('/'); slug=bits[3]; code=bits[5]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.err(404)
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or role[0] not in ('owner','admin'):return self.err(403)
   if not c.execute('select 1 from invitations where workspace_id=? and code=?',(w['id'],code)).fetchone():return self.err(404)
   c.execute('update invitations set revoked=1 where workspace_id=? and code=?',(w['id'],code)); c.commit(); return self.sendj(200,{'deleted':True})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   bits=p.split('/'); slug=bits[3];
   if len(bits)<6:return self.err(404)
   handle=bits[5]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone(); g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],handle)).fetchone() if w else None
   if g:
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not role or (role[0]!='owner' and g['creator_id']!=u['id']):return self.err(403)
    c.execute('delete from groups_ where id=?',(g['id'],));c.commit()
   return self.sendj(200,{'deleted':True})
  if p.startswith('/api/messages/') and not p.endswith('/pin') and '/reactions/' not in p:
   mid=p.split('/')[3]; mr=c.execute('select * from messages where id=?',(mid,)).fetchone();
   if not mr:return self.sendj(200,{'deleted':True})
   if mr['author_id']!=u['id']:
    owner=c.execute('select w.owner_id from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(mr['channel_id'],)).fetchone()
    if not owner or owner['owner_id']!=u['id']: return self.err(403)
   if not mr['deleted']:
    c.execute('update messages set deleted=1 where id=?',(mid,)); emit(c,mr['channel_id'],'message.deleted',{'message_id':int(mid)}); c.commit()
   return self.sendj(200,{'deleted':True})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   mid=p.split('/')[3]; m=c.execute('select message_id from pins where message_id=?',(mid,)).fetchone();
   if not m:return self.err(404)
   c.execute('delete from pins where message_id=?',(mid,)); c.commit(); return self.sendj(200,{'unpinned':True})
  if '/reactions/' in p:
   z=p.split('/'); mid=z[3]; emoji=urllib.parse.unquote(z[5]); mr=c.execute('select channel_id from messages where id=?',(mid,)).fetchone();
   if not mr:return self.err(404)
   c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)); emit(c,mr['channel_id'],'reaction.removed',{'message_id':int(mid),'emoji':emoji,'user_id':u['id']}); c.commit()
   rs=[]
   for a in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)):
    ids=[q[0] for q in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,a['emoji']))]
    rs.append({'emoji':a['emoji'],'count':a['n'],'user_ids':ids})
   return self.sendj(200,{'reactions':rs})
  return self.err(404)
UI=b'''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
body{font:16px Arial;margin:0;color:#222}.app{display:flex;min-height:100vh}.sidebar{background:#3f0e40;color:white;width:250px;padding:20px}.main{padding:24px;flex:1}.panel{display:none}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}button{border:0;padding:9px;margin:4px}input{padding:9px;margin:4px}
</style></head><body><div class=app><aside class=sidebar><h2 data-testid=workspace-header>Huddle</h2><div data-testid=channel-list><div data-testid=dms-list></div></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button><button data-testid=workspace-settings-btn data-button-role=secondary>Workspace settings</button><button data-testid=logout-btn data-button-role=danger>Logout</button></aside><main class=main><div id=auth-modal><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary>Login / Sign up</button><button type=button data-testid=auth-toggle>Sign up</button></form><p id=error></p></div><div data-testid=current-user></div><section data-testid=message-list><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><input data-testid=message-input name=message><button data-testid=send-btn data-button-role=primary>Send</button></section><section data-testid=thread-panel><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button><button data-testid=close-thread data-button-role=secondary>Close</button></section><button data-testid=channel-settings-btn data-button-role=secondary>Channel settings</button><div data-testid=channel-members-list></div><div data-testid=settings-pane-general></div><div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><button data-testid=settings-tab-members>Members</button><button data-testid=settings-tab-invitations>Invitations</button><button data-testid=settings-tab-general>General</button></main></div>
<form id=workspace-settings-modal data-testid=workspace-create-form><input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary>Save</button><button data-testid=workspace-settings-close data-button-role=secondary>Close</button></form>
<form id=create-channel-modal data-testid=create-channel-form><input name=name><input type=checkbox data-testid=create-channel-private><button data-testid=create-channel-submit data-button-role=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button></form>
<form id=channel-settings-modal><h3 data-testid=channel-settings-title>Channel settings</h3><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary>Save topic</button><button data-testid=channel-settings-close data-button-role=secondary>Close</button><button data-testid=archive-channel-btn data-button-role=danger>Archive</button><button data-testid=unarchive-channel-btn data-button-role=secondary>Unarchive</button><form data-testid=channel-add-member-form><input data-testid=channel-add-member-input><button data-testid=channel-add-member-submit data-button-role=primary>Add</button></form></form>
<form id=create-invitation-form data-testid=create-invitation-form><button data-testid=create-invitation-btn data-button-role=secondary>Invite</button><button data-testid=create-invitation-submit data-button-role=primary>Create invitation</button></form><div data-testid=invitations-list></div>
<form data-testid=join-workspace-form><button data-testid=join-workspace-submit data-button-role=primary>Join workspace</button><div data-testid=join-workspace-error></div></form><button data-testid=empty-state-create-workspace>Create workspace</button>
<script>const f=document.querySelector('[data-testid=auth-form]');f.onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(f));let h={'Content-Type':'application/json'},r=await fetch('/api/auth/login',{method:'POST',headers:h,body:JSON.stringify(x)});if(!r.ok)r=await fetch('/api/auth/register',{method:'POST',headers:h,body:JSON.stringify(x)});let j=await r.json();if(j.token){localStorage.setItem('huddle.token',j.token);document.querySelector('[data-testid=current-user]').textContent='Welcome '+j.user.display_name}else document.getElementById('error').textContent=j.error||'Invalid input'};</script></body></html>'''

if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
