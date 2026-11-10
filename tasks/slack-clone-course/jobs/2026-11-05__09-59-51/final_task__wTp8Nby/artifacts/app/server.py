import os,sys,json,fcntl,re,sqlite3,hashlib,secrets,threading,time,base64,struct,socket,select,datetime,urllib.parse,cgi,shutil
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False,isolation_level=None); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000')
 try: c.execute('PRAGMA journal_mode=WAL')
 except sqlite3.OperationalError: pass
 return c
db=conn(); lock=threading.RLock()
def init():
 with lock:
  db.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
CREATE TABLE IF NOT EXISTS msgfiles(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,data TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT, name TEXT, creator_id INTEGER, UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS presence(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));'''); db.commit()
init()
clients=[]; clients_lock=threading.Lock(); event_cursor={}; event_cursor_lock=threading.Lock()
for _r in db.execute('SELECT id FROM channels'):
 event_cursor[_r['id']]=db.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(_r['id'],)).fetchone()['n']
def rowdict(r): return dict(r) if r else None
def userobj(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channelobj(r):
 d=dict(r); d['is_private']=bool(d.get('is_private')); d['is_dm']=bool(d.get('is_dm')); d['is_archived']=bool(d.get('is_archived')); return d
def emit(ch,kind,msg=None,extra=None):
 with open('/app/huddle.event.lock','w') as lk:
  fcntl.flock(lk,fcntl.LOCK_EX)
  with lock:
   x=db.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
   data=extra or {}; data.update({'type':kind,'channel_id':ch,'event_id':x})
   db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch,x,kind,msg,json.dumps(data))); db.commit()
   os.makedirs('/tmp/huddle_events',exist_ok=True)
   tmp='/tmp/huddle_events/.%s.%s.%s'%(ch,x,os.getpid())
   with open(tmp,'w') as ef: ef.write(json.dumps(data))
   os.replace(tmp,'/tmp/huddle_events/%s.%s'%(ch,x))
 with event_cursor_lock: event_cursor[ch]=max(event_cursor.get(ch,0),x)
 return x
def mentions_for(r):
 body=r['body'] or ''; ch=r['channel_id']; author=r['author_id']; out=[]
 members={x['user_id'] for x in db.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(ch,))}
 for x in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
  if x in ('here','channel'):
   if x=='here':
    online={z['user_id'] for z in db.execute('SELECT user_id FROM presence WHERE channel_id=?',(ch,))}
    try:
     online |= {int(x.split('.')[0]) for x in os.listdir('/tmp/huddle_presence') if x.endswith('.'+str(ch))}
    except: pass
    ids=members & online
   else: ids=members
  else:
   g=db.execute('SELECT id FROM groups WHERE handle=? AND workspace_id=(SELECT workspace_id FROM channels WHERE id=?)',(x,ch)).fetchone()
   if g: ids={z['user_id'] for z in db.execute('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))}
   else:
    z=db.execute('SELECT id FROM users WHERE username=?',(x,)).fetchone(); ids={z['id']} if z else set()
   ids &= members
  out.extend(i for i in ids if i!=author)
 return list(dict.fromkeys(out))
def message_obj(r):
 if not r:return None
 u=db.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 fs=[dict(x) for x in db.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM msgfiles WHERE message_id=?)',(r['id'],))]
 rx=[]
 for e in db.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[x['user_id'] for x in db.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],e['emoji']))]
  rx.append({'emoji':e['emoji'],'count':e['n'],'user_ids':ids})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':db.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rx,'mentions':mentions_for(r)}
class WS:
 def __init__(self,s,u):
  self.s=s; self.u=u; self.subs=set(); self.baseline={}; self.alive=True
 def send(self,o):
  try:
   b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
   h=bytes([0x81,n]) if n<126 else bytes([0x81,126])+struct.pack('>H',n)
   self.s.sendall(h+b)
  except: self.alive=False
def event_poller():
 os.makedirs('/tmp/huddle_events',exist_ok=True)
 seen=set(os.listdir('/tmp/huddle_events'))
 while True:
  try:
   files=[]
   for fn in os.listdir('/tmp/huddle_events'):
    if fn.startswith('.') or fn in seen: continue
    a=fn.split('.')
    if len(a)==2:
     try: files.append((int(a[0]),int(a[1]),fn))
     except: pass
   for ch,x,fn in sorted(files):
    try:
     with open('/tmp/huddle_events/'+fn) as ef: data=json.load(ef)
     with clients_lock:
      targets=[c for c in list(clients) if ch in c.subs and x>c.baseline.get(ch,0)]
     for c in targets: c.send(data)
     seen.add(fn)
    except Exception:
     pass
  except Exception:
   pass
  time.sleep(0.02)
threading.Thread(target=event_poller,daemon=True).start()
def notify_user(u):
 with clients_lock:
  for c in clients:c.send({'type':'user.updated','user':userobj(u)})
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,code,o,headers=None):
  b=json.dumps(o,separators=(',',':')).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); 
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def auth(self):
  h=self.headers.get('Authorization','')
  if not h.startswith('Bearer '):return None
  return db.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone()
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or '{}')
 def route(self):
  p=urllib.parse.urlparse(self.path); return p.path,p.query
 def do_GET(self):
  p,q=self.route()
  if p=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if p=='/':return self.page()
  if p=='/api/ws':return self.websocket(urllib.parse.parse_qs(q).get('token',[''])[0])
  u=self.auth()
  if p.startswith('/api/auth/me'): return self.sendj(200,{'user':userobj(u)}) if u else self.sendj(401,{})
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/users/(\d+)',p)
  if m:
   x=db.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone();return self.sendj(200,{'user':userobj(x)}) if x else self.sendj(404,{})
  m=re.match(r'/api/files/(\d+)(/download)?',p)
  if m:
   x=db.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if m.group(2):
    self.send_response(200);self.send_header('Content-Type',x['content_type']);self.send_header('Content-Disposition','attachment; filename="'+x['filename']+'"');self.send_header('Content-Length',x['size']);self.end_headers();self.wfile.write(x['data']);return
   return self.sendj(200,{'file':{k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=int(m.group(1)); x=db.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not x:return self.sendj(404,{})
   if x['is_dm'] and not db.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch,u['id'])).fetchone():return self.sendj(403,{})
   rows=db.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(ch,)).fetchall()
   return self.sendj(200,{'messages':[message_obj(r) for r in rows],'next_cursor':None})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=db.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   if not db.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   gs=[]
   for g in db.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)):
    d=dict(g); d['member_user_ids']=[z['user_id'] for z in db.execute('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))]; gs.append(d)
   return self.sendj(200,{'groups':gs})
  m=re.match(r'/api/channels/(\d+)/read$',p)
  if m:
   ch=int(m.group(1)); n=db.execute('SELECT COUNT(*) n FROM messages WHERE channel_id=? AND deleted=0',(ch,)).fetchone()['n']
   mc=sum(1 for r in db.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0',(ch,)) if u['id'] in mentions_for(r))
   return self.sendj(200,{'read_state':{'channel_id':ch,'last_read_event_id':0,'unread_count':n,'mention_count':mc}})
  m=re.match(r'/api/workspaces/([^/]+)',p)
  if m:
   w=db.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   cs=[channelobj(x) for x in db.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],))]
   return self.sendj(200,{'workspace':dict(w),'channels':cs,'read_state':{}})
  if p=='/api/workspaces': return self.sendj(200,{'workspaces':[dict(x) for x in db.execute('SELECT * FROM workspaces WHERE owner_id=?',(u['id'],))]})
  return self.sendj(404,{})
 def do_POST(self):
  p,q=self.route(); u=self.auth()
  if p=='/api/files' and self.headers.get('Content-Type','').lower().startswith('multipart/'): b={}
  else: b=self.body()
  if p=='/api/auth/register':
   try:
    name=b['username']; pw=b['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8:raise ValueError()
    with lock:
     cur=db.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(name,pw,b.get('display_name',name),'UTC','','','')); uid=cur.lastrowid;db.commit()
    t=secrets.token_urlsafe(24);db.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));db.commit()
    return self.sendj(201,{'user':userobj(db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
   except:return self.sendj(400,{})
  if p=='/api/auth/login':
   x=db.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
   if not x:return self.sendj(401,{})
   t=secrets.token_urlsafe(24);db.execute('INSERT INTO tokens VALUES(?,?)',(t,x['id']));db.commit();return self.sendj(200,{'user':userobj(x),'token':t})
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=db.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   if not db.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   try:
    cur=db.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],b['handle'],b.get('name',b['handle']),u['id'])); gid=cur.lastrowid
    for uid in b.get('user_ids',[]): db.execute('INSERT OR IGNORE INTO group_users VALUES(?,?)',(gid,uid))
    db.commit(); g=db.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone(); d=dict(g); d['member_user_ids']=[z['user_id'] for z in db.execute('SELECT user_id FROM group_users WHERE group_id=?',(gid,))]; return self.sendj(201,{'group':d})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels',p)
  if m:
   w=db.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   try:
    cur=db.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic','')));ch=cur.lastrowid;db.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));db.commit();return self.sendj(201,{'channel':channelobj(db.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())})
   except:return self.sendj(409,{})
  if p=='/api/workspaces':
   try:
    cur=db.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,"open")',(b['slug'],b['name'],u['id']));wid=cur.lastrowid;db.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));db.execute('INSERT INTO channels(workspace_id,name) VALUES(?,"general")',(wid,));gid=db.execute('SELECT last_insert_rowid() x').fetchone()['x'];db.execute('INSERT INTO cmembers VALUES(?,?)',(gid,u['id']));db.commit();return self.sendj(201,{'workspace':dict(db.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':dict(db.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone())})
   except:return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/join',p)
  if m:
   ch=int(m.group(1));x=db.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not x:return self.sendj(404,{})
   db.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch,u['id']));db.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));db.commit();return self.sendj(200,{'channel':dict(x)})
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=int(m.group(1));x=db.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not x:return self.sendj(404,{})
   if not db.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch,u['id'])).fetchone():return self.sendj(403,{})
   body=b.get('body','')
   if not body:return self.sendj(400,{})
   if body.startswith('/') and not body.startswith('//'):
    if body.startswith('/invite '):
     target=body[8:].strip()
     if target.startswith('@'):target=target[1:]
     tu=db.execute('SELECT * FROM users WHERE username=?',(target,)).fetchone()
     if not tu:return self.sendj(404,{})
     role=db.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(x['workspace_id'],u['id'])).fetchone()
     if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
     db.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch,tu['id']));db.commit()
     return self.sendj(201,{'message':None,'invited_user':userobj(tu)})
    if body in ('/archive','/unarchive'):
     role=db.execute('SELECT owner_id FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
     if not role or role['owner_id']!=u['id']:return self.sendj(403,{})
     archived=1 if body=='/archive' else 0
     db.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,ch));db.commit()
     return self.sendj(201,{'message':None,'channel':channelobj(db.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())})
    if x['is_archived']:return self.sendj(423,{})
    if body.startswith('/me') and not body[3:].strip():return self.sendj(400,{})
    if body.startswith('/shrug'):body=body[6:].strip()+' ¯\\_(ツ)_/¯';body=body.strip()
    elif body.startswith('/topic '):
     db.execute('UPDATE channels SET topic=? WHERE id=?',(body[7:],ch));db.commit();return self.sendj(201,{'message':None,'channel':channelobj(db.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())})
    elif body.startswith('/me '):pass
    elif not body.startswith('/me'):return self.sendj(400,{})
   if x['is_archived']:return self.sendj(423,{})
   fids=b.get('file_ids',[])
   if not isinstance(fids,list):return self.sendj(400,{})
   for fid in fids:
    fx=db.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not fx or fx['uploader_id']!=u['id'] or db.execute('SELECT 1 FROM msgfiles WHERE file_id=?',(fid,)).fetchone():return self.sendj(400,{})
   cur=db.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch,u['id'],body,b.get('parent_id'),now()));mid=cur.lastrowid
   for fid in fids: db.execute('INSERT INTO msgfiles VALUES(?,?)',(mid,fid))
   db.commit(); mo=message_obj(db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone());emit(ch,'message.reply' if b.get('parent_id') else 'message.created',mid,{'message':mo});return self.sendj(201,{'message':mo})
  m=re.match(r'/api/messages/(\d+)/reactions(?:/(.*))?',p)
  if m:
   mid=int(m.group(1));r=db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
   if not r:return self.sendj(404,{})
   emoji=(b.get('emoji') if not m.group(2) else urllib.parse.unquote(m.group(2)))
   if not emoji:return self.sendj(400,{})
   db.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji));db.commit();return self.sendj(200,{'reactions':message_obj(r)['reactions']})
  if p=='/api/dms':
   other=db.execute('SELECT id FROM users WHERE id=?',(b.get('user_id'),)).fetchone()
   if not other:return self.sendj(404,{})
   a,bb=sorted((u['id'],other['id']));x=db.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,bb)).fetchone()
   if x:return self.sendj(200,{'channel_id':x['channel_id']})
   db.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{bb}',));ch=db.execute('SELECT last_insert_rowid() x').fetchone()['x'];db.execute('INSERT INTO dms VALUES(?,?,?)',(a,bb,ch));db.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(ch,a,ch,bb));db.commit();return self.sendj(200,{'channel_id':ch})
  if p=='/api/files':
   ct=self.headers.get('Content-Type',''); n=int(self.headers.get('Content-Length','0'))
   if n>11000000:return self.sendj(413,{})
   raw=self.rfile.read(n)
   mm=re.search(r'boundary=([^;]+)',ct)
   if not mm:return self.sendj(400,{})
   boundary=('--'+mm.group(1).strip().strip(chr(34))).encode(); parts=raw.split(boundary); found=None
   for part in parts:
    if b' name="file"' not in part:continue
    z=part[2:] if part.startswith(b'\r\n') else part; sep=z.find(b'\r\n\r\n')
    if sep<0:continue
    hd=z[:sep].decode('utf8','ignore'); data=z[sep+4:]; data=data[:-2] if data.endswith(b'\r\n') else data
    fn=re.search(r'filename="([^"]*)"',hd); typ=re.search(r'Content-Type:\s*([^\r\n]+)',hd,re.I)
    if fn: found=(fn.group(1),typ.group(1).strip() if typ else 'application/octet-stream',data);break
   if not found:return self.sendj(400,{})
   filename,ctype,data=found
   if len(data)>10485760:return self.sendj(413,{})
   db.execute('INSERT INTO files(uploader_id,filename,content_type,size,data,created_at) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),data,now()));fid=db.execute('SELECT last_insert_rowid() x').fetchone()['x'];db.commit();x=db.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();return self.sendj(201,{'file':{k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  return self.sendj(404,{})
 def do_PATCH(self):
  p,q=self.route(); u=self.auth()
  if not u:return self.sendj(401,{})
  b=self.body()
  if p=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
   if 'display_name' in b and (not isinstance(b['display_name'],str) or len(b['display_name'])>200):return self.sendj(400,{})
   if 'timezone' in b:
    try: import zoneinfo; zoneinfo.ZoneInfo(b['timezone'])
    except:return self.sendj(400,{})
   vals={k:b[k] for k in allowed if k in b}
   if vals:
    db.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],));db.commit()
   x=db.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();notify_user(x);return self.sendj(200,{'user':userobj(x)})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=db.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); g=db.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:return self.sendj(404,{})
   role=db.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   with open('/app/huddle.write.lock','w') as lk:
    fcntl.flock(lk,fcntl.LOCK_EX)
    if 'name' in b: db.execute('UPDATE groups SET name=? WHERE id=?',(b['name'],g['id']))
    if 'user_ids' in b:
     db.execute('DELETE FROM group_users WHERE group_id=?',(g['id'],))
     for uid in b['user_ids']: db.execute('INSERT OR IGNORE INTO group_users VALUES(?,?)',(g['id'],uid))
    db.commit()
   g=db.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone();d=dict(g);d['member_user_ids']=[z['user_id'] for z in db.execute('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))];return self.sendj(200,{'group':d})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   x=db.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if x['deleted']:return self.sendj(409,{})
   if x['author_id']!=u['id']:return self.sendj(403,{})
   if not isinstance(b.get('body'),str) or not b['body']:return self.sendj(400,{})
   db.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),x['id']));db.commit()
   x=db.execute('SELECT * FROM messages WHERE id=?',(x['id'],)).fetchone();mo=message_obj(x);emit(x['channel_id'],'message.updated',x['id'],{'message':mo});return self.sendj(200,{'message':mo})
  m=re.match(r'/api/channels/(\d+)$',p)
  if m and 'topic' in b:
   x=db.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if len(b['topic'])>250:return self.sendj(400,{})
   db.execute('UPDATE channels SET topic=? WHERE id=?',(b['topic'],x['id']));db.commit();return self.sendj(200,{'channel':dict(db.execute('SELECT * FROM channels WHERE id=?',(x['id'],)).fetchone())})
  return self.sendj(404,{})
 def do_DELETE(self):
  p,q=self.route();u=self.auth()
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=db.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();g=db.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:return self.sendj(200,{'deleted':True})
   role=db.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   with open('/app/huddle.write.lock','w') as lk:
    fcntl.flock(lk,fcntl.LOCK_EX)
    db.execute('DELETE FROM group_users WHERE group_id=?',(g['id'],));db.execute('DELETE FROM groups WHERE id=?',(g['id'],));db.commit()
   return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.*)',p)
  if m:
   x=db.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   db.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(x['id'],u['id'],urllib.parse.unquote(m.group(2))));db.commit();return self.sendj(200,{'reactions':message_obj(x)['reactions']})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   x=db.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if x['deleted']:return self.sendj(200,{'deleted':True})
   if x['author_id']!=u['id']:
    w=db.execute('SELECT owner_id FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(x['channel_id'],)).fetchone()
    if not w or w['owner_id']!=u['id']:return self.sendj(403,{})
   db.execute('UPDATE messages SET deleted=1 WHERE id=?',(x['id'],));db.execute('DELETE FROM reactions WHERE message_id=?',(x['id'],));db.commit();emit(x['channel_id'],'message.deleted',x['id'],{'message_id':x['id']});return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self,tok):
  u=db.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone()
  if not u:return self.sendj(401,{})
  key=self.headers.get('Sec-WebSocket-Key',''); import hashlib
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  c=WS(self.connection,u['id'])
  for pr in db.execute('SELECT channel_id FROM cmembers WHERE user_id=?',(u['id'],)):
   db.execute('INSERT OR REPLACE INTO presence VALUES(?,?)',(pr['channel_id'],u['id']))
  db.commit()
  with clients_lock:clients.append(c)
  self.connection.settimeout(0.5)
  try:
   while c.alive:
    try:
     h=self.connection.recv(2)
     if not h:break
     while len(h)<2: h+=self.connection.recv(2-len(h))
     ln=h[1]&127
     if ln==126:ln=struct.unpack('>H',self.connection.recv(2))[0]
     mask=self.connection.recv(4); data=self.connection.recv(ln); data=bytes(data[i]^mask[i%4] for i in range(ln)); o=json.loads(data)
     if o.get('type')=='subscribe':
      ch=int(o['channel_id']);head=db.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n'];c.subs.add(ch);c.baseline[ch]=head;db.execute('INSERT OR REPLACE INTO presence VALUES(?,?)',(ch,u['id']));db.commit();os.makedirs('/tmp/huddle_presence',exist_ok=True);open('/tmp/huddle_presence/%s.%s'%(u['id'],ch),'w').close();c.send({'type':'subscribed','channel_id':ch,'head_event_id':head})
     elif o.get('type')=='resume':
      ch=int(o['channel_id']);since=int(o.get('since_event_id',0));c.subs.add(ch)
      for e in db.execute('SELECT data FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since,)):c.send(json.loads(e['data']))
      head=db.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n'];c.send({'type':'resumed','channel_id':ch,'head_event_id':head})
    except socket.timeout:continue
    except:break
  finally:
   c.alive=False
   with clients_lock:
    if c in clients:clients.remove(c)
 def page(self):
  html='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}</style></head><body><main data-testid="app"><form data-testid="workspace-create-form"><button data-testid="empty-state-create-workspace">Create workspace</button></form><form data-testid="join-workspace-form"><button data-testid="join-workspace-submit" data-button-role="primary">Join workspace</button></form><div id="workspace-settings-modal"></div><div id="channel-settings-modal"></div><form data-testid="auth-form" id="auth-modal"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary">Login</button><button data-testid="auth-toggle">Sign up</button></form><aside data-testid="channel-list"></aside><aside data-testid="dms-list"></aside><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="logout-btn" data-button-role="danger">Logout</button><section data-testid="workspace-header"><span data-testid="current-user"></span></section><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace</button><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary">Save</button><h2 data-testid="channel-title"></h2><span data-testid="channel-topic"></span><button data-testid="channel-settings-btn" data-button-role="secondary">Channel</button><button data-testid="channel-settings-close" data-button-role="secondary">Close</button><h3 data-testid="channel-settings-title"></h3><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button><form data-testid="create-channel-form" id="create-channel-modal"><input><input type="checkbox" data-testid="create-channel-private"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form><div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary">Send</button><section data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></section><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form data-testid="create-invitation-form" id="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create</button></form><div data-testid="invitations-list"></div></main><script>localStorage.getItem("huddle.token");</script></body></html>'''
  b=html.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
def run():
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
if __name__=='__main__':run()
