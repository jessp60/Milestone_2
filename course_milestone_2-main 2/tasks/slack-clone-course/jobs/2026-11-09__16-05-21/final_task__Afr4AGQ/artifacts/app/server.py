import os,sys,json,re,sqlite3,hashlib,secrets,base64,struct,threading,time,datetime,urllib.parse, mimetypes,select
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/huddle.db'; NODE=int(os.environ.get('NODE_ID','0'))
lock=threading.RLock()
OPEN_WS=set()
DB_READY=False
DB_INIT_LOCK=threading.Lock()
def now():
 return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 global DB_READY
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False)
 c.row_factory=sqlite3.Row
 c.execute('pragma busy_timeout=30000')
 if not DB_READY:
  with DB_INIT_LOCK:
   if not DB_READY:
    c.executescript('''
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions_json TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at REAL,max_uses INTEGER,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_event INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,seen_at REAL);
''')
    try:
     c.execute('alter table reads add column mention_count integer default 0')
    except:
     pass
    try:
     c.execute("alter table messages add column mentions_json text default '[]'")
    except:
     pass
    c.commit()
    DB_READY=True
 return c
def emit(c,cid,kind,payload):
 ev=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]
 c.execute('insert into events values(?,?,?,?)',(cid,ev,kind,json.dumps(payload)))
 return ev
def user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def msg(c,r):
 if not r:return None
 author=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rs=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(author),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id in (select file_id from message_files where message_id=?)',(r['id'],)).fetchall()],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs],'mentions':json.loads(r['mentions_json'] or '[]') if 'mentions_json' in r.keys() else []}
