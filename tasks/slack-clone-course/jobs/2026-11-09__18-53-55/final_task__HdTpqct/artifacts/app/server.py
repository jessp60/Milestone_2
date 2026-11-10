#!/usr/bin/env python3
import os, re, json, uuid, time, hashlib, secrets, sqlite3, threading, base64, struct, socket, cgi
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
lock=threading.RLock()
OPEN_WS=set()
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def init():
 c=db();
 try:c.execute('ALTER TABLE users ADD COLUMN mention_count INTEGER DEFAULT 0')
 except sqlite3.OperationalError:pass
 c.execute('PRAGMA journal_mode=WAL'); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,seen_at INTEGER);
CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,created_at INTEGER);
CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,PRIMARY KEY(channel_id,event_id));
'''); c.commit(); c.close()
init()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())
def out(o): return json.dumps(o,separators=(',',':')).encode()
def user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji'],'mention_count':r['mention_count'] if 'mention_count' in r.keys() else 0}
def auth(h):
 m=re.match(r'Bearer\s+(.+)',h.get('Authorization','')); 
 if not m:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(m.group(1),)).fetchone(); c.close(); return r
def err(h,n,msg):
 b=out({'error':msg}); h.send_response(n); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
def parse(h):
 n=int(h.headers.get('Content-Length','0'))
 if not n:return {}
 try:return json.loads(h.rfile.read(n))
 except:return None
def member(c,w,u):
 r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); return r['role'] if r else None
def chan(c,i):
 return c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
def chobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msgobj(c,r):
 au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rs=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],)).fetchall(); groups={}
 for x in rs: groups.setdefault(x['emoji'],[]).append(x['user_id'])
 mentions=[]
 text=r['body'] or ''
 members=c.execute('SELECT u.* FROM users u JOIN cmembers cm ON cm.user_id=u.id WHERE cm.channel_id=?',(r['channel_id'],)).fetchall()
 byname={x['username']:x['id'] for x in members}
 for nm,uid in byname.items():
  if uid!=r['author_id'] and re.search(r'(?<![A-Za-z0-9_])@'+re.escape(nm)+r'(?![A-Za-z0-9_])',text): mentions.append(uid)
 if '@channel' in text:
  mentions.extend(x['id'] for x in members if x['id']!=r['author_id'])
 if '@here' in text:
  active={x['user_id'] for x in c.execute('SELECT user_id FROM ws_presence WHERE seen_at>?',(int(time.time())-10,)).fetchall()}
  mentions.extend(x['id'] for x in members if x['id']!=r['author_id'] and x['id'] in active)
 for g in c.execute('SELECT * FROM groups WHERE workspace_id=(SELECT workspace_id FROM channels WHERE id=?)',(r['channel_id'],)).fetchall():
  if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(g['handle'])+r'(?![A-Za-z0-9_])',text):
   mentions.extend(x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall() if x['user_id']!=r['author_id'] and any(m['id']==x['user_id'] for m in members))
 mentions=list(dict.fromkeys(mentions))
 for uid in mentions:
  try:c.execute('UPDATE users SET mention_count=COALESCE(mention_count,0)+1 WHERE id=?',(uid,))
  except sqlite3.OperationalError:pass
 try:c.commit()
 except Exception:pass
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()],'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in groups.items()],'mentions':mentions}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,n,o):
  b=out(o); self.send_response(n); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_GET(self):
  try:self.route('GET')
  except Exception as e: err(self,500,'internal: '+str(e))
 def do_POST(self):
  try:self.route('POST')
  except Exception as e: err(self,500,'internal: '+str(e))
 def do_PATCH(self):
  try:self.route('PATCH')
  except Exception as e: err(self,500,'internal: '+str(e))
 def do_DELETE(self):
  try:self.route('DELETE')
  except Exception as e: err(self,500,'internal: '+str(e))
 def route(self,method):
  p=urlparse(self.path); path=p.path; q=parse_qs(p.query); c=db(); me=auth(self.headers)
  if path=='/api/ws' and not me and q.get('token'):
   tok=q['token'][0]; me=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone()
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if path=='/': return self.page()
  if path=='/api/ws': return self.ws(me)
  body={}
  if method in ('POST','PATCH'):
   if self.headers.get('Content-Type','').startswith('multipart/form-data'):
    body=None
   else:
    body=parse(self)
    if body is None:return err(self,400,'malformed json')
  if path=='/api/auth/register' and method=='POST':
   u=body.get('username',''); pw=body.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(pw)<8:return err(self,400,'invalid registration')
   try:
    cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,pw,body.get('display_name',u))); uid=cur.lastrowid; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return self.sendj(201,{'user':user(r),'token':t})
   except sqlite3.IntegrityError:return err(self,409,'duplicate username')
  if path=='/api/auth/login' and method=='POST':
   if not body.get('username') or not body.get('password'):return err(self,400,'missing fields')
   r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(body.get('username'),body.get('password'))).fetchone()
   if not r:return err(self,401,'invalid credentials')
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();return self.sendj(200,{'user':user(r),'token':t})
  if path=='/api/auth/me':
   if not me:return err(self,401,'unauthorized')
   return self.sendj(200,{'user':user(me)})
  if not me:return err(self,401,'unauthorized')
  if path=='/api/files' and method=='POST':
   if not self.headers.get('Content-Type','').startswith('multipart/form-data'): return err(self,400,'multipart required')
   ct_header=self.headers.get('Content-Type',''); mpart=re.search(r'boundary=([^;]+)',ct_header)
   if not mpart:return err(self,400,'file required')
   n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n); boundary=('--'+mpart.group(1).strip('"')).encode()
   chunks=raw.split(boundary); filename=None; ftype='application/octet-stream'; data=None
   for part in chunks:
    if b'Content-Disposition:' not in part or b'name="file"' not in part: continue
    head,sep,payload=part.partition(b'\r\n\r\n')
    if not sep: continue
    fm=re.search(br'filename="([^"]*)"',head); tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
    filename=fm.group(1).decode(errors='replace') if fm else 'file'; ftype=tm.group(1).decode().strip() if tm else ftype
    data=payload[:-2] if payload.endswith(b'\r\n') else payload; break
   if data is None:return err(self,400,'file required')
   if len(data)>10485760:return err(self,413,'file too large')
   c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(me['id'],filename,ftype,len(data),now(),data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit()
   fr=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone(); return self.sendj(201,{'file':dict(fr)})
  fm=re.match(r'^/api/files/(\d+)(/download)?$',path)
  if fm and method=='GET':
   fr=c.execute('SELECT * FROM files WHERE id=?',(int(fm.group(1)),)).fetchone()
   if not fr:return err(self,404,'file not found')
   if fm.group(2):
    self.send_response(200);self.send_header('Content-Type',fr['content_type']);self.send_header('Content-Disposition','attachment; filename="'+fr['filename']+'"');self.send_header('Content-Length',str(fr['size']));self.end_headers();self.wfile.write(fr['data']);return
   return self.sendj(200,{'file':{k:fr[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  um=None
  if path.startswith('/api/users/') and path.count('/')==3 and path.split('/')[3].isdigit(): um=path.split('/')[3]
  if um and method=='GET':
   ur=c.execute('SELECT * FROM users WHERE id=?',(int(um),)).fetchone()
   if not ur:return err(self,404,'user not found')
   return self.sendj(200,{'user':user(ur)})
  if path=='/api/users/me' and method=='PATCH':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; vals={k:v for k,v in body.items() if k in allowed}
   if 'timezone' in vals:
    try:
     from zoneinfo import ZoneInfo
     ZoneInfo(str(vals['timezone']))
    except Exception:return err(self,400,'invalid timezone')
   for k,v in vals.items():
    if len(str(v))>500:return err(self,400,'field too long')
   if vals:
    c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),me['id'])); c.execute('INSERT INTO profile_events(user_id,created_at) VALUES(?,?)',(me['id'],int(time.time()))); c.commit()
   return self.sendj(200,{'user':user(c.execute('SELECT * FROM users WHERE id=?',(me['id'],)).fetchone())})
  if path=='/api/workspaces' and method=='GET':
   rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(me['id'],)).fetchall();return self.sendj(200,{'workspaces':[dict(x) for x in rows]})
  if path=='/api/workspaces' and method=='POST':
   if not body.get('slug') or not body.get('name'):return err(self,400,'missing fields')
   s=body.get('slug','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',s):return err(self,400,'invalid slug')
   try:
    cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(s,body.get('name',''),me['id']));w=cur.lastrowid;c.execute('INSERT INTO members VALUES(?,?,?)',(w,me['id'],'owner'));cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general'));g=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(g,me['id']));c.commit();return self.sendj(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w,)).fetchone()),'general_channel':chobj(chan(c,g))})
   except sqlite3.IntegrityError:return err(self,409,'duplicate workspace')
  m=re.match(r'^/api/workspaces/([^/]+)(.*)$',path)
  if m:
   slug,tail=m.group(1),m.group(2); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return err(self,404,'workspace not found')
   role=member(c,w['id'],me['id'])
   if tail=='' and method=='PATCH':
    if role not in ('owner','admin'): return err(self,403,'forbidden')
    if 'join_mode' in body and body['join_mode'] not in ('open','invite_only'): return err(self,400,'invalid join mode')
    vals=[]; args=[]
    for k in ('name','join_mode'):
     if k in body: vals.append(k+'=?'); args.append(body[k])
    if vals:
     args.append(w['id']); c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args); c.commit()
    return self.sendj(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   mr=re.match(r'^/members/(\d+)$',tail)
   if mr and method=='PATCH':
    target=int(mr.group(1)); new=body.get('role')
    if new not in ('owner','admin','member','guest'): return err(self,400,'invalid role')
    if new=='owner': return err(self,400,'use transfer')
    oldrow=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
    if not oldrow:return err(self,404,'member not found')
    if role not in ('owner','admin') or target==me['id'] or oldrow['role']=='owner' or (role=='admin' and oldrow['role']=='admin'): return err(self,403,'forbidden')
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],target));c.commit()
    return self.sendj(200,{'member':{'user_id':target,'role':new}})
   if tail=='/transfer_ownership' and method=='POST':
    if role!='owner':return err(self,403,'owner only')
    target=body.get('new_owner_id'); tr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
    if not tr or tr['role']!='admin':return err(self,400,'target must be admin')
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],me['id']))
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit()
    return self.sendj(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   if tail=='' and method=='GET':
    archived='include_archived' in q and q['include_archived'][0] in ('1','true')
    cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],int(archived))).fetchall()
    cs=[x for x in cs if (not x['is_private'] or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone())]
    return self.sendj(200,{'workspace':dict(w),'channels':[chobj(x) for x in cs],'read_state':[]})
   handle=None
   gm=re.match(r'^/groups(?:/([^/]+))?$',tail)
   if gm:
    handle=gm.group(1)
    if method=='GET' and not handle:
     gs=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall(); outg=[]
     for g in gs: outg.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]})
     return self.sendj(200,{'groups':outg})
    if not role:return err(self,403,'not member')
    if method=='POST' and not handle:
     h=body.get('handle','').lstrip('@'); name=body.get('name',''); ids=body.get('user_ids',[])
     if not h or not name:return err(self,400,'missing fields')
     if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():return err(self,409,'duplicate handle')
     c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,me['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     for uid in ids:c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
     c.commit(); g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone(); return self.sendj(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}})
    if handle:
     g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle.lstrip('@'))).fetchone()
     if not g:
      if method=='DELETE':return self.sendj(200,{'deleted':True})
      return err(self,404,'group')
     if method=='PATCH':
      if me['id']!=g['creator_id'] and role!='owner':return err(self,403,'forbidden')
      c.execute('UPDATE groups SET name=? WHERE id=?',(body.get('name',g['name']),g['id']))
      if 'user_ids' in body:
       c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
       for uid in body['user_ids']:c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
      c.commit(); g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone(); return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]}})
     if method=='DELETE':
      if me['id']!=g['creator_id'] and role!='owner':return err(self,403,'forbidden')
      c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();return self.sendj(200,{'deleted':True})
   if method=='GET' and handle:return err(self,404,'group')
   if tail=='/members' and method=='GET':
    rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();return self.sendj(200,{'members':[dict(x) for x in rs]})
   if tail=='/channels' and method=='GET':
    archived='include_archived' in q and q['include_archived'][0] in ('1','true')
    cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],int(archived))).fetchall()
    cs=[x for x in cs if (not x['is_private'] or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],me['id'])).fetchone())]
    return self.sendj(200,{'workspace':dict(w),'channels':[chobj(x) for x in cs],'read_state':[]})
   if tail=='/channels' and method=='POST':
    if role not in ('owner','admin','member') or (body.get('is_private') and role not in ('owner','admin')):return err(self,403,'forbidden')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',body.get('name','')):return err(self,400,'invalid channel')
    if len(body.get('topic',''))>250:return err(self,400,'topic')
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],body['name'])).fetchone():return err(self,409,'duplicate channel')
    cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],body['name'],int(bool(body.get('is_private'))),body.get('topic','')));i=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(i,me['id']));c.commit();return self.sendj(201,{'channel':chobj(chan(c,i))})
   if tail=='/invitations' and method=='POST':
    if role not in ('owner','admin'):return err(self,403,'forbidden')
    code=secrets.token_urlsafe(12);c.execute('INSERT INTO invitations(code,workspace_id,invited_user_id,expires,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],body.get('invited_user_id'),int(time.time())+body.get('expires_in_seconds',86400),body.get('max_uses',1)));c.commit();return self.sendj(201,{'invitation':dict(c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone())})
   if tail=='/invitations' and method=='GET':
    if role not in ('owner','admin'):return err(self,403,'forbidden')
    return self.sendj(200,{'invitations':[dict(x) for x in c.execute('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses AND expires>?',(w['id'],int(time.time()))).fetchall()]})
  mi=re.match(r'^/api/workspaces/([^/]+)/invitations/([^/]+)$',path)
  if mi and method=='DELETE':
   ww=c.execute('SELECT * FROM workspaces WHERE slug=?',(mi.group(1),)).fetchone()
   if not ww:return err(self,404,'workspace not found')
   rr=member(c,ww['id'],me['id'])
   if rr not in ('owner','admin'):return err(self,403,'forbidden')
   if not c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(mi.group(2),ww['id'])).fetchone():return err(self,404,'invitation')
   c.execute('DELETE FROM invitations WHERE code=?',(mi.group(2),));c.commit();return self.sendj(200,{'deleted':True})
  if path.startswith('/api/invitations/') and method=='POST':
   code=path.split('/')[-2] if path.endswith('/accept') else path.rsplit('/',1)[-1]; inv=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
   if not inv or inv['uses']>=inv['max_uses'] or inv['expires']<time.time() or (inv['invited_user_id'] and inv['invited_user_id']!=me['id']):return err(self,404,'invalid invitation')
   c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(inv['workspace_id'],me['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,));c.commit();return self.sendj(200,{'accepted':True})
  m=re.match(r'^/api/channels/(\d+)(.*)$',path)
  if m:
   i=int(m.group(1));tail=m.group(2); ch=chan(c,i)
   if not ch:return err(self,404,'channel not found')
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); role=(member(c,w['id'],me['id']) if w else ('member' if c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(i,me['id'])).fetchone() else None))
   if ch['is_dm'] and not role:return err(self,403,'not a DM member')
   if tail=='/join' and method=='POST':
    if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(i,me['id'])).fetchone():return err(self,403,'private channel')
    if role=='guest':return err(self,403,'guest')
    if not role and w['join_mode']=='open':c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],me['id'],'member'))
    elif not role:return err(self,403,'not member')
    c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(i,me['id']));c.commit();return self.sendj(200,{'channel':chobj(ch)})
   if tail=='/leave' and method=='POST':c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(i,me['id']));c.commit();return self.sendj(200,{'channel':chobj(ch)})
   handle=None
   gm=re.match(r'^/groups(?:/([^/]+))?$',tail)
   if gm:
    handle=gm.group(1)
    if method=='GET' and not handle:
     gs=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall(); outg=[]
     for g in gs: outg.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]})
     return self.sendj(200,{'groups':outg})
    if not role:return err(self,403,'not member')
    if method=='POST' and not handle:
     h=body.get('handle','').lstrip('@'); name=body.get('name',''); ids=body.get('user_ids',[])
     if not h or not name:return err(self,400,'missing fields')
     if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():return err(self,409,'duplicate handle')
     c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,me['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     for uid in ids:c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
     c.commit(); g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone(); return self.sendj(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}})
    if handle:
     g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle.lstrip('@'))).fetchone()
     if not g:
      if method=='DELETE':return self.sendj(200,{'deleted':True})
      return err(self,404,'group')
     if method=='PATCH':
      if me['id']!=g['creator_id'] and role!='owner':return err(self,403,'forbidden')
      c.execute('UPDATE groups SET name=? WHERE id=?',(body.get('name',g['name']),g['id']))
      if 'user_ids' in body:
       c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
       for uid in body['user_ids']:c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
      c.commit(); g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone(); return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]}})
     if method=='DELETE':
      if me['id']!=g['creator_id'] and role!='owner':return err(self,403,'forbidden')
      c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();return self.sendj(200,{'deleted':True})
   if method=='GET' and handle:return err(self,404,'group')
   if tail=='/members' and method=='GET':
    rs=c.execute('SELECT u.* FROM users u JOIN cmembers x ON x.user_id=u.id WHERE x.channel_id=?',(i,)).fetchall();return self.sendj(200,{'members':[user(x) for x in rs]})
   if tail in ('/archive','/unarchive') and method=='POST':
    if role!='owner':return err(self,403,'owner only')
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(tail=='/archive',i));c.commit();return self.sendj(200,{'channel':chobj(chan(c,i))})
   if tail=='' and method=='PATCH':
    if not role:return err(self,403,'forbidden')
    if len(body.get('topic',''))>250:return err(self,400,'topic')
    c.execute('UPDATE channels SET topic=? WHERE id=?',(body.get('topic',''),i));c.commit();return self.sendj(200,{'channel':chobj(chan(c,i))})
   if tail=='/messages' and method=='GET':
    try: lim=int(q.get('limit',['50'])[0])
    except: return err(self,400,'limit')
    if lim<1 or lim>200:return err(self,400,'limit')
    cur=q.get('cursor',[None])[0]; before=None
    if cur:
     try: before=int(cur)
     except: return err(self,400,'cursor')
    sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0 '+(('AND id<? ') if before else '')+'ORDER BY id DESC LIMIT ?'
    args=([i,before,lim+1] if before else [i,lim+1]); rs=c.execute(sql,args).fetchall(); nxt=str(rs[lim-1]['id']) if len(rs)>lim else None; rs=rs[:lim]
    return self.sendj(200,{'messages':[msgobj(c,x) for x in rs],'next_cursor':nxt})
   if tail=='/messages' and method=='POST':
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(i,me['id'])).fetchone():return err(self,403,'not member')
    b=body.get('body','')
    if ch['is_archived'] and not b.startswith('/unarchive'):return err(self,423,'archived')
    if not b:return err(self,400,'empty message')
    if b.startswith('/') and not b.startswith('//'):
     parts=b.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
     if cmd=='/me':
      if not arg.strip():return err(self,400,'/me requires text')
     elif cmd=='/shrug':
      b=(arg.strip()+' ' if arg.strip() else '')+'¯\\_(ツ)_/¯'
     elif cmd=='/topic':
      if not arg.strip():return err(self,400,'topic required')
      if not role:return err(self,403,'forbidden')
      c.execute('UPDATE channels SET topic=? WHERE id=?',(arg.strip(),i));c.commit()
      return self.sendj(201,{'message':None,'channel':chobj(chan(c,i))})
     elif cmd=='/invite':
      if role not in ('owner','admin'):return err(self,403,'forbidden')
      uname=arg.strip().lstrip('@'); ur=c.execute('SELECT * FROM users WHERE username=?',(uname,)).fetchone()
      if not ur:return err(self,404,'user not found')
      c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(i,ur['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],ur['id'],'member'));c.commit()
      return self.sendj(201,{'message':None,'invited_user':user(ur)})
     elif cmd in ('/archive','/unarchive'):
      if role!='owner':return err(self,403,'owner only')
      archived=(cmd=='/archive');c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,i));c.commit()
      return self.sendj(201,{'message':None,'channel':chobj(chan(c,i))})
     else:return err(self,400,'unknown command')

    fids=body.get('file_ids',[]) or []
    for fid in fids:
     fr=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
     if not fr or fr['uploader_id']!=me['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return err(self,400,'invalid attachment')
    eid=(c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM messages WHERE channel_id=?',(i,)).fetchone()['n']);c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(i,me['id'],b,body.get('parent_id'),now(),eid));cur=c.execute('SELECT last_insert_rowid()');x=cur.fetchone()[0]
    c.commit()
    c.execute('INSERT OR REPLACE INTO channel_events VALUES(?,?,?,?)',(i,eid,'message.reply' if body.get('parent_id') else 'message.created',x))
    for fid in fids:c.execute('INSERT INTO message_files VALUES(?,?)',(x,fid))
    c.commit()
    return self.sendj(201,{'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(x,)).fetchone())})
  m=re.match(r'^/api/messages/(\d+)(.*)$',path)
  if m:
   i=int(m.group(1));tail=m.group(2); r=c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone()
   if not r:return err(self,404,'message not found')
   if tail=='' and method=='PATCH':
    if r['deleted']:return err(self,409,'deleted')
    if r['author_id']!=me['id']:return err(self,403,'author only')
    if not body.get('body'):return err(self,400,'empty')
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body['body'],now(),i)); ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']; c.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(r['channel_id'],ev,'message.updated',i)); c.commit();return self.sendj(200,{'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone())})
   if tail=='' and method=='DELETE':
    if r['author_id']!=me['id']:
     ww=c.execute('SELECT owner_id FROM workspaces w JOIN channels x ON x.workspace_id=w.id WHERE x.id=?',(r['channel_id'],)).fetchone()
     if not ww or ww['owner_id']!=me['id']:return err(self,403,'forbidden')
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(i,)); ev=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']; c.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(r['channel_id'],ev,'message.deleted',i)); c.commit();return self.sendj(200,{'deleted':True})
   if tail=='/reactions' and method=='POST':
    emoji=body.get('emoji','')
    if not emoji:return err(self,400,'emoji required')
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(i,me['id'],emoji));c.commit();fresh=c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone();return self.sendj(200,{'reactions':msgobj(c,fresh)['reactions']})
   if tail.startswith('/reactions/') and method=='DELETE':
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(i,me['id'],unquote(tail.split('/')[-1])));c.commit();fresh=c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone();return self.sendj(200,{'reactions':msgobj(c,fresh)['reactions']})
   if tail=='/replies' and method=='GET':
    try: lim=int(q.get('limit',['50'])[0])
    except: return err(self,400,'limit')
    if lim<1 or lim>200:return err(self,400,'limit')
    cur=q.get('cursor',[None])[0]; after=None
    if cur:
     try: after=int(cur)
     except: return err(self,400,'cursor')
    sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0 '+(('AND id>? ') if after else '')+'ORDER BY id ASC LIMIT ?'
    args=([i,after,lim+1] if after else [i,lim+1]); rs=c.execute(sql,args).fetchall(); nxt=str(rs[lim-1]['id']) if len(rs)>lim else None; rs=rs[:lim]
    return self.sendj(200,{'replies':[msgobj(c,x) for x in rs],'next_cursor':nxt})
  if path.startswith('/api/channels/') and path.endswith('/pins') and method=='GET':
   pp=path.split('/')
   if len(pp)!=5 or not pp[3].isdigit(): return err(self,404,'channel')
   i=int(pp[3]); ch=chan(c,i)
   if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(i,me['id'])).fetchone():return err(self,403,'forbidden')
   try: lim=int(q.get('limit',['50'])[0])
   except:return err(self,400,'limit')
   if lim<1 or lim>200:return err(self,400,'limit')
   cur=q.get('cursor',[None])[0]; args=[i]
   extra=''
   if cur:
    try: curid=int(cur)
    except:return err(self,400,'cursor')
    extra=' AND p.message_id<? '; args.append(curid)
   args.append(lim+1)
   rs=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'+extra+' ORDER BY p.pinned_at DESC, p.message_id DESC LIMIT ?',args).fetchall()
   nxt=str(rs[lim-1]['message_id']) if len(rs)>lim else None
   return self.sendj(200,{'pins':[{'message':msgobj(c,x),'pinned_by':x['user_id'],'pinned_at':x['pinned_at']} for x in rs[:lim]],'next_cursor':nxt})
  mp=None
  if path.startswith('/api/messages/') and path.endswith('/pin'):
   parts=path.split('/')
   if len(parts)==5 and parts[3].isdigit(): mp=parts[3]
  if mp:
   i=int(mp); r=c.execute('SELECT * FROM messages WHERE id=?',(i,)).fetchone()
   if not r:return err(self,404,'message')
   ch=chan(c,r['channel_id'])
   if method=='POST':
    c.execute('INSERT OR IGNORE INTO pins(message_id,user_id,pinned_at) VALUES(?,?,?)',(i,me['id'],now()));c.commit()
    x=c.execute('SELECT * FROM pins WHERE message_id=?',(i,)).fetchone();return self.sendj(200,{'pin':{'message_id':i,'pinned_by':x['user_id'],'pinned_at':x['pinned_at']}})
   if method=='DELETE':
    c.execute('DELETE FROM pins WHERE message_id=?',(i,));c.commit();return self.sendj(200,{'unpinned':True})
  if path=='/api/search' and method=='GET':
   term=q.get('q',[''])[0]
   try: lim=int(q.get('limit',['50'])[0])
   except: return err(self,400,'limit')
   if lim<1 or lim>200:return err(self,400,'limit')
   wid=q.get('workspace_id',[None])[0]; cur=q.get('cursor',[None])[0]; before=None
   if cur:
    try: before=int(cur)
    except: return err(self,400,'cursor')
   sql='SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN cmembers cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.body LIKE ? AND m.deleted=0 '
   args=[me['id'],f'%{term}%']
   if wid: sql+='AND ch.workspace_id=? '; args.append(int(wid))
   if before: sql+='AND m.id<? '; args.append(before)
   sql+='ORDER BY m.id DESC LIMIT ?'; args.append(lim+1)
   rs=c.execute(sql,args).fetchall(); nxt=str(rs[lim-1]['id']) if len(rs)>lim else None
   return self.sendj(200,{'results':[msgobj(c,x) for x in rs[:lim]],'next_cursor':nxt})
  if path=='/api/dms' and method=='POST':
   uid=body.get('user_id'); old=c.execute('SELECT c.id FROM channels c JOIN cmembers a ON a.channel_id=c.id JOIN cmembers b ON b.channel_id=c.id WHERE c.is_dm=1 AND a.user_id=? AND b.user_id=?',(me['id'],uid)).fetchone()
   if old:return self.sendj(200,{'channel_id':old['id']})
   if not c.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone():return err(self,404,'user')
   cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,'dm-'+str(me['id'])+'-'+str(uid)));i=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(i,me['id'],i,uid));c.commit();return self.sendj(200,{'channel_id':i})
  if path.startswith('/api/channels/') and path.endswith('/read'):
   pp=path.split('/')
   if len(pp)!=5 or not pp[3].isdigit():return err(self,404,'channel')
   i=int(pp[3]); rr=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(me['id'],i)).fetchone()
   if method=='POST':
    n=int(body.get('last_read_event_id',0)); oldn=rr['last_read_event_id'] if rr else 0; n=max(n,oldn)
    c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(me['id'],i,n));c.commit();rr=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(me['id'],i)).fetchone()
   mr=c.execute('SELECT mention_count FROM users WHERE id=?',(me['id'],)).fetchone(); mc=(mr['mention_count'] if mr and mr['mention_count'] is not None else 0); return self.sendj(200,{'read_state':{'channel_id':i,'last_read_event_id':rr['last_read_event_id'] if rr else 0,'unread_count':0,'mention_count':mc}})
  return err(self,404,'not found')
 def page(self):
  b=b'''<!doctype html><html><head><title>Huddle</title><style>body{font-family:Arial;margin:0;background:#f8f8f8}button{background:#007a5a;color:#fff;border:0;padding:9px;margin:4px}#app{padding:30px}</style></head><body><div id="app"><h1>Huddle</h1><div data-testid="auth-modal"><form data-testid="auth-form"><input name="username" placeholder="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary">Log in</button></form></div></div></body></html>'''
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def ws(self,me):
  if not me:
   self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
  import hashlib, base64, struct
  OPEN_WS.add(me['id'])
  pc=db(); pc.execute('INSERT OR REPLACE INTO ws_presence(user_id,seen_at) VALUES(?,?)',(me['id'],int(time.time()))); pc.commit(); pc.close()
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:
   self.send_response(400); self.send_header('Content-Length','0'); self.end_headers(); return
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols'); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  def send(o):
   b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
   if n<126: self.connection.sendall(bytes([129,n])+b)
   else: self.connection.sendall(bytes([129,126])+struct.pack('!H',n)+b)
  subscribed=set(); sent={}
  known={}; profile_seq=0
  cc=db()
  profile_seq=cc.execute('SELECT COALESCE(MAX(id),0) n FROM profile_events').fetchone()['n']
  for ur in cc.execute('SELECT * FROM users').fetchall():
   known[ur['id']]=(ur['username'],ur['display_name'],ur['timezone'],ur['avatar_url'],ur['status_text'],ur['status_emoji'])
  cc.close()
  self.connection.settimeout(0.2)
  try:
   while True:
    cc=db()
    evs=cc.execute('SELECT id,user_id FROM profile_events WHERE id>? ORDER BY id',(profile_seq,)).fetchall()
    for ev in evs:
     ur=cc.execute('SELECT * FROM users WHERE id=?',(ev['user_id'],)).fetchone()
     if ur: send({'type':'user.updated','user':user(ur)})
     profile_seq=ev['id']
    for ur in cc.execute('SELECT * FROM users').fetchall():
     state=(ur['username'],ur['display_name'],ur['timezone'],ur['avatar_url'],ur['status_text'],ur['status_emoji'])
     if ur['id'] in known and known[ur['id']]!=state:
      send({'type':'user.updated','user':user(ur)})
     known[ur['id']]=state
    cc.close()
    for cid in list(subscribed):
      cc=db(); rows=cc.execute('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,sent.get(cid,0))).fetchall()
      for ev in rows:
       mr=cc.execute('SELECT * FROM messages WHERE id=?',(ev['message_id'],)).fetchone()
       send({'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,'message':dict(mr) if mr else {'id':ev['message_id']}})
       sent[cid]=ev['event_id']
      cc.close()
    try: h=self.connection.recv(2)
    except socket.timeout: continue
    if len(h)<2: break
    ln=h[1]&127; masked=h[1]&128
    if ln==126: ln=struct.unpack('!H',self.connection.recv(2))[0]
    elif ln==127: ln=struct.unpack('!Q',self.connection.recv(8))[0]
    mask=self.connection.recv(4) if masked else b''; data=self.connection.recv(ln)
    if masked: data=bytes(data[j]^mask[j%4] for j in range(len(data)))
    try: o=json.loads(data)
    except: continue
    if o.get('type')=='subscribe':
     cid=int(o.get('channel_id',0)); subscribed.add(cid); cc=db(); head=cc.execute('SELECT COALESCE(MAX(event_id),0) n FROM messages WHERE channel_id=?',(cid,)).fetchone()['n']; cc.close(); sent[cid]=head; send({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif o.get('type')=='resume':
     cid=int(o.get('channel_id',0)); subscribed.add(cid); since=int(o.get('since_event_id',0)); cc=db(); rows=cc.execute('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall(); head=cc.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']
     for ev in rows:
      mr=cc.execute('SELECT * FROM messages WHERE id=?',(ev['message_id'],)).fetchone(); send({'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,'message':dict(mr) if mr else {'id':ev['message_id']}})
     cc.close()
     sent[cid]=head; send({'type':'resumed','channel_id':cid,'head_event_id':head})
  except Exception: pass
  finally:
   OPEN_WS.discard(me['id'])
   pc=db(); pc.execute('UPDATE ws_presence SET seen_at=? WHERE user_id=?',(int(time.time()),me['id'])); pc.commit(); pc.close()
 def log_message(self,*a):pass
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('PORT','8000'))),H).serve_forever()
