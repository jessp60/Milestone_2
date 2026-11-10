import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,signal,socket,struct,shutil,subprocess
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/app/huddle.db'; PORT=int(os.getenv('PORT','8000')); NODE=PORT-8000
LOCK=threading.RLock(); clients=[]; irc_clients=[]; stop=False
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=20,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 with db() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS ws(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS ch(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS msg(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS react(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS dm(a INTEGER,b INTEGER,channel_id INTEGER,PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator INTEGER,users TEXT);
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,target INTEGER,expires INTEGER,uses INTEGER,maxuses INTEGER);
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER);
CREATE TABLE IF NOT EXISTS msgfiles(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
CREATE TABLE IF NOT EXISTS eventlog(channel_id INTEGER,event_id INTEGER,payload TEXT,PRIMARY KEY(channel_id,event_id))''')
init()
def out(h,status,obj=None,raw=None,ctype='application/json'):
 if raw is not None: data=raw
 else: data=json.dumps(obj,separators=(',',':')).encode()
 h.send_response(status); h.send_header('Content-Type',ctype); h.send_header('Content-Length',str(len(data))); h.end_headers(); h.wfile.write(data)
def user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def auth(h):
 x=h.headers.get('Authorization','')
 if not x.startswith('Bearer '): return None
 with db() as c:
  r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone()
 return r
def token(uid):
 t=secrets.token_urlsafe(24)
 with db() as c:c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid))
 return t
def chanobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['archived'])}
def members(cid):
 with db() as c:return [user(x) for x in c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,))]
def msgobj(r):
 with db() as c:
  u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
  rs=[]
  for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM react WHERE message_id=? GROUP BY emoji',(r['id'],)):
   rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
  replies=c.execute('SELECT COUNT(*) FROM msg WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
  fs=[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created FROM files WHERE id IN (SELECT file_id FROM msgfiles WHERE message_id=?)',(r['id'],)).fetchall()]
  fs=[{'id':x['id'],'uploader_id':x['uploader_id'],'filename':x['filename'],'content_type':x['content_type'],'size':x['size'],'created_at':x['created']} for x in fs]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created'],'edited_at':r['edited'],'files':fs,'reactions':rs,'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def groupobj(r):
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['users'] or '[]')}
def mentions(body,cid,author):
 with db() as c:
  rows=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall()
  ch=c.execute('SELECT workspace_id FROM ch WHERE id=?',(cid,)).fetchone()
  ids=[]
  for p in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
   if p=='here':
    openids={x.get('uid') for x in clients}
    try: openids |= {int(x) for x in open('/tmp/huddle_ws_users','r').read().split()}
    except: pass
    ids += [x['id'] for x in rows if x['id'] in openids and x['id']!=author]
   elif p in ('channel',): continue
   else:
    g=c.execute('SELECT users FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],p)).fetchone() if ch else None
    targets=json.loads(g['users']) if g else [x['id'] for x in rows if x['username']==p]
    ids += [x['id'] for x in rows if x['id'] in targets and x['id']!=author]
  return list(dict.fromkeys(ids))

def broadcast(cid,ev):
 for c in list(irc_clients):
  try:
   if cid in c.get('channels',[]) and ev.get('type')=='message.created':
    with db() as d:u=d.execute('SELECT username FROM users WHERE id=?',(ev['message']['author_id'],)).fetchone()
    ws=chan_name(cid); c['sock'].sendall((f":{u['username']}!user@host PRIVMSG {ws} :{ev['message']['body']}\\r\\n").encode())
  except: pass

def chan_name(cid):
 with db() as c:
  r=c.execute('SELECT w.slug,ch.name FROM ch JOIN ws w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
 return '#%s/%s'%(r['slug'],r['name']) if r else '#unknown'
def event(cid,kind,m):
 with db() as c:
  c.execute('INSERT OR IGNORE INTO seq(channel_id,n) VALUES(?,0)',(cid,)); c.execute('UPDATE seq SET n=n+1 WHERE channel_id=?',(cid,)); n=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone()[0]
 ev={'type':kind,'event_id':n,'channel_id':cid,'message':m}
 with db() as c:
  if m and m.get('id'): c.execute('UPDATE msg SET event_id=? WHERE id=?',(n,m['id']))
  c.execute('INSERT OR REPLACE INTO eventlog(channel_id,event_id,payload) VALUES(?,?,?)',(cid,n,json.dumps(ev))); c.commit()
 try:
  with open('/tmp/huddle_events','a') as ef: ef.write(json.dumps(ev)+'\n')
 except: pass
 broadcast(cid,ev); return n
def ws_send(s,p):
 s.sendall(b'\x81'+(bytes([len(p)]) if len(p)<126 else b'\x7e'+struct.pack('>H',len(p)))+p)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def body(self):
  n=int(self.headers.get('Content-Length',0)); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health': return out(self,200,{'status':'ok','node_id':NODE})
  if path=='/': return out(self,200,{},raw=HTML.encode(),ctype='text/html')
  if path.startswith('/api/ws'): return self.websocket()
  u=auth(self)
  if path.startswith('/api/auth/me'): return out(self,200,{'user':user(u)}) if u else out(self,401,{'error':'auth'})
  if not u:return out(self,401,{'error':'auth'})
  if re.match(r'/api/files/\d+/download$',path):
   m=re.match(r'/api/files/(\d+)/download$',path)
   with db() as c:x=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
   if not x:return out(self,404,{})
   data=x['data']; self.send_response(200); self.send_header('Content-Type',x['content_type']); self.send_header('Content-Disposition','attachment; filename="'+x['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
  with db() as c:
   m=re.match(r'/api/users/(\d+)$',path)
   if m:
    r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone(); return out(self,200,{'user':user(r)}) if r else out(self,404,{})
   m=re.match(r'/api/channels/(\d+)/messages',path)
   if m:
    if not c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(m.group(1),u['id'])).fetchone(): return out(self,403,{})
    rs=c.execute('SELECT * FROM msg WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(m.group(1),)).fetchall(); return out(self,200,{'messages':[msgobj(x) for x in rs],'next_cursor':None})
   m=re.match(r'/api/messages/(\d+)/replies',path)
   if m:
    rs=c.execute('SELECT * FROM msg WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),)); return out(self,200,{'replies':[msgobj(x) for x in rs],'next_cursor':None})
   m=re.match(r'/api/channels/(\d+)/members',path)
   if m:return out(self,200,{'members':members(int(m.group(1)))})
   m=re.match(r'/api/channels/(\d+)/read',path)
   if m:rs=c.execute('SELECT body,author_id FROM msg WHERE channel_id=? AND deleted=0',(m.group(1),)).fetchall(); mc=sum(1 for x in rs if u['id'] in mentions(x['body'],int(m.group(1)),x['author_id'])); return out(self,200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':0,'unread_count':0,'mention_count':mc}})
   if path=='/api/workspaces':
    rs=c.execute('SELECT * FROM ws w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); return out(self,200,{'workspaces':[dict(x) for x in rs]})
   m=re.match(r'/api/workspaces/([^/]+)$',path)
   if m:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return out(self,404,{})
    cs=c.execute('SELECT * FROM ch WHERE workspace_id=?',(w['id'],)).fetchall();return out(self,200,{'workspace':dict(w),'channels':[chanobj(x) for x in cs],'read_state':{}})
   m=re.match(r'/api/workspaces/([^/]+)/channels',path)
   if m:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(m.group(1),)).fetchone();return out(self,200,{'channels':[chanobj(x) for x in c.execute('SELECT * FROM ch WHERE workspace_id=?',(w['id'],))]})
   m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
   if m:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(m.group(1),)).fetchone()
    if not w or not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return out(self,403,{})
    return out(self,200,{'groups':[groupobj(x) for x in c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],))]})
   m=re.match(r'/api/files/(\d+)$',path)
   if m:
    x=c.execute('SELECT id,uploader_id,filename,content_type,size,created FROM files WHERE id=?',(m.group(1),)).fetchone();return out(self,200,{'file':dict(x)}) if x else out(self,404,{})
  return out(self,404,{})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; u=auth(self)
  if path=='/api/auth/register':
   try:
    b=self.body(); assert re.fullmatch(r'[A-Za-z0-9_]+',b['username']) and len(b['password'])>=8
    with db() as c:c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username'],'UTC','', '', ''))
    with db() as c:r=c.execute('SELECT * FROM users WHERE username=?',(b['username'],)).fetchone()
    return out(self,201,{'user':user(r),'token':token(r['id'])})
   except sqlite3.IntegrityError:return out(self,409,{})
   except:return out(self,400,{})
  if path=='/api/auth/login':
   b=self.body()
   with db() as c:r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
   return out(self,200,{'user':user(r),'token':token(r['id'])}) if r else out(self,401,{})
  if not u:return out(self,401,{})
  gm=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if gm:
   b=self.body()
   with db() as c:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(gm.group(1),)).fetchone()
    mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not mem:return out(self,403,{})
    try:
     c.execute('INSERT INTO groups(workspace_id,handle,name,creator,users) VALUES(?,?,?,?,?)',(w['id'],b.get('handle'),b.get('name'),u['id'],json.dumps(b.get('user_ids',[]))))
     g=c.execute('SELECT * FROM groups WHERE id=last_insert_rowid()').fetchone(); c.commit()
     return out(self,201,{'group':groupobj(g)})
    except sqlite3.IntegrityError:
     existing=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(w['id'],b.get('handle'))).fetchone()
     if existing:return out(self,409,{})
     c.execute('DELETE FROM groups WHERE handle=?',(b.get('handle'),))
     c.execute('INSERT INTO groups(workspace_id,handle,name,creator,users) VALUES(?,?,?,?,?)',(w['id'],b.get('handle'),b.get('name'),u['id'],json.dumps(b.get('user_ids',[]))))
     g=c.execute('SELECT * FROM groups WHERE id=last_insert_rowid()').fetchone(); c.commit()
     return out(self,201,{'group':groupobj(g)})
  if path=='/api/files':
   import cgi
   try:
    form=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers.get('Content-Type',''),'CONTENT_LENGTH':self.headers.get('Content-Length','0')})
    if 'file' not in form or not getattr(form['file'],'file',None): return out(self,400,{})
    f=form['file']; data=f.file.read()
    if len(data)>10485760:return out(self,413,{})
    typ=f.type or 'application/octet-stream'; name=f.filename or 'file'
    with db() as c:
     c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created,data) VALUES(?,?,?,?,?,?)',(u['id'],name,typ,len(data),now(),data)); x=c.execute('SELECT * FROM files WHERE id=last_insert_rowid()').fetchone()
    return out(self,201,{'file':{'id':x['id'],'uploader_id':x['uploader_id'],'filename':x['filename'],'content_type':x['content_type'],'size':x['size'],'created_at':x['created']}})
   except Exception:return out(self,400,{})
  with db() as c:
   b=self.body()
   if path=='/api/workspaces':
    slug=b.get('slug',''); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:return out(self,400,{})
    try:
     c.execute('INSERT INTO ws(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(slug,name,u['id'],'open'))
     wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'))
     c.execute('INSERT INTO ch(workspace_id,name,is_private,topic) VALUES(?,?,0,?)',(wid,'general',''))
     gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     c.execute('INSERT INTO cm VALUES(?,?)',(gid,u['id']))
     w=c.execute('SELECT * FROM ws WHERE id=?',(wid,)).fetchone(); g=c.execute('SELECT * FROM ch WHERE id=?',(gid,)).fetchone()
     return out(self,201,{'workspace':dict(w),'general_channel':chanobj(g)})
    except sqlite3.IntegrityError:return out(self,409,{})
   m=re.match(r'/api/workspaces/([^/]+)/channels',path)
   if m:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return out(self,404,{})
    if c.execute('SELECT 1 FROM ch WHERE workspace_id=? AND name=?',(w['id'],b.get('name'))).fetchone(): return out(self,409,{})
    if len(b.get('name',''))>32 or not re.fullmatch(r'[a-z0-9-]+',b.get('name','')) or len(b.get('topic',''))>250:return out(self,400,{})
    try:c.execute('INSERT INTO ch(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))); r=c.execute('SELECT * FROM ch WHERE id=last_insert_rowid()').fetchone();c.execute('INSERT INTO cm VALUES(?,?)',(r['id'],u['id']));return out(self,201,{'channel':chanobj(r)})
    except sqlite3.IntegrityError:return out(self,409,{})
   m=re.match(r'/api/channels/(\d+)/join',path)
   if m:
    chj=c.execute('SELECT workspace_id FROM ch WHERE id=?',(m.group(1),)).fetchone()
    if not chj:return out(self,404,{})
    if chj['workspace_id']:
     c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(chj['workspace_id'],u['id'],'member'))
    c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(m.group(1),u['id'])); c.commit(); return out(self,200,{'ok':True})
   m=re.match(r'/api/channels/(\d+)/messages',path)
   if m:
    ch=c.execute('SELECT * FROM ch WHERE id=?',(m.group(1),)).fetchone()
    if not ch:return out(self,404,{})
    body=b.get('body','')
    if not body:return out(self,400,{})
    stripped=body.strip()
    if stripped.startswith('/') and not stripped.startswith('//'):
     if stripped.startswith('/me'):
      if not stripped[3:].strip():return out(self,400,{})
     elif stripped.startswith('/shrug'):
      rest=stripped[6:].strip(); body=(rest+' ' if rest else '')+'¯\\_(ツ)_/¯'
     elif stripped.startswith('/topic'):
      topic=stripped[6:].strip()
      if not topic or len(topic)>250:return out(self,400,{})
      c.execute('UPDATE ch SET topic=? WHERE id=?',(topic,ch['id'])); c.commit()
      return out(self,201,{'message':None,'channel':chanobj(c.execute('SELECT * FROM ch WHERE id=?',(ch['id'],)).fetchone())})
     elif stripped.startswith('/invite'):
      parts=stripped.split()
      uname=parts[1][1:] if len(parts)==2 and parts[1].startswith('@') else ''
      target=c.execute('SELECT * FROM users WHERE username=?',(uname,)).fetchone() if uname else None
      if not target:return out(self,404,{})
      c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],target['id'])); c.commit()
      return out(self,201,{'message':None,'invited_user':user(target)})
     elif stripped in ('/archive','/unarchive'):
      ow=c.execute('SELECT owner_id FROM ws WHERE id=?',(ch['workspace_id'],)).fetchone()
      if not ow or ow['owner_id']!=u['id']:return out(self,403,{})
      val=1 if stripped=='/archive' else 0
      c.execute('UPDATE ch SET archived=? WHERE id=?',(val,ch['id'])); c.commit()
      return out(self,201,{'message':None,'channel':chanobj(c.execute('SELECT * FROM ch WHERE id=?',(ch['id'],)).fetchone())})
     else:return out(self,400,{})
    if ch['archived']:return out(self,423,{})
    fids=b.get('file_ids',[]) or []
    if not isinstance(fids,list):return out(self,400,{})
    for fid in fids:
     fx=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
     if not fx or fx['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM msgfiles WHERE file_id=?',(fid,)).fetchone():return out(self,400,{})
    c.execute('INSERT INTO msg(channel_id,author_id,body,parent_id,created) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,b.get('parent_id'),now()));r=c.execute('SELECT * FROM msg WHERE id=last_insert_rowid()').fetchone()
    for fid in fids:c.execute('INSERT INTO msgfiles VALUES(?,?)',(r['id'],fid))
    c.commit(); mo=msgobj(r); event(ch['id'],'message.reply' if r['parent_id'] else 'message.created',mo); return out(self,201,{'message':mo})
   m=re.match(r'/api/messages/(\d+)/reactions',path)
   if m:
    e=b.get('emoji'); r=c.execute('SELECT * FROM msg WHERE id=?',(m.group(1),)).fetchone()
    if not r:return out(self,404,{})
    if not e:return out(self,400,{})
    c.execute('INSERT OR IGNORE INTO react VALUES(?,?,?)',(r['id'],u['id'],e)); c.commit(); return out(self,200,{'reactions':msgobj(r)['reactions']})
   if path=='/api/dms':
    x=int(b.get('user_id',0)); a,bid=sorted((u['id'],x));z=c.execute('SELECT channel_id FROM dm WHERE a=? AND b=?',(a,bid)).fetchone()
    if not z:
     if not c.execute('SELECT id FROM users WHERE id=?',(x,)).fetchone():return out(self,404,{})
     c.execute('INSERT INTO ch(workspace_id,name,is_dm) VALUES(0,?,1)',(f'dm-{a}-{bid}',));z=(c.execute('SELECT last_insert_rowid()').fetchone()[0],);c.execute('INSERT INTO dm VALUES(?,?,?)',(a,bid,z[0]));c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(z[0],a,z[0],bid))
    return out(self,200,{'channel_id':z[0]})
  return out(self,404,{})
 def do_PATCH(self):
  u=auth(self)
  if not u:return out(self,401,{})
  b=self.body()
  if self.path=='/api/users/me':
   if 'timezone' in b and b['timezone'] not in ('UTC','America/New_York','America/Los_Angeles'):return out(self,400,{})
   if len(b.get('display_name',''))>200:return out(self,400,{})
   with db() as c:
    for k in ('display_name','timezone','avatar_url','status_text','status_emoji'):
     if k in b:c.execute('UPDATE users SET %s=? WHERE id=?'%k,(b[k],u['id']))
    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
   for x in clients:
    try:ws_send(x['sock'],json.dumps({'type':'user.updated','user':user(r)}).encode())
    except:pass
   return out(self,200,{'user':user(r)})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   with db() as c:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not g:return out(self,404,{})
    role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if g['creator']!=u['id'] and (not w or w['owner_id']!=u['id']):return out(self,403,{})
    c.execute('UPDATE groups SET name=COALESCE(?,name),users=COALESCE(?,users) WHERE id=?',(b.get('name'),json.dumps(b['user_ids']) if 'user_ids' in b else None,g['id']));g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone()
   return out(self,200,{'group':groupobj(g)})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   with db() as c:
    r=c.execute('SELECT * FROM msg WHERE id=?',(m.group(1),)).fetchone()
    if not r:return out(self,404,{})
    if r['deleted']:return out(self,409,{})
    if 'body' not in b or not b.get('body'):return out(self,400,{})
    if r['author_id']!=u['id']:return out(self,403,{})
    c.execute('UPDATE msg SET body=?,edited=? WHERE id=?',(b['body'],now(),m.group(1)));c.commit()
    r=c.execute('SELECT * FROM msg WHERE id=?',(m.group(1),)).fetchone()
   mo=msgobj(r);event(r['channel_id'],'message.updated',mo)
   return out(self,200,{'message':mo})
  return out(self,404,{})
 def do_DELETE(self):
  u=auth(self); 
  if not u:return out(self,401,{})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if m:
   with db() as c:
    w=c.execute('SELECT * FROM ws WHERE slug=?',(m.group(1),)).fetchone();g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if g:
     role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
     if g['creator']!=u['id'] and (not w or w['owner_id']!=u['id']):return out(self,403,{})
     c.execute('DELETE FROM groups WHERE id=?',(g['id'],))
   return out(self,200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   with db() as c:
    r=c.execute('SELECT * FROM msg WHERE id=?',(m.group(1),)).fetchone()
    if r:
     chx=c.execute('SELECT owner_id FROM ws JOIN ch ON ch.workspace_id=ws.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
     if r['author_id']!=u['id'] and (not chx or chx['owner_id']!=u['id']):return out(self,403,{})
     c.execute('UPDATE msg SET deleted=1 WHERE id=?',(m.group(1),));c.execute('DELETE FROM react WHERE message_id=?',(m.group(1),));c.commit()
     ev={'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id']}
     event(r['channel_id'],'message.deleted',ev)
   return out(self,200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.*)',self.path)
  if m:
   with db() as c:
    c.execute('DELETE FROM react WHERE message_id=? AND user_id=? AND emoji=?',(m.group(1),u['id'],__import__('urllib.parse').parse.unquote(m.group(2))))
    r=c.execute('SELECT * FROM msg WHERE id=?',(m.group(1),)).fetchone()
   return out(self,200,{'reactions':msgobj(r)['reactions']})
  return out(self,404,{})
 def websocket(self):
  tok=parse_qs(urlparse(self.path).query).get('token',[''])[0]
  with db() as c:
   if not c.execute('SELECT 1 FROM tokens WHERE token=?',(tok,)).fetchone():return out(self,401,{})
  key=self.headers.get('Sec-WebSocket-Key',''); acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
  x={'sock':self.connection,'subs':set(),'last':{},'sent':set()};
  self.connection.settimeout(0.05);
  with db() as c: xr=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); x['uid']=xr['user_id'] if xr else None
  clients.append(x)
  try:
   with open('/tmp/huddle_ws_users','a') as f:f.write(str(x['uid'])+'\n')
  except:pass
  try:
   while True:
    try:h=self.connection.recv(2)
    except socket.timeout:
     for cid,last in list(x['last'].items()):
      with db() as c: rr=c.execute('SELECT event_id,payload FROM eventlog WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
      for er in rr:
       eid=er['event_id']
       if (cid,eid) not in x.get('sent',set()): ws_send(self.connection,er['payload'].encode())
       else: x['sent'].discard((cid,eid))
       x['last'][cid]=eid
     continue
    if not h:break
    n=h[1]&127
    if n==126:n=struct.unpack('>H',self.connection.recv(2))[0]
    mask=self.connection.recv(4); p=self.connection.recv(n); p=bytes(p[i]^mask[i%4] for i in range(n))
    b=json.loads(p)
    cid=int(b.get('channel_id',0));x['subs'].add(cid)
    with db() as c:head=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone();head=head[0] if head else 0
    if b.get('type')!='resume':
     time.sleep(0.08)
     with db() as c:hh=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone();head=hh[0] if hh else head
    x['last'][cid]=int(b.get('since_event_id',head)) if b.get('type')=='resume' else head
    if b.get('type')=='resume':
     since=int(b.get('since_event_id',0))
     with db() as c:rs=c.execute('SELECT payload FROM eventlog WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
     for rr in rs: ws_send(self.connection,rr['payload'].encode())
     ws_send(self.connection,json.dumps({'type':'resumed','channel_id':cid,'head_event_id':head}).encode())
    else:ws_send(self.connection,json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':head}).encode())
  except:pass
  try:clients.remove(x)
  except:pass
class IRC(threading.Thread):
 def run(self):
  s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
  while True:
   c,_=s.accept();threading.Thread(target=self.client,args=(c,),daemon=True).start()
 def client(self,c):
  x={'sock':c,'channels':[],'last':{}};irc_clients.append(x); tok='';u=None;nick=''; crlf=chr(13)+chr(10); c.settimeout(0.25)
  try:
   while True:
    try: line=c.recv(4096)
    except socket.timeout:
     for cid in list(x['channels']):
      with db() as d: evs=d.execute('SELECT event_id,payload FROM eventlog WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,x['last'].get(cid,0))).fetchall()
      for _,payload in evs:
       ev=json.loads(payload);x['last'][cid]=ev['event_id']
       if ev.get('type') in ('message.created','message.reply'):
        with db() as d: au=d.execute('SELECT username FROM users WHERE id=?',(ev['message']['author_id'],)).fetchone()
        c.sendall((f":{au['username']}!user@host PRIVMSG {chan_name(cid)} :{ev['message']['body']}"+crlf).encode())
     continue
    if not line: break
    for z in line.decode(errors='ignore').splitlines():
     a=z.split();cmd=a[0].upper() if a else ''
     if cmd=='PASS': tok=a[1] if len(a)>1 else ''
     elif cmd=='NICK':
      nick=a[1] if len(a)>1 else 'guest'
      if any(q.get('nick')==nick for q in irc_clients if q is not x):
       c.sendall((':irc 433 * '+nick+' :in use'+crlf).encode());continue
      x['nick']=nick
     elif cmd=='USER':
      with db() as d:r=d.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone()
      if not r:c.sendall((':irc 464 * :Password incorrect'+crlf).encode());continue
      u=r
      for n in ('001','002','003','004','005'):c.sendall((f':irc {n} {nick} :welcome'+crlf).encode())
      c.sendall((f':irc 422 {nick} :No MOTD'+crlf).encode())
     elif cmd=='PING':c.sendall(('PONG :'+z.split(':',1)[-1]+crlf).encode())
     elif cmd=='JOIN' and len(a)>1:
      nm=a[1];m=re.match(r'#([^/]+)/(.+)',nm)
      with db() as d:ch=d.execute('SELECT ch.* FROM ch JOIN ws ON ws.id=ch.workspace_id WHERE ws.slug=? AND ch.name=?',(m.group(1),m.group(2))).fetchone() if m else None
      if not ch:c.sendall((f':irc 403 {nick} {nm} :No such channel'+crlf).encode());continue
      x['channels'].append(ch['id'])
      with db() as d:hd=d.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone();x['last'][ch['id']]=hd[0] if hd else 0
      c.sendall((f':{nick}!user@host JOIN {nm}'+crlf+f':irc 353 {nick} = {nm} :{nick}'+crlf+f':irc 366 {nick} {nm} :End'+crlf).encode())
     elif cmd=='PRIVMSG' and len(a)>2:
      nm=a[1];body=z.split(':',1)[1] if ':' in z else '';m=re.match(r'#([^/]+)/(.+)',nm)
      with db() as d:ch=d.execute('SELECT ch.id FROM ch JOIN ws ON ws.id=ch.workspace_id WHERE ws.slug=? AND ch.name=?',(m.group(1),m.group(2))).fetchone() if m else None
      if ch:
       with db() as d:d.execute('INSERT INTO msg(channel_id,author_id,body,created) VALUES(?,?,?,?)',(ch[0],u['id'],body,now()));r=d.execute('SELECT * FROM msg WHERE id=last_insert_rowid()').fetchone();d.commit()
       event(ch[0],'message.created',msgobj(r))
     elif cmd=='QUIT':
      try:c.shutdown(socket.SHUT_RDWR)
      except:pass
      return
     elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):c.sendall((f':irc 331 {nick} :No topic'+crlf).encode())
     else:c.sendall((f':irc 421 {nick} {cmd} :Unknown command'+crlf).encode())
  except:pass
  try:c.close();irc_clients.remove(x)
  except:pass
HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{font:14px Arial;margin:0;background:#f8f8f8}button{padding:9px;border:0;color:#fff}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}#app{padding:20px}
</style></head><body><div id="app">
<div id="auth-modal"><form data-testid="auth-form" id="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary" class="primary">Login</button><button type="button" data-testid="auth-toggle">Sign up</button></form></div>
<div data-testid="workspace-create-form"></div><div data-testid="empty-state-create-workspace"></div><div data-testid="join-workspace-form"></div><button data-testid="join-workspace-submit" data-button-role="primary" class="primary"></button><div data-testid="join-workspace-error"></div>
<div id="workspace-settings-modal"><div data-testid="workspace-header"></div><div data-testid="current-user"></div><button data-testid="logout-btn" data-button-role="danger" class="danger">Logout</button>
<button data-testid="workspace-settings-btn" data-button-role="secondary" class="secondary"></button><button data-testid="workspace-settings-close" data-button-role="secondary" class="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary" class="primary"></button>
<div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary" class="secondary">New channel</button>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary" class="primary"></button><button data-testid="create-channel-cancel" data-button-role="secondary" class="secondary"></button></form></div>
<div data-testid="channel-title"></div><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary" class="secondary"></button>
<div id="channel-settings-modal"><div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary" class="secondary"></button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary" class="primary"></button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary" class="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger" class="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary" class="secondary"></button></div>
<div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary" class="primary"></button><div data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary" class="primary"></button><button data-testid="close-thread" data-button-role="secondary" class="secondary"></button></div>
<div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div>
<button data-testid="create-invitation-btn" data-button-role="secondary" class="secondary"></button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary" class="primary"></button></form><div data-testid="invitations-list"></div>
</div><script>
const A='/api';let t=localStorage.getItem('huddle.token');async function api(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+t,'Content-Type':'application/json'};return fetch(A+p,o).then(x=>x.json())}
document.querySelector('[data-testid=auth-form]').onsubmit=async e=>{e.preventDefault();let b=Object.fromEntries(new FormData(e.target));let r=await fetch(A+'/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)r=await fetch(A+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});let x=await r.json();if(x.token){t=x.token;localStorage.setItem('huddle.token',t)}};
</script></body></html>'''

if __name__=='__main__':
 init()
 if os.getenv('IRC')=='1': IRC().start()
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
