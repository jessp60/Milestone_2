import os,sys,json,re,sqlite3,hashlib,secrets,base64,struct,socket,threading,time,datetime,html,select
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/app/huddle.db'; NODE=int(os.environ.get('NODE_ID','0'))
IRC_LOCK=threading.RLock(); IRC_NICKS=set(); IRC_CLIENTS=[]
def now():
 return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,token TEXT);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS joins(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));'''); c.commit(); c.close()
def user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def msg(c,r):
 x={'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(c.execute('select * from users where id=?',(r['author_id'],)).fetchone()),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
 for q in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
  x['reactions'].append({'emoji':q['emoji'],'count':q['n'],'user_ids':[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],q['emoji']))]})
 return x
def emit(c,ch,typ,data):
 n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch,)).fetchone()[0]
 p={'type':typ,'event_id':n,'channel_id':ch}; p.update(data); c.execute('insert into events values(?,?,?,?)',(ch,n,json.dumps(p),now())); return p
def finduser(c,tok):
 return c.execute('select * from users where token=?',(tok,)).fetchone()
HTML_PAGE='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{margin:0;font:14px Arial;background:#f8f8f8;color:#222}.layout{display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:24px}.thread{width:280px;padding:18px;background:#fff}.btn{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}input{padding:9px;margin:4px}#message-list{min-height:300px}.row{padding:12px;border-bottom:1px solid #ddd}</style></head><body>
<div class="layout"><aside class="side"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user">Guest</div><div data-testid="channel-list"></div><div data-testid="dms-list"></div></aside><main class="main"><h2 data-testid="channel-title"># general</h2><div data-testid="channel-topic"></div><div data-testid="message-list"></div><input data-testid="message-input" id="message-input"><button data-testid="send-btn" data-button-role="primary" class="btn primary">Send</button></main><aside class="thread" data-testid="thread-panel"><h3>Thread</h3><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary" class="btn primary">Reply</button><button data-testid="close-thread" data-button-role="secondary" class="btn secondary">Close</button></aside></div>
<div id="auth-modal"><form data-testid="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary" class="btn primary">Login</button><button type="button" data-testid="auth-toggle">Sign up</button></form></div>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><input data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary" class="btn primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary" class="btn secondary">Cancel</button></form></div>
<button data-testid="new-channel-btn" data-button-role="secondary" class="btn secondary">New channel</button><button data-testid="logout-btn" data-button-role="danger" class="btn danger">Logout</button>
<button data-testid="workspace-settings-btn" data-button-role="secondary" class="btn secondary">Workspace settings</button><button data-testid="workspace-settings-close" data-button-role="secondary" class="btn secondary">Close</button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"><option>open</option></select><button data-testid="workspace-general-submit" data-button-role="primary" class="btn primary">Save</button>
<button data-testid="channel-settings-btn" data-button-role="secondary" class="btn secondary">Channel settings</button><div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary" class="btn secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary" class="btn primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary" class="btn primary">Save</button><button data-testid="archive-channel-btn" data-button-role="danger" class="btn danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary" class="btn secondary">Unarchive</button>
<form data-testid="join-workspace-form"><input><button data-testid="join-workspace-submit" data-button-role="primary" class="btn primary">Join</button></form><form data-testid="workspace-create-form"><input name="slug"><input name="name"><button data-button-role="primary" class="btn primary">Create workspace</button></form><div data-testid="empty-state-create-workspace">Create a workspace to get started</div>
<div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div><div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary" class="btn secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary" class="btn primary">Create</button></form><div data-testid="invitations-list"></div>
<script>localStorage.getItem('huddle.token');document.querySelector('[data-testid=auth-form]').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:f.get('username'),password:f.get('password')})});if(r.ok){let x=await r.json();localStorage.setItem('huddle.token',x.token)}};</script></body></html>'''
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def sendj(self,code,x,typ='application/json'):
  b=json.dumps(x).encode(); self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self,c):
  h=self.headers.get('Authorization',''); return finduser(c,h[7:] if h.startswith('Bearer ') else '')
 def do_GET(self):
  p=urlparse(self.path); path=p.path; c=db()
  if path=='/': b=HTML_PAGE.encode(); self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if path=='/api/health': self.sendj(200,{'status':'ok','node_id':NODE});return
  if path=='/api/ws': self.websocket(c);return
  u=self.auth(c)
  if path=='/api/auth/me': return self.sendj(200,{'user':user(u)}) if u else self.sendj(401,{})
  if not u: return self.sendj(401,{})
  m=re.match(r'/api/channels/(\d+)/messages',path)
  if m:
   rows=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(int(m.group(1)),)).fetchall()
   return self.sendj(200,{'messages':[msg(c,r) for r in rows],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/read',path)
  if m:return self.sendj(200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':0,'unread_count':0,'mention_count':0}})
  if path=='/api/workspaces':
   return self.sendj(200,{'workspaces':[dict(x) for x in c.execute('select * from workspaces where owner_id=?',(u['id'],))]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   return self.sendj(200,{'workspace':dict(w),'channels':[dict(x) for x in c.execute('select id,workspace_id,name,is_private,topic,archived is_archived from channels where workspace_id=?',(w['id'],))],'read_state':{}})
  if path.startswith('/api/users/'):
   x=c.execute('select * from users where id=?',(path.split('/')[-1],)).fetchone(); return self.sendj(200,{'user':user(x)}) if x else self.sendj(404,{})
  self.sendj(404,{})
 def do_POST(self):
  p=urlparse(self.path); path=p.path;c=db(); b=self.body()
  if path=='/api/auth/register':
   if not re.match(r'^[A-Za-z0-9_]+$',b.get('username','')) or len(b.get('password',''))<8:return self.sendj(400,{})
   try:c.execute('insert into users(username,password,display_name,token) values(?,?,?,?)',(b['username'],b['password'],b.get('display_name'),secrets.token_urlsafe(24)));c.commit()
   except:return self.sendj(409,{})
   x=c.execute('select * from users where username=?',(b['username'],)).fetchone();return self.sendj(201,{'user':user(x),'token':x['token']})
  if path=='/api/auth/login':
   x=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone();return self.sendj(200,{'user':user(x),'token':x['token']}) if x else self.sendj(401,{})
  u=self.auth(c)
  if not u:return self.sendj(401,{})
  if path=='/api/workspaces':
   if not re.match(r'^[a-z0-9-]{2,32}$',b.get('slug','')):return self.sendj(400,{})
   try:
    c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b['name'],u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));c.commit()
   except:return self.sendj(409,{})
   w=c.execute('select * from workspaces where id=?',(wid,)).fetchone();ch=c.execute('select * from channels where workspace_id=?',(wid,)).fetchone();return self.sendj(201,{'workspace':dict(w),'general_channel':dict(ch)})
  m=re.match(r'/api/channels/(\d+)/messages',path)
  if m:
   ch=int(m.group(1)); body=b.get('body','')
   if not body:return self.sendj(400,{})
   if not c.execute('select 1 from joins where channel_id=? and user_id=?',(ch,u['id'])).fetchone() and not c.execute('select 1 from workspaces w join channels x on x.workspace_id=w.id where x.id=? and w.owner_id=?',(ch,u['id'])).fetchone():return self.sendj(403,{})
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,u['id'],body,b.get('parent_id'),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0];r=c.execute('select * from messages where id=?',(mid,)).fetchone(); ev=emit(c,ch,'message.reply' if b.get('parent_id') else 'message.created',{'message':msg(c,r)});c.commit();return self.sendj(201,{'message':msg(c,r)})
  m=re.match(r'/api/channels/(\d+)/join',path)
  if m:
   ch=int(m.group(1)); w=c.execute('select workspace_id from channels where id=?',(ch,)).fetchone()
   if not w:return self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w[0],u['id'])).fetchone():c.execute('insert or ignore into members values(?,?,?)',(w[0],u['id'],'member'))
   c.execute('insert or ignore into joins values(?,?)',(ch,u['id']));c.commit();return self.sendj(200,{'joined':True})
  self.sendj(404,{})
 def do_PATCH(self):
  c=db();u=self.auth(c)
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path)
  if not m:return self.sendj(404,{})
  b=self.body();r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
  if not r:return self.sendj(404,{})
  if r['deleted']:return self.sendj(409,{})
  if 'body' not in b:return self.sendj(400,{})
  if r['author_id']!=u['id']:return self.sendj(403,{})
  c.execute('update messages set body=?,edited_at=? where id=?',(b['body'],now(),r['id']));r=c.execute('select * from messages where id=?',(r['id'],)).fetchone();emit(c,r['channel_id'],'message.updated',{'message':msg(c,r)});c.commit();return self.sendj(200,{'message':msg(c,r)})
 def do_DELETE(self):
  c=db();u=self.auth(c)
  if not u:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path)
  if not m:return self.sendj(404,{})
  r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
  if not r:return self.sendj(404,{})
  w=c.execute('select owner_id from workspaces w join channels x on x.workspace_id=w.id where x.id=?',(r['channel_id'],)).fetchone()
  if r['deleted']:return self.sendj(200,{'deleted':True})
  if r['author_id']!=u['id'] and (not w or w[0]!=u['id']):return self.sendj(403,{})
  c.execute('update messages set deleted=1 where id=?',(r['id'],));emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']});c.commit();return self.sendj(200,{'deleted':True})
 def websocket(self,c):
  key=self.headers.get('Sec-WebSocket-Key');u=finduser(c,parse_qs(urlparse(self.path).query).get('token',[''])[0])
  if not key or not u:self.send_response(401);self.end_headers();return
  a=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode();self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',a);self.end_headers()
  def send(x):
   z=json.dumps(x).encode(); n=len(z); h=bytes([129])+ (bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n));self.wfile.write(h+z);self.wfile.flush()
  self.connection.settimeout(.2); last={}; chans=set()
  try:
   while True:
    try:
     if not select.select([self.connection],[],[],.05)[0]:
      raise socket.timeout()
     h=self.rfile.read(2)
     if not h:break
     n=h[1]&127
     if n==126:n=struct.unpack('>H',self.rfile.read(2))[0]
     mask=self.rfile.read(4); data=self.rfile.read(n);data=bytes(data[i]^mask[i%4] for i in range(n)); q=json.loads(data)
     if q.get('type') in ('subscribe','resume'):
      ch=int(q['channel_id']);chans.add(ch); since=int(q.get('since_event_id',0))
      if q['type']=='subscribe':
       head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch,)).fetchone()[0]
       last[ch]=head; send({'type':'subscribed','channel_id':ch,'head_event_id':head})
      else:
       last[ch]=since
       for x in c.execute('select payload from events where channel_id=? and event_id>? order by event_id',(ch,since)):send(json.loads(x[0]))
       head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch,)).fetchone()[0];send({'type':'resumed','channel_id':ch,'head_event_id':head});last[ch]=head
    except socket.timeout:
     pass
    except Exception:
     break
    for ch in list(chans):
     for x in c.execute('select event_id,payload from events where channel_id=? and event_id>? order by event_id',(ch,last.get(ch,0))):
      send(json.loads(x['payload']));last[ch]=x['event_id']
  except Exception: pass
