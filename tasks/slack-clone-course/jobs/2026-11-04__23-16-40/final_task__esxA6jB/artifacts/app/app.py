import os,sys,json,sqlite3,hashlib,secrets,re,time,threading,socket,base64,struct,subprocess,signal
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
SCHEMA='''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT "",archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,data TEXT,UNIQUE(channel_id,event_id));
'''
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row; c.executescript(SCHEMA); return c
db().close()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+f'{int(time.time()*1000)%1000:03d}Z'
def user(c,uid):
 r=c.execute('select id,username,display_name from users where id=?',(uid,)).fetchone()
 return dict(r) if r else None
def auth(c,h):
 if not h or not h.startswith('Bearer '): return None
 r=c.execute('select user_id from tokens where token=?',(h[7:],)).fetchone()
 return r['user_id'] if r else None
def emit(c,ch,kind,mid=None,data=None):
 n=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(ch,)).fetchone()['n']
 c.execute('insert into events(channel_id,event_id,kind,message_id,data) values(?,?,?,?,?)',(ch,n,kind,mid,json.dumps(data or {}))); c.commit(); return n
def msgobj(c,r):
 d=dict(r); d['author']=user(c,d['author_id']); d['parent_id']=d.get('parent_id'); d['reply_count']=c.execute('select count(*) n from messages where parent_id=? and deleted=0',(d['id'],)).fetchone()['n']; d['files']=[]; d['reactions']=[]
 for x in c.execute('select emoji,user_id from reactions where message_id=?',(d['id'],)):
  q=next((z for z in d['reactions'] if z['emoji']==x['emoji']),None)
  if not q:q={'emoji':x['emoji'],'count':0,'user_ids':[]};d['reactions'].append(q)
  q['count']+=1;q['user_ids'].append(x['user_id'])
 return d
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
body{margin:0;font:14px Arial;display:flex;height:100vh}.side{width:240px;background:#3f0f40;color:white;padding:18px}.main{flex:1;padding:25px}.thread{width:300px;border-left:1px solid #ddd;padding:15px}button{border:0;padding:9px 14px;color:#fff;border-radius:3px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}input{padding:9px;margin:4px} .msg{padding:12px;border-bottom:1px solid #eee}</style></head><body>
<form data-testid=workspace-create-form><input name=workspace><button data-testid=empty-state-create-workspace data-button-role=primary>Create workspace</button></form><form data-testid=join-workspace-form><input name=join><button data-testid=join-workspace-submit data-button-role=primary>Join workspace</button></form><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><div data-testid=channel-list></div><div data-testid=dms-list></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button><button data-testid=logout-btn data-button-role=danger>Logout</button></aside>
<main class=main><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><form id=send><input data-testid=message-input name=body><button data-testid=send-btn data-button-role=primary>Send</button></form></main><aside class=thread><h3>Thread</h3><div data-testid=thread-panel></div><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button><button data-testid=close-thread data-button-role=secondary>Close</button></aside>
<div id=auth-modal><form data-testid=auth-form><input name=username><input name=password type=password><button data-testid=auth-submit data-button-role=primary>Login</button><button type=button data-testid=auth-toggle>Sign up</button></form></div>
<div id=create-channel-modal><form data-testid=create-channel-form><input name=name><input data-testid=create-channel-private type=checkbox><button data-testid=create-channel-submit data-button-role=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button></form></div>
<div id=channel-settings-modal><button data-testid=channel-settings-btn data-button-role=secondary>Settings</button><h3 data-testid=channel-settings-title></h3><button data-testid=channel-settings-close data-button-role=secondary>Close</button><div data-testid=channel-members-list></div><form data-testid=channel-add-member-form><input data-testid=channel-add-member-input><button data-testid=channel-add-member-submit data-button-role=primary>Add</button></form><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary>Topic</button><button data-testid=archive-channel-btn data-button-role=danger>Archive</button><button data-testid=unarchive-channel-btn data-button-role=secondary>Unarchive</button></div>
<div id=workspace-settings-modal><button data-testid=workspace-settings-btn data-button-role=secondary>Workspace</button><button data-testid=workspace-settings-close data-button-role=secondary>Close</button><input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary>Save</button><div data-testid=settings-tab-members></div><div data-testid=settings-tab-invitations></div><div data-testid=settings-tab-general></div><div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div><button data-testid=create-invitation-btn data-button-role=secondary>Create invitation</button><form id=create-invitation-form data-testid=create-invitation-form><button data-testid=create-invitation-submit data-button-role=primary>Invite</button></form><div data-testid=invitations-list></div></div>
<script>localStorage.getItem('huddle.token');</script></body></html>'''
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def sendj(self,code,x):
  b=json.dumps(x).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
  if self.path=='/': b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  c=db(); p=self.path.split('?')[0]; uid=auth(c,self.headers.get('Authorization'))
  if p=='/api/health': self.sendj(200,{'status':'ok','node_id':int(os.getenv('NODE','0'))});return
  if p=='/api/auth/me':
   if not uid:self.sendj(401,{});return
   self.sendj(200,{'user':user(c,uid)});return
  if not uid:self.sendj(401,{});return
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   rows=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc',(m.group(1),)).fetchall();self.sendj(200,{'messages':[msgobj(c,x) for x in rows],'next_cursor':None});return
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:self.sendj(404,{});return
   self.sendj(200,{'workspace':dict(w),'channels':[dict(x) for x in c.execute('select id,workspace_id,name,is_private,topic,archived is_archived from channels where workspace_id=?',(w['id'],))]});return
  self.sendj(404,{})
 def do_POST(self):
  c=db();p=self.path.split('?')[0]; n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n); 
  try:x=json.loads(raw or b'{}')
  except:x={}
  uid=auth(c,self.headers.get('Authorization'))
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',x.get('username','')) or len(x.get('password',''))<8:self.sendj(400,{});return
   try:c.execute('insert into users(username,password,display_name) values(?,?,?)',(x['username'],x['password'],x.get('display_name') or x['username']));c.commit()
   except:self.sendj(409,{});return
   uid=c.execute('select last_insert_rowid() n').fetchone()['n']; t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();self.sendj(201,{'user':user(c,uid),'token':t});return
  if p=='/api/auth/login':
   r=c.execute('select * from users where username=? and password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:self.sendj(401,{});return
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();self.sendj(200,{'user':dict(r),'token':t});return
  if not uid:self.sendj(401,{});return
  if p=='/api/workspaces':
   try:c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(x['slug'],x['name'],uid));wid=c.execute('select last_insert_rowid() n').fetchone()['n'];c.execute('insert into members values(?,?,?)',(wid,uid,'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));c.commit();gid=c.execute('select last_insert_rowid() n').fetchone()['n'];self.sendj(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('select * from channels where id=?',(gid,)).fetchone())})
   except:self.sendj(409,{})
   return
  m=re.match(r'/api/channels/(\d+)/join',p)
  if m:c.execute('insert or ignore into members(workspace_id,user_id,role) select workspace_id,?,? from channels where id=?',(uid,'member',m.group(1)));c.commit();self.sendj(200,{});return
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   if not x.get('body'):self.sendj(400,{});return
   ch=int(m.group(1)); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,uid,x['body'],x.get('parent_id'),now()));mid=c.execute('select last_insert_rowid() n').fetchone()['n']; ev=emit(c,ch,'message.reply' if x.get('parent_id') else 'message.created',mid);self.sendj(201,{'message':msgobj(c,c.execute('select * from messages where id=?',(mid,)).fetchone())});return
  m=re.match(r'/api/messages/(\d+)/reactions',p)
  if m:c.execute('insert or ignore into reactions values(?,?,?)',(m.group(1),uid,x.get('emoji','👍')));c.commit();self.sendj(200,{'reactions':[]});return
  self.sendj(404,{})
 def do_PATCH(self):
  c=db();uid=auth(c,self.headers.get('Authorization')); 
  if not uid:self.sendj(401,{});return
  n=int(self.headers.get('Content-Length','0'));x=json.loads(self.rfile.read(n) or b'{}');m=re.match(r'/api/messages/(\d+)',self.path)
  if not m:self.sendj(404,{});return
  r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
  if not r:self.sendj(404,{});return
  if r['deleted']:self.sendj(409,{});return
  if 'body' not in x:self.sendj(400,{});return
  if r['author_id']!=uid:self.sendj(403,{});return
  t=now();c.execute('update messages set body=?,edited_at=? where id=?',(x['body'],t,r['id']));emit(c,r['channel_id'],'message.updated',r['id']);self.sendj(200,{'message':msgobj(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone())})
 def do_DELETE(self):
  c=db();uid=auth(c,self.headers.get('Authorization')); 
  if not uid:self.sendj(401,{});return
  m=re.match(r'/api/messages/(\d+)',self.path)
  if not m:self.sendj(404,{});return
  r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
  if not r:self.sendj(404,{});return
  w=c.execute('select owner_id from workspaces join channels on channels.workspace_id=workspaces.id where channels.id=?',(r['channel_id'],)).fetchone()
  if r['deleted']:self.sendj(200,{'deleted':True});return
  if uid not in (r['author_id'],w['owner_id']):self.sendj(403,{});return
  c.execute('update messages set deleted=1 where id=?',(r['id'],));c.execute('delete from reactions where message_id=?',(r['id'],));emit(c,r['channel_id'],'message.deleted',r['id']);self.sendj(200,{'deleted':True})
 def log_message(self,*a):pass
 def log_request(self,*a):pass
def ws_loop(sock,uid,key):
 c=db()
 accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
 sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
 sock.settimeout(.15); buf=b''; chans=set(); seen={}
 while True:
  try:
   z=sock.recv(4096)
   if z: buf+=z
   elif z==b'': return
  except socket.timeout: pass
  while len(buf)>=2:
   b1,b2=buf[0],buf[1]; ln=b2&127; pos=2
   if ln==126:
    if len(buf)<4:break
    ln=struct.unpack('>H',buf[2:4])[0];pos=4
   elif ln==127:
    if len(buf)<10:break
    ln=struct.unpack('>Q',buf[2:10])[0];pos=10
   if b2&128:
    if len(buf)<pos+4+ln:break
    mask=buf[pos:pos+4];pos+=4
   else: mask=None
   if len(buf)<pos+ln:break
   payload=buf[pos:pos+ln];buf=buf[pos+ln:]
   if mask: payload=bytes(v^mask[i%4] for i,v in enumerate(payload))
   if (b1&15)==8:return
   if (b1&15)==1:
    try:o=json.loads(payload.decode())
    except:continue
    if o.get('type') in ('subscribe','resume'):
     ch=int(o['channel_id']);chans.add(ch)
     if o['type']=='resume':
      since=int(o.get('since_event_id',0))
      for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,since)):
       q={'type':e['kind'],'event_id':e['event_id'],'channel_id':ch}
       if e['message_id']:
        rr=c.execute('select * from messages where id=?',(e['message_id'],)).fetchone()
        if rr:q['message']=msgobj(c,rr)
       sendws(sock,q)
     head=c.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(ch,)).fetchone()['n']
     seen[ch]=head
     sendws(sock,{'type':'subscribed' if o['type']=='subscribe' else 'resumed','channel_id':ch,'head_event_id':head})
  for ch in list(chans):
   last=seen.get(ch,0)
   for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,last)):
    q={'type':e['kind'],'event_id':e['event_id'],'channel_id':ch}
    if e['message_id']:
     rr=c.execute('select * from messages where id=?',(e['message_id'],)).fetchone()
     if rr:q['message']=msgobj(c,rr)
    sendws(sock,q);seen[ch]=e['event_id']
def sendws(s,o):
 b=json.dumps(o).encode(); n=len(b)
 if n<126:h=bytes([129,n])
 elif n<65536:h=bytes([129,126])+struct.pack('>H',n)
 else:h=bytes([129,127])+struct.pack('>Q',n)
 s.sendall(h+b)
class WS(H):
 def do_GET(self):
  if self.path.startswith('/api/ws'):
   c=db();tok=self.path.split('token=')[-1];r=c.execute('select user_id from tokens where token=?',(tok,)).fetchone()
   if not r:self.send_response(401);self.end_headers();return
   ws_loop(self.connection,r['user_id'],self.headers.get('Sec-WebSocket-Key',''));return
  super().do_GET()
def node(port):
 global H;H=WS;ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
def irc():
 clients=[]; lock=threading.Lock()
 def send(s,x):
  try:s.sendall((x+'\r\n').encode())
  except:pass
 def lookup(c,name):
  return c.execute("select ch.id,w.slug,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ('#'||w.slug||'/'||ch.name)=?",(name,)).fetchone()
 def broadcaster():
  while True:
   time.sleep(.08); c=db()
   with lock: snap=list(clients)
   for cl in snap:
    for ch in list(cl['joined']):
     last=cl['seen'].get(ch,0)
     for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,last)):
      cl['seen'][ch]=e['event_id']
      if not e['message_id']:continue
      r=c.execute('select m.body,u.username from messages m join users u on u.id=m.author_id where m.id=?',(e['message_id'],)).fetchone()
      if not r:continue
      q=c.execute('select w.slug,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(ch,)).fetchone()
      if q:send(cl['sock'],':%s!%s@host PRIVMSG #%s/%s :%s'%(r['username'],r['username'],q['slug'],q['name'],r['body']))
 threading.Thread(target=broadcaster,daemon=True).start()
 def cli(s):
  tok=nick=None; registered=False; user_seen=False; joined=set(); seen={}
  with lock: clients.append({'sock':s,'nick':None,'joined':joined,'seen':seen})
  cl=clients[-1]
  try:
   while True:
    line=s.recv(4096).decode()
    if not line:break
    for q in line.replace('\r','').split('\n'):
     if not q:continue
     a=q.split();cmd=a[0].upper()
     if cmd=='PASS':tok=q[5:].strip()
     elif cmd=='NICK' and len(a)>1:
      nn=a[1]
      with lock: collision=any(x is not cl and x.get('nick')==nn for x in clients)
      if collision:send(s,':irc 433 * %s :Nickname in use'%nn)
      else:
       nick=nn;cl['nick']=nn
       if user_seen and tok and not registered:
        c=db();r=c.execute('select user_id from tokens where token=?',(tok,)).fetchone()
        if r:
         for n in range(1,6):send(s,':irc %03d %s :welcome'%(n,nick))
         send(s,':irc 422 %s :No MOTD'%nick);registered=True
     elif cmd=='USER' and tok:
      user_seen=True
      c=db();r=c.execute('select user_id from tokens where token=?',(tok,)).fetchone()
      if not r:send(s,':irc 464 * :Password incorrect');continue
      for n in range(1,6):send(s,':irc %03d %s :welcome'%(n,nick))
      send(s,':irc 422 %s :No MOTD'%nick);registered=True
     elif cmd=='PING':send(s,':irc PONG :'+q.split(':',1)[-1])
     elif cmd=='JOIN' and len(a)>1:
      name=a[1];c=db();r=lookup(c,name)
      if not r:send(s,':irc 403 %s %s :No such channel'%(nick,name));continue
      joined.add(r['id']);seen[r['id']]=c.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(r['id'],)).fetchone()['n']
      send(s,':%s!%s@host JOIN %s'%(nick,nick,name));send(s,':irc 353 %s = %s :%s'%(nick,name,nick));send(s,':irc 366 %s %s :End of NAMES list'%(nick,name))
     elif cmd=='PRIVMSG' and len(a)>2:
      name=a[1];body=q.split(' :',1)[-1];c=db();r=lookup(c,name);u=c.execute('select user_id from tokens where token=?',(tok,)).fetchone()
      if r and u:
       c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(r['id'],u['user_id'],body,now()));mid=c.execute('select last_insert_rowid() n').fetchone()['n'];emit(c,r['id'],'message.created',mid)
     elif cmd=='QUIT':return
     elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):pass
     elif cmd not in ('PASS','USER','NICK'):send(s,':irc 421 %s %s :Unknown command'%(nick or '*',cmd))
  finally:
   with lock:
    if cl in clients:clients.remove(cl)
   try:s.close()
   except OSError:pass
 def accept():
  srv=socket.socket();srv.setsockopt(1,2,1);srv.bind(('0.0.0.0',6667));srv.listen()
  while True:
   x,_=srv.accept();threading.Thread(target=cli,args=(x,),daemon=True).start()
 threading.Thread(target=accept,daemon=True).start()
 while True:time.sleep(60)
if __name__=='__main__':
 if sys.argv[1]=='node':node(int(sys.argv[2]))
 elif sys.argv[1]=='irc':irc()
 elif sys.argv[1]=='sup':
  threading.Thread(target=irc,daemon=True).start(); ps={}
  while 1:
   for i,p in enumerate((8000,8001,8002)):
    if i not in ps or ps[i].poll() is not None: ps[i]=subprocess.Popen([sys.executable,__file__,'node',str(p)],env={**os.environ,'NODE':str(i)})
   time.sleep(1)
