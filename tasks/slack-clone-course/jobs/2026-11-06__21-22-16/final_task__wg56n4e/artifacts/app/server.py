#!/usr/bin/env python3
import os,sys,json,re,sqlite3,hashlib,secrets,base64,struct,socket,threading,time,urllib.parse,cgi,io,fcntl
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
db=sqlite3.connect(DB,check_same_thread=False,timeout=2); db.execute('PRAGMA busy_timeout=2000'); db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=NORMAL'); lock=threading.RLock()
def init():
 with lock:
  db.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS chanmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS attachments(file_id INTEGER PRIMARY KEY,message_id INTEGER);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER, UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS groupmembers(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS event_seq(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);'''); db.commit()
init()
sockets=[]; socklock=threading.Lock(); irc_clients=[]; irclock=threading.Lock()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+f'{int(time.time()*1000)%1000:03d}Z'
def q(sql,args=(),one=False):
 for attempt in range(20):
  try:
   with lock:
    c=db.execute(sql,args); rows=c.fetchall()
   return (rows[0] if rows else None) if one else rows
  except sqlite3.OperationalError as e:
   if 'locked' not in str(e).lower() or attempt==19: raise
   time.sleep(0.025*(attempt+1))
def commit(sql,args=()):
 for attempt in range(40):
  try:
   with open('/app/data/write.lock','w') as lf:
    fcntl.flock(lf,fcntl.LOCK_EX)
    with lock:
     db.execute(sql,args); db.commit()
    fcntl.flock(lf,fcntl.LOCK_UN)
   return
  except sqlite3.OperationalError as e:
   try: db.rollback()
   except Exception: pass
   if 'locked' not in str(e).lower() or attempt==39: raise
   time.sleep(0.025*(attempt+1))
def userobj(r):
 return {'id':r[0],'username':r[1],'display_name':r[3] or r[1],'timezone':r[4] or 'UTC','avatar_url':r[5] or '','status_text':r[6] or '','status_emoji':r[7] or ''}
def auth(h):
 m=re.search(r'Bearer\s+(.+)',h or '')
 if not m:return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(m.group(1),),True); return r
def channel_obj(r): return {'id':r[0],'workspace_id':r[1],'name':r[2],'is_private':bool(r[3]),'is_dm':bool(r[4]),'topic':r[5] or '','is_archived':bool(r[6])}
def emit(ch,kind,payload):
 msg=payload.copy()
 if kind in ('message.created','message.reply') and isinstance(msg.get('message'),dict):
  eid=int(msg['message'].get('event_id') or 0)
  commit('INSERT OR REPLACE INTO event_seq(channel_id,event_id) VALUES(?,?)',(ch,eid))
 else:
  row=q('SELECT event_id FROM event_seq WHERE channel_id=?',(ch,),True); eid=(row[0] if row else 0)+1
  commit('INSERT OR REPLACE INTO event_seq(channel_id,event_id) VALUES(?,?)',(ch,eid))
 msg.update(type=kind,event_id=eid,channel_id=ch,origin_pid=os.getpid())
 with open('/app/data/events.log','a') as ef:
  ef.write(json.dumps(msg,separators=(',',':'))+'\n')
 if kind in ('message.created','message.reply') and isinstance(msg.get('message'),dict):
  cc=q('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(ch,),True)
  if cc:
   body=msg['message'].get('body',''); author=(msg['message'].get('author') or {}).get('username','web')
   with irclock:
    for x in list(irc_clients):
     if ch in x.get('channels',[]):
      try: x['send'](':%s!%s@host PRIVMSG #%s/%s :%s' % (author,author,cc[1],cc[0],body))
      except: pass
 return eid
def msgobj(r):
 author=q('SELECT * FROM users WHERE id=?',(r[2],),True)
 rr=q('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r[0],))
 d={'id':r[0],'channel_id':r[1],'author_id':r[2],'author':userobj(author),'body':r[3],'parent_id':r[4],'reply_count':q('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r[0],),True)[0],'created_at':r[5],'edited_at':r[6],'files':[dict(zip(('id','uploader_id','filename','content_type','size','created_at'),f[:6])) for f in q('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r[0],))],'reactions':[],'mentions':[],'event_id':r[8]}
 for e in sorted(set(x[0] for x in rr)): d['reactions'].append({'emoji':e,'count':sum(y[0]==e for y in rr),'user_ids':[y[1] for y in rr if y[0]==e]})
 members=[x[0] for x in q('SELECT user_id FROM chanmembers WHERE channel_id=?',(r[1],))]
 for handle in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',r[3] or ''):
  ids=[]
  if handle in ('channel','here'):
   ids=members
   if handle=='here':
    with socklock:
     active=[x[0] for x in sockets if x[1]==r[1]]
    ids=[x for x in ids if any(True for _ in active)]
  else:
   z=q('SELECT id FROM users WHERE username=?',(handle,),True)
   if z: ids=[z[0]]
   else:
    g=q('SELECT id FROM groups_ WHERE handle=? AND workspace_id=(SELECT workspace_id FROM channels WHERE id=?)',(handle,r[1]),True)
    if g:
     try: ov=json.load(open('/app/data/group_overrides.json'))
     except Exception: ov={}
     ids=ov.get(str(g[0]),{}).get('member_user_ids',[x[0] for x in q('SELECT user_id FROM groupmembers WHERE group_id=?',(g[0],))])
  for uid in ids:
   if uid!=r[2] and uid in members and uid not in d['mentions']: d['mentions'].append(uid)
 return d
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,code,obj,headers=None):
  b=json.dumps(obj).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); 
  for k,v in (headers or {}).items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n) if n else b'{}'
 def json(self):
  try:return json.loads(self.body())
  except:return None
 def routepath(self): return urllib.parse.urlsplit(self.path).path
 def do_GET(self):
  p=self.routepath(); u=auth(self.headers.get('Authorization'))
  if p=='/': return self.html()
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if p.startswith('/api/ws'):
   if not u:
    tok=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('token',[''])[0]; u=auth('Bearer '+tok)
   return self.websocket(u)
  if p=='/api/auth/me': return self.sendj(200,{'user':userobj(u)}) if u else self.sendj(401,{})
  if p.startswith('/api/users/'):
   if not u:return self.sendj(401,{})
   x=q('SELECT * FROM users WHERE id=?',(p.split('/')[-1],),True); return self.sendj(200,{'user':userobj(x)}) if x else self.sendj(404,{})
  if not u:return self.sendj(401,{})
  if p=='/api/workspaces': return self.sendj(200,{'workspaces':[dict(zip(('id','slug','name','owner_id','join_mode'),x)) for x in q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u[0],))]})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(.*))?$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   tail=m.group(2) or ''
   if tail=='invitations':
    role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True)
    if not role or role[0] not in ('owner','admin'): return self.sendj(403,{})
    rows=[{'code':x[0],'workspace_id':x[1],'creator_id':x[2],'expires_at':x[3],'max_uses':x[4],'uses':x[5],'invited_user_id':x[6]} for x in q('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND revoked=0 AND (expires_at IS NULL OR expires_at>?)',(w[0],time.time()))]
    return self.sendj(200,{'invitations':rows})
   if tail=='members': return self.sendj(200,{'members':[{'user_id':x[0],'role':x[1]} for x in q('SELECT user_id,role FROM members WHERE workspace_id=?',(w[0],))]})
   if tail=='groups': return self.sendj(200,{'groups':self.groups(w[0])})
   cs=[]
   inc=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('include_archived',['0'])[0] in ('1','true')
   for x in q('SELECT * FROM channels WHERE workspace_id=?'+('' if inc else ' AND is_archived=0'),(w[0],)):
    if x[3] and not q('SELECT 1 FROM chanmembers WHERE channel_id=? AND user_id=?',(x[0],u[0]),True): continue
    cs.append(channel_obj(x))
   if tail=='channels': return self.sendj(200,{'channels':cs})
   return self.sendj(200,{'workspace':dict(zip(('id','slug','name','owner_id','join_mode'),w)),'channels':cs,'read_state':[self.readstate(u[0],x['id']) for x in cs]})
  m=re.match(r'/api/messages/(\d+)/replies$',p)
  if m:
   parent=int(m.group(1)); pr=q('SELECT * FROM messages WHERE id=?',(parent,),True)
   if not pr:return self.sendj(404,{})
   qs=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); raw=qs.get('limit',['50'])[0]
   try: limit=int(raw)
   except: return self.sendj(400,{})
   if limit<1 or limit>200:return self.sendj(400,{})
   cur=qs.get('cursor',[None])[0]; after=None
   if cur is not None:
    try: after=int(cur)
    except: return self.sendj(400,{})
   sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0'+(' AND id>?' if after is not None else '')+' ORDER BY id ASC LIMIT ?'
   rows=q(sql,(parent,after,limit+1) if after is not None else (parent,limit+1)); more=len(rows)>limit; rows=rows[:limit]
   return self.sendj(200,{'replies':[msgobj(x) for x in rows],'next_cursor':str(rows[-1][0]) if more and rows else None})
  m=re.match(r'/api/channels/(\d+)(?:/(.*))?$',p)
  if m:
   cid=int(m.group(1)); tail=m.group(2) or ''; c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.sendj(404,{})
   if tail=='replies':
    qs=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); raw=qs.get('limit',['50'])[0]
    try: limit=int(raw)
    except: return self.sendj(400,{})
    if limit<1 or limit>200:return self.sendj(400,{})
    cur=qs.get('cursor',[None])[0]; after=None
    if cur is not None:
     try: after=int(cur)
     except: return self.sendj(400,{})
    sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0'+(' AND id>?' if after is not None else '')+' ORDER BY id ASC LIMIT ?'
    args=(cid,after,limit+1) if after is not None else (cid,limit+1)
    rows=q(sql,args); more=len(rows)>limit; rows=rows[:limit]
    return self.sendj(200,{'replies':[msgobj(x) for x in rows],'next_cursor':str(rows[-1][0]) if more and rows else None})
   if tail=='members': return self.sendj(200,{'members':[userobj(q('SELECT * FROM users WHERE id=?',(x[0],),True)) for x in q('SELECT user_id FROM chanmembers WHERE channel_id=?',(cid,))]})
   if tail=='read': return self.sendj(200,{'read_state':self.readstate(u[0],cid)})
   if tail=='messages':
    if c[4] and not q('SELECT 1 FROM chanmembers WHERE channel_id=? AND user_id=?',(cid,u[0]),True): return self.sendj(403,{})
    qs=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); raw=qs.get('limit',['50'])[0]
    try: limit=int(raw)
    except: return self.sendj(400,{})
    if limit<1 or limit>200:return self.sendj(400,{})
    cur=qs.get('cursor',[None])[0]; before=None
    if cur is not None:
     try: before=int(cur)
     except: return self.sendj(400,{})
    sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0'+(' AND id<?' if before is not None else '')+' ORDER BY id DESC LIMIT ?'
    args=(cid,before,limit+1) if before is not None else (cid,limit+1)
    rows=q(sql,args); more=len(rows)>limit; rows=rows[:limit]
    return self.sendj(200,{'messages':[msgobj(x) for x in rows],'next_cursor':str(rows[-1][0]) if more and rows else None})
   if tail=='pins':
    if c[3] and not q('SELECT 1 FROM chanmembers WHERE channel_id=? AND user_id=?',(cid,u[0]),True): return self.sendj(403,{})
    qs=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); raw=qs.get('limit',['50'])[0]
    try: limit=int(raw)
    except: return self.sendj(400,{})
    if limit<1 or limit>200:return self.sendj(400,{})
    cur=qs.get('cursor',[None])[0]; before=None
    if cur is not None:
     try: before=int(cur)
     except: return self.sendj(400,{})
    sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'+(' AND p.message_id<?' if before is not None else '')+' ORDER BY p.message_id DESC LIMIT ?'
    rows=q(sql,(cid,before,limit+1) if before is not None else (cid,limit+1)); more=len(rows)>limit; rows=rows[:limit]
    out=[{'message':msgobj(x[3:]),'pinned_by':x[1],'pinned_at':x[2]} for x in rows]
    return self.sendj(200,{'pins':out,'next_cursor':str(rows[-1][0]) if more and rows else None})
  if p.startswith('/api/files/'):
   f=q('SELECT id,uploader_id,filename,content_type,size,created_at,data FROM files WHERE id=?',(p.split('/')[3],),True)
   if not f:return self.sendj(404,{})
   if p.endswith('/download'):
    b=f[6]; self.send_response(200); self.send_header('Content-Type',f[3]); self.send_header('Content-Disposition',f'attachment; filename="{f[2]}"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); return self.wfile.write(b)
   return self.sendj(200,{'file':dict(zip(('id','uploader_id','filename','content_type','size','created_at'),f[:6]))})
  if p=='/api/search':
   qs=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); term=qs.get('q',[''])[0]; raw=qs.get('limit',['50'])[0]
   try: limit=int(raw)
   except: return self.sendj(400,{})
   if limit<1 or limit>200:return self.sendj(400,{})
   cur=qs.get('cursor',[None])[0]; before=None
   if cur is not None:
    try: before=int(cur)
    except: return self.sendj(400,{})
   wid=qs.get('workspace_id',[None])[0]
   sql='SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? AND EXISTS (SELECT 1 FROM members wm WHERE wm.workspace_id=c.workspace_id AND wm.user_id=?) AND (c.is_private=0 OR EXISTS (SELECT 1 FROM chanmembers cm WHERE cm.channel_id=c.id AND cm.user_id=?))'
   args=['%'+term+'%',u[0],u[0]]
   if wid is not None: sql+=' AND c.workspace_id=?'; args.append(wid)
   if before is not None: sql+=' AND m.id<?'; args.append(before)
   sql+=' ORDER BY m.id DESC LIMIT ?'; args.append(limit+1)
   rows=q(sql,tuple(args)); more=len(rows)>limit; rows=rows[:limit]
   return self.sendj(200,{'results':[msgobj(x) for x in rows],'next_cursor':str(rows[-1][0]) if more and rows else None})
  return self.sendj(404,{})
 def do_POST(self):
  p=self.routepath(); u=auth(self.headers.get('Authorization')); d={} if p=='/api/files' else self.json()
  if d is None and p not in ('/api/auth/register','/api/auth/login'): return self.sendj(400,{})
  if p in ('/api/auth/register','/api/auth/login') and d is None:return self.sendj(400,{})
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(d.get('password',''))<8:return self.sendj(400,{})
   try:
    commit('INSERT INTO users(username,password,display_name,timezone) VALUES(?,?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username'],'UTC')); uid=q('SELECT last_insert_rowid()',(),True)[0]; tok=secrets.token_urlsafe(24); commit('INSERT INTO tokens VALUES(?,?)',(tok,uid)); return self.sendj(201,{'user':userobj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':tok})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  if p=='/api/auth/login':
   x=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password')),True)
   if not x:return self.sendj(401,{})
   tok=secrets.token_urlsafe(24);commit('INSERT INTO tokens VALUES(?,?)',(tok,x[0]));return self.sendj(200,{'user':userobj(x),'token':tok})
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None
   if not w:return self.sendj(404,{})
   if not role or role[0] not in ('owner','admin'):return self.sendj(403,{})
   code=secrets.token_urlsafe(12); target=d.get('invited_user_id'); uses=int(d.get('max_uses',1))
   if uses<1:return self.sendj(400,{})
   exp=time.time()+int(d['expires_in_seconds']) if 'expires_in_seconds' in d else None
   commit('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w[0],u[0],exp,uses,0,target))
   return self.sendj(201,{'invitation':{'code':code,'workspace_id':w[0],'max_uses':uses,'invited_user_id':target}})
  m=re.match(r'/api/invitations/([^/]+)/accept$',p)
  if m:
   inv=q('SELECT * FROM invitations WHERE code=?',(m.group(1),),True)
   if not inv:return self.sendj(404,{})
   if inv[6] is not None and inv[6]!=u[0]:return self.sendj(403,{})
   if inv[7] or (inv[3] and inv[3]<time.time()) or inv[5]>=inv[4] or (inv[6] is not None and inv[5]>=1):return self.sendj(404,{})
   commit('UPDATE invitations SET uses=uses+1 WHERE code=?',(m.group(1),))
   commit('INSERT OR REPLACE INTO members VALUES(?,?,?)',(inv[1],u[0],'member'))
   return self.sendj(200,{'workspace_id':inv[1]})
  if p=='/api/files':
   if not self.headers.get('Content-Type','').startswith('multipart/form-data'): return self.sendj(400,{})
   try:
    fs=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers['Content-Type']})
    if 'file' not in fs:return self.sendj(400,{})
    part=fs['file']; data=part.file.read()
    if len(data)>10*1024*1024:return self.sendj(413,{})
    fn=part.filename or 'file'; ct=part.type or 'application/octet-stream'
    commit('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u[0],fn,ct,len(data),now(),data))
    f=q('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=last_insert_rowid()',(),True)
    return self.sendj(201,{'file':dict(zip(('id','uploader_id','filename','content_type','size','created_at'),f))})
   except Exception:return self.sendj(400,{})
  if p=='/api/workspaces':
   if not isinstance(d,dict) or not d.get('slug') or not d.get('name') or not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))):return self.sendj(400,{})
   try:
    commit('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(d['slug'],d.get('name',''),u[0],'open')); w=q('SELECT last_insert_rowid()',(),True)[0];commit('INSERT INTO members VALUES(?,?,?)',(w,u[0],'owner'));commit('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general')); c=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True);return self.sendj(201,{'workspace':dict(zip(('id','slug','name','owner_id','join_mode'),q('SELECT * FROM workspaces WHERE id=?',(w,),True))),'general_channel':channel_obj(c)})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None
   if not w:return self.sendj(404,{})
   if not role:return self.sendj(403,{})
   h=d.get('handle',''); name=d.get('name','')
   if not re.fullmatch(r'[A-Za-z0-9_-]+',str(h)) or not name:return self.sendj(400,{})
   try:
    commit('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w[0],h,name,u[0]))
    g=q('SELECT last_insert_rowid()',(),True)[0]
    for uid in d.get('user_ids',[]): 
     if q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w[0],uid),True): commit('INSERT OR IGNORE INTO groupmembers VALUES(?,?)',(g,uid))
    return self.sendj(201,{'group':self.groups(w[0])[-1]})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True)
   if not role or role[0]=='guest':return self.sendj(403,{})
   if not isinstance(d,dict) or not d.get('name') or not re.fullmatch(r'[a-z0-9-]{1,32}',str(d.get('name',''))) or len(str(d.get('topic','')))>250:return self.sendj(400,{})
   if d.get('is_private') and role[0] not in ('owner','admin'):return self.sendj(403,{})
   try:
    commit('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w[0],d.get('name'),int(bool(d.get('is_private'))),d.get('topic','')))
    cc=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True); commit('INSERT OR IGNORE INTO chanmembers VALUES(?,?)',(cc[0],u[0])); return self.sendj(201,{'channel':channel_obj(cc)})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',p)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c[1],u[0]),True)
   if c[3] and not q('SELECT 1 FROM chanmembers WHERE channel_id=? AND user_id=?',(c[0],u[0]),True): return self.sendj(403,{})
   if role and role[0]=='guest': return self.sendj(403,{})
   if not role:
    wm=q('SELECT join_mode FROM workspaces WHERE id=?',(c[1],),True)
    if c[3] or (wm and wm[0]=='invite_only'): return self.sendj(403,{})
    commit('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c[1],u[0],'member'))
   commit('INSERT OR IGNORE INTO chanmembers VALUES(?,?)',(c[0],u[0]));commit('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c[1],u[0],'member'));emit(c[0],'member.joined',{'user_id':u[0]});return self.sendj(200,{'channel':channel_obj(c)})
  m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); target=int(d.get('new_owner_id',0)); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None; tr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],target),True) if w else None
   if not w:return self.sendj(404,{})
   if not role or role[0]!='owner':return self.sendj(403,{})
   if not tr or tr[0]!='admin':return self.sendj(400,{})
   commit('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w[0],u[0]));commit('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w[0],target));commit('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w[0]));return self.sendj(200,{'workspace':dict(zip(('id','slug','name','owner_id','join_mode'),q('SELECT * FROM workspaces WHERE id=?',(w[0],),True)))})
  m=re.match(r'/api/channels/(\d+)/(leave|archive|unarchive)$',p)
  if m:
   cid=int(m.group(1)); action=m.group(2); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.sendj(404,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c[1],u[0]),True)
   if action=='leave':
    commit('DELETE FROM chanmembers WHERE channel_id=? AND user_id=?',(cid,u[0])); emit(cid,'member.left',{'user_id':u[0]}); return self.sendj(200,{'channel':channel_obj(c)})
   if not role or role[0]!='owner':return self.sendj(403,{})
   commit('UPDATE channels SET is_archived=? WHERE id=?',(1 if action=='archive' else 0,cid)); emit(cid,'channel.archived' if action=='archive' else 'channel.unarchived',{'channel':channel_obj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   return self.sendj(200,{'channel':channel_obj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  m=re.match(r'/api/channels/(\d+)/messages$',p)
  if m:return self.message(int(m.group(1)),u,d)
  m=re.match(r'/api/messages/(\d+)/pin$',p)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.sendj(404,{})
   commit('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r[0],u[0],now()))
   z=q('SELECT * FROM pins WHERE message_id=?',(r[0],),True);return self.sendj(200,{'pin':{'message_id':z[0],'pinned_by':z[1],'pinned_at':z[2]}})
  m=re.match(r'/api/messages/(\d+)/reactions$',p)
  if m:
   mid=int(m.group(1)); x=q('SELECT channel_id FROM messages WHERE id=?',(mid,),True)
   if not x:return self.sendj(404,{})
   if not isinstance(d.get('emoji'),str) or not d.get('emoji'): return self.sendj(400,{})
   commit('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u[0],d.get('emoji',''))); emit(x[0],'reaction.added',{'message_id':mid,'emoji':d.get('emoji',''),'user_id':u[0]}); return self.sendj(200,{'reactions':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))['reactions']})
  if p=='/api/dms':
   other=int(d.get('user_id')); target=q('SELECT id FROM users WHERE id=?',(other,),True)
   if not target:return self.sendj(404,{})
   a=min(u[0],other);b=max(u[0],other); key=f'dm-{a}-{b}'; c=q('SELECT * FROM channels WHERE name=?',(key,),True)
   if not c:
    commit('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,key)); c=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True)
   commit('INSERT OR IGNORE INTO chanmembers VALUES(?,?)',(c[0],u[0])); commit('INSERT OR IGNORE INTO chanmembers VALUES(?,?)',(c[0],other))
   return self.sendj(200,{'channel_id':c[0]})
  if re.match(r'/api/channels/\d+/read$',p):
   cid=int(p.split('/')[3]); c=q('SELECT id FROM channels WHERE id=?',(cid,),True)
   if not c:return self.sendj(404,{})
   old=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u[0],cid),True); val=max(old[0] if old else 0,int(d.get('last_read_event_id',0))); commit('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u[0],cid,val)); return self.sendj(200,{'read_state':self.readstate(u[0],cid)})
  return self.sendj(404,{})
 def message(self,cid,u,d):
  c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
  if not c:return self.sendj(404,{})
  file_ids=d.get('file_ids') or []
  if not isinstance(file_ids,list): return self.sendj(400,{})
  for fid in file_ids:
   f=q('SELECT uploader_id FROM files WHERE id=?',(fid,),True)
   if not f or f[0]!=u[0] or q('SELECT 1 FROM attachments WHERE file_id=?',(fid,),True): return self.sendj(400,{})
  body=d.get('body','')
  if not isinstance(body,str) or not body.strip():return self.sendj(400,{})
  if body.startswith('//'):
   pass
  elif body.startswith('/'):
   cmd=body.split(None,1)[0]
   arg=body[len(cmd):].strip()
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c[1],u[0]),True)
   if cmd=='/me':
    if not arg:return self.sendj(400,{})
   elif cmd=='/shrug':
    body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
   elif cmd=='/topic':
    if not arg:return self.sendj(400,{})
    commit('UPDATE channels SET topic=? WHERE id=?',(arg,cid))
    return self.sendj(201,{'message':None,'channel':channel_obj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   elif cmd=='/invite':
    if not arg.startswith('@'):return self.sendj(400,{})
    target=q('SELECT * FROM users WHERE username=?',(arg[1:],),True)
    if not target:return self.sendj(404,{})
    if not role or role[0] not in ('owner','admin'):return self.sendj(403,{})
    commit('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c[1],target[0],'member'))
    commit('INSERT OR IGNORE INTO chanmembers VALUES(?,?)',(cid,target[0]))
    return self.sendj(201,{'message':None,'invited_user':userobj(target)})
   elif cmd in ('/archive','/unarchive'):
    if not role or role[0]!='owner':return self.sendj(403,{})
    archived=1 if cmd=='/archive' else 0
    commit('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid))
    return self.sendj(201,{'message':None,'channel':channel_obj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   else:return self.sendj(400,{})
  if c[6]:return self.sendj(423,{})
  mid=None
  t=now(); parent=d.get('parent_id')
  commit('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,COALESCE((SELECT MAX(event_id)+1 FROM messages WHERE channel_id=?),1))',(cid,u[0],body,parent,t,cid))
  mid=q('SELECT last_insert_rowid()',(),True)[0]
  for fid in file_ids: commit('INSERT INTO attachments(file_id,message_id) VALUES(?,?)',(fid,mid))
  r=q('SELECT * FROM messages WHERE id=?',(mid,),True); mo=msgobj(r)
  emit(cid,'message.reply' if parent else 'message.created',{'message':mo})
  return self.sendj(201,{'message':mo})
 def groups(self,wid):
  try:
   ov=json.load(open('/app/data/group_overrides.json'))
  except Exception: ov={}
  out=[]
  for g in q('SELECT id,handle,name,creator_id FROM groups_ WHERE workspace_id=?',(wid,)):
   key=str(g[0])
   if ov.get(key,{}).get('deleted'): continue
   ids=[x[0] for x in q('SELECT user_id FROM groupmembers WHERE group_id=?',(g[0],))]
   z={'id':g[0],'handle':g[1],'name':g[2],'member_user_ids':ids}
   z.update({k:v for k,v in ov.get(key,{}).items() if k in ('name','member_user_ids')})
   out.append(z)
  return out
 def group_overlay(self,gid,changes):
  path='/app/data/group_overrides.json'
  try: ov=json.load(open(path))
  except Exception: ov={}
  ov.setdefault(str(gid),{}).update(changes)
  tmp=path+'.tmp'
  with open(tmp,'w') as f: json.dump(ov,f)
  os.replace(tmp,path)
 def readstate(self,uid,cid):
  x=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(uid,cid),True)
  z=q('SELECT username FROM users WHERE id=?',(uid,),True)
  count=0
  if z:
   pat=re.compile(r'(?<!\w)@'+re.escape(z[0])+r'\b')
   for (body,) in q('SELECT body FROM messages WHERE channel_id=? AND deleted=0 AND author_id<>?',(cid,uid)):
    if pat.search(body or ''): count+=1
  return {'channel_id':cid,'last_read_event_id':x[0] if x else 0,'unread_count':0,'mention_count':count}
 def do_PATCH(self):
  p=self.routepath();u=auth(self.headers.get('Authorization'));d=self.json()
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); target=int(m.group(2)); actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None
   if not w or not actor:return self.sendj(404,{})
   if d.get('role') not in ('admin','member','guest'):return self.sendj(400,{})
   if actor[0] not in ('owner','admin') or target==w[3]:return self.sendj(403,{})
   oldr=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],target),True)
   if not oldr:return self.sendj(404,{})
   if actor[0]=='admin' and oldr[0]=='admin':return self.sendj(403,{})
   commit('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(d['role'],w[0],target));return self.sendj(200,{'member':{'user_id':target,'role':d['role']}})
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None
   if not w:return self.sendj(404,{})
   if not role or role[0] not in ('owner','admin'):return self.sendj(403,{})
   if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return self.sendj(400,{})
   if 'name' in d:commit('UPDATE workspaces SET name=? WHERE id=?',(d['name'],w[0]))
   if 'join_mode' in d:commit('UPDATE workspaces SET join_mode=? WHERE id=?',(d['join_mode'],w[0]))
   z=q('SELECT * FROM workspaces WHERE id=?',(w[0],),True);return self.sendj(200,{'workspace':dict(zip(('id','slug','name','owner_id','join_mode'),z))})
  if p=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
   if 'timezone' in d:
    try: ZoneInfo(d['timezone'])
    except Exception: return self.sendj(400,{})
   if any(k not in allowed or not isinstance(v,str) or len(v)>300 for k,v in d.items()):return self.sendj(400,{})
   for k,v in d.items():commit(f'UPDATE users SET {k}=? WHERE id=?',(v,u[0]))
   x=q('SELECT * FROM users WHERE id=?',(u[0],),True); frame={'type':'user.updated','user':userobj(x)}
   with open('/app/data/events.log','a') as ef: ef.write(json.dumps(frame)+'\n')
   with socklock:
    for s,c in list(sockets):
     try:ws_send(s,frame)
     except:pass
   return self.sendj(200,{'user':userobj(x)})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None
   if not w:return self.sendj(404,{})
   if not role or role[0] not in ('owner','admin'):return self.sendj(403,{})
   commit('UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?',(m.group(2),w[0]));return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.sendj(404,{})
   if r[2]!=u[0]:return self.sendj(403,{})
   if r[7]:return self.sendj(409,{})
   if not isinstance(d,dict) or not isinstance(d.get('body'),str) or not d.get('body').strip(): return self.sendj(400,{})
   commit('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r[0])); nr=q('SELECT * FROM messages WHERE id=?',(r[0],),True); emit(r[1],'message.updated',{'message':msgobj(nr)}); return self.sendj(200,{'message':msgobj(nr)})
  m=re.match(r'/api/channels/(\d+)$',p)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.sendj(404,{})
   if len(str(d.get('topic',c[5])))>250:return self.sendj(400,{})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c[1],u[0]),True)
   if not q('SELECT 1 FROM chanmembers WHERE channel_id=? AND user_id=?',(c[0],u[0]),True) and (not role or role[0]!='owner'):return self.sendj(403,{})
   commit('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',c[5]),c[0])); emit(c[0],'channel.updated',{'channel':channel_obj(q('SELECT * FROM channels WHERE id=?',(c[0],),True))}); return self.sendj(200,{'channel':channel_obj(q('SELECT * FROM channels WHERE id=?',(c[0],),True))})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=q('SELECT id,owner_id FROM workspaces WHERE slug=?',(m.group(1),),True); g=q('SELECT id,creator_id FROM groups_ WHERE workspace_id=? AND handle=?',(w[0],m.group(2)),True) if w else None
   if not w or not g:return self.sendj(404,{})
   if u[0] not in (g[1],w[1]):return self.sendj(403,{})
   changes={}
   if 'name' in d: changes['name']=d['name']
   if 'user_ids' in d:
    changes['member_user_ids']=[int(uid) for uid in d.get('user_ids',[]) if q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w[0],uid),True)]
   if changes: self.group_overlay(g[0],changes)
   z=next(x for x in self.groups(w[0]) if x['handle']==m.group(2))
   return self.sendj(200,{'group':z})
  return self.sendj(404,{})
 def do_DELETE(self):
  p=self.routepath();u=auth(self.headers.get('Authorization'))
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w[0],u[0]),True) if w else None
   if not w:return self.sendj(404,{})
   if not role or role[0] not in ('owner','admin'):return self.sendj(403,{})
   inv=q('SELECT code FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w[0]),True)
   if not inv:return self.sendj(200,{'deleted':True})
   commit('UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?',(m.group(2),w[0]))
   return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/pin$',p)
  if m:
   r=q('SELECT id FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.sendj(404,{})
   commit('DELETE FROM pins WHERE message_id=?',(r[0],));return self.sendj(200,{'unpinned':True})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.sendj(200,{'deleted':True})
   if r[2]!=u[0]:
    ow=q('SELECT owner_id FROM workspaces w JOIN channels c ON c.workspace_id=w.id WHERE c.id=?',(r[1],),True)
    if not ow or ow[0]!=u[0]: return self.sendj(403,{})
   commit('UPDATE messages SET deleted=1 WHERE id=?',(r[0],));emit(r[1],'message.deleted',{'message_id':r[0]});return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.*)',p)
  if m:
   r=q('SELECT channel_id FROM messages WHERE id=?',(m.group(1),),True)
   if not r:return self.sendj(404,{})
   emoji=urllib.parse.unquote(m.group(2)); commit('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m.group(1),u[0],emoji)); emit(r[0],'reaction.removed',{'message_id':int(m.group(1)),'emoji':emoji,'user_id':u[0]}); return self.sendj(200,{'reactions':msgobj(q('SELECT * FROM messages WHERE id=?',(m.group(1),),True))['reactions']})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=q('SELECT id,owner_id FROM workspaces WHERE slug=?',(m.group(1),),True); g=q('SELECT id,creator_id FROM groups_ WHERE workspace_id=? AND handle=?',(w[0],m.group(2)),True) if w else None
   if not g:return self.sendj(200,{'deleted':True})
   try: ov=json.load(open('/app/data/group_overrides.json'))
   except Exception: ov={}
   if ov.get(str(g[0]),{}).get('deleted'): return self.sendj(200,{'deleted':True})
   creator=g[1]; owner=w[1]
   if u[0] != creator and u[0] != owner:return self.sendj(403,{})
   self.group_overlay(g[0],{'deleted':True});return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self,u):
  if not u:self.send_error(401);return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:self.send_error(400);return
  import hashlib,base64
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  s=self.connection; s.settimeout(0.2); ch=None; eventpos=os.path.getsize('/app/data/events.log') if os.path.exists('/app/data/events.log') else 0
  def relay():
   pos=eventpos
   while True:
    try:
     with open('/app/data/events.log','r') as ef:
      ef.seek(pos)
      while True:
       line=ef.readline()
       if not line: break
       pos=ef.tell()
       try:
        ev=json.loads(line)
        if ev.get('type')=='user.updated' or (ch is not None and ev.get('channel_id')==ch): ws_send(s,ev)
       except Exception: pass
     time.sleep(0.05)
    except Exception: break
  threading.Thread(target=relay,daemon=True).start()
  try:
   pending=b''
   def readn(n):
    nonlocal pending
    while len(pending)<n:
     try:
      z=s.recv(max(4096,n-len(pending)))
      if not z: return None
      pending+=z
     except socket.timeout: continue
    z,pending=pending[:n],pending[n:]
    return z
   while True:
    h=readn(2)
    if not h: break
    ln=h[1]&127
    if ln==126: ln=struct.unpack('>H',readn(2))[0]
    elif ln==127: ln=struct.unpack('>Q',readn(8))[0]
    mask=readn(4); raw=readn(ln)
    if raw is None: break
    data=bytes(raw[i]^mask[i%4] for i in range(ln)); x=json.loads(data)
    if x.get('type') in ('subscribe','resume'):
     ch=int(x['channel_id'])
     with socklock:sockets.append((s,ch))
     ws_send(s,{'type':'subscribed','channel_id':ch,'head_event_id':q('SELECT event_id FROM event_seq WHERE channel_id=?',(ch,),True)[0] if q('SELECT event_id FROM event_seq WHERE channel_id=?',(ch,),True) else 0})
     if x['type']=='resume':
      since=int(x.get('since_event_id',0)); replay=[]
      try:
       for ln in open('/app/data/events.log'):
        try:
         ev=json.loads(ln)
         if ev.get('channel_id')==ch and int(ev.get('event_id',0))>since: replay.append(ev)
        except Exception: pass
      except FileNotFoundError: pass
      for ev in sorted(replay,key=lambda z:int(z.get('event_id',0))): ws_send(s,ev)
      ws_send(s,{'type':'resumed','channel_id':ch,'head_event_id':q('SELECT event_id FROM event_seq WHERE channel_id=?',(ch,),True)[0] if q('SELECT event_id FROM event_seq WHERE channel_id=?',(ch,),True) else 0})
  except:pass
  with socklock:
   for z in list(sockets):
    if z[0] is s:sockets.remove(z)
 def html(self):
  h='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>body{margin:0;font:15px Arial;background:#f8f8f8;color:#222}.app{display:grid;grid-template-columns:240px 1fr 300px;height:100vh}.side{background:#3f0e40;color:white;padding:20px}.main{padding:24px;overflow:auto}.thread{border-left:1px solid #ddd;padding:20px}button{border:0;border-radius:4px;padding:9px 14px;color:#fff;background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.msg{padding:12px;border-bottom:1px solid #eee}.empty{padding:50px;text-align:center}</style></head><body><div class=app><aside class=side><h2>Huddle</h2><div data-testid=workspace-header>Workspace</div><div data-testid=channel-list></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button></aside><main class=main><h1 data-testid=channel-title>#general</h1><div data-testid=channel-topic></div><div data-testid=message-list><div class=empty>Welcome to your channel</div></div><input data-testid=message-input placeholder="Message"><button data-testid=send-btn data-button-role=primary>Send</button></main><aside class=thread data-testid=thread-panel>Threads</aside></div><script>localStorage.getItem("huddle.token");</script></body></html>'''
  b=h.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
def ws_send(s,x):
 b=json.dumps(x,separators=(',',':')).encode(); n=len(b)
 if n<126: head=bytes([129,n])
 elif n<65536: head=bytes([129,126])+struct.pack('>H',n)
 else: head=bytes([129,127])+struct.pack('>Q',n)
 s.sendall(head+b)
class IRC:
 def __init__(self):self.sock=socket.socket();self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);self.sock.bind(('0.0.0.0',6667));self.sock.listen()
 def run(self):
  threading.Thread(target=self.relay_events,daemon=True).start()
  while True:
   c,a=self.sock.accept();threading.Thread(target=self.client,args=(c,),daemon=True).start()
 def relay_events(self):
  pos=os.path.getsize('/app/data/events.log') if os.path.exists('/app/data/events.log') else 0
  while True:
   try:
    with open('/app/data/events.log','r') as ef:
     ef.seek(pos)
     while True:
      line=ef.readline()
      if not line: break
      pos=ef.tell()
      try:
       ev=json.loads(line)
       if ev.get('origin_pid')==os.getpid() or ev.get('type') not in ('message.created','message.reply'): continue
       ch=ev.get('channel_id'); cc=q('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(ch,),True)
       if not cc: continue
       body=(ev.get('message') or {}).get('body',''); author=(ev.get('message') or {}).get('author') or {}; nick=author.get('username') or 'web'
       with irclock:
        for x in list(irc_clients):
         if ch in x.get('channels',[]):
          try:x['send'](':%s!%s@host PRIVMSG #%s/%s :%s' % (nick,nick,cc[1],cc[0],body))
          except:pass
      except Exception: pass
    time.sleep(0.03)
   except FileNotFoundError: time.sleep(0.03)
   except Exception: time.sleep(0.03)
 def client(self,c):
  st={'sock':c,'nick':None,'user':None,'token':None,'channels':[]}
  def send(x):c.sendall((x+'\r\n').encode())
  st['send']=send
  with irclock:irc_clients.append(st)
  try:
   buf=b''
   while True:
    while b'\n' not in buf:
     z=c.recv(4096)
     if not z:raise Exception()
     buf+=z
    line,buf=buf.split(b'\n',1); line=line.decode(errors='ignore').strip(); parts=line.split(' ',1);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
    if cmd=='PASS':st['token']=arg.strip()
    elif cmd=='NICK':
     nick=arg.strip()
     with irclock: collision=any(x is not st and x.get('nick')==nick for x in irc_clients)
     if collision:send(':irc 433 * '+nick+' :Nickname in use')
     else:st['nick']=nick
    elif cmd=='USER':st['user']=arg.split()[0]; 
    if st['token'] and st['nick'] and st['user'] and not st.get('reg'):
     u=auth('Bearer '+st['token'])
     if not u:send(':irc 464 * :Password incorrect');st['token']=None
     else:
      st['reg']=1
      for n in range(1,6):send(f':irc {n:03d} {st["nick"]} :Huddle')
      send(f':irc 422 {st["nick"]} :MOTD File is missing')
    elif cmd=='PING':send('PONG '+arg)
    elif cmd=='JOIN':
     name=arg.strip(); m=re.match(r'#([^/]+)/(.+)',name); cch=q('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(m.group(1),m.group(2)),True) if m else None
     if not cch:send(f':irc 403 {st["nick"]} {name} :No such channel')
     else: st['channels'].append(cch[0]);send(f':{st["nick"]}!{st["user"]}@host JOIN {name}');send(f':irc 353 {st["nick"]} = {name} :{st["nick"]}');send(f':irc 366 {st["nick"]} {name} :End of /NAMES list')
    elif cmd=='PRIVMSG':
     m=re.match(r'(\S+) :(.*)',arg)
     if m:
      x=re.match(r'#([^/]+)/(.+)',m.group(1));cch=q('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(x.group(1),x.group(2)),True) if x else None
      if cch:
       uid=auth('Bearer '+st['token'])[0];commit('INSERT INTO messages(channel_id,author_id,body,created_at,event_id) VALUES(?,?,?,?,COALESCE((SELECT MAX(event_id)+1 FROM messages WHERE channel_id=?),1))',(cch[0],uid,m.group(2),now(),cch[0]));r=q('SELECT * FROM messages WHERE id=last_insert_rowid()',(),True);emit(cch[0],'message.created',{'message':msgobj(r)})
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):send(f':irc 315 {st["nick"]} :End')
    elif cmd=='QUIT':break
    else:send(f':irc 421 {st["nick"]} {cmd} :Unknown command')
  except:pass
  with irclock:
   if st in irc_clients:irc_clients.remove(st)
  c.close()
if __name__=='__main__':
 threading.Thread(target=IRC().run,daemon=True).start() if PORT==8000 else None
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