def envelope(h,obj,status=200): h.send_json(obj,status)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def send_json(self,x,status=200):
  b=json.dumps(x,separators=(',',':')).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def readjson(self):
  try:
   n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
  except: raise ValueError()
 def auth(self):
  t=self.headers.get('Authorization','')
  if not t.startswith('Bearer '): return None
  return db().execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t[7:],)).fetchone()
 def reqpath(self): return urllib.parse.urlsplit(self.path).path
 def do_GET(self):
  p=self.reqpath(); q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); c=db()
  if p=='/api/health': return envelope(self,{'status':'ok','node_id':NODE})
  if p.startswith('/api/ws'): return self.websocket()
  u=self.auth()
  if p.startswith('/api/auth/') and p!='/api/auth/me':
   return self.auth_route(c,p,'GET')
  if not u:return envelope(self,{'error':'unauthorized'},401)
  if p=='/api/auth/me':return envelope(self,{'user':user(u)})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   ww=c.execute('select id from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not ww:return envelope(self,{},404)
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(ww['id'],u['id'])).fetchone():return envelope(self,{},403)
   rows=c.execute('select id,handle,name,user_ids from groups_ where workspace_id=?',(ww['id'],)).fetchall()
   groups=[]
   for gr in rows:
    try: ids=json.loads(gr['user_ids'] or '[]')
    except Exception: ids=[]
    groups.append({'id':gr['id'],'handle':gr['handle'],'name':gr['name'],'member_user_ids':ids})
   return envelope(self,{'groups':groups})
  if p.startswith('/api/users/'):
   x=p.split('/')[-1]; r=u if x=='me' else c.execute('select * from users where id=?',(x,)).fetchone()
   return envelope(self,{'user':user(r)} if r else {'error':'not found'},200 if r else 404)
  if p=='/api/dms':
   target=d.get('user_id')
   if not c.execute('select id from users where id=?',(target,)).fetchone() or target==u['id']:return envelope(self,{},404)
   pair=','.join(map(str,sorted((u['id'],int(target)))))
   ch=c.execute('select id from channels where is_dm=1 and name=?',(pair,)).fetchone()
   if not ch:
    c.execute('insert into channels(workspace_id,name,is_private,is_dm,topic) values(0,?,1,1,"")',(pair,));cid=c.execute('select last_insert_rowid()').fetchone()[0]
    c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.execute('insert into cmembers values(?,?)',(cid,target));c.commit()
   else: cid=ch['id']
   return envelope(self,{'channel_id':cid})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not w:return envelope(self,{},404)
   mem=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not mem:return envelope(self,{},403)
   if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',d.get('handle','')) or not d.get('name'):return envelope(self,{},400)
   if c.execute('select id from groups_ where workspace_id=? and handle=?',(w['id'],d['handle'])).fetchone():return envelope(self,{},409)
   ids=d.get('user_ids',[])
   c.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],d['handle'],d['name'],u['id'],json.dumps(ids)));gid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit()
   return envelope(self,{'group':{'id':gid,'handle':d['handle'],'name':d['name'],'member_user_ids':ids}},201)
  if p=='/api/workspaces':
   rows=c.execute('select distinct w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall()
   return envelope(self,{'workspaces':[dict(x) for x in rows]})
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3];w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   rr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not rr or rr['role'] not in ('owner','admin'):return envelope(self,{},403)
   rows=c.execute('select * from invitations where workspace_id=? and uses<max_uses and expires_at>?',(w['id'],time.time())).fetchall()
   return envelope(self,{'invitations':[dict(x) for x in rows]})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(.*))?$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return envelope(self,{'error':'not found'},404)
   tail=m.group(2)
   if tail=='members':
    rows=c.execute('select m.*,u.username,u.display_name from members m join users u on u.id=m.user_id where workspace_id=?',(w['id'],)).fetchall()
    return envelope(self,{'members':[dict(x) for x in rows]})
   if tail=='groups':
    rows=c.execute('select id,handle,name,user_ids from groups_ where workspace_id=?',(w['id'],)).fetchall()
    out=[]
    for x in rows:
     try: ids=json.loads(x['user_ids'] or '[]')
     except Exception: ids=[]
     out.append({'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':ids})
    return envelope(self,{'groups':out})
   ch=c.execute('select * from channels where workspace_id=? and (archived=0 or ?=1) and (is_private=0 or id in (select channel_id from cmembers where user_id=?))',(w['id'],1 if q.get('include_archived') else 0,u['id'])).fetchall()
   states=[]
   for x in ch:
    rr=c.execute('select * from reads where user_id=? and channel_id=?',(u['id'],x['id'])).fetchone()
    states.append({'channel_id':x['id'],'last_read_event_id':rr['last_event'] if rr else 0,'unread_count':0,'mention_count':rr['mention_count'] if rr else 0})
   return envelope(self,{'workspace':dict(w),'channels':[self.channel(x) for x in ch],'read_state':states})
  if p.startswith('/api/channels/'):
   z=p.split('/'); cid=z[3] if len(z)>3 else ''
   ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return envelope(self,{'error':'not found'},404)
   tail='/'.join(z[4:])
   if tail=='members': 
    rows=c.execute('select u.* from cmembers m join users u on u.id=m.user_id where m.channel_id=?',(cid,)).fetchall(); return envelope(self,{'members':[user(x) for x in rows]})
   if tail=='messages':
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return envelope(self,{},403)
    try: lim=int(q.get('limit',['50'])[0])
    except: return envelope(self,{},400)
    if lim<1 or lim>200:return envelope(self,{},400)
    cur=q.get('cursor',[None])[0]
    if cur is not None and not re.fullmatch(r'\d+',cur):return envelope(self,{},400)
    sql='select * from messages where channel_id=? and deleted=0 and parent_id is null'
    args=[cid]
    if cur is not None: sql+=' and id<?';args.append(int(cur))
    sql+=' order by id desc limit ?';args.append(lim+1)
    rows=c.execute(sql,args).fetchall(); more=len(rows)>lim; rows=rows[:lim]
    nxt=str(rows[-1]['id']) if more and rows else None
    return envelope(self,{'messages':[msg(c,x) for x in rows],'next_cursor':nxt})
   if tail=='pins':
    if ch['is_private'] and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return envelope(self,{},403)
    try: lim=int(q.get('limit',['50'])[0])
    except: return envelope(self,{},400)
    if lim<1 or lim>200:return envelope(self,{},400)
    cur=q.get('cursor',[None])[0]
    if cur is not None and not re.fullmatch(r'\d+',cur):return envelope(self,{},400)
    sql='select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=?';args=[cid]
    if cur is not None: sql+=' and p.message_id<?';args.append(int(cur))
    sql+=' order by p.message_id desc limit ?';args.append(lim+1)
    rows=c.execute(sql,args).fetchall();more=len(rows)>lim;rows=rows[:lim]
    return envelope(self,{'pins':[{'message':msg(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':str(rows[-1]['message_id']) if more and rows else None})
   if tail=='read':
    r=c.execute('select * from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone(); return envelope(self,{'read_state':{'channel_id':int(cid),'last_read_event_id':r['last_event'] if r else 0,'unread_count':0,'mention_count':(r['mention_count'] if r and 'mention_count' in r.keys() else 0)}})
  if p.startswith('/api/messages/') and p.endswith('/replies'):
   mid=int(p.split('/')[3])
   if not c.execute('select id from messages where id=?',(mid,)).fetchone():return envelope(self,{},404)
   try: lim=int(q.get('limit',['50'])[0])
   except: return envelope(self,{},400)
   if lim<1 or lim>200:return envelope(self,{},400)
   cur=q.get('cursor',[None])[0]
   if cur is not None and not re.fullmatch(r'\d+',cur):return envelope(self,{},400)
   sql='select * from messages where parent_id=? and deleted=0';args=[mid]
   if cur is not None:sql+=' and id>?';args.append(int(cur))
   sql+=' order by id limit ?';args.append(lim+1)
   rows=c.execute(sql,args).fetchall();more=len(rows)>lim;rows=rows[:lim]
   return envelope(self,{'replies':[msg(c,x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
  if p.startswith('/api/files/') and p.endswith('/download'):
   fid=p.split('/')[3]; x=c.execute('select filename,content_type,data from files where id=?',(fid,)).fetchone()
   if not x:return envelope(self,{},404)
   b=x['data']; self.send_response(200); self.send_header('Content-Type',x['content_type'] or 'application/octet-stream'); self.send_header('Content-Disposition','attachment; filename="'+x['filename'].replace('"','')+'"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if p.startswith('/api/files/'):
   fid=p.split('/')[3]; x=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone()
   return envelope(self,{'file':dict(x)} if x else {'error':'not found'},200 if x else 404)
  if p.startswith('/api/search'):
   term=q.get('q',[''])[0]
   try:lim=int(q.get('limit',['50'])[0])
   except:return envelope(self,{},400)
   if lim<1 or lim>200:return envelope(self,{},400)
   cur=q.get('cursor',[None])[0]
   if cur is not None and not re.fullmatch(r'\d+',cur):return envelope(self,{},400)
   sql='select m.* from messages m join channels ch on ch.id=m.channel_id join cmembers cm on cm.channel_id=ch.id and cm.user_id=? where m.body like ? and m.deleted=0';args=[u['id'],'%'+term+'%']
   if q.get('workspace_id'):
    sql+=' and ch.workspace_id=?';args.append(q['workspace_id'][0])
   if cur is not None:sql+=' and m.id<?';args.append(int(cur))
   sql+=' order by m.id desc limit ?';args.append(lim+1)
   rows=c.execute(sql,args).fetchall();more=len(rows)>lim;rows=rows[:lim]
   return envelope(self,{'results':[msg(c,x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
  return envelope(self,{'error':'not found'},404)
 def channel(self,x): return {'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'],'is_archived':bool(x['archived'])}
 def auth_route(self,c,p,method):
  try:d=self.readjson()
  except:return envelope(self,{'error':'bad json'},400)
  if p.endswith('register'):
   if not re.fullmatch(r'[A-Za-z0-9_]+',d.get('username','')) or len(d.get('password',''))<8:return envelope(self,{'error':'invalid'},400)
   try:c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name',d['username']),'UTC','','',''));c.commit()
   except sqlite3.IntegrityError:return envelope(self,{'error':'duplicate'},409)
   u=c.execute('select * from users where username=?',(d['username'],)).fetchone(); t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,u['id'],));c.commit();return envelope(self,{'user':user(u),'token':t},201)
  if p.endswith('login'):
   u=c.execute('select * from users where username=? and password=?',(d.get('username'),d.get('password'))).fetchone()
   if not u:return envelope(self,{'error':'invalid'},401)
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,u['id'],));c.commit();return envelope(self,{'user':user(u),'token':t})
 def do_POST(self):
  p=self.reqpath(); c=db()
  if p in ['/api/auth/register','/api/auth/login']:return self.auth_route(c,p,'POST')
  u=self.auth()
  if not u:return envelope(self,{'error':'unauthorized'},401)
  if p=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return envelope(self,{},400)
   raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
   bm=re.search(r'boundary=(?:"([^"]+)"|([^;]+))',ct)
   if not bm:return envelope(self,{},400)
   boundary=(bm.group(1) or bm.group(2)).encode()
   part=None
   for seg in raw.split(b'--'+boundary):
    if b'Content-Disposition:' not in seg and b'content-disposition:' not in seg: continue
    chunks=seg.split(b'\r\n\r\n',1)
    if len(chunks)!=2: continue
    head,body=chunks
    if not re.search(rb'name\s*=\s*["\']file["\']',head,re.I): continue
    if body.endswith(b'\r\n'): body=body[:-2]
    part=(head,body)
    break
   if part is None:return envelope(self,{},400)
   head,data=part
   dm=re.search(rb'filename=["\']?([^"\'\r\n;]+)',head,re.I)
   tm=re.search(rb'content-type:\s*([^\r\n]+)',head,re.I)
   fn=(dm.group(1).decode(errors='replace') if dm else 'file')
   typ=(tm.group(1).decode(errors='replace').strip() if tm else 'application/octet-stream')
   if len(data)>10*1024*1024:return envelope(self,{},413)
   c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),data,now())); fid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit()
   f=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone()
   return envelope(self,{'file':dict(f)},201)
  try:d=self.readjson()
  except:return envelope(self,{'error':'bad json'},400)
  if p=='/api/dms':
   target=d.get('user_id')
   if not c.execute('select id from users where id=?',(target,)).fetchone() or target==u['id']:return envelope(self,{},404)
   pair=','.join(map(str,sorted((u['id'],int(target)))))
   ch=c.execute('select id from channels where is_dm=1 and name=?',(pair,)).fetchone()
   if not ch:
    c.execute('insert into channels(workspace_id,name,is_private,is_dm,topic) values(0,?,1,1,"")',(pair,));cid=c.execute('select last_insert_rowid()').fetchone()[0]
    c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.execute('insert into cmembers values(?,?)',(cid,target));c.commit()
   else: cid=ch['id']
   return envelope(self,{'channel_id':cid})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not w:return envelope(self,{},404)
   mem=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not mem:return envelope(self,{},403)
   if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',d.get('handle','')) or not d.get('name'):return envelope(self,{},400)
   if c.execute('select id from groups_ where workspace_id=? and handle=?',(w['id'],d['handle'])).fetchone():return envelope(self,{},409)
   ids=d.get('user_ids',[])
   c.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],d['handle'],d['name'],u['id'],json.dumps(ids)));gid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit()
   return envelope(self,{'group':{'id':gid,'handle':d['handle'],'name':d['name'],'member_user_ids':ids}},201)
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')) or not d.get('name'):return envelope(self,{},400)
   try:c.execute('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(d['slug'],d.get('name',''),u['id'],'open')); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,0,"")',(wid,'general'));gid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(gid,u['id']));c.commit();w=c.execute('select * from workspaces where id=?',(wid,)).fetchone();return envelope(self,{'workspace':dict(w),'general_channel':self.channel(c.execute('select * from channels where id=?',(gid,)).fetchone())},201)
   except sqlite3.IntegrityError:return envelope(self,{},409)
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role:return envelope(self,{},403)
   if d.get('is_private') and role['role'] not in ('owner','admin'):return envelope(self,{},403)
   if role['role']=='guest':return envelope(self,{},403)
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')) or len(d.get('topic',''))>250:return envelope(self,{},400)
   if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],d['name'])).fetchone():return envelope(self,{},409)
   try:
    c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')))
    cid=c.execute('select last_insert_rowid()').fetchone()[0]
    c.execute('insert into cmembers values(?,?)',(cid,u['id']))
    c.commit()
    return envelope(self,{'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())},201)
   except sqlite3.IntegrityError:return envelope(self,{},409)
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3];w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   rr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not rr or rr['role'] not in ('owner','admin'):return envelope(self,{},403)
   code=secrets.token_urlsafe(12); target=d.get('invited_user_id'); c.execute('insert into invitations values(?,?,?,?,?,0)',(code,w['id'],target,time.time()+d.get('expires_in_seconds',86400),1 if target else d.get('max_uses',1)));c.commit()
   return envelope(self,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':d.get('max_uses',1)}},201)
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3];iv=c.execute('select * from invitations where code=?',(code,)).fetchone()
   if not iv or iv['uses']>=iv['max_uses'] or iv['expires_at']<time.time():return envelope(self,{},404)
   if iv['invited_user_id'] and iv['invited_user_id']!=u['id']:return envelope(self,{},403)
   c.execute('insert or ignore into members values(?,?,?)',(iv['workspace_id'],u['id'],'member'));c.execute('update invitations set uses=uses+1 where code=?',(code,));c.commit();return envelope(self,{'accepted':True})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   if w['owner_id']!=u['id']:return envelope(self,{},403)
   target=d.get('new_owner_id'); r=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
   if not r or r['role']!='admin':return envelope(self,{},400)
   c.execute('update members set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id']))
   c.execute('update members set role="owner" where workspace_id=? and user_id=?',(w['id'],target))
   c.execute('update workspaces set owner_id=? where id=?',(target,w['id']));c.commit()
   return envelope(self,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if re.match(r'/api/channels/\d+/(leave|archive|unarchive)$',p):
   z=p.split('/');cid=int(z[3]); action=z[4];ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return envelope(self,{},404)
   if action=='leave':
    c.execute('delete from cmembers where channel_id=? and user_id=?',(cid,u['id']));emit(c,cid,'member.left',{'user_id':u['id']});c.commit()
    return envelope(self,{'channel':self.channel(ch)})
   w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if w['owner_id']!=u['id']:return envelope(self,{},403)
   c.execute('update channels set archived=? where id=?',(1 if action=='archive' else 0,cid));emit(c,cid,'channel.archived' if action=='archive' else 'channel.unarchived',{'channel_id':cid});c.commit()
   return envelope(self,{'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if re.match(r'/api/channels/\d+/read$',p):
   cid=int(p.split('/')[3]);ch=c.execute('select id from channels where id=?',(cid,)).fetchone()
   if not ch:return envelope(self,{},404)
   old=c.execute('select last_event,mention_count from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone()
   val=max(old['last_event'] if old else 0,int(d.get('last_read_event_id',0)))
   c.execute('insert or replace into reads(user_id,channel_id,last_event,mention_count) values(?,?,?,?)',(u['id'],cid,val,old['mention_count'] if old else 0));c.commit()
   return envelope(self,{'read_state':{'channel_id':cid,'last_read_event_id':val,'unread_count':0,'mention_count':(old['mention_count'] if old and 'mention_count' in old.keys() else 0)}})
  if re.match(r'/api/channels/\d+/join$',p):
   cid=int(p.split('/')[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return envelope(self,{},404)
   w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone(); m=c.execute('select * from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   cm0=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone()
   if ch['is_private'] and not cm0:return envelope(self,{},403)
   if m and m['role']=='guest':return envelope(self,{},403)
   if w['join_mode']=='invite_only' and not m:return envelope(self,{},403)
   if m is None:c.execute('insert or ignore into members values(?,?,?)',(w['id'],u['id'],'member'))
   c.execute('insert or ignore into cmembers values(?,?)',(cid,u['id']));emit(c,cid,'member.joined',{'user_id':u['id']});c.commit();return envelope(self,{'channel':self.channel(ch)})
  if re.match(r'/api/channels/\d+/messages$',p):
   cid=int(p.split('/')[3]);ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return envelope(self,{},404)
   if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():return envelope(self,{},403)
   if ch['archived'] and not (isinstance(d.get('body'),str) and d.get('body').startswith('/unarchive')):return envelope(self,{},423)
   body=d.get('body','')
   if not isinstance(body,str) or not body.strip():return envelope(self,{},400)
   if body.startswith('/') and not body.startswith('//'):
    cmd,_,arg=body[1:].partition(' ')
    arg=arg.strip()
    w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
    role=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    if cmd=='me':
     if not arg:return envelope(self,{},400)
    elif cmd=='shrug':
     body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if not arg or not role or role['role'] not in ('owner','admin'):return envelope(self,{},403)
     if len(arg)>250:return envelope(self,{},400)
     c.execute('update channels set topic=? where id=?',(arg,cid));emit(c,cid,'channel.updated',{'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())});c.commit()
     return envelope(self,{'message':None,'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())},201)
    elif cmd=='invite':
     m=re.fullmatch(r'@?([A-Za-z0-9_]+)',arg)
     if not m:return envelope(self,{},400)
     target=c.execute('select * from users where username=?',(m.group(1),)).fetchone()
     if not target:return envelope(self,{},404)
     c.execute('insert or ignore into cmembers values(?,?)',(cid,target['id']));c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],target['id'],'member'));c.commit()
     return envelope(self,{'message':None,'invited_user':user(target)},201)
    elif cmd in ('archive','unarchive'):
     if not role or role['role']!='owner':return envelope(self,{},403)
     val=1 if cmd=='archive' else 0
     c.execute('update channels set archived=? where id=?',(val,cid));emit(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel_id':cid});c.commit()
     return envelope(self,{'message':None,'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())},201)
    else:return envelope(self,{},400)
   d['body']=body
   file_ids=d.get('file_ids',[])
   for fid in file_ids:
    f=c.execute('select * from files where id=?',(fid,)).fetchone()
    if not f or f['uploader_id']!=u['id'] or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone():return envelope(self,{},400)
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(cid,u['id'],d['body'],d.get('parent_id'),now()))
   mid=c.execute('select last_insert_rowid()').fetchone()[0]
   for fid in file_ids:c.execute('insert into message_files values(?,?)',(mid,fid))
   kind='message.reply' if d.get('parent_id') else 'message.created'
   mm=msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone())
   names=set(re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',d['body']))
   mids=[]
   members=[int(x['user_id']) for x in c.execute('select user_id from cmembers where channel_id=?',(cid,)).fetchall()]
   active_presence={int(x['user_id']) for x in c.execute('select user_id from ws_presence').fetchall()}
   active_presence.update(int(x) for x in OPEN_WS)
   for nm in names:
    if nm=='channel':
     mids += [x for x in members if x!=u['id']]
     continue
    if nm=='here':
     mids += [x for x in members if x!=u['id'] and x in active_presence]
     continue
    rr=c.execute('select id from users where username=?',(nm,)).fetchone()
    if rr and rr['id']!=u['id'] and rr['id'] in members: mids.append(rr['id'])
    gg=c.execute('select user_ids from groups_ where workspace_id=? and handle=?',(ch['workspace_id'],nm)).fetchone()
    if gg: mids += [x for x in json.loads(gg['user_ids']) if x in members]
   mids=[x for x in sorted(set(mids)) if x!=u['id']]
   mm['mentions']=mids
   for mid2 in mids:
    oldr=c.execute('select mention_count,last_event from reads where user_id=? and channel_id=?',(mid2,cid)).fetchone()
    c.execute('insert or replace into reads(user_id,channel_id,last_event,mention_count) values(?,?,?,?)',(mid2,cid,oldr['last_event'] if oldr else 0,(oldr['mention_count'] if oldr else 0)+1))
   c.execute('update messages set body=?,mentions_json=? where id=?',(d['body'],json.dumps(mids),mid))
   emit(c,cid,kind,mm);c.commit();return envelope(self,{'message':mm},201)
  if re.match(r'/api/messages/\d+/reactions$',p):
   midpart=p.split('/')[3] if len(p.split('/'))>3 else ''
   if not midpart.isdigit():return envelope(self,{},404)
   mid=int(midpart); emoji=d.get('emoji',''); rmsg=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not rmsg:return envelope(self,{},404)
   if not isinstance(emoji,str) or not emoji:return envelope(self,{},400)
   c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],emoji)); emit(c,rmsg['channel_id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id']});c.commit()
   rs=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(mid,)).fetchall()
   return envelope(self,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs]})
  if re.match(r'/api/messages/\d+/pin$',p):
   midpart=p.split('/')[3] if len(p.split('/'))>3 else ''
   if not midpart.isdigit():return envelope(self,{},404)
   mid=int(midpart);
   if not c.execute('select id from messages where id=?',(mid,)).fetchone():return envelope(self,{},404)
   c.execute('insert or ignore into pins values(?,?,?)',(mid,u['id'],now()));c.commit();return envelope(self,{'pin':{'message_id':mid,'pinned_by':u['id'],'pinned_at':now()}})
  return envelope(self,{},404)
 def do_PATCH(self):
  p=self.reqpath();u=self.auth();c=db()
  if not u:return envelope(self,{},401)
  try:d=self.readjson()
  except:return envelope(self,{},400)
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not w:return envelope(self,{},404)
   g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],gm.group(2))).fetchone()
   if not g:return envelope(self,{},404)
   rr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not rr or rr['role']!='owner'):return envelope(self,{},403)
   name=d.get('name',g['name']); ids=d.get('user_ids',json.loads(g['user_ids']))
   c.execute('update groups_ set name=?,user_ids=? where id=?',(name,json.dumps(ids),g['id']));c.commit()
   return envelope(self,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
  if p=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
   if 'timezone' in d:
    try: ZoneInfo(d['timezone'])
    except Exception: return envelope(self,{},400)
   if any(k not in allowed or len(str(v))>200 for k,v in d.items()):return envelope(self,{},400)
   for k,v in d.items():
    if k in allowed:c.execute('update users set '+k+'=? where id=?',(v,u['id']))
   updated_user=user(c.execute('select * from users where id=?',(u['id'],)).fetchone())
   c.execute('insert into global_events(kind,payload) values(?,?)',('user.updated',json.dumps({'type':'user.updated','user':updated_user})))
   c.commit();return envelope(self,{'user':updated_user})
  if p.startswith('/api/workspaces/') and '/members/' not in p and not p.endswith('/channels'):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or role['role'] not in ('owner','admin'):return envelope(self,{},403)
   if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return envelope(self,{},400)
   c.execute('update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit()
   return envelope(self,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3];w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   if w['owner_id']!=u['id']:return envelope(self,{},403)
   target=d.get('new_owner_id'); r=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
   if not r or r['role']!='admin':return envelope(self,{},400)
   c.execute('update members set role="admin" where workspace_id=? and user_id=?',(w['id'],u['id']));c.execute('update members set role="owner" where workspace_id=? and user_id=?',(w['id'],target));c.execute('update workspaces set owner_id=? where id=?',(target,w['id']));c.commit()
   return envelope(self,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
  if re.match(r'/api/channels/\d+$',p):
   cid=int(p.split('/')[3]); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return envelope(self,{},404)
   if len(d.get('topic',''))>250:return envelope(self,{},400)
   wrole=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
   cm=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone()
   if not cm and (not wrole or wrole['role'] not in ('owner','admin')):return envelope(self,{},403)
   c.execute('update channels set topic=? where id=?',(d.get('topic',''),cid));emit(c,cid,'channel.updated',{'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())});c.commit();return envelope(self,{'channel':self.channel(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if re.match(r'/api/messages/\d+$',p):
   midpart=p.split('/')[3] if len(p.split('/'))>3 else ''
   if not midpart.isdigit():return envelope(self,{},404)
   mid=int(midpart);r=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not r:return envelope(self,{},404)
   if r['deleted']:return envelope(self,{},409)
   if r['author_id']!=u['id']:return envelope(self,{},403)
   if not isinstance(d.get('body'),str) or not d.get('body').strip():return envelope(self,{},400)
   c.execute('update messages set body=?,edited_at=? where id=?',(d.get('body',''),now(),mid))
   updated=msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone())
   emit(c,r['channel_id'],'message.updated',updated)
   c.commit()
   return envelope(self,{'message':updated})
  if '/members/' in p:
   z=p.split('/');slug=z[3]; ww=c.execute('select id from workspaces where slug=?',(slug,)).fetchone()
   if not ww:return envelope(self,{},404)
   wid=ww['id'];
   if len(z)<6 or not z[5].isdigit():return envelope(self,{},400)
   target=int(z[5]); role=c.execute('select role from members where workspace_id=? and user_id=?',(wid,u['id'])).fetchone(); 
   if not role or role['role'] not in ('owner','admin'):return envelope(self,{},403)
   if d.get('role') not in ('owner','admin','member','guest'):return envelope(self,{},400)
   oldrole=c.execute('select role from members where workspace_id=? and user_id=?',(wid,target)).fetchone()
   if not oldrole:return envelope(self,{},404)
   if d.get('role')=='owner' or target==u['id'] or (role['role']=='admin' and oldrole['role'] in ('owner','admin')):return envelope(self,{},400 if d.get('role')=='owner' else 403)
   c.execute('update members set role=? where workspace_id=? and user_id=?',(d.get('role'),wid,target));c.commit();return envelope(self,{'member':{'user_id':target,'role':d.get('role')}})
  return envelope(self,{},404)
 def do_DELETE(self):
  p=self.reqpath();u=self.auth();c=db()
  if not u:return envelope(self,{},401)
  if p.startswith('/api/workspaces/') and '/invitations/' in p:
   z=p.split('/');slug=z[3];code=z[5];w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return envelope(self,{},404)
   rr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not rr or rr['role'] not in ('owner','admin'):return envelope(self,{},403)
   c.execute('delete from invitations where code=?',(code,));c.commit();return envelope(self,{'deleted':True})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if gm:
   w=c.execute('select * from workspaces where slug=?',(gm.group(1),)).fetchone()
   if not w:return envelope(self,{'deleted':True})
   g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],gm.group(2))).fetchone()
   if not g:return envelope(self,{'deleted':True})
   rr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if g['creator_id']!=u['id'] and (not rr or rr['role']!='owner'):return envelope(self,{},403)
   c.execute('delete from groups_ where id=?',(g['id'],));c.commit();return envelope(self,{'deleted':True})
  if '/reactions/' in p:
   z=p.split('/'); midpart=z[3] if len(z)>3 else ''; emoji=urllib.parse.unquote(z[5]) if len(z)>5 else ''; 
   if not midpart.isdigit() or not emoji:return envelope(self,{},404)
   mid=int(midpart); rmsg=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not rmsg:return envelope(self,{},404)
   c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji));emit(c,rmsg['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id']});c.commit()
   rs=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(mid,)).fetchall()
   return envelope(self,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs]})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   midpart=p.split('/')[3] if len(p.split('/'))>3 else ''
   if not midpart.isdigit():return envelope(self,{},404)
   mid=int(midpart);
   if not c.execute('select id from messages where id=?',(mid,)).fetchone():return envelope(self,{},404)
   c.execute('delete from pins where message_id=?',(mid,));c.commit();return envelope(self,{'unpinned':True})
  if p.startswith('/api/messages/'):
   midpart=p.split('/')[3] if len(p.split('/'))>3 else ''
   if not midpart.isdigit():return envelope(self,{},404)
   mid=int(midpart);r=c.execute('select m.*,w.owner_id from messages m join channels ch on ch.id=m.channel_id join workspaces w on w.id=ch.workspace_id where m.id=?',(mid,)).fetchone()
   if not r:return envelope(self,{},404)
   if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return envelope(self,{},403)
   c.execute('update messages set deleted=1 where id=?',(mid,));emit(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit();return envelope(self,{'deleted':True})
  return envelope(self,{},404)
 def websocket(self):
  t=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('token',[''])[0]; c=db();u=c.execute('select user_id from tokens where token=?',(t,)).fetchone()
  if not u:self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
  OPEN_WS.add(u['user_id']); c.execute('insert or replace into ws_presence values(?,?)',(u['user_id'],time.time())); c.commit()
  key=self.headers.get('Sec-WebSocket-Key'); acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
  global_last=[c.execute('select coalesce(max(id),0) from global_events').fetchone()[0]]
  frame_lock=threading.Lock()
  # Minimal protocol: subscribe/resume and poll events.
  def frame(x):
   b=json.dumps(x).encode(); n=len(b); hdr=bytes([129])+((bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n)))
   with frame_lock:
    self.wfile.write(hdr+b);self.wfile.flush()
  def global_poller():
   while True:
    try:
     cc=db()
     for ge in cc.execute('select * from global_events where id>? order by id',(global_last[0],)):
      frame(json.loads(ge['payload'])); global_last[0]=ge['id']
     time.sleep(.05)
    except Exception:
     return
  threading.Thread(target=global_poller,daemon=True).start()
  try:
   while True:
    self.connection.settimeout(None)
    try:
     if not select.select([self.connection],[],[],.02)[0]: raise TimeoutError()
     h=self.rfile.read(2)
     if not h:break
     n=h[1]&127
     if n==126:n=struct.unpack('>H',self.rfile.read(2))[0]
     mask=self.rfile.read(4); data=bytes(x^mask[i%4] for i,x in enumerate(self.rfile.read(n))); d=json.loads(data); cid=int(d.get('channel_id',0))
     if d.get('type') in ('subscribe','resume'):
      head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0]
      if d['type']=='resume':
       for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,int(d.get('since_event_id',0)))):
        frame(dict(json.loads(e['payload']),type=e['kind'],event_id=e['event_id'],channel_id=cid))
       frame({'type':'resumed','channel_id':cid,'head_event_id':head})
      else: frame({'type':'subscribed','channel_id':cid,'head_event_id':head})
      last=head
     else: continue
    except TimeoutError:
     for ge in c.execute('select * from global_events where id>? order by id',(global_last[0],)):
      frame(json.loads(ge['payload']))
      global_last[0]=ge['id']
     if 'cid' in locals() and 'last' in locals():
      for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last)):
       frame(dict(json.loads(e['payload']),type=e['kind'],event_id=e['event_id'],channel_id=cid)); last=e['event_id']
     continue
    # poll global profile events on every connection
    for ge in c.execute('select * from global_events where id>? order by id',(global_last[0],)):
     frame(json.loads(ge['payload'])); global_last[0]=ge['id']
    # short poll
    end=time.time()+.2
    while time.time()<end:
     for ge in c.execute('select * from global_events where id>? order by id',(global_last[0],)):
      frame(json.loads(ge['payload'])); global_last[0]=ge['id']
     for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last)):
      frame(dict(json.loads(e['payload']),type=e['kind'],event_id=e['event_id'],channel_id=cid));last=e['event_id']
     time.sleep(.03)
   self.connection.close()
  except Exception: pass
  finally:
   OPEN_WS.discard(u['user_id'])
   try:
    cc=db(); cc.execute('update ws_presence set seen_at=? where user_id=?',(time.time(),u['user_id'])); cc.commit()
   except: pass
 def do_HEAD(self): self.send_response(404);self.send_header('Content-Length','0');self.end_headers()
if __name__=='__main__':
 port=int(sys.argv[1]); db()
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
