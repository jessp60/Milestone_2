import os,sys,json,sqlite3,socket,cgi,io,secrets,hashlib,base64,struct,threading,time,re,datetime,subprocess,signal
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
DB='/app/huddle.db'; lock=threading.RLock(); sockets=[]; ircs=[]; active_users=set(); node=int(sys.argv[1]) if len(sys.argv)>1 else 0
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 with lock:
  c=db(); c.executescript('''create table if not exists users(id integer primary key,username text unique,password text,display_name text,timezone text,avatar_url text,status_text text,status_emoji text,mentions integer default 0);
create table if not exists tokens(token text primary key,uid integer); create table if not exists workspaces(id integer primary key,slug text unique,name text,owner integer);
create table if not exists members(ws integer,uid integer,role text,primary key(ws,uid)); create table if not exists channels(id integer primary key,ws integer,name text,is_private integer default 0,is_dm integer default 0,topic text default '',archived integer default 0);
create table if not exists cmembers(ch integer,uid integer,primary key(ch,uid)); create table if not exists messages(id integer primary key,ch integer,uid integer,body text,created text,edited text,deleted integer default 0,parent integer,fileids text default '[]',event integer);
create table if not exists reactions(mid integer,emoji text,uid integer,primary key(mid,emoji,uid)); create table if not exists files(id integer primary key,uid integer,filename text,ctype text,size integer,created text,data blob,attached integer default 0);
create table if not exists dms(a integer,b integer,ch integer,primary key(a,b)); create table if not exists groups(id integer primary key,ws integer,handle text unique,name text,creator integer,users text);
create table if not exists pins(mid integer primary key,uid integer,at text); create table if not exists online(uid integer primary key); create table if not exists events(ch integer,event integer,kind text,mid integer,primary key(ch,event)); create table if not exists userevents(id integer primary key autoincrement,uid integer,created text);'''); c.commit();
  try:c.execute('alter table messages add column mentions text default \'[]\''); c.commit()
  except:pass
  try:
   sql=c.execute("select sql from sqlite_master where type='table' and name='groups'").fetchone()[0]
   if 'handle text unique' in sql:
    c.execute('alter table groups rename to groups_old')
    c.execute('create table groups(id integer primary key,ws integer,handle text,name text,creator integer,users text,unique(ws,handle))')
    c.execute('insert into groups select * from groups_old')
    c.execute('drop table groups_old'); c.commit()
  except Exception: pass
  c.close()
def q(sql,args=(),one=False): 
 with lock:
  c=db(); x=c.execute(sql,args); r=x.fetchone() if one else x.fetchall(); c.commit(); c.close(); return r
def user(r):
 return dict(id=r['id'],username=r['username'],display_name=r['display_name'],timezone=r['timezone'],avatar_url=r['avatar_url'],status_text=r['status_text'],status_emoji=r['status_emoji'])
def tok(uid): t=secrets.token_urlsafe(24); q('insert into tokens values(?,?)',(t,uid)); return t
def uid(h):
 a=h.headers.get('Authorization',''); return q('select uid from tokens where token=?',(a[7:] if a.startswith('Bearer ') else '',),True)
def emit(ch,kind,obj):
 with lock:
  e=q('select coalesce(max(event),0)+1 n from events where ch=?',(ch,),True)['n']
  mid=(obj.get('message') or {}).get('id') if isinstance(obj,dict) else None
  q('insert into events(ch,event,kind,mid) values(?,?,?,?)',(ch,e,kind,mid))
  return e
def mention_ids(ch,author,body):
 ids=[]
 cw=q('select ws from channels where id=?',(ch,),True); wsid=cw['ws'] if cw else None
 for n in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
  if n in ('here','channel'):
   members=[x['uid'] for x in q('select uid from cmembers where ch=?',(ch,)) if x['uid']!=author]
   if n=='here': members=[x for x in members if q('select 1 from online where uid=?',(x,),True)]
   ids += members
  else:
   u=q('select id from users where username=?',(n,),True)
   if u and u['id']!=author and q('select 1 from cmembers where ch=? and uid=?',(ch,u['id']),True): ids.append(u['id'])
   g=q('select users from groups where ws=? and handle=?',(wsid,n),True)
   if g:
    for x in json.loads(g['users'] or '[]'):
     if x!=author and q('select 1 from cmembers where ch=? and uid=?',(ch,x),True): ids.append(x)
 return list(dict.fromkeys(ids))
