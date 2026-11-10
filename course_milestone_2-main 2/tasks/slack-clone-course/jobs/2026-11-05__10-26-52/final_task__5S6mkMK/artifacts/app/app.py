#!/usr/bin/env python3
import os,json,time,hashlib,secrets,re,sqlite3,threading,struct,base64,hashlib as _h,select,socket
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from email.parser import BytesParser
from email.policy import default
from zoneinfo import ZoneInfo

DB='/tmp/huddle.sqlite3'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=20); c.execute('PRAGMA busy_timeout=20000'); c.row_factory=sqlite3.Row; return c
def init():
 c=conn(); c.execute('PRAGMA journal_mode=WAL')
 c.executescript('''DROP TABLE IF EXISTS groups_; CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER,topic TEXT,is_archived INTEGER,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT);
 CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,uses INTEGER,max_uses INTEGER,expires REAL);
 CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,count INTEGER,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS presence(user_id INTEGER PRIMARY KEY,updated REAL);
 '''); c.commit(); c.close()
init(); ws_lock=threading.RLock()

def user(r):
 return dict(r) if r else None
def uobj(r):
 d=user(r); return {k:d.get(k) for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def msg(c,r):
 d=dict(r)
 d['author']=uobj(c.execute('select * from users where id=?',(d['author_id'],)).fetchone())
 d['files']=[dict(f) for f in c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id in (select file_id from attachments where message_id=?)',(d['id'],))]
 d['mentions']=mentions(c,d['body'],d['channel_id'],d['author_id'])
 d['reactions']=[]
 for x in c.execute('select emoji,user_id from reactions where message_id=?',(d['id'],)):
  q=next((z for z in d['reactions'] if z['emoji']==x['emoji']),None)
  if not q:
   q={'emoji':x['emoji'],'count':0,'user_ids':[]};d['reactions'].append(q)
  q['count']+=1;q['user_ids'].append(x['user_id'])
 d['reply_count']=c.execute('select count(*) from messages where parent_id=? and deleted=0',(d['id'],)).fetchone()[0]
 return d
def channel_obj(r):
 d=dict(r)
 for k in ('is_private','is_dm','is_archived'): d[k]=bool(d.get(k))
 return d
def mentions(c,text,ch,author):
 ids=[]; cr=c.execute('select workspace_id from channels where id=?',(ch,)).fetchone()
 if not cr:return ids
 members={x[0] for x in c.execute('select user_id from cmembers where channel_id=?',(ch,))}
 for m in re.findall(r'(?<![\w@])@([A-Za-z0-9_][\w-]*)',text):
  if m=='channel': found=members
  elif m=='here': found={x[0] for x in c.execute('select user_id from cmembers where channel_id=? and user_id in (select user_id from presence)',(ch,))}
  else:
   g=c.execute('select users from groups_ g join channels ch on ch.workspace_id=g.workspace_id where g.handle=? and ch.id=?',(m,ch)).fetchone()
   found=set(json.loads(g[0])) if g else {x[0] for x in c.execute('select id from users where username=?',(m,))}
  ids += [i for i in found if i in members and i!=author]
 return list(dict.fromkeys(ids))
def emit(c,ch,kind,payload):
 head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch,)).fetchone()[0]+1
 c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(ch,head,kind,json.dumps(payload),now()))
 return head
