import os,sys,json,re,sqlite3,secrets,hashlib,datetime,time,threading,socket,base64,struct,urllib.parse,mimetypes,select,zoneinfo,cgi,subprocess
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
def ensure_redis():
 try:
  z=socket.create_connection(('127.0.0.1',6379),timeout=.15); z.close(); return
 except OSError: pass
 try: subprocess.Popen(['redis-server','--bind','127.0.0.1','--port','6379','--save','','--appendonly','no'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
 except OSError: pass
def ensure_irc():
 try:
  z=socket.create_connection(('127.0.0.1',6667),timeout=.15); z.close(); return
 except OSError: pass
 try:
  subprocess.Popen(['python3','/app/irc.py'],stdout=open('/tmp/huddle-irc.log','ab'),stderr=subprocess.STDOUT,start_new_session=True)
 except OSError: pass
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 ensure_redis()
 ensure_irc()
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,mentions INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,target INTEGER,revoked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator INTEGER);
 CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,created_at TEXT); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id)); CREATE TABLE IF NOT EXISTS dms(user1 INTEGER,user2 INTEGER,channel_id INTEGER,PRIMARY KEY(user1,user2)); CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY)'''); c.commit(); c.close()
init()
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channelobj(r):
 d=dict(r)
 for k in ('is_private','is_dm','is_archived'): d[k]=bool(d.get(k,0))
 return d
def auth(h):
 m=re.search(r'Bearer\s+(.+)',h or '')
 if not m:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(m.group(1),)).fetchone();c.close();return r
def out(h,status,obj,ctype='application/json'):
 b=obj if isinstance(obj,bytes) else json.dumps(obj,separators=(',',':')).encode()
 h.send_response(status);h.send_header('Content-Type',ctype);h.send_header('Content-Length',str(len(b)));h.end_headers();h.wfile.write(b)
def body(h):
 try:return json.loads(h.rfile.read(int(h.headers.get('Content-Length',0)) or 0) or b'{}')
 except:return None
def jmsg(c,r):
 rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)).fetchall()
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
 members=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.workspace_id=?',(ch['workspace_id'],)).fetchall() if ch else []
 mentions=[]
 text=r['body'] or ''
 if '@channel' in text:
  mentions += [x['id'] for x in members if x['id']!=r['author_id'] and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],x['id'])).fetchone()]
 for x in members:
  if x['id']==r['author_id']: continue
  if re.search(r'(?<![A-Za-z0-9_@])@'+re.escape(x['username'])+r'\b',text):
   if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],x['id'])).fetchone(): mentions.append(x['id'])
 for g in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_-]+)',text):
  gr=c.execute('SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],g)).fetchone() if ch else None
  if gr:
   for gm in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gr['id'],)).fetchall():
    uid=gm['user_id']
    if uid!=r['author_id'] and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],uid)).fetchone():mentions.append(uid)
 if '@here' in text:
  for x in members:
   if x['id']!=r['author_id'] and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],x['id'])).fetchone() and c.execute('SELECT 1 FROM online_users WHERE user_id=?',(x['id'],)).fetchone():mentions.append(x['id'])
 mentions=list(dict.fromkeys(mentions))
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(r['id'],)).fetchall()],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rs],'mentions':mentions}
def event(c,ch,kind,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload,created_at) VALUES(?,?,?,?,?)',(ch,n,kind,json.dumps(payload),now())); return n
def ws_frame(data):
 b=data.encode(); l=len(b)
 if l<126:return bytes([129,l])+b
 return bytes([129,126])+struct.pack('>H',l)+b
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x):pass
 def do_GET(self): self.route()
 def do_POST(self): self.route()
 def do_PATCH(self): self.route()
 def do_DELETE(self): self.route()
 def route(self):
  p=urllib.parse.urlparse(self.path); path=p.path; q=urllib.parse.parse_qs(p.query)
  if path=='/api/health':return out(self,200,{'status':'ok','node_id':NODE})
  if path=='/' or path=='/index.html': return out(self,200,HTML,'text/html')
  if path=='/api/ws': return self.websocket(q.get('token',[''])[0])
  u=auth(self.headers.get('Authorization'))
  public=path in ('/api/auth/register','/api/auth/login')
  if not u and not public:return out(self,401,{'error':'unauthorized'})
  c=db()
  if path=='/api/files' and self.command=='POST':
   if not u:return out(self,401,{'error':'unauthorized'})
   if not self.headers.get('Content-Type','').startswith('multipart/form-data'):return out(self,400,{'error':'invalid'})
   try:
    fs=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers.get('Content-Type'),'CONTENT_LENGTH':self.headers.get('Content-Length','0')})
    if 'file' not in fs or not getattr(fs['file'],'filename',None):return out(self,400,{'error':'missing file'})
    part=fs['file']; raw=part.file.read()
    if len(raw)>10*1024*1024:return out(self,413,{'error':'too large'})
    fn=part.filename; ct=part.type or 'application/octet-stream'; ts=now()
    c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,ct,len(raw),ts,raw)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit()
    return out(self,201,{'file':{'id':fid,'uploader_id':u['id'],'filename':fn,'content_type':ct,'size':len(raw),'created_at':ts}})
   except Exception:return out(self,400,{'error':'invalid'})
  fm=re.fullmatch(r'/api/files/(\d+)(/download)?',path)
  if fm and self.command=='GET':
   fr=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at,data FROM files WHERE id=?',(int(fm.group(1)),)).fetchone()
   if not fr:return out(self,404,{'error':'not found'})
   if fm.group(2):
    raw=fr['data']; self.send_response(200);self.send_header('Content-Type',fr['content_type']);self.send_header('Content-Disposition','attachment; filename="'+fr['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
   return out(self,200,{'file':{k:fr[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  data=body(self) if self.command in ('POST','PATCH') else {}
  if self.command in ('POST','PATCH') and data is None:
   return out(self,400,{'error':'invalid json'})
  try:
   if path=='/api/auth/register':
    if not data or not re.fullmatch(r'[A-Za-z0-9_]+',data.get('username','')) or len(data.get('password',''))<8:return out(self,400,{'error':'invalid'})
    try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(data['username'],hashlib.sha256(data['password'].encode()).hexdigest(),data.get('display_name') or data['username']));c.commit()
    except:return out(self,409,{'error':'duplicate'})
    uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();return out(self,201,{'user':userobj(r),'token':t})
   if path=='/api/auth/login':
    r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(data.get('username'),hashlib.sha256(data.get('password','').encode()).hexdigest())).fetchone()
    if not r:return out(self,401,{'error':'invalid'})
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();return out(self,200,{'user':userobj(r),'token':t})
   if path=='/api/auth/me':return out(self,200,{'user':userobj(u)})
   um=re.fullmatch(r'/api/users/(\d+)',path)
   if um and self.command=='GET':
    ur=c.execute('SELECT * FROM users WHERE id=?',(int(um.group(1)),)).fetchone()
    if not ur:return out(self,404,{'error':'not found'})
    return out(self,200,{'user':userobj(ur)})
   if path=='/api/users/me':
    allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; vals={k:v for k,v in data.items() if k in allowed}
    if 'timezone' in vals:
     try: zoneinfo.ZoneInfo(vals['timezone'])
     except: return out(self,400,{'error':'invalid timezone'})
    for k,v in vals.items():
     if not isinstance(v,str) or len(v)>500:return out(self,400,{'error':'invalid'})
    if vals:
     c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],)); c.execute('INSERT INTO profile_events(user_id,created_at) VALUES(?,?)',(u['id'],now())); c.commit()
    return out(self,200,{'user':userobj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())})
   if path=='/api/search' and self.command=='GET':
    term=q.get('q',[''])[0]
    try:
     lim=int(q.get('limit',['50'])[0])
     if lim<1 or lim>200:return out(self,400,{'error':'invalid limit'})
    except:return out(self,400,{'error':'invalid limit'})
    cur=q.get('cursor',[None])[0]; before=None
    if cur:
     try: before=int(base64.urlsafe_b64decode(cur.encode()).decode())
     except:return out(self,400,{'error':'invalid cursor'})
    sql='SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN channel_members cm ON cm.channel_id=ch.id WHERE cm.user_id=? AND m.deleted=0 AND m.body LIKE ?'
    args=[u['id'],'%'+term+'%']
    if q.get('workspace_id'):
     sql+=' AND ch.workspace_id=?';args.append(int(q['workspace_id'][0]))
    if before is not None:sql+=' AND m.id<?';args.append(before)
    sql+=' ORDER BY m.id DESC LIMIT ?';args.append(lim+1)
    rows=c.execute(sql,args).fetchall(); more=len(rows)>lim; rows=rows[:lim]
    nxt=base64.urlsafe_b64encode(str(rows[-1]['id']).encode()).decode() if more and rows else None
    return out(self,200,{'results':[jmsg(c,x) for x in rows],'next_cursor':nxt})
   if path=='/api/dms' and self.command=='POST':
    target=data.get('user_id')
    if not target or not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone() or int(target)==u['id']:return out(self,404,{'error':'not found'})
    a,b=sorted((u['id'],int(target))); old=c.execute('SELECT channel_id FROM dms WHERE user1=? AND user2=?',(a,b)).fetchone()
    if old:return out(self,200,{'channel_id':old['channel_id']})
    c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private,topic) VALUES(NULL,?,?,1,?)',(f'dm-{a}-{b}',1,'')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b));c.commit()
    return out(self,200,{'channel_id':cid})
   if path=='/api/workspaces' and self.command=='POST':
    if not data or 'slug' not in data or 'name' not in data:return out(self,400,{'error':'missing'})
    if not re.fullmatch(r'[a-z0-9-]{2,32}',data.get('slug','')):return out(self,400,{'error':'invalid'})
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(data['slug'],data.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.commit()
    except:return out(self,409,{'error':'duplicate'})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();return out(self,201,{'workspace':dict(w),'general_channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())})
   if path=='/api/workspaces' and self.command=='GET':
    rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();return out(self,200,{'workspaces':[dict(x) for x in rows]})
   am=re.fullmatch(r'/api/invitations/([^/]+)/accept',path)
   if am and self.command=='POST':
    inv=c.execute('SELECT * FROM invites WHERE code=?',(am.group(1),)).fetchone()
    if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['target'] and inv['uses']>=1) or (inv['expires'] and inv['expires']<=time.time()):return out(self,404,{'error':'not found'})
    if inv['target'] and inv['target']!=u['id']:return out(self,403,{'error':'forbidden'})
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(am.group(1),));c.commit()
    return out(self,200,{'workspace_id':inv['workspace_id']})
   m=re.fullmatch(r'/api/workspaces/([^/]+)(?:/(.*))?',path)
   if m:
    slug,tail=m.group(1),m.group(2); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return out(self,404,{'error':'not found'})
    mem=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    mmem=re.fullmatch(r'members/(\d+)',tail or '')
    if mmem and self.command=='PATCH':
     target=int(mmem.group(1)); role=data.get('role')
     tm=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
     actor=mem
     if not tm:return out(self,404,{'error':'not found'})
     if role not in ('owner','admin','member','guest'):return out(self,400,{'error':'invalid role'})
     if role=='owner':return out(self,400,{'error':'use transfer'})
     if not actor or actor['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
     if actor['role']=='admin' and tm['role'] in ('owner','admin'):return out(self,403,{'error':'forbidden'})
     if tm['role']=='owner' or (target==u['id'] and actor['role']=='owner'):return out(self,403,{'error':'forbidden'})
     c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target));c.commit()
     return out(self,200,{'member':{'user_id':target,'role':role}})
    if tail=='members':return out(self,200,{'members':[dict(x) for x in c.execute('SELECT * FROM members WHERE workspace_id=?',(w['id'],))]})
    if tail=='transfer_ownership' and self.command=='POST':
     if not mem or mem['role']!='owner':return out(self,403,{'error':'forbidden'})
     target=data.get('new_owner_id'); tm=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
     if not tm or tm['role']!='admin':return out(self,400,{'error':'target must be admin'})
     c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
     c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit()
     return out(self,200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
    gm=re.fullmatch(r'groups(?:/([^/]+))?',tail or '')
    if gm:
     if not mem:return out(self,403,{'error':'forbidden'})
     handle=gm.group(1)
     if not handle and self.command=='GET':
      gs=[]
      for g in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall():
       ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]
       gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
      return out(self,200,{'groups':gs})
     if not handle and self.command=='POST':
      if not data.get('handle') or c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],data['handle'])).fetchone():return out(self,409,{'error':'duplicate'})
      c.execute('INSERT INTO groups_(workspace_id,handle,name,creator) VALUES(?,?,?,?)',(w['id'],data['handle'],data.get('name',''),u['id']));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
      for uid in data.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
      c.commit(); g=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone()
      return out(self,201,{'group':{'id':gid,'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gid,))]}})
     g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
     if not g:
      if self.command=='DELETE':return out(self,200,{'deleted':True})
      return out(self,404,{'error':'not found'})
     creator=g['creator']==u['id']; admin=mem['role'] in ('owner','admin')
     if self.command=='PATCH':
      if not (creator or admin):return out(self,403,{'error':'forbidden'})
      vals=[];args=[]
      if 'name' in data:vals.append('name=?');args.append(data['name'])
      if vals:c.execute('UPDATE groups_ SET '+','.join(vals)+' WHERE id=?',tuple(args)+(g['id'],))
      if 'user_ids' in data:
       c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
       for uid in data['user_ids']:c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
      c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone()
      return out(self,200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]}})
     if self.command=='DELETE':
      if not (creator or admin):return out(self,403,{'error':'forbidden'})
      c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();return out(self,200,{'deleted':True})
    if tail=='invitations' and self.command=='POST':
     if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
     code=secrets.token_urlsafe(12); exp=data.get('expires_in_seconds'); uses=int(data.get('max_uses',1)); target=data.get('invited_user_id')
     c.execute('INSERT INTO invites(code,workspace_id,created_by,expires,max_uses,target) VALUES(?,?,?,?,?,?)',(code,w['id'],u['id'],time.time()+exp if exp else None,uses,target));c.commit()
     return out(self,201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':uses,'uses':0,'invited_user_id':target}})
    if tail=='invitations' and self.command=='GET':
     if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
     rows=c.execute('SELECT * FROM invites WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires IS NULL OR expires>?)',(w['id'],time.time())).fetchall()
     return out(self,200,{'invitations':[dict(x) for x in rows]})
    im=re.fullmatch(r'invitations/(.+)',tail or '')
    if im and self.command=='DELETE':
     if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
     c.execute('UPDATE invites SET revoked=1 WHERE workspace_id=? AND code=?',(w['id'],im.group(1)));c.commit();return out(self,200,{'deleted':True})
    if self.command=='PATCH' and tail is None:
     if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
     if 'join_mode' in data and data['join_mode'] not in ('open','invite_only'):return out(self,400,{'error':'invalid'})
     vals=[]; args=[]
     for k in ('name','join_mode'):
      if k in data: vals.append(k+'=?');args.append(data[k])
     if vals:c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',tuple(args)+(w['id'],));c.commit()
     return out(self,200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
    if tail is None:
     rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
     ch=[channelobj(x) for x in rows if (not x['is_private'] or c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone() or (mem and mem['role'] in ('owner','admin')))]
     states=[]
     for x in rows:
      rr=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone()
      head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(x['id'],)).fetchone()[0]; last=rr['last_read'] if rr else 0
      states.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':rr['mentions'] if rr else 0})
     return out(self,200,{'workspace':dict(w),'channels':ch,'read_state':states})
    if tail=='channels' and self.command=='GET':
     rows2=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
     visible=[channelobj(x) for x in rows2 if (not x['is_private'] or c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone() or (mem and mem['role'] in ('owner','admin')))]
     return out(self,200,{'channels':visible})
    if tail=='channels' and self.command=='POST':
     if not mem or (mem['role']=='guest') or (data.get('is_private') and mem['role'] not in ('owner','admin')):return out(self,403,{'error':'forbidden'})
     if not re.fullmatch(r'[a-z0-9-]{1,32}',data.get('name','')) or len(data.get('topic',''))>250:return out(self,400,{'error':'invalid'})
     if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],data['name'])).fetchone():return out(self,409,{'error':'duplicate'})
     c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],data['name'],int(bool(data.get('is_private'))),data.get('topic','')));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.commit();return out(self,201,{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())})
   cm=re.fullmatch(r'/api/channels/(\d+)(?:/(.*))?',path)
   if cm:
    cid=int(cm.group(1)); tail=cm.group(2); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return out(self,404,{'error':'not found'})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch['workspace_id'] else None
    mem=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
    if tail in ('archive','unarchive') and self.command=='POST':
     if not mem or mem['role']!='owner':return out(self,403,{'error':'forbidden'})
     val=1 if tail=='archive' else 0
     c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid))
     event(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel_id':cid})
     c.commit()
     return out(self,200,{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    if tail=='join':
     already=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
     if mem and mem['role']=='guest' and not already:return out(self,403,{'error':'forbidden'})
     if not mem and w['join_mode']!='open':return out(self,403,{'error':'forbidden'})
     if ch['is_private'] and not already:return out(self,403,{'error':'forbidden'})
     if not mem and w['join_mode']=='open' and not ch['is_private']:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
     c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']))
     event(c,cid,'member.joined',{'user_id':u['id']});c.commit();return out(self,200,{'channel':channelobj(ch)})
    if tail=='leave':
     c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id']));event(c,cid,'member.left',{'user_id':u['id']});c.commit();return out(self,200,{'channel':channelobj(ch)})
    if tail=='members':return out(self,200,{'members':[userobj(x) for x in c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))]})
    if tail in ('messages','messages/'):
     if self.command=='POST':
      text=str(data.get('body',''))
      if not text:return out(self,400,{'error':'empty'})
      if text.startswith('/') and not text.startswith('//'):
       parts=text.split(None,1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
       if cmd=='/me':
        if not arg.strip():return out(self,400,{'error':'invalid command'})
       elif cmd=='/shrug':
        text=(arg.strip()+' ' if arg.strip() else '')+'¯\\_(ツ)_/¯'
       elif cmd=='/topic':
        if not mem or mem['role'] not in ('owner','admin') and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
        if not arg.strip() or len(arg)>250:return out(self,400,{'error':'invalid topic'})
        c.execute('UPDATE channels SET topic=? WHERE id=?',(arg.strip(),cid));event(c,cid,'channel.updated',{'channel_id':cid,'topic':arg.strip()});c.commit()
        return out(self,201,{'message':None,'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
       elif cmd=='/invite':
        if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
        name=arg.strip().lstrip('@'); target=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
        if not target:return out(self,404,{'error':'not found'})
        c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,target['id']));c.commit()
        return out(self,201,{'message':None,'invited_user':userobj(target)})
       elif cmd in ('/archive','/unarchive'):
        if not mem or mem['role']!='owner':return out(self,403,{'error':'forbidden'})
        val=1 if cmd=='/archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));event(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel_id':cid});c.commit()
        return out(self,201,{'message':None,'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
       else:return out(self,400,{'error':'unknown command'})
      if ch['is_archived']:return out(self,423,{'error':'archived'})
      if text.startswith('/shrug'): data['body']=text
      else:data['body']=text
      fids=data.get('file_ids') or []
      if not isinstance(fids,list) or len(set(fids))!=len(fids):return out(self,400,{'error':'invalid files'})
      for fid in fids:
       fr=c.execute('SELECT uploader_id FROM files WHERE id=?',(fid,)).fetchone()
       if not fr or fr['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return out(self,400,{'error':'invalid files'})
      c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],data['body'],data.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
      for fid in fids:c.execute('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(mid,fid))
      msg=jmsg(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())
      for mentioned in msg['mentions']:
       c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id,last_read,mentions) VALUES(?,?,0,0)',(mentioned,cid))
       c.execute('UPDATE reads SET mentions=mentions+1 WHERE user_id=? AND channel_id=?',(mentioned,cid))
      event(c,cid,'message.reply' if data.get('parent_id') else 'message.created',msg);c.commit();return out(self,201,{'message':msg})
     try:
      lim=int(q.get('limit',['50'])[0])
      if lim<1 or lim>200:return out(self,400,{'error':'invalid limit'})
     except:return out(self,400,{'error':'invalid limit'})
     cur=q.get('cursor',[None])[0]; before=None
     if cur:
      try: before=int(base64.urlsafe_b64decode(cur.encode()).decode())
      except:return out(self,400,{'error':'invalid cursor'})
     sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0'
     args=[cid]
     if before is not None: sql+=' AND id<?';args.append(before)
     sql+=' ORDER BY id DESC LIMIT ?';args.append(lim+1)
     rows=c.execute(sql,args).fetchall(); more=len(rows)>lim; rows=rows[:lim]
     nxt=base64.urlsafe_b64encode(str(rows[-1]['id']).encode()).decode() if more and rows else None
     return out(self,200,{'messages':[jmsg(c,x) for x in rows],'next_cursor':nxt})
    if tail=='read':
     head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; rr=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); last=rr['last_read'] if rr else 0; mentions=rr['mentions'] if rr else 0
     if self.command=='POST':
      try:last=max(last,int(data.get('last_read_event_id',0)))
      except:return out(self,400,{'error':'invalid'})
      c.execute('INSERT OR REPLACE INTO reads(user_id,channel_id,last_read,mentions) VALUES(?,?,?,?)',(u['id'],cid,last,mentions));c.commit()
     return out(self,200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':mentions}})
    if tail=='pins':
     if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
     try:
      lim=int(q.get('limit',['50'])[0])
      if lim<1 or lim>200:return out(self,400,{'error':'invalid limit'})
     except:return out(self,400,{'error':'invalid limit'})
     cur=q.get('cursor',[None])[0]; before=None
     if cur:
      try: before=int(base64.urlsafe_b64decode(cur.encode()).decode())
      except:return out(self,400,{'error':'invalid cursor'})
     sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'
     args=[cid]
     if before is not None:sql+=' AND p.message_id<?';args.append(before)
     sql+=' ORDER BY p.message_id DESC LIMIT ?';args.append(lim+1)
     rows=c.execute(sql,args).fetchall();more=len(rows)>lim;rows=rows[:lim]
     nxt=base64.urlsafe_b64encode(str(rows[-1]['message_id']).encode()).decode() if more and rows else None
     pins=[{'message':jmsg(c,c.execute('SELECT * FROM messages WHERE id=?',(x['message_id'],)).fetchone()),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows]
     return out(self,200,{'pins':pins,'next_cursor':nxt})
    if self.command=='PATCH':
     if 'topic' in data:
      if len(data.get('topic',''))>250:return out(self,400,{'error':'invalid'})
      if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone() and (not mem or mem['role']!='owner'):return out(self,403,{'error':'forbidden'})
      c.execute('UPDATE channels SET topic=? WHERE id=?',(data['topic'],cid))
      event(c,cid,'channel.updated',{'channel_id':cid,'topic':data['topic']});c.commit()
      return out(self,200,{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   rr=re.fullmatch(r'/api/messages/(\d+)/replies',path)
   if rr and self.command=='GET':
    pid=int(rr.group(1))
    if not c.execute('SELECT 1 FROM messages WHERE id=?',(pid,)).fetchone():return out(self,404,{'error':'not found'})
    try:
     lim=int(q.get('limit',['50'])[0])
     if lim<1 or lim>200:return out(self,400,{'error':'invalid limit'})
    except:return out(self,400,{'error':'invalid limit'})
    cur=q.get('cursor',[None])[0]; after=0
    if cur:
     try: after=int(base64.urlsafe_b64decode(cur.encode()).decode())
     except:return out(self,400,{'error':'invalid cursor'})
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id ASC LIMIT ?', (pid,after,lim+1)).fetchall()
    more=len(rows)>lim; rows=rows[:lim]
    nxt=base64.urlsafe_b64encode(str(rows[-1]['id']).encode()).decode() if more and rows else None
    return out(self,200,{'replies':[jmsg(c,x) for x in rows],'next_cursor':nxt})
   pm=re.fullmatch(r'/api/messages/(\d+)/pin',path)
   if pm:
    mid=int(pm.group(1)); mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not mr:return out(self,404,{'error':'not found'})
    if self.command=='POST':
     c.execute('INSERT OR IGNORE INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)',(mid,u['id'],now()));c.commit()
     x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
     return out(self,200,{'pin':{'message_id':mid,'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}})
    if self.command=='DELETE':
     c.execute('DELETE FROM pins WHERE message_id=?',(mid,));c.commit();return out(self,200,{'unpinned':True})
   mm=re.fullmatch(r'/api/messages/(\d+)(?:/reactions(?:/(.*))?)?',path)
   if mm:
    mid=int(mm.group(1));r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return out(self,404,{'error':'not found'})
    if mm.group(2) is not None:
     emoji=urllib.parse.unquote(mm.group(2));c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
     event(c,r['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji});c.commit();return out(self,200,{'reactions':jmsg(c,r)['reactions']})
    if path.endswith('/reactions'):
     if not data.get('emoji'):return out(self,400,{'error':'emoji'})
     c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],data['emoji']))
     event(c,r['channel_id'],'reaction.added',{'message_id':mid,'emoji':data['emoji']});c.commit();return out(self,200,{'reactions':jmsg(c,r)['reactions']})
    if self.command=='PATCH':
     if r['deleted']:return out(self,409,{'error':'deleted'})
     if r['author_id']!=u['id']:return out(self,403,{'error':'forbidden'})
     if 'body' not in data or not str(data.get('body','')).strip():return out(self,400,{'error':'empty'})
     c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(data['body'],now(),mid))
     msg2=jmsg(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone());event(c,r['channel_id'],'message.updated',msg2);c.commit();return out(self,200,{'message':msg2})
    if self.command=='DELETE':
     owner=c.execute('SELECT owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
     if r['author_id']!=u['id'] and (not owner or owner['owner_id']!=u['id']):return out(self,403,{'error':'forbidden'})
     c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));event(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit();return out(self,200,{'deleted':True})
   return out(self,404,{'error':'not found'})
  finally:c.close()
 def websocket(self,token):
  c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone(); c.close()
  if not r:self.send_error(401); return
  c=db(); c.execute('INSERT OR REPLACE INTO online_users(user_id) VALUES(?)',(r['user_id'],)); c.commit(); c.close()
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:self.send_error(400); return
  acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',acc); self.end_headers()
  sock=self.connection; sock.setblocking(False); subs={}; profile_seen=0
  def readn(n):
   out=b''
   while len(out)<n:
    try:
     z=sock.recv(n-len(out))
     if not z:return None
     out+=z
    except BlockingIOError:return None
   return out
  while True:
   try:
    ready,_,_=select.select([sock],[],[],0.03)
    if ready:
     h=sock.recv(2)
     if not h or len(h)<2:break
     ln=h[1]&127
     if ln==126:ln=struct.unpack('>H',readn(2))[0]
     elif ln==127:ln=struct.unpack('>Q',readn(8))[0]
     mask=readn(4); raw=readn(ln)
     if not mask or raw is None:break
     o=json.loads(bytes(v^mask[i%4] for i,v in enumerate(raw))); typ=o.get('type'); cid=int(o.get('channel_id'))
     if typ=='subscribe':
      c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close();subs[cid]=head
      sock.sendall(ws_frame(json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':head})))
     elif typ=='resume':
      c=db();since=int(o.get('since_event_id',0)); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close()
      for e in rows:
       z=json.loads(e['payload']);z.update(type=e['kind'],event_id=e['event_id'],channel_id=cid);sock.sendall(ws_frame(json.dumps(z)))
      sock.sendall(ws_frame(json.dumps({'type':'resumed','channel_id':cid,'head_event_id':head})));subs[cid]=head
   except (BrokenPipeError,ConnectionResetError,ValueError,struct.error,TypeError):break
   c=db(); prows=c.execute('SELECT * FROM profile_events WHERE id>? ORDER BY id',(profile_seen,)).fetchall()
   for pe in prows:
    ur=c.execute('SELECT * FROM users WHERE id=?',(pe['user_id'],)).fetchone()
    try:sock.sendall(ws_frame(json.dumps({'type':'user.updated','user':userobj(ur)})));profile_seen=pe['id']
    except (BrokenPipeError,ConnectionResetError):return
   c.close()
   for cid,last in list(subs.items()):
    c=db();rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall();c.close()
    for e in rows:
     z=json.loads(e['payload']);z.update(type=e['kind'],event_id=e['event_id'],channel_id=cid)
     try:sock.sendall(ws_frame(json.dumps(z)));subs[cid]=e['event_id']
     except (BrokenPipeError,ConnectionResetError):return

HTML=b'''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
body{font-family:Arial;margin:0;color:#222}.app{display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:#fff;padding:20px}.main{padding:30px;flex:1}.thread{width:320px;padding:20px;border-left:1px solid #ddd}input{padding:10px;margin:5px}button{padding:9px;border:0;color:#fff;margin:3px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}
</style></head><body>
<div id=auth-modal><form id=auth-form data-testid=auth-form><h1>Sign in</h1><input name=username placeholder=username><input name=password type=password placeholder=password><button id=auth-submit data-testid=auth-submit data-button-role=primary class=primary>Sign in</button><button id=auth-toggle data-testid=auth-toggle type=button>Sign up</button></form></div>
<div id=app class=app><aside class=side>
<h2 data-testid=workspace-header id=workspace-header>Huddle</h2><div data-testid=current-user id=current-user></div>
<button id=logout-btn data-testid=logout-btn data-button-role=danger class=danger>Logout</button>
<form id=workspace-create-form data-testid=workspace-create-form><input name=workspace placeholder="Workspace name"><button data-button-role=primary class=primary>New workspace</button></form>
button
<div id=empty-state-create-workspace data-testid=empty-state-create-workspace></div>
<form id=join-workspace-form data-testid=join-workspace-form><input name=join placeholder="Workspace slug"><button id=join-workspace-submit data-testid=join-workspace-submit data-button-role=primary class=primary>Join</button></form>
<div id=join-workspace-error data-testid=join-workspace-error></div>
<button id=workspace-settings-btn data-testid=workspace-settings-btn data-button-role=secondary class=secondary>Workspace settings</button>
<div id=channel-list data-testid=channel-list></div><div id=dms-list data-testid=dms-list></div>
<button id=new-channel-btn data-testid=new-channel-btn data-button-role=secondary class=secondary>New channel</button>
</aside><main class=main>
<h2 id=channel-title data-testid=channel-title></h2><div id=channel-topic data-testid=channel-topic></div>
<button id=channel-settings-btn data-testid=channel-settings-btn data-button-role=secondary class=secondary>Channel settings</button>
<div id=message-list data-testid=message-list></div><input id=message-input data-testid=message-input><button id=send-btn data-testid=send-btn data-button-role=primary class=primary>Send</button>
</main><aside id=thread-panel data-testid=thread-panel class=thread><button id=close-thread data-testid=close-thread data-button-role=secondary class=secondary>Close</button><input id=thread-input data-testid=thread-input><button id=thread-send data-testid=thread-send data-button-role=primary class=primary>Reply</button></aside></div>
<div id=create-channel-modal><form id=create-channel-form data-testid=create-channel-form><input name=name><label><input id=create-channel-private data-testid=create-channel-private type=checkbox>Private</label><button id=create-channel-submit data-testid=create-channel-submit data-button-role=primary class=primary>Create</button><button id=create-channel-cancel data-testid=create-channel-cancel data-button-role=secondary class=secondary>Cancel</button></form></div>
<div id=channel-settings-modal><h3 id=channel-settings-title data-testid=channel-settings-title>Channel settings</h3><button id=channel-settings-close data-testid=channel-settings-close data-button-role=secondary class=secondary>Close</button><div id=channel-members-list data-testid=channel-members-list></div><form id=channel-add-member-form data-testid=channel-add-member-form><input id=channel-add-member-input data-testid=channel-add-member-input><button id=channel-add-member-submit data-testid=channel-add-member-submit data-button-role=primary class=primary>Add</button></form><input id=channel-topic-input data-testid=channel-topic-input><button id=channel-topic-submit data-testid=channel-topic-submit data-button-role=primary class=primary>Save topic</button><button id=archive-channel-btn data-testid=archive-channel-btn data-button-role=danger class=danger>Archive</button><button id=unarchive-channel-btn data-testid=unarchive-channel-btn data-button-role=secondary class=secondary>Unarchive</button></div>
<div id=workspace-settings-modal><button id=workspace-settings-close data-testid=workspace-settings-close data-button-role=secondary class=secondary>Close</button><input id=workspace-name-input data-testid=workspace-name-input><select id=workspace-join-mode data-testid=workspace-join-mode></select><button id=workspace-general-submit data-testid=workspace-general-submit data-button-role=primary class=primary>Save</button><button id=settings-tab-members data-testid=settings-tab-members>Members</button><button id=settings-tab-invitations data-testid=settings-tab-invitations>Invitations</button><button id=settings-tab-general data-testid=settings-tab-general>General</button><div id=settings-pane-members data-testid=settings-pane-members></div><div id=settings-pane-invitations data-testid=settings-pane-invitations></div><div id=settings-pane-general data-testid=settings-pane-general></div><div id=member-row data-testid=member-row></div><div id=role-badge data-testid=role-badge></div><select id=role-select data-testid=role-select></select><button id=create-invitation-btn data-testid=create-invitation-btn data-button-role=secondary class=secondary>Create invitation</button><form id=create-invitation-form data-testid=create-invitation-form><button id=create-invitation-submit data-testid=create-invitation-submit data-button-role=primary class=primary>Create</button></form><div id=invitations-list data-testid=invitations-list></div></div>
<script>const A='/api';let t=localStorage.getItem('huddle.token');authForm.onsubmit=async e=>{e.preventDefault();let f=new FormData(authForm),r=await fetch(A+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(f))});let j=await r.json();if(r.ok){t=j.token;localStorage.setItem('huddle.token',t);auth-modal.style.display='none';load()}};logout-btn.onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};async function load(){let r=await fetch(A+'/workspaces',{headers:{Authorization:'Bearer '+t}}),j=await r.json();if(j.workspaces?.[0]){let w=j.workspaces[0];workspace-header.textContent=w.name;let d=await fetch(A+'/workspaces/'+w.slug,{headers:{Authorization:'Bearer '+t}}),x=await d.json();channel-list.innerHTML=(x.channels||[]).map(c=>'<button data-testid=channel-entry data-channel-id='+c.id+' data-channel-name='+c.name+'>#'+c.name+'</button>').join('');channel-list.onclick=e=>{if(e.target.dataset.channelId)show(e.target.dataset.channelId)}}}async function show(id){let r=await fetch(A+'/channels/'+id+'/messages',{headers:{Authorization:'Bearer '+t}}),j=await r.json();message-list.innerHTML=(j.messages||[]).map(m=>'<div data-testid=message><b>'+m.author.display_name+'</b> <span data-testid=message-body>'+m.body+'</span></div>').join('')}send-btn.onclick=async()=>{let id=document.querySelector('[data-channel-id]')?.dataset.channelId;if(id&&message-input.value.trim())await fetch(A+'/channels/'+id+'/messages',{method:'POST',headers:{Authorization:'Bearer '+t,'Content-Type':'application/json'},body:JSON.stringify({body:message-input.value})});if(id)show(id)};if(t){auth-modal.style.display='none';load()}</script></body></html>'''

if __name__=='__main__': ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