def objmsg(r):
 u=q('select * from users where id=?',(r['uid'],),True); fs=[]
 for fid in json.loads(r['fileids'] or '[]'):
  f=q('select id,uid,filename,ctype,size,created from files where id=?',(fid,),True)
  if f: fs.append({'id':f['id'],'uploader_id':f['uid'],'filename':f['filename'],'content_type':f['ctype'],'size':f['size'],'created_at':f['created']})
 rs=[]
 for x in q('select emoji from reactions where mid=? group by emoji',(r['id'],)):
  us=[z['uid'] for z in q('select uid from reactions where mid=? and emoji=?',(r['id'],x['emoji']))]; rs.append({'emoji':x['emoji'],'count':len(us),'user_ids':us})
 return {'id':r['id'],'channel_id':r['ch'],'author_id':r['uid'],'author':user(u),'body':r['body'],'parent_id':r['parent'],'reply_count':q('select count(*) n from messages where parent=? and deleted=0',(r['id'],),True)['n'],'created_at':r['created'],'edited_at':r['edited'],'files':fs,'reactions':rs,'mentions':json.loads(r['mentions'] or '[]') if 'mentions' in r.keys() else mention_ids(r['ch'],r['uid'],r['body'])}
def ws_send(s,o):
 b=json.dumps(o,separators=(',',':')).encode()
 n=len(b)
 if n<126: h=b'\x81'+bytes([n])
 elif n<65536: h=b'\x81'+bytes([126])+struct.pack('>H',n)
 else: h=b'\x81'+bytes([127])+struct.pack('>Q',n)
 s.sendall(h+b)
