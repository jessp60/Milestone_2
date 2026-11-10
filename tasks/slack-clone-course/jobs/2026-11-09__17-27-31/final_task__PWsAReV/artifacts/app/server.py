#!/usr/bin/env python3
import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,threading,time,datetime,urllib.parse, mimetypes, fcntl, zoneinfo, socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/huddle.db'
PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
lock=threading.RLock()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def initdb():
 with open('/app/.schema.lock','w') as lf:
  fcntl.flock(lf,fcntl.LOCK_EX)
  return _initdb_locked()
def _initdb_locked():
 c=sqlite3.connect(DB,timeout=60,check_same_thread=False,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=2500')
 c.executescript('''PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,user_ids TEXT);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,message_id INTEGER,created_at TEXT);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
 '''); c.close()
DB_READY=True

def db():
 c=sqlite3.connect(DB,timeout=60,check_same_thread=False,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=2500'); return c

initdb()
sockets={}; socklock=threading.Lock(); EVENTFILE='/app/ws-events.log'; ONLINEFILE='/app/ws-online.log'; open(EVENTFILE,'a').close(); open('/app/ws-subs.log','a').close(); open(ONLINEFILE,'a').close()
def jdump(x): return json.dumps(x,separators=(',',':'))
def user(r):
 return dict(r) if r else None
def uobj(r):
 d=user(r); return {k:d[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def token_user(h):
 if not h or not h.startswith('Bearer '): return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def table_exists(c,n): return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(n,)).fetchone() is not None
def chan(c,r):
 d=dict(r); d['is_private']=bool(d['is_private']); d['is_dm']=bool(d['is_dm']); d['is_archived']=bool(d['is_archived']); return d
def msg(c,r):
 d=dict(r); au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); d['author']=uobj(au); d['parent_id']=d['parent_id']; d['edited_at']=d['edited_at']; d['files']=[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(r['id'],))] if table_exists(c,'message_files') else []; d['mentions']=json.loads(r['body_mentions']) if 'body_mentions' in r.keys() and r['body_mentions'] else []
 d['reactions']=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  d['reactions'].append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]})
 d['reply_count']=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
 return d
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendraw(self,status,data,ctype='application/octet-stream',headers=None):
  self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data)))
  for k,v in (headers or {}).items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(data)
 def sendj(self,status,obj,headers=None):
  b=jdump(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  for k,v in (headers or {}).items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'))
  try: return json.loads(self.rfile.read(n) or b'{}')
  except Exception: raise ValueError('malformed json')
 def auth(self):
  r=token_user(self.headers.get('Authorization')); 
  if not r: self.sendj(401,{'error':'unauthorized'})
  return r
 def reqpath(self):
  q=urllib.parse.urlsplit(self.path); return q.path,q.query
 def do_GET(self):
  p,q=self.reqpath(); c=db()
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if p=='/': return self.html()
  if p=='/api/ws': return self.ws()
  me=self.auth()
  if not me:return
  if p=='/api/auth/me': return self.sendj(200,{'user':uobj(me)})
  if p=='/api/dms':
   b=self.body(); target=b.get('user_id')
   if not target:return self.sendj(400,{})
   if not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.sendj(404,{})
   rows=[]
   for z in c.execute('SELECT ch.id FROM channels ch JOIN cmembers cm ON cm.channel_id=ch.id WHERE ch.is_dm=1 AND cm.user_id=?',(me['id'],)):
    if c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(z['id'],target)).fetchone(): rows.append(z['id'])
   if rows:return self.sendj(200,{'channel_id':rows[0]})
   c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)',(0,'dm-'+str(me['id'])+'-'+str(target))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id'])); c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,target)); c.commit()
   return self.sendj(200,{'channel_id':cid})
  if p=='/api/workspaces':
   rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(me['id'],)).fetchall(); return self.sendj(200,{'workspaces':[dict(x) for x in rows]})
  m=re.match(r'/api/users/(\d+)$',p)
  if m:
   r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone(); return self.sendj(200,{'user':uobj(r)}) if r else self.sendj(404,{'error':'not found'})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(members|groups|invitations|channels))?$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{'error':'not found'})
   include_arch=('include_archived=1' in q or 'include_archived=true' in q or 'include_archived=True' in q)
  chans=[]
  for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],int(include_arch))):
   if not x['is_private'] or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone() or w['owner_id']==me['id']: chans.append(chan(c,x))
   sub=m.group(2)
   if sub=='channels':return self.sendj(200,{'channels':chans})
   if sub=='members':
    rows=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)).fetchall()
    return self.sendj(200,{'members':[dict(x) for x in rows]})
   if sub=='groups':
    return self.sendj(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
   if sub=='invitations':
    rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
    if not rr or rr['role'] not in ('owner','admin'):return self.sendj(403,{})
    rows=c.execute('SELECT * FROM invites WHERE workspace_id=? AND uses<max_uses AND expires>?',(w['id'],int(time.time()))).fetchall()
    return self.sendj(200,{'invitations':[dict(x) for x in rows]})
   return self.sendj(200,{'workspace':dict(w),'channels':chans,'read_state':[]})
  m=re.match(r'/api/channels/(\d+)(?:/(members|messages|pins|read))?$',p)
  if m:
   x=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{'error':'not found'})
   if m.group(2)=='members':
    return self.sendj(200,{'members':[uobj(z) for z in c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(x['id'],))]})
   if m.group(2)=='messages':
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone(): return self.sendj(403,{})
    qs=urllib.parse.parse_qs(q)
    try: lim=int(qs.get('limit',['50'])[0])
    except Exception: return self.sendj(400,{'error':'invalid limit'})
    if lim<1 or lim>200:return self.sendj(400,{'error':'invalid limit'})
    cur=qs.get('cursor',[None])[0]
    if cur is not None and (not cur.isdigit() or int(cur)<1):return self.sendj(400,{'error':'invalid cursor'})
    args=[x['id']]; where='channel_id=? AND deleted=0'
    if cur: where+=' AND id<=?'; args.append(int(cur))
    rows=c.execute('SELECT * FROM messages WHERE '+where+' ORDER BY id DESC LIMIT ?',tuple(args+[lim+1])).fetchall()
    more=len(rows)>lim; rows=rows[:lim]
    nxt=str(rows[-1]['id']) if more and rows else None
    return self.sendj(200,{'messages':[msg(c,z) for z in rows],'next_cursor':nxt})
   if m.group(2)=='pins':
    rows=c.execute('SELECT p.*,m.*,p.pinned_by pb FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(x['id'],)).fetchall(); return self.sendj(200,{'pins':[{'message':msg(c,z),'pinned_by':z['pb'],'pinned_at':z['pinned_at']} for z in rows],'next_cursor':None})
   if m.group(2)=='read':
    z=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(me['id'],x['id'])).fetchone(); return self.sendj(200,{'read_state':{'channel_id':x['id'],'last_read_event_id':z['last_read_event_id'] if z else 0,'unread_count':0,'mention_count':z['mention_count'] if z and 'mention_count' in z.keys() else 0}})
   return self.sendj(200,{'channel':chan(c,x)})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   qs=urllib.parse.parse_qs(q)
   try: lim=int(qs.get('limit',['50'])[0])
   except Exception:return self.sendj(400,{})
   if lim<1 or lim>200:return self.sendj(400,{})
   cur=qs.get('cursor',[None])[0]
   if cur is not None and not cur.isdigit():return self.sendj(400,{})
   sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0'; args=[m.group(1)]
   if cur:sql+=' AND id>=?';args.append(int(cur))
   sql+=' ORDER BY id LIMIT ?';args.append(lim+1)
   rows=c.execute(sql,tuple(args)).fetchall(); more=len(rows)>lim; rows=rows[:lim]
   return self.sendj(200,{'replies':[msg(c,z) for z in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
  if p.startswith('/api/search'):
   term=urllib.parse.parse_qs(q).get('q',[''])[0]; rows=c.execute('SELECT * FROM messages WHERE body LIKE ? AND deleted=0 ORDER BY id DESC',('%'+term+'%',)).fetchall(); return self.sendj(200,{'results':[msg(c,z) for z in rows],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)/download$',p)
  if m:
   z=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
   if not z:return self.sendj(404,{})
   return self.sendraw(200,z['data'],z['content_type'],{'Content-Disposition':'attachment; filename="'+z['filename']+'"'})
  m=re.match(r'/api/files/(\d+)$',p)
  if m:
   z=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(m.group(1),)).fetchone(); return self.sendj(200,{'file':dict(z)}) if z else self.sendj(404,{})
  return self.sendj(404,{'error':'not found'})
 def _post_impl(self):
  p,q=self.reqpath(); c=db()
  if p=='/api/files':
   me=self.auth()
   if not me:return
   raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.sendj(400,{})
   boundary=ct.split('boundary=',1)[-1].strip().strip('"').encode(); fp=next((x for x in raw.split(b'--'+boundary) if b'filename=' in x),None)
   if not fp:return self.sendj(400,{})
   hd,_,data=fp.partition(b'\\r\
\\r\
'); data=data[:-2] if data.endswith(b'\\r\
') else data; mm=re.search(br'filename="([^"]*)"',hd); tm=re.search(br'Content-Type:\\s*([^\\r\
]+)',hd,re.I)
   if not mm:return self.sendj(400,{})
   fn=mm.group(1).decode(errors='replace'); typ=tm.group(1).decode().strip() if tm else 'application/octet-stream'
   if len(data)>10*1024*1024:return self.sendj(413,{})
   created=now(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(me['id'],fn,typ,len(data),created,data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit()
   return self.sendj(201,{'file':{'id':fid,'uploader_id':me['id'],'filename':fn,'content_type':typ,'size':len(data),'created_at':created}})
  if p=='/api/auth/register':
   try:
    b=self.body(); u=b['username']; pw=b['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(pw)<8:return self.sendj(400,{})
    c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,pw,b.get('display_name',u))); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); return self.sendj(201,{'user':uobj(c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  if p=='/api/auth/login':
   b=self.body(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
   if not r:return self.sendj(401,{})
   t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); return self.sendj(200,{'user':uobj(r),'token':t})
  me=self.auth()
  if not me:return
  b=self.body()
  if p=='/api/workspaces':
   if not isinstance(b,dict) or not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')) or not b.get('name'): return self.sendj(400,{})
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(b['slug'],b['name'],me['id'],'open')); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute('INSERT INTO members VALUES(?,?,?)',(wid,me['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id'])); c.commit()
    return self.sendj(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':chan(c,c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})

  if p=='/api/dms':
   target=b.get('user_id')
   if not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.sendj(404,{})
   for z in c.execute('SELECT ch.id FROM channels ch JOIN cmembers cm ON cm.channel_id=ch.id WHERE ch.is_dm=1 AND cm.user_id=?',(me['id'],)):
    if c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(z['id'],target)).fetchone():return self.sendj(200,{'channel_id':z['id']})
   c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)',(0,'dm-'+str(me['id'])+'-'+str(target))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id'])); c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,target)); c.commit(); return self.sendj(200,{'channel_id':cid})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone() if w else None
   if not w:return self.sendj(404,{})
   if not rr or rr['role']=='guest' or (b.get('is_private') and rr['role'] not in ('owner','admin')):return self.sendj(403,{})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.sendj(400,{})
   try:
    c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id'])); c.commit(); return self.sendj(201,{'channel':chan(c,c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/messages/(\d+)/reactions$',p)
  if m:
   z=c.execute('SELECT id FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
   emoji=b.get('emoji')
   if not z:return self.sendj(404,{})
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{})
   c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(m.group(1),me['id'],emoji)); c.commit()
   rr=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(v) for v in x['ids'].split(',')]} for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(m.group(1),))]
   return self.sendj(200,{'reactions':rr})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive|leave|join)$',p)
  if m:
   x=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if m.group(2)=='leave':c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id']))
   elif m.group(2)=='join':
    if x['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone():return self.sendj(403,{})
    c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(x['id'],me['id']))
   else:
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
    if not w or w['owner_id']!=me['id']:return self.sendj(403,{})
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(m.group(2)=='archive'),x['id']))
   c.commit(); return self.sendj(200,{'channel':chan(c,c.execute('SELECT * FROM channels WHERE id=?',(x['id'],)).fetchone())})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:
   x=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone():return self.sendj(403,{})
   body=b.get('body','')
   if x['is_archived']:return self.sendj(423,{})
   if not body:return self.sendj(400,{})
   eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(x['id'],)).fetchone()['n']; created=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id,body_mentions) VALUES(?,?,?,?,?,?,?)',(x['id'],me['id'],body,b.get('parent_id'),created,eid,'[]')); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(x['id'],eid,'message.created',mid,created)); c.commit(); return self.sendj(201,{'message':msg(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())})
  return self.sendj(404,{})
 def _patch_impl(self):
  p,q=self.reqpath(); c=db(); me=self.auth()
  if not me:return
  b=self.body()
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   z=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not z:return self.sendj(404,{})
   if z['deleted']:return self.sendj(409,{})
   if z['author_id']!=me['id']:return self.sendj(403,{})
   c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b.get('body',''),now(),z['id'])); c.commit(); return self.sendj(200,{'message':msg(c,c.execute('SELECT * FROM messages WHERE id=?',(z['id'],)).fetchone())})
  m=re.match(r'/api/users/me$',p)
  if m:
   allowed=['display_name','timezone','avatar_url','status_text','status_emoji']
   if any(k not in allowed for k in b): return self.sendj(400,{})
   limits={'display_name':200,'timezone':100,'avatar_url':500,'status_text':250,'status_emoji':32}
   for k,v in b.items():
    if not isinstance(v,str) or len(v)>limits[k]: return self.sendj(400,{})
   if 'timezone' in b:
    try: zoneinfo.ZoneInfo(b['timezone'])
    except Exception: return self.sendj(400,{})
   vals={k:b[k] for k in allowed if k in b}
   if not vals:return self.sendj(200,{'user':uobj(me)})
   c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),me['id'])); c.commit(); updated=uobj(c.execute('SELECT * FROM users WHERE id=?',(me['id'],)).fetchone()); open(EVENTFILE,'a').write(jdump({'type':'user.updated','user':updated})+'\\n'); return self.sendj(200,{'user':updated})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w or not g:return self.sendj(404,{})
   rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   if not rr:return self.sendj(403,{})
   if rr['role']!='owner' and g['creator_id']!=me['id']:return self.sendj(403,{})
   name=b.get('name',g['name']); rawids=b['user_ids'] if 'user_ids' in b else json.loads(g['user_ids']); ids=[int(x) for x in rawids]
   c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(name,json.dumps(ids),g['id'])); c.commit()
   return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
  m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone() if w else None
   brole=b.get('role')
   target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],m.group(2))).fetchone() if w else None
   if not w or not target:return self.sendj(404,{})
   if brole=='owner':return self.sendj(400,{})
   if brole not in ('admin','member','guest'):return self.sendj(400,{})
   if role and role['role']=='owner' and int(m.group(2))!=me['id'] and brole!='owner' or role and role['role']=='admin' and target['role']=='member' and brole in ('member','guest'):
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(brole,w['id'],m.group(2))); c.commit(); return self.sendj(200,{'member':{'user_id':int(m.group(2)),'role':brole}})
   return self.sendj(403,{})
  m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); b2=b.get('new_owner_id')
   if not w:return self.sendj(404,{})
   cur=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   tar=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b2)).fetchone()
   if not cur or cur['role']!='owner':return self.sendj(403,{})
   if not tar or tar['role']!='admin':return self.sendj(400,{})
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],me['id']))
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b2))
   c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(b2,w['id'])); c.commit(); return self.sendj(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   if not role or role['role'] not in ('owner','admin'):return self.sendj(403,{})
   vals=[] 
   if 'name' in b: vals.append(('name',b['name']))
   if 'join_mode' in b:
    if b['join_mode'] not in ('open','invite_only'): return self.sendj(400,{})
    vals.append(('join_mode',b['join_mode']))
   if vals:
    c.execute('UPDATE workspaces SET '+','.join(k+'=?' for k,v in vals)+' WHERE id=?',tuple(v for k,v in vals)+(w['id'],)); c.commit()
   return self.sendj(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',p)
  if m:
   x=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
   if not w or w['owner_id']!=me['id']:return self.sendj(403,{})
   val=1 if m.group(2)=='archive' else 0
   c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,x['id'])); c.commit()
   return self.sendj(200,{'channel':chan(c,c.execute('SELECT * FROM channels WHERE id=?',(x['id'],)).fetchone())})
  m=re.match(r'/api/channels/(\d+)$',p)
  if m:
   x=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
   if not x:return self.sendj(404,{})
   if len(b.get('topic',''))>250:return self.sendj(400,{})
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
   member=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone()
   if not member and (not w or w['owner_id']!=me['id']):return self.sendj(403,{})
   c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),m.group(1))); c.commit(); return self.sendj(200,{'channel':chan(c,c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone())})
  return self.sendj(404,{})
 def _delete_impl(self):
  p,q=self.reqpath(); c=db(); me=self.auth()
  if not me:return
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:return self.sendj(200,{'deleted':True})
   rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   if not rr:return self.sendj(403,{})
   if rr['role']!='owner' and g['creator_id']!=me['id']:return self.sendj(403,{})
   c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],)); c.commit()
   return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); z=c.execute('SELECT * FROM invites WHERE code=?',(m.group(2),)).fetchone()
   rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone() if w else None
   if not w or not z:return self.sendj(404,{})
   if not rr or rr['role'] not in ('owner','admin'):return self.sendj(403,{})
   c.execute('DELETE FROM invites WHERE code=?',(m.group(2),)); c.commit(); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   z=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not z:return self.sendj(404,{})
   w=c.execute('SELECT w.owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(z['channel_id'],)).fetchone()
   if z['author_id']!=me['id'] and (not w or w['owner_id']!=me['id']):return self.sendj(403,{})
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(m.group(1),)); c.commit(); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',p)
  if m:
   c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m.group(1),me['id'],urllib.parse.unquote(m.group(2)))); c.commit()
   rr=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(v) for v in x['ids'].split(',')]} for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(m.group(1),))]
   return self.sendj(200,{'reactions':rr})
  m=re.match(r'/api/messages/(\d+)/pin$',p)
  if m:c.execute('DELETE FROM pins WHERE message_id=?',(m.group(1),)); c.commit(); return self.sendj(200,{'unpinned':True})
  return self.sendj(404,{})
 def do_POST(self):
  return self._post_impl()
 def do_PATCH(self):
  return self._patch_impl()
 def do_DELETE(self):
  return self._delete_impl()
 def html(self):
  b='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}</style></head><body><div id=app><h1>Huddle</h1><div data-testid=auth-modal><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary>Log in</button></form></div><div data-testid=channel-list></div><button data-testid=logout-btn data-button-role=danger>Logout</button><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button></div></body></html>'''
  x=b.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(x))); self.end_headers(); self.wfile.write(x)
 def ws(self):
  tok=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('token',[''])[0]
  if not token_user('Bearer '+tok): self.send_error(401); return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send_error(400)
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols'); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  uid=token_user('Bearer '+tok)['id']; open(ONLINEFILE,'a').write(str(uid)+','+str(time.time())+'\\n'); self.request.settimeout(.05); pos=os.path.getsize(EVENTFILE)
  def frame(obj):
   b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
   h=bytes([0x81])
   if n<126:h+=bytes([n])
   elif n<65536:h+=bytes([126])+struct.pack('>H',n)
   else:h+=bytes([127])+struct.pack('>Q',n)
   self.request.sendall(h+b)
  try:
   while True:
    try:
     with open(EVENTFILE) as f:
      f.seek(pos)
      lines=f.readlines()
      pos=f.tell()
     for line in lines:
      try: frame(json.loads(line))
      except Exception: pass
    except Exception: pass
    try:
     h=self.request.recv(2)
     if not h: break
     if len(h)==2:
      n=h[1]&127; masked=h[1]&128
      if n==126:n=struct.unpack('>H',self.request.recv(2))[0]
      elif n==127:n=struct.unpack('>Q',self.request.recv(8))[0]
      mask=self.request.recv(4) if masked else b''; data=self.request.recv(n)
      if masked:data=bytes(v^mask[i%4] for i,v in enumerate(data))
      if h[0]&15==8:break
      if h[0]&15==1:
       try:
        obj=json.loads(data)
        if obj.get('type')=='subscribe':
         try: open('/app/ws-subs.log','a').write(str(token_user('Bearer '+tok)['id'])+','+str(obj.get('channel_id'))+','+str(time.time())+'\\n')
         except Exception: pass
         c=db(); z=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(obj.get('channel_id'),)).fetchone(); c.close()
         frame({'type':'subscribed','channel_id':obj.get('channel_id'),'head_event_id':z['n']})
       except Exception: pass
    except socket.timeout: pass
  except Exception: pass
  self.close_connection=True
def broadcast(ch,e):
 pass
def run():
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
if __name__=='__main__': run()