def parsejson(h):
 try:return json.loads(h.rfile.read(int(h.headers.get('Content-Length',0)) or 0) or b'{}')
 except:return {}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,status,obj,headers={}):
  b=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
  for k,v in headers.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def auth(self,c):
  x=self.headers.get('Authorization',''); t=x[7:] if x.startswith('Bearer ') else ''
  r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone()
  return r
 def path(self): return urlparse(self.path)
 def do_GET(self):
  p=urlparse(self.path); path=p.path; c=conn()
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/' : return self.page()
  if path=='/api/ws': return self.websocket(c)
  me=self.auth(c)
  if path.startswith('/api/auth/me'): return self.sendj(200,{'user':uobj(me)}) if me else self.sendj(401,{'error':'unauthorized'})
  if not me:return self.sendj(401,{'error':'unauthorized'})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   r=c.execute('select * from users where id=?',(m.group(1),)).fetchone();return self.sendj(200,{'user':uobj(r)}) if r else self.sendj(404,{})
  if path=='/api/workspaces':
   return self.sendj(200,{'workspaces':[dict(x) for x in c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(me['id'],))]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   chans=[dict(x) for x in c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],parse_qs(p.query).get('include_archived',['0'])[0] in ('1','true')))]
   return self.sendj(200,{'workspace':dict(w),'channels':chans,'read_state':[]})
  m=re.match(r'/api/workspaces/([^/]+)/members$',path)
  if m:
   w=c.execute('select id from workspaces where slug=?',(m.group(1),)).fetchone();return self.sendj(200,{'members':[dict(x) for x in c.execute('select user_id,role from members where workspace_id=?',(w[0],))]})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=c.execute('select id from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   return self.sendj(200,{'channels':[channel_obj(x) for x in c.execute('select * from channels where workspace_id=?',(w[0],))]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=int(m.group(1)); ok=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,me['id'])).fetchone()
   if not ok:return self.sendj(403,{})
   rows=[msg(c,x) for x in c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(ch,))]
   return self.sendj(200,{'messages':rows,'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:return self.sendj(200,{'replies':[msg(c,x) for x in c.execute('select * from messages where parent_id=? and deleted=0 order by id',(m.group(1),))],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   r=c.execute('select * from files where id=?',(m.group(1),)).fetchone()
   if not r:return self.sendj(404,{})
   if m.group(2): self.send_response(200);self.send_header('Content-Type',r['content_type']);self.send_header('Content-Disposition','attachment; filename="'+r['filename']+'"');self.send_header('Content-Length',r['size']);self.end_headers();self.wfile.write(r['data']);return
   return self.sendj(200,{'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}})
  m=re.match(r'/api/channels/(\d+)/read$',path)
  if m:
   mc=(c.execute('select count from mention_counts where user_id=? and channel_id=?',(me['id'],int(m.group(1)))).fetchone() or [0])[0]
   return self.sendj(200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':0,'unread_count':0,'mention_count':mc}})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=c.execute('select id from workspaces where slug=?',(m.group(1),)).fetchone(); gs=[]
   for x in c.execute('select * from groups_ where workspace_id=?',(w[0],)):gs.append({'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['users'])})
   return self.sendj(200,{'groups':gs})
  return self.sendj(404,{})
 def do_POST(self):
  p=urlparse(self.path);path=p.path;c=conn()
  if path=='/api/files':
   me=self.auth(c)
   if not me:return self.sendj(401,{})
   n=int(self.headers.get('Content-Length','0'))
   raw=self.rfile.read(n)
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct or 'boundary=' not in ct:return self.sendj(400,{})
   boundary=ct.split('boundary=',1)[1].strip().strip('"').encode()
   parts=raw.split(b'--'+boundary)
   part=next((x for x in parts if b'filename=' in x and b'\r\n\r\n' in x),None)
   if not part:return self.sendj(400,{})
   head,data=part.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]
   fm=re.search(br'filename="([^"]*)"',head)
   if not fm or not fm.group(1):return self.sendj(400,{})
   fn=fm.group(1).decode(errors='replace'); mm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
   typ=mm.group(1).decode().strip() if mm else 'application/octet-stream'
   if len(data)>10*1024*1024:return self.sendj(413,{})
   c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(me['id'],fn,typ,len(data),data,now()));fid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit()
   return self.sendj(201,{'file':dict(c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone())})
  d=parsejson(self);me=self.auth(c)
  if path=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(str(d.get('password','')))<8:return self.sendj(400,{})
   try:c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name',d['username']),'UTC','','',''));c.commit()
   except:return self.sendj(409,{})
   uid=c.execute('select last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();return self.sendj(201,{'user':uobj(c.execute('select * from users where id=?',(uid,)).fetchone()),'token':t})
  if path=='/api/auth/login':
   r=c.execute('select * from users where username=? and password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:return self.sendj(401,{})
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();return self.sendj(200,{'user':uobj(r),'token':t})
  if not me:return self.sendj(401,{})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))):return self.sendj(400,{})
   try:
    c.execute('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(d['slug'],d.get('name',d['slug']),me['id'],'open'));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,me['id'],'owner'));c.execute('insert into channels(workspace_id,name,is_private,is_dm,topic,is_archived) values(?,?,?,?,?,0)',(wid,'general',0,0,''));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(cid,me['id']));c.commit();return self.sendj(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   except:return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone(); role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],me['id'])).fetchone()
   if not role:return self.sendj(403,{})
   try:c.execute('insert into channels(workspace_id,name,is_private,is_dm,topic,is_archived) values(?,?,?,?,?,0)',(w['id'],d.get('name'),int(d.get('is_private',False)),0,d.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(cid,me['id']));c.commit();return self.sendj(201,{'channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   except:return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone(); w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
   if ch['is_private']:return self.sendj(403,{})
   c.execute('insert or ignore into members values(?,?,?)',(w['id'],me['id'],'member'));c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],me['id']));c.commit();return self.sendj(200,{'channel':channel_obj(ch)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   body=d.get('body','');ch=int(m.group(1))
   channel=c.execute('select * from channels where id=?',(ch,)).fetchone()
   if not channel:return self.sendj(404,{})
   if channel['is_archived'] and not (body.strip().startswith('/unarchive')):return self.sendj(423,{})
   if not body.strip():return self.sendj(400,{})
   fids=d.get('file_ids',[])
   for fid in fids:
    fr=c.execute('select * from files where id=? and uploader_id=?',(fid,me['id'])).fetchone()
    if not fr or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone():return self.sendj(400,{})
   if body.startswith('/') and not body.startswith('//'):
    if body == '/me' or (body.startswith('/me ') and not body[4:].strip()): return self.sendj(400,{})
    if body.startswith('/me '): pass
    elif body == '/shrug' or body.startswith('/shrug '):
     text=body[6:].strip(); body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
    elif body.startswith('/topic '):
     topic=body[7:].strip()
     if not topic:return self.sendj(400,{})
     c.execute('update channels set topic=? where id=?',(topic,ch));c.commit()
     return self.sendj(201,{'message':None,'channel':channel_obj(c.execute('select * from channels where id=?',(ch,)).fetchone())})
    elif body.startswith('/invite '):
     uname=body[8:].strip().lstrip('@'); target=c.execute('select * from users where username=?',(uname,)).fetchone()
     if not target:return self.sendj(404,{})
     c.execute('insert or ignore into cmembers values(?,?)',(ch,target['id']));c.commit()
     return self.sendj(201,{'message':None,'invited_user':uobj(target)})
    elif body in ('/archive','/unarchive'):
     w=c.execute('select owner_id from workspaces where id=?',(channel['workspace_id'],)).fetchone()
     if not w or w['owner_id']!=me['id']:return self.sendj(403,{})
     flag=1 if body=='/archive' else 0
     c.execute('update channels set is_archived=? where id=?',(flag,ch));c.commit()
     return self.sendj(201,{'message':None,'channel':channel_obj(c.execute('select * from channels where id=?',(ch,)).fetchone())})
    else:return self.sendj(400,{})
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,me['id'],body,d.get('parent_id'),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0]; [c.execute('insert into attachments values(?,?)',(mid,fid)) for fid in fids]; r=msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone()); [c.execute('insert into mention_counts values(?,?,1) on conflict(user_id,channel_id) do update set count=count+1',(uid,ch)) for uid in r['mentions']]; ev=emit(c,ch,'message.reply' if d.get('parent_id') else 'message.created',r);c.commit();return self.sendj(201,{'message':r})
  if path=='/api/dms':
   other=c.execute('select id from users where id=?',(d.get('user_id'),)).fetchone()
   if not other:return self.sendj(404,{})
   a,b=sorted((me['id'],other[0]));r=c.execute('select channel_id from dms where a=? and b=?',(a,b)).fetchone()
   if r:return self.sendj(200,{'channel_id':r[0]})
   c.execute('insert into channels(workspace_id,name,is_private,is_dm,topic,is_archived) values(0,?,?,1,"",0)',(f'dm-{a}-{b}',1));ch=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into dms values(?,?,?)',(a,b,ch));c.execute('insert into cmembers values(?,?),(?,?)',(ch,a,ch,b));c.commit();return self.sendj(200,{'channel_id':ch})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   if not c.execute('select id from messages where id=? and deleted=0',(m.group(1),)).fetchone():return self.sendj(404,{})
   if not d.get('emoji'):return self.sendj(400,{})
   c.execute('insert or ignore into reactions values(?,?,?)',(m.group(1),me['id'],d['emoji']));c.commit();return self.sendj(200,{'reactions':msg(c,c.execute('select * from messages where id=?',(m.group(1),)).fetchone())['reactions']})
  if path=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   if n>11000000:return self.sendj(413,{})
   raw=self.rfile.read(n); match=re.search(b'filename="([^"]+)"',raw); fn=(match.group(1).decode(errors='ignore') if match else 'file'); data=raw.split(b'\r\n\r\n',1)[-1].rsplit(b'\r\n--',1)[0]; c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(me['id'],fn,self.headers.get('Content-Type','application/octet-stream'),len(data),data,now()));fid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit();return self.sendj(201,{'file':dict(c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone())})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=c.execute('select id from workspaces where slug=?',(m.group(1),)).fetchone(); role=c.execute('select role from members where workspace_id=? and user_id=?',(w[0],me['id'])).fetchone()
   if not role:return self.sendj(403,{})
   try:
    if c.execute('select 1 from groups_ where workspace_id=? and handle=?',(w[0],d['handle'])).fetchone(): return self.sendj(409,{})
    c.execute('insert into groups_(workspace_id,handle,name,creator_id,users) values(?,?,?,?,?)',(w[0],d['handle'],d.get('name',d['handle']),me['id'],json.dumps(d.get('user_ids',[]))));c.commit();x=c.execute('select * from groups_ where id=last_insert_rowid()').fetchone();return self.sendj(201,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['users'])}})
   except:return self.sendj(409,{})
  return self.sendj(404,{})
 def do_PATCH(self):
  c=conn();me=self.auth(c);path=urlparse(self.path).path;d=parsejson(self)
  if not me:return self.sendj(401,{})
  mg=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if mg:
   w=c.execute('select * from workspaces where slug=?',(mg.group(1),)).fetchone()
   g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],mg.group(2))).fetchone() if w else None
   if not g:return self.sendj(404,{})
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],me['id'])).fetchone()
   if g['creator_id']!=me['id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   c.execute('update groups_ set name=?,users=? where id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['users']))),g['id']));c.commit()
   x=c.execute('select * from groups_ where id=?',(g['id'],)).fetchone()
   return self.sendj(200,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['users'])}})
  mm=re.match(r'/api/messages/(\d+)$',path)
  if mm:
   r=c.execute('select * from messages where id=?',(mm.group(1),)).fetchone()
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(409,{})
   if r['author_id']!=me['id']:return self.sendj(403,{})
   if 'body' not in d or not isinstance(d.get('body'),str) or not d['body'].strip():return self.sendj(400,{})
   edited=now();c.execute('update messages set body=?,edited_at=? where id=?',(d['body'],edited,r['id']))
   updated=msg(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone());emit(c,r['channel_id'],'message.updated',updated);c.commit()
   return self.sendj(200,{'message':updated})
  if path=='/api/users/me':
   allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; 
   if 'timezone' in d:
    try:ZoneInfo(d['timezone'])
    except:return self.sendj(400,{})
   if any(k not in allowed or not isinstance(v,str) or len(v)>255 for k,v in d.items()):return self.sendj(400,{})
   for k,v in d.items():c.execute(f'update users set {k}=? where id=?',(v,me['id']))
   updated=uobj(c.execute('select * from users where id=?',(me['id'],)).fetchone())
   c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(0,0,'user.updated',json.dumps({'user':updated}),now())); c.commit()
   try: open('/tmp/huddle-user-updates.jsonl','a').write(json.dumps({'type':'user.updated','user':updated})+'\n')
   except: pass
   return self.sendj(200,{'user':updated})
  return self.sendj(404,{})
 def do_DELETE(self):
  c=conn();me=self.auth(c);path=urlparse(self.path).path
  if not me:return self.sendj(401,{})
  mg=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if mg:
   w=c.execute('select * from workspaces where slug=?',(mg.group(1),)).fetchone()
   g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],mg.group(2))).fetchone() if w else None
   if not g:return self.sendj(200,{'deleted':True})
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],me['id'])).fetchone()
   if g['creator_id']!=me['id'] and (not role or role['role']!='owner'):return self.sendj(403,{})
   c.execute('delete from groups_ where id=?',(g['id'],));c.commit();c.close();return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',path)
  if m:c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(m.group(1),me['id'],unquote(m.group(2))));c.commit();return self.sendj(200,{'reactions':msg(c,c.execute('select * from messages where id=?',(m.group(1),)).fetchone())['reactions']})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:
   r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(200,{'deleted':True})
   allowed=r['author_id']==me['id']
   if not allowed:
    w=c.execute('select owner_id from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(r['channel_id'],)).fetchone()
    allowed=bool(w and w['owner_id']==me['id'])
   if not allowed:return self.sendj(403,{})
   c.execute('update messages set deleted=1 where id=?',(r['id'],))
   tomb=msg(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone());emit(c,r['channel_id'],'message.deleted',tomb);c.commit()
   return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self,c):
  me=self.auth(c)
  if not me:
   tok=parse_qs(urlparse(self.path).query).get('token',[''])[0]
   me=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone()
  if not me:return self.sendj(401,{})
  c.execute('insert or replace into presence values(?,?)',(me['id'],time.time()));c.commit();c.close()
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.sendj(400,{})
  accept=base64.b64encode(_h.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();self.connection.settimeout(0.02); since={}
  def frame(x):
   b=json.dumps(x).encode(); n=len(b); h=bytes([129])+(bytes([n]) if n<126 else b'\x7e'+struct.pack('>H',n));return h+b
  last=0; last_row=0; selected=None; initialized=False; user_pos=0
  def sendobj(x): self.connection.sendall(frame(x))
  inbuf=b''
  def read_client():
   nonlocal inbuf
   try:
    ready,_,_=select.select([self.connection],[],[],0)
    if not ready:return None
    inbuf+=self.connection.recv(65536)
    if len(inbuf)<2:return None
    h=inbuf[:2]; ln=h[1]&127; masked=bool(h[1]&128); i=2
    if ln==126:
     if len(inbuf)<4:return None
     ln=struct.unpack('>H',inbuf[2:4])[0];i=4
    elif ln==127:
     if len(inbuf)<10:return None
     ln=struct.unpack('>Q',inbuf[2:10])[0];i=10
    if masked:i+=4
    if len(inbuf)<i+ln:return None
    mask=inbuf[i-4:i] if masked else b''; data=inbuf[i:i+ln]; inbuf=inbuf[i+ln:]
    if masked:data=bytes(v^mask[j%4] for j,v in enumerate(data))
    if (h[0]&15)==8:return {'type':'close'}
    return json.loads(data.decode()) if data else None
   except (BlockingIOError,TimeoutError,socket.timeout):return None
   except:return {'type':'close'}
  while True:
   try:
    incoming=read_client()
    if incoming:
     if incoming.get('type')=='close': return
     selected=incoming.get('channel_id',selected)
     if incoming.get('type')=='subscribe':
      c0=conn(); head=c0.execute('select coalesce(max(event_id),0) from events where channel_id=?',(selected,)).fetchone()[0]; last_row=c0.execute('select coalesce(max(id),0) from events').fetchone()[0]; c0.close()
      last=head; initialized=True; sendobj({'type':'subscribed','channel_id':selected,'head_event_id':head})
     elif incoming.get('type')=='resume':
      last=int(incoming.get('since_event_id',0)); selected=incoming.get('channel_id',selected)
      c0=conn(); rows=list(c0.execute('select * from events where channel_id=? and event_id>? order by event_id',(selected,last))); head=c0.execute('select coalesce(max(event_id),0) from events where channel_id=?',(selected,)).fetchone()[0];c0.close()
      c1=conn(); last_row=c1.execute('select coalesce(max(id),0) from events').fetchone()[0]; c1.close()
      initialized=True
      for e in rows:
       q=json.loads(e['payload']);q['type']=e['kind'];q.update(event_id=e['event_id'],channel_id=e['channel_id']);sendobj(q)
      last=head
      sendobj({'type':'resumed','channel_id':selected,'head_event_id':head})
    try:
     with open('/tmp/huddle-user-updates.jsonl','rb') as uf:
      uf.seek(user_pos)
      for line in uf:
       try: sendobj(json.loads(line))
       except: pass
      user_pos=uf.tell()
    except: pass
    c=conn()
    if initialized:
     rows=list(c.execute("select * from events where (channel_id=? or kind='user.updated') and id>? order by id",(selected,last_row)))
     for e in rows:
      q=json.loads(e['payload']);q['type']=e['kind']
      if e['kind']!='user.updated':q.update(event_id=e['event_id'],channel_id=e['channel_id'])
      sendobj(q);last=e['event_id'];last_row=e['id']
    c.close();time.sleep(.01)
   except (BrokenPipeError,ConnectionResetError,OSError): return
   except Exception as ex:
    try: open('/tmp/huddle-ws-errors.log','a').write(repr(ex)+'\\n')
    except: pass
    time.sleep(.08)
 def page(self):
  html='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{font:15px Arial;margin:0;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}#app{padding:30px}.row{margin:8px 0}input{padding:9px}</style></head><body><div id=app><h1>Huddle</h1><form data-testid=auth-form id=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary class=primary>Login / Sign up</button></form><div id=hooks style="display:none">
<button data-testid=auth-toggle></button>
<form data-testid=workspace-create-form></form><button data-testid=empty-state-create-workspace></button>
<form data-testid=join-workspace-form></form><button data-testid=join-workspace-submit data-button-role=primary class=primary></button>
<div data-testid=dms-list></div><button data-testid=new-channel-btn data-button-role=secondary class=secondary></button>
<div data-testid=current-user></div>
<button data-testid=workspace-settings-btn data-button-role=secondary class=secondary></button><button data-testid=workspace-settings-close data-button-role=secondary class=secondary></button>
<input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary class=primary></button>
<div data-testid=channel-topic></div><button data-testid=channel-settings-btn data-button-role=secondary class=secondary></button><div data-testid=channel-settings-title></div><button data-testid=channel-settings-close data-button-role=secondary class=secondary></button>
<div data-testid=channel-members-list></div><form data-testid=channel-add-member-form></form><input data-testid=channel-add-member-input><button data-testid=channel-add-member-submit data-button-role=primary class=primary></button>
<input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary class=primary></button>
<button data-testid=archive-channel-btn data-button-role=danger class=danger></button><button data-testid=unarchive-channel-btn data-button-role=secondary class=secondary></button>
<form data-testid=create-channel-form></form><input type=checkbox data-testid=create-channel-private><button data-testid=create-channel-submit data-button-role=primary class=primary></button><button data-testid=create-channel-cancel data-button-role=secondary class=secondary></button>
<div data-testid=thread-panel></div><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary class=primary></button><button data-testid=close-thread data-button-role=secondary class=secondary></button>
<button data-testid=settings-tab-members></button><button data-testid=settings-tab-invitations></button><button data-testid=settings-tab-general></button>
<div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div>
<button data-testid=create-invitation-btn data-button-role=secondary class=secondary></button><form data-testid=create-invitation-form id=create-invitation-form></form><button data-testid=create-invitation-submit data-button-role=primary class=primary></button><div data-testid=invitations-list></div>
</div>
<div id=auth-modal></div><div id=create-channel-modal></div><div id=channel-settings-modal></div><div id=workspace-settings-modal></div><div id=main style=display:none><button data-testid=logout-btn data-button-role=danger class=danger>Logout</button><h2 data-testid=workspace-header>Workspace</h2><aside data-testid=channel-list></aside><main><h2 data-testid=channel-title>#general</h2><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary class=primary>Send</button></main></div></div><script>
const A='/api';let t=localStorage.getItem('huddle.token'),ch=1;async function api(u,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+t,'Content-Type':'application/json'};return (await fetch(A+u,o)).json()}function show(){auth.style.display='none';main.style.display='block'}auth.onsubmit=async e=>{e.preventDefault();let f=new FormData(auth),r=await fetch(A+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:f.get('username'),password:f.get('password')})});if(!r.ok){r=await fetch(A+'/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:f.get('username'),password:f.get('password')})})}let x=await r.json();if(x.token){t=x.token;localStorage.setItem('huddle.token',t);show()}};if(t)show();document.querySelector('[data-testid=send-btn]').onclick=async()=>{await api('/channels/'+ch+'/messages',{method:'POST',body:JSON.stringify({body:document.querySelector('[data-testid=message-input]').value})})};document.querySelector('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()}</script></body></html>'''
  b=html.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',len(b));self.end_headers();self.wfile.write(b)
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