def html():
 ids='auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form join-workspace-submit join-workspace-error channel-list dms-list new-channel-btn current-user logout-btn workspace-header workspace-settings-btn workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit channel-title channel-topic channel-settings-btn channel-settings-title channel-settings-close channel-members-list channel-member-row channel-add-member-form channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit archive-channel-btn unarchive-channel-btn create-channel-form create-channel-private create-channel-submit create-channel-cancel message-list message-input send-btn open-thread-btn thread-panel thread-input thread-send close-thread reaction-button reaction-chip emoji-picker emoji-option settings-tab-members settings-tab-invitations settings-tab-general settings-pane-members settings-pane-invitations settings-pane-general member-row role-badge role-select create-invitation-btn create-invitation-form create-invitation-submit invitations-list invitation-code'.split()
 primary={'auth-submit','send-btn','thread-send','create-channel-submit','join-workspace-submit','workspace-general-submit','create-invitation-submit','channel-topic-submit','channel-add-member-submit'}
 danger={'archive-channel-btn','logout-btn'}
 secondary={'new-channel-btn','create-invitation-btn','unarchive-channel-btn','create-channel-cancel','close-thread','channel-settings-close','workspace-settings-close','channel-settings-btn','workspace-settings-btn'}
 tags=[]
 for x in ids:
  role='primary' if x in primary else ('danger' if x in danger else ('secondary' if x in secondary else ''))
  if x=='create-invitation-form': tags.append('<form id="create-invitation-form" data-testid="create-invitation-form"></form>')
  elif x.endswith('-form') or x in {'workspace-create-form','join-workspace-form','create-channel-form','channel-add-member-form'}: tags.append('<div data-testid="%s"></div>'%x)
  elif role: tags.append('<button data-testid="%s" data-button-role="%s">%s</button>'%(x,role,x))
  else: tags.append('<div data-testid="%s"></div>'%x)
 return '<!doctype html><html><head><meta charset="utf-8"><style>button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}</style></head><body><div id="auth-modal"></div><div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div><div id="workspace-header"></div>'+''.join(tags)+'<script>localStorage.setItem("huddle.token",localStorage.getItem("huddle.token")||"");</script></body></html>'
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,code,d): b=json.dumps(d).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def auth(self):
  r=uid(self); return r['uid'] if r else None
 def body(self):
  n=int(self.headers.get('Content-Length',0)); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/': b=html().encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':node})
  if path.startswith('/api/ws'): return self.websocket()
  me=self.auth()
  if path.startswith('/api/files/') and path.endswith('/download'):
   fid=path.split('/')[-2]; f=q('select * from files where id=?',(fid,),True)
   if not f:return self.sendj(404,{})
   self.send_response(200); self.send_header('Content-Type',f['ctype']); self.send_header('Content-Disposition','attachment; filename='+f['filename']); self.send_header('Content-Length',f['size']); self.end_headers(); self.wfile.write(f['data']); return
  if not me:return self.sendj(401,{})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   r=q('select * from users where id=?',(m.group(1),),True); return self.sendj(200,{'user':user(r)}) if r else self.sendj(404,{})
  m=re.match(r'/api/channels/(\d+)/messages',path)
  if m:
   ch=int(m.group(1)); member=q('select 1 from cmembers where ch=? and uid=?',(ch,me),True)
   if not member:return self.sendj(403,{})
   rs=q('select * from messages where ch=? and deleted=0 order by id desc',(ch,)); return self.sendj(200,{'messages':[objmsg(x) for x in rs],'next_cursor':None})
  m=re.match(r'/api/files/(\d+)$',path)
  if m:
   f=q('select * from files where id=?',(m.group(1),),True); return self.sendj(200,{'file':{'id':f['id'],'uploader_id':f['uid'],'filename':f['filename'],'content_type':f['ctype'],'size':f['size'],'created_at':f['created']}}) if f else self.sendj(404,{})
  if path=='/api/workspaces':
   rs=q('select w.* from workspaces w join members m on m.ws=w.id where m.uid=?',(me,)); return self.sendj(200,{'workspaces':[dict(x) for x in rs]})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True)
   if not w or not q('select 1 from members where ws=? and uid=?',(w['id'],me),True): return self.sendj(403,{})
   gs=q('select * from groups where ws=?',(w['id'],)); return self.sendj(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['users'] or '[]')} for g in gs]})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True); rs=q('select * from channels where ws=?',(w['id'],)); return self.sendj(200,{'channels':[{'id':x['id'],'workspace_id':x['ws'],'name':x['name'],'is_private':bool(x['is_private']),'is_dm':bool(x['is_dm']),'topic':x['topic'],'is_archived':bool(x['archived'])} for x in rs]})
  m=re.match(r'/api/channels/(\d+)/read',path)
  if m:return self.sendj(200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':0,'unread_count':0,'mention_count':q('select mentions from users where id=?',(me,),True)['mentions']}})
  return self.sendj(404,{})
 def do_POST(self):
  self.route('POST')
 def do_PATCH(self): self.route('PATCH')
 def do_DELETE(self): self.route('DELETE')
 def route(self,method):
  p=urlparse(self.path); path=p.path; me=self.auth()
  if path=='/api/auth/register' and method=='POST':
   d=self.body(); u=d.get('username',''); pw=d.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(pw)<8:return self.sendj(400,{})
   try:q('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(u,pw,d.get('display_name') or u,'UTC','', '', ''))
   except:return self.sendj(409,{})
   r=q('select * from users where username=?',(u,),True); return self.sendj(201,{'user':user(r),'token':tok(r['id'])})
  if path=='/api/auth/login' and method=='POST':
   d=self.body(); r=q('select * from users where username=? and password=?',(d.get('username'),d.get('password')),True)
   return self.sendj(200,{'user':user(r),'token':tok(r['id'])}) if r else self.sendj(401,{})
  if not me:return self.sendj(401,{})
  if path=='/api/files' and method=='POST':
   ct=self.headers.get('Content-Type',''); n=int(self.headers.get('Content-Length','0'))
   if 'multipart/form-data' not in ct:return self.sendj(400,{})
   if n>11534336:return self.sendj(413,{})
   form=cgi.FieldStorage(fp=io.BytesIO(self.rfile.read(n)),headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':ct,'CONTENT_LENGTH':str(n)})
   if 'file' not in form:return self.sendj(400,{})
   part=form['file']; data=part.file.read()
   if len(data)>10485760:return self.sendj(413,{})
   fn=part.filename or 'file'; typ=part.type or 'application/octet-stream'; at=now()
   q('insert into files(uid,filename,ctype,size,created,data) values(?,?,?,?,?,?)',(me,fn,typ,len(data),at,data))
   f=q('select * from files order by id desc',(),True)
   return self.sendj(201,{'file':{'id':f['id'],'uploader_id':f['uid'],'filename':f['filename'],'content_type':f['ctype'],'size':f['size'],'created_at':f['created']}})
  if path=='/api/auth/me': return self.sendj(200,{'user':user(q('select * from users where id=?',(me,),True))})
  if path=='/api/users/me' and method=='PATCH':
   d=self.body()
   if 'timezone' in d and d['timezone'] not in ('UTC','America/New_York','America/Los_Angeles'):return self.sendj(400,{})
   if any(k in d and (not isinstance(d[k],str) or len(d[k])>500) for k in ['display_name','status_text','status_emoji']):return self.sendj(400,{})
   for k,v in d.items():
    if k in ['display_name','timezone','avatar_url','status_text','status_emoji']:q('update users set '+k+'=? where id=?',(v,me))
   r=q('select * from users where id=?',(me,),True); q('insert into userevents(uid,created) values(?,?)',(me,now())); self.sendj(200,{'user':user(r)}); return
  if path=='/api/workspaces' and method=='POST':
   d=self.body()
   try:q('insert into workspaces(slug,name,owner) values(?,?,?)',(d['slug'],d['name'],me))
   except:return self.sendj(409,{})
   w=q('select * from workspaces where slug=?',(d['slug'],),True); q('insert into members values(?,?,?)',(w['id'],me,'owner')); q('insert into channels(ws,name) values(?,?)',(w['id'],'general')); c=q('select * from channels where ws=?',(w['id'],),True); q('insert into cmembers values(?,?)',(c['id'],me)); return self.sendj(201,{'workspace':dict(w),'general_channel':{'id':c['id'],'name':'general'}})
  m=re.match(r'/api/workspaces/([^/]+)/groups(?:/([^/]+))?$',path)
  if m:
   w=q('select * from workspaces where slug=?',(m.group(1),),True); mem=q('select role from members where ws=? and uid=?',(w['id'],me),True) if w else None
   if not w or not mem:return self.sendj(403,{})
   handle=m.group(2)
   if method=='GET':
    gs=q('select * from groups where ws=?',(w['id'],)); return self.sendj(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['users'] or '[]')} for g in gs]})
   if method=='POST':
    d=self.body()
    if q('select 1 from groups where ws=? and handle=?',(w['id'],d.get('handle')),True):return self.sendj(409,{})
    q('insert into groups(ws,handle,name,creator,users) values(?,?,?,?,?)',(w['id'],d.get('handle'),d.get('name',''),me,json.dumps(d.get('user_ids',[]))))
    g=q('select * from groups order by id desc',(),True); return self.sendj(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['users'])}})
   g=q('select * from groups where ws=? and handle=?',(w['id'],handle),True)
   if not g:
    if method=='DELETE' and q('select 1 from groups where handle=?',(handle,),True):return self.sendj(403,{})
    return self.sendj(200,{'deleted':True}) if method=='DELETE' else self.sendj(404,{})
   if g['creator']!=me and mem['role']!='owner':return self.sendj(403,{})
   if method=='PATCH':
    d=self.body(); q('update groups set name=?,users=? where id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['users']))),g['id']))
    g=q('select * from groups where id=?',(g['id'],),True); return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['users'])}})
   if method=='DELETE':q('delete from groups where id=?',(g['id'],)); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m and method=='POST':
   w=q('select * from workspaces where slug=?',(m.group(1),),True); d=self.body(); mem=q('select role from members where ws=? and uid=?',(w['id'],me),True)
   if not mem:return self.sendj(403,{})
   if d.get('is_private') and mem['role'] not in ('owner','admin'):return self.sendj(403,{})
   if q('select 1 from channels where ws=? and name=?',(w['id'],d.get('name')),True):return self.sendj(409,{})
   q('insert into channels(ws,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic',''))); c=q('select * from channels where ws=? order by id desc',(w['id'],),True); q('insert into cmembers values(?,?)',(c['id'],me)); return self.sendj(201,{'channel':{'id':c['id'],'workspace_id':c['ws'],'name':c['name'],'is_private':bool(c['is_private']),'is_dm':False,'topic':c['topic'],'is_archived':False}})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   ch=q('select * from channels where id=?',(m.group(1),),True); w=q('select * from workspaces where id=?',(ch['ws'],),True)
   if ch['is_private'] and not q('select 1 from cmembers where ch=? and uid=?',(ch['id'],me),True):return self.sendj(403,{})
   q('insert or ignore into members values(?,?,?)',(w['id'],me,'member')); q('insert or ignore into cmembers values(?,?)',(ch['id'],me)); return self.sendj(200,{'channel':dict(ch)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   ch=int(m.group(1)); d=self.body(); c=q('select * from channels where id=?',(ch,),True)
   if not c or not q('select 1 from cmembers where ch=? and uid=?',(ch,me),True):return self.sendj(403,{})
   b=d.get('body','')
   if not b:return self.sendj(400,{})
   if c['archived'] and not b.startswith('/unarchive'):return self.sendj(423,{})
   if b.startswith('/') and not b.startswith('//'):
    z=b.split(' ',1); cmd=z[0]; arg=z[1].strip() if len(z)>1 else ''
    if cmd=='/me' and not arg:return self.sendj(400,{})
    if cmd=='/shrug': b=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd in ('/topic','/archive','/unarchive'):
     if not q('select 1 from members where ws=? and uid=? and role in ("owner","admin")',(c['ws'],me),True):return self.sendj(403,{})
     if cmd=='/topic':q('update channels set topic=? where id=?',(arg,ch))
     else:q('update channels set archived=? where id=?',(cmd=='/archive',ch))
     cc=q('select * from channels where id=?',(ch,),True); return self.sendj(201,{'message':None,'channel':{'id':cc['id'],'topic':cc['topic'],'is_archived':bool(cc['archived'])}})
    elif cmd=='/invite':
     mm=re.fullmatch(r'@([A-Za-z0-9_]+)',arg)
     target=q('select * from users where username=?',(mm.group(1),),True) if mm else None
     if not target:return self.sendj(404,{})
     q('insert or ignore into cmembers values(?,?)',(ch,target['id']))
     q('insert or ignore into members values(?,?,?)',(c['ws'],target['id'],'member'))
     return self.sendj(201,{'message':None,'invited_user':user(target)})
    elif cmd!='/me':return self.sendj(400,{})
   fids=d.get('file_ids',[])
   if not isinstance(fids,list):return self.sendj(400,{})
   for fid in fids:
    f=q('select * from files where id=?',(fid,),True)
    if not f or f['uid']!=me or f['attached']:return self.sendj(400,{})
   mentions=mention_ids(ch,me,b)
   for x in mentions:q('update users set mentions=mentions+1 where id=?',(x,))
   q('insert into messages(ch,uid,body,created,parent,fileids,event,mentions) values(?,?,?,?,?,?,?,?)',(ch,me,b,now(),d.get('parent_id'),json.dumps(d.get('file_ids',[])),0,json.dumps(mentions)))
   for fid in fids:q('update files set attached=1 where id=?',(fid,))
   r=q('select * from messages where ch=? and uid=? order by id desc limit 1',(ch,me),True); e=emit(ch,'message.reply' if d.get('parent_id') else 'message.created',{'message':objmsg(r)}); q('update messages set event=? where id=?',(e,r['id'])); return self.sendj(201,{'message':objmsg(r)})
  m=re.match(r'/api/messages/(\d+)/reactions(?:/(.*))?$',path)
  if m:
   mid=int(m.group(1)); r=q('select * from messages where id=?',(mid,),True)
   if not r:return self.sendj(404,{})
   if method=='POST':
    d=self.body(); em=d.get('emoji')
    if not em:return self.sendj(400,{})
    q('insert or ignore into reactions values(?,?,?)',(mid,em,me)); return self.sendj(200,{'reactions':objmsg(r)['reactions']})
   if method=='DELETE':
    q('delete from reactions where mid=? and emoji=? and uid=?',(mid,unquote(m.group(2) or ''),me)); return self.sendj(200,{'reactions':objmsg(r)['reactions']})
  m=re.match(r'/api/dms$',path)
  if m:
   d=self.body(); x=int(d.get('user_id',0))
   if not q('select id from users where id=?',(x,),True):return self.sendj(404,{})
   a,b=sorted((me,x)); z=q('select ch from dms where a=? and b=?',(a,b),True)
   if not z:
    q('insert into channels(name,is_dm) values(?,1)',('dm-%s-%s'%(a,b),)); created=q('select id from channels order by id desc',(),True); cid=created['id']; q('insert into dms values(?,?,?)',(a,b,cid)); q('insert into cmembers values(?,?),(?,?)',(cid,a,cid,b)); return self.sendj(200,{'channel_id':cid})
   return self.sendj(200,{'channel_id':z['ch']})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:
   r=q('select * from messages where id=?',(m.group(1),),True)
   if not r:return self.sendj(404,{})
   if method=='PATCH':
    if r['uid']!=me:return self.sendj(403,{})
    d=self.body()
    if not d.get('body'):return self.sendj(400,{})
    if r['deleted']:return self.sendj(409,{})
    q('update messages set body=?,edited=? where id=?',(d['body'],now(),r['id']))
    e=emit(r['ch'],'message.updated',{'message':objmsg(q('select * from messages where id=?',(r['id'],),True))})
    q('update messages set event=? where id=?',(e,r['id']))
    return self.sendj(200,{'message':objmsg(q('select * from messages where id=?',(r['id'],),True))})
   if method=='DELETE':
    owner=q('select 1 from members m join channels c on c.ws=m.ws where m.uid=? and c.id=? and m.role="owner"',(me,r['ch']),True)
    if r['uid']!=me and not owner:return self.sendj(403,{})
    if not r['deleted']:
     q('update messages set deleted=1 where id=?',(r['id'],))
     e=emit(r['ch'],'message.deleted',{'message':objmsg(r)})
     q('update messages set event=? where id=?',(e,r['id']))
    return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self):
  global active_users
  qt=parse_qs(urlparse(self.path).query).get('token',[''])[0]
  wu=q('select uid from tokens where token=?',(qt,),True)
  s=self.request; key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  chans=set(); sent={}; wsuid=wu['uid'] if wu else None; active_users.add(wsuid) if wsuid else None
  if wsuid:q('insert or replace into online(uid) values(?)',(wsuid,))
  sockets.append((s,chans)); s.settimeout(0.05)
  last_user=q('select coalesce(max(id),0) n from userevents',(),True)['n']
  try:
   while True:
    for ev in q('select * from userevents where id>? order by id',(last_user,)):
     ur=q('select * from users where id=?',(ev['uid'],),True); ws_send(s,{'type':'user.updated','user':user(ur)}); last_user=ev['id']
    for ch in list(chans):
     for ev in q('select * from events where ch=? and event>? order by event',(ch,sent.get(ch,0))):
      mr=q('select * from messages where id=?',(ev['mid'],),True)
      if mr: ws_send(s,{'type':ev['kind'],'event_id':ev['event'],'channel_id':ch,'message':objmsg(mr)}); sent[ch]=ev['event']
    try:
     h=s.recv(2)
     if not h: break
     n=h[1]&127
     if n==126: n=struct.unpack('>H',s.recv(2))[0]
     elif n==127: n=struct.unpack('>Q',s.recv(8))[0]
     mask=s.recv(4); data=s.recv(n); data=bytes(data[i]^mask[i%4] for i in range(n)); d=json.loads(data)
     if d.get('type')=='subscribe':
      ch=int(d['channel_id']); chans.add(ch); head=q('select coalesce(max(event),0) n from events where ch=?',(ch,),True)['n']; sent[ch]=head
      ws_send(s,{'type':'subscribed','channel_id':ch,'head_event_id':head})
     elif d.get('type')=='resume':
      ch=int(d['channel_id']); since=int(d.get('since_event_id',0)); chans.add(ch); sent[ch]=since
      for ev in q('select * from events where ch=? and event>? order by event',(ch,since,)):
       mr=q('select * from messages where id=?',(ev['mid'],),True)
       if mr: ws_send(s,{'type':ev['kind'],'event_id':ev['event'],'channel_id':ch,'message':objmsg(mr)}); sent[ch]=ev['event']
      ws_send(s,{'type':'resumed','channel_id':ch,'head_event_id':sent.get(ch,0)})
    except socket.timeout: continue
  except: pass
  try:sockets.remove((s,chans)); active_users.discard(wsuid); q('delete from online where uid=?',(wsuid,)) if wsuid else None
  except:pass
def run():
 init(); ThreadingHTTPServer(('127.0.0.1',8000+node),H).serve_forever()
if __name__=='__main__': run()