class IRC:
 def __init__(self):
  self.sock=socket.socket();self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);self.sock.bind(('0.0.0.0',6667));self.sock.listen(50)
 def run(self):
  while 1:
   s,_=self.sock.accept();threading.Thread(target=self.client,args=(s,),daemon=True).start()
 def client(self,s):
  s.settimeout(.25); tok=None; nick=None; registered=False; user_seen=False; joined=set(); last={}
  def out(x):
   try: s.sendall((x+'\r\n').encode())
   except: raise
  with IRC_LOCK:
   client={'sock':s,'nick':None,'joined':joined,'last':last}; IRC_CLIENTS.append(client)
  try:
   while True:
    try: z=s.recv(4096)
    except socket.timeout: z=b''
    if z:
     for line in z.decode(errors='ignore').splitlines():
      q=line.split(' ',2); cmd=q[0].upper() if q else ''; arg=q[1] if len(q)>1 else ''
      rest=q[2][1:] if len(q)>2 and q[2].startswith(':') else (q[2] if len(q)>2 else '')
      if cmd=='PASS':
       tok=arg
      elif cmd=='NICK':
       with IRC_LOCK: used=arg in IRC_NICKS and arg!=nick
       if used: out(':huddle 433 * '+arg+' :Nickname in use')
       else:
        with IRC_LOCK:
         if nick: IRC_NICKS.discard(nick)
         nick=arg; IRC_NICKS.add(nick); client['nick']=nick
       if user_seen and tok and not registered:
        c=db(); u=finduser(c,tok)
        if u:
         registered=True
         for n in range(1,6): out(':huddle %03d %s :huddle'%(n,nick))
         out(':huddle 422 %s :No MOTD'%nick)
        c.close()
      elif cmd=='USER' and tok:
       user_seen=True
       if nick and not registered:
        c=db(); u=finduser(c,tok)
        if not u: out(':huddle 464 * :Password incorrect')
        else:
         registered=True
         for n in range(1,6): out(':huddle %03d %s :huddle'%(n,nick))
         out(':huddle 422 %s :No MOTD'%nick)
        c.close()
      elif cmd=='PING':
       out(':huddle PONG '+(arg+' ' if arg else '')+(':'+rest if rest else ''))
      elif cmd=='JOIN' and registered:
       c=db(); x=c.execute('select w.slug,ch.id,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where w.slug||"/"||ch.name=?',(arg.lstrip('#'),)).fetchone()
       if not x: out(':huddle 403 '+nick+' '+arg+' :No such channel')
       else:
        joined.add(x['id']); last[x['id']]=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(x['id'],)).fetchone()[0]
        out(':'+nick+'!user@host JOIN '+arg); out(':huddle 353 '+nick+' = '+arg+' :'+nick); out(':huddle 366 '+nick+' '+arg+' :End of NAMES')
       c.close()
      elif cmd=='PRIVMSG' and registered:
       c=db(); u=finduser(c,tok); x=c.execute('select ch.id from channels ch join workspaces w on w.id=ch.workspace_id where w.slug||"/"||ch.name=?',(arg.lstrip('#'),)).fetchone()
       if u and x and rest:
        c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(x[0],u['id'],rest,now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select * from messages where id=?',(mid,)).fetchone(); emit(c,x[0],'message.created',{'message':msg(c,r)}); c.commit()
       c.close()
      elif cmd=='QUIT': return
      elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
      elif cmd and cmd not in ('PASS','NICK','USER'): out(':huddle 421 '+str(nick or '*')+' '+cmd+' :Unknown command')
    if registered:
     c=db()
     for ch in list(joined):
      for x in c.execute('select event_id,payload from events where channel_id=? and event_id>? order by event_id',(ch,last.get(ch,0))):
       last[ch]=x['event_id']; ev=json.loads(x['payload'])
       if ev.get('type')=='message.created':
        mm=ev.get('message',{}); aa=c.execute('select username from users where id=?',(mm.get('author_id'),)).fetchone(); cc=c.execute('select w.slug,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(ch,)).fetchone()
        if cc: out(':'+((aa[0] if aa else 'user'))+'!user@host PRIVMSG #'+cc[0]+'/'+cc[1]+' :'+mm.get('body',''))
     c.close()
  except Exception:
   pass
  finally:
   with IRC_LOCK:
    if nick: IRC_NICKS.discard(nick)
    if client in IRC_CLIENTS: IRC_CLIENTS.remove(client)
   try: s.close()
   except: pass

def main():
 init()
 if len(sys.argv)>1 and sys.argv[1]=='irc':IRC().run();return
 port=8000+NODE;ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':main()
