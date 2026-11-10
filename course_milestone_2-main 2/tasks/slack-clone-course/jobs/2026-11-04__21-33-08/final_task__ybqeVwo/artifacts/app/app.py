import sys,os,json,re,sqlite3,time,uuid,hashlib,base64,struct,socket,threading,datetime
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/huddle.db'; PORT=int(sys.argv[2]) if len(sys.argv)>2 else 8000; NODE=(PORT-8000)//1
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=20,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,token TEXT);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INT);
CREATE TABLE IF NOT EXISTS members(ws INT,user INT,role TEXT,PRIMARY KEY(ws,user));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,ws INT,name TEXT,is_private INT DEFAULT 0,topic TEXT DEFAULT '',archived INT DEFAULT 0,UNIQUE(ws,name));
CREATE TABLE IF NOT EXISTS cmembers(ch INT,user INT,PRIMARY KEY(ch,user));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,ch INT,author INT,body TEXT,created TEXT,edited TEXT,deleted INT DEFAULT 0,parent INT);
CREATE TABLE IF NOT EXISTS reactions(mid INT,user INT,emoji TEXT,PRIMARY KEY(mid,user,emoji));
CREATE TABLE IF NOT EXISTS events(ch INT,event_id INTEGER,payload TEXT,PRIMARY KEY(ch,event_id));
CREATE TABLE IF NOT EXISTS cursors(ch INT,head INT,PRIMARY KEY(ch));
'''); c.commit(); c.close()
init()
def userobj(r): return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def msgobj(c,r):
 u=c.execute('select * from users where id=?',(r['author'],)).fetchone()
 rx=[]
 for x in c.execute('select emoji,count(*) n from reactions where mid=? group by emoji',(r['id'],)):
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[z['user'] for z in c.execute('select user from reactions where mid=? and emoji=?',(r['id'],x['emoji']))]})
 return {'id':r['id'],'channel_id':r['ch'],'author_id':r['author'],'author':userobj(u),'body':r['body'],'parent_id':r['parent'],'reply_count':c.execute('select count(*) from messages where parent=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':rx,'mentions':[]}
def emit(c,ch,kind,extra):
 h=c.execute('select head from cursors where ch=?',(ch,)).fetchone()
 n=(h['head'] if h else 0)+1
 c.execute('insert or replace into cursors(ch,head) values(?,?)',(ch,n))
 p={'type':kind,'event_id':n,'channel_id':ch}; p.update(extra)
 c.execute('insert into events(ch,event_id,payload) values(?,?,?)',(ch,n,json.dumps(p))); c.commit(); return p
def auth(c,h):
 a=h.get('Authorization',''); t=a[7:] if a.startswith('Bearer ') else ''
 return c.execute('select * from users where token=?',(t,)).fetchone()
def js(h,o,status=200): 
 b=json.dumps(o).encode(); h.send_response(status); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f3f;color:white;padding:18px}.main{flex:1;padding:25px}.thread{width:300px;border-left:1px solid #ddd;padding:15px}button{border:0;padding:9px 14px;color:#fff;border-radius:3px}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}input{padding:9px;margin:4px}.modal{display:none}</style></head><body><div class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><div data-testid=channel-list></div><div data-testid=dms-list></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button><button data-testid=logout-btn data-button-role=danger>Logout</button></aside><main class=main><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button></main><aside class=thread data-testid=thread-panel><h3>Thread</h3><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button><button data-testid=close-thread data-button-role=secondary>Close</button></aside></div>
<div id=auth-modal><form data-testid=auth-form><input name=username><input name=password type=password><button data-testid=auth-submit data-button-role=primary>Login</button></form><button data-testid=auth-toggle>Sign up</button></div>
<div id=create-channel-modal><form data-testid=create-channel-form><input name=name><input data-testid=create-channel-private type=checkbox><button data-testid=create-channel-submit data-button-role=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button></form></div>
<div id=channel-settings-modal><h3 data-testid=channel-settings-title>Channel settings</h3><button data-testid=channel-settings-btn data-button-role=secondary>Settings</button><button data-testid=channel-settings-close data-button-role=secondary>Close</button><div data-testid=channel-members-list></div><form data-testid=channel-add-member-form><input data-testid=channel-add-member-input><button data-testid=channel-add-member-submit data-button-role=primary>Add</button></form><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary>Save</button><button data-testid=archive-channel-btn data-button-role=danger>Archive</button><button data-testid=unarchive-channel-btn data-button-role=secondary>Unarchive</button></div>
<div id=workspace-settings-modal><input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-settings-btn data-button-role=secondary>Settings</button><button data-testid=workspace-settings-close data-button-role=secondary>Close</button><button data-testid=workspace-general-submit data-button-role=primary>Save</button><button data-testid=settings-tab-members>Members</button><button data-testid=settings-tab-invitations>Invitations</button><button data-testid=settings-tab-general>General</button><div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div><button data-testid=create-invitation-btn data-button-role=secondary>Create invitation</button><form id=create-invitation-form data-testid=create-invitation-form><button data-testid=create-invitation-submit data-button-role=primary>Create</button></form><div data-testid=invitations-list></div></div>
<form data-testid=workspace-create-form><input name=slug><input name=name><button data-testid=empty-state-create-workspace>Create workspace</button></form><form data-testid=join-workspace-form><input><button data-testid=join-workspace-submit data-button-role=primary>Join</button></form><div data-testid=join-workspace-error></div><script>localStorage.getItem('huddle.token');</script></body></html>'''
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  if self.path=='/' : b=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  c=db(); p=self.path.split('?')[0]; q=self.path.split('?',1)[1] if '?' in self.path else ''
  if p=='/api/health': js(self,{'status':'ok','node_id':NODE}); return
  u=auth(c,self.headers)
  if p=='/api/auth/me': js(self,{'user':userobj(u)} if u else {'error':'unauthorized'},200 if u else 401); return
  if p.startswith('/api/ws'):
   if not u:
    tok=self.path.split('token=',1)[1].split('&',1)[0] if 'token=' in self.path else ''
    u=c.execute('select * from users where token=?',(tok,)).fetchone()
   return self.websocket(c,u)
  if not u: js(self,{'error':'unauthorized'},401); return
  if p=='/api/workspaces':
   a=[] 
   for w in c.execute('select * from workspaces where id in(select ws from members where user=?)',(u['id'],)): a.append(dict(w))
   js(self,{'workspaces':a}); return
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w: js(self,{},404); return
   cs=[dict(x) for x in c.execute('select id,ws workspace_id,name,is_private,topic,archived is_archived from channels where ws=?',(w['id'],))]
   js(self,{'workspace':dict(w),'channels':cs,'read_state':{}}); return
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   rows=c.execute('select * from messages where ch=? and deleted=0 order by id desc',(int(m.group(1)),)).fetchall()
   js(self,{'messages':[msgobj(c,x) for x in rows],'next_cursor':None}); return
  m=re.match(r'/api/channels/(\d+)/read',p)
  if m: js(self,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':0,'unread_count':0,'mention_count':0}}); return
  if p.startswith('/api/channels/'):
   ch=c.execute('select * from channels where id=?',(int(p.split('/')[3]),)).fetchone()
   js(self,{'channel':dict(ch)} if ch else {},200 if ch else 404); return
  js(self,{},404)
 def do_POST(self):
  c=db(); p=self.path.split('?')[0]; u=auth(c,self.headers)
  if p=='/api/auth/register':
   try:
    x=self.body(); name=x['username']; 
    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(x['password'])<8: raise ValueError()
    t=uuid.uuid4().hex+uuid.uuid4().hex; c.execute('insert into users(username,password,display_name,token) values(?,?,?,?)',(name,x['password'],x.get('display_name',name),t)); c.commit(); r=c.execute('select * from users where id=last_insert_rowid()').fetchone(); js(self,{'user':userobj(r),'token':t},201)
   except sqlite3.IntegrityError: js(self,{},409)
   except: js(self,{},400)
   return
  if p=='/api/auth/login':
   x=self.body(); r=c.execute('select * from users where username=? and password=?',(x.get('username'),x.get('password'))).fetchone(); js(self,{'user':userobj(r),'token':r['token']} if r else {},200 if r else 401); return
  if not u: js(self,{},401); return
  if p=='/api/workspaces':
   x=self.body()
   try:
    c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(x['slug'],x['name'],u['id'])); w=c.execute('select * from workspaces where id=last_insert_rowid()').fetchone(); c.execute('insert into members values(?,?,?)',(w['id'],u['id'],'owner')); c.execute('insert into channels(ws,name) values(?,?)',(w['id'],'general')); ch=c.execute('select * from channels where id=last_insert_rowid()').fetchone(); c.execute('insert into cmembers values(?,?)',(ch['id'],u['id'])); c.commit(); js(self,{'workspace':dict(w),'general_channel':dict(ch)},201)
   except: js(self,{},409)
   return
  m=re.match(r'/api/channels/(\d+)/join',p)
  if m:
   ch=int(m.group(1)); r=c.execute('select * from channels where id=?',(ch,)).fetchone()
   if not r: js(self,{},404); return
   c.execute('insert or ignore into cmembers values(?,?)',(ch,u['id'])); c.execute('insert or ignore into members values(?,?,?)',(r['ws'],u['id'],'member')); c.commit(); js(self,{'channel':dict(r)}); return
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   x=self.body(); body=x.get('body','')
   if not body: js(self,{},400); return
   ch=int(m.group(1)); c.execute('insert into messages(ch,author,body,created,parent) values(?,?,?,?,?)',(ch,u['id'],body,now(),x.get('parent_id'))); r=c.execute('select * from messages where id=last_insert_rowid()').fetchone(); ev=emit(c,ch,'message.reply' if x.get('parent_id') else 'message.created',{'message':msgobj(c,r)}); js(self,{'message':msgobj(c,r)},201); return
  m=re.match(r'/api/messages/(\d+)/reactions',p)
  if m:
   x=self.body(); c.execute('insert or ignore into reactions values(?,?,?)',(int(m.group(1)),u['id'],x.get('emoji',''))); c.commit(); r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone(); js(self,{'reactions':msgobj(c,r)['reactions']}); return
  js(self,{},404)
 def do_PATCH(self):
  c=db(); u=auth(c,self.headers)
  if not u: js(self,{},401); return
  m=re.match(r'/api/messages/(\d+)',self.path)
  if m:
   r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
   if not r: js(self,{},404); return
   if r['deleted']: js(self,{},409); return
   if r['author']!=u['id']: js(self,{},403); return
   x=self.body()
   if 'body' not in x or not x['body']: js(self,{},400); return
   c.execute('update messages set body=?,edited=? where id=?',(x['body'],now(),r['id'])); c.commit(); r=c.execute('select * from messages where id=?',(r['id'],)).fetchone(); emit(c,r['ch'],'message.updated',{'message':msgobj(c,r)}); js(self,{'message':msgobj(c,r)}); return
  js(self,{},404)
 def do_DELETE(self):
  c=db(); u=auth(c,self.headers)
  if not u: js(self,{},401); return
  m=re.match(r'/api/messages/(\d+)',self.path)
  if not m: js(self,{},404); return
  r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
  if not r: js(self,{},404); return
  role=c.execute('select role from members where ws=(select ws from channels where id=?) and user=?',(r['ch'],u['id'])).fetchone()
  if r['deleted']: js(self,{'deleted':True}); return
  if r['author']!=u['id'] and (not role or role['role']!='owner'): js(self,{},403); return
  c.execute('update messages set deleted=1 where id=?',(r['id'],)); c.execute('delete from reactions where mid=?',(r['id'],)); c.commit(); emit(c,r['ch'],'message.deleted',{'message_id':r['id']}); js(self,{'deleted':True})
 def websocket(self,c,u):
  if not u: self.send_response(401); self.end_headers(); return
  key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers(); self.wfile.flush()
  def send(x):
   z=json.dumps(x).encode(); n=len(z); h=bytes([129,n]) if n<126 else bytes([129,126])+struct.pack('>H',n); self.wfile.write(h+z); self.wfile.flush()
  def readframe():
   h=self.rfile.read(2)
   if len(h)<2: return None
   n=h[1]&127
   if n==126: n=struct.unpack('>H',self.rfile.read(2))[0]
   elif n==127: n=struct.unpack('>Q',self.rfile.read(8))[0]
   mask=self.rfile.read(4); z=self.rfile.read(n)
   return bytes(v^mask[i%4] for i,v in enumerate(z))
  try:
   z=readframe()
   if not z: return
   x=json.loads(z); ch=int(x.get('channel_id')); q=db(); rr=q.execute('select head from cursors where ch=?',(ch,)).fetchone(); head=(rr['head'] if rr else 0); q.close()
   if x.get('type')=='subscribe':
    last=head; send({'type':'subscribed','channel_id':ch,'head_event_id':head})
   elif x.get('type')=='resume':
    last=int(x.get('since_event_id',0))
    q=db()
    for r in q.execute('select payload from events where ch=? and event_id>? order by event_id',(ch,last)):
     y=json.loads(r['payload']); send(y); last=y['event_id']
    q.close(); send({'type':'resumed','channel_id':ch,'head_event_id':last})
   else: return
   while True:
    time.sleep(.05); q=db()
    for r in q.execute('select payload from events where ch=? and event_id>? order by event_id',(ch,last)):
     y=json.loads(r['payload']); send(y); last=y['event_id']
    q.close()
  except Exception as e:
   open('/tmp/ws-errors.log','a').write(repr(e)+'\n')
class IRC(threading.Thread):
 def __init__(self): super().__init__(daemon=True); self.s=socket.socket(); self.s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); self.s.bind(('0.0.0.0',6667)); self.s.listen()
 def run(self):
  threading.Thread(target=self.dispatch,daemon=True).start()
  while 1:
   try: IRCClient(self.s.accept()[0]).start()
   except: pass
 def dispatch(self):
  while True:
   try:
    with IRCClient.lock: clients=list(IRCClient.clients)
    for x in clients:
     if not x.reg: continue
     q=db()
     for ch in list(x.joined):
      for r in q.execute('select event_id,payload from events where ch=? and event_id>? order by event_id',(ch,x.last_sent.get(ch,0))):
       z=json.loads(r['payload']); x.last_sent[ch]=z['event_id']
       if z.get('type') not in ('message.created','message.reply'): continue
       m=z.get('message') or {}; au=q.execute('select username from users where id=?',(m.get('author_id'),)).fetchone()
       nm=q.execute('select w.slug||\'/\'||c.name n from channels c join workspaces w on w.id=c.ws where c.id=?',(ch,)).fetchone()
       if au and nm: x.out(':%s!%s@host PRIVMSG #%s :%s'%(au['username'],au['username'],nm['n'],m.get('body','')))
     q.close()
   except Exception: pass
   time.sleep(.05)
class IRCClient(threading.Thread):
 clients=[]; lock=threading.Lock()
 def __init__(self,s): super().__init__(daemon=True); self.s=s; self.buf=''; self.tok=None; self.nick=None; self.user=None; self.reg=False; self.joined=[]; self.joined_names=[]; self.user_sent=False; self.last_sent={}
 def out(self,x): self.s.sendall((x+'\r\n').encode())
 def run(self):
  with self.lock:self.clients.append(self)
  try:
   while 1:
    d=self.s.recv(4096)
    if not d: break
    self.buf+=d.decode(errors='ignore')
    while '\n' in self.buf:
     l=self.buf.split('\n',1)[0].strip('\r'); self.buf=self.buf.split('\n',1)[1]; self.cmd(l)
  except: pass
  try:self.s.close()
  except:pass
  with self.lock:
   if self in self.clients:self.clients.remove(self)
 def cmd(self,l):
  a=l.split(); cmd=a[0].upper() if a else ''
  if cmd=='PASS':
   c=db(); self.user=c.execute('select * from users where token=?',(a[1] if len(a)>1 else '',)).fetchone()
   if not self.user:self.out(':huddle 464 * :Password incorrect')
  elif cmd=='NICK':
   n=a[1] if len(a)>1 else ''
   with self.lock: used=any(x.nick==n and x is not self for x in self.clients)
   if used:self.out(':huddle 433 * '+n+' :Nickname in use'); return
   self.nick=n
   if self.user_sent and self.user and not self.reg:
    self.reg=True
    for q in range(1,6): self.out(':huddle %03d %s :welcome'%(q,self.nick))
    self.out(':huddle 422 %s :No MOTD'%self.nick)
  elif cmd=='USER' and self.user and not self.reg:
   self.user_sent=True
   if self.nick:
    self.reg=True
   for n in range(1,6):self.out(':huddle %03d %s :welcome'%(n,self.nick))
   self.out(':huddle 422 %s :No MOTD'%self.nick)
  elif cmd=='PING':self.out(':huddle PONG huddle '+l.split(':',1)[-1])
  elif cmd=='QUIT': self.s.close()
  elif cmd=='JOIN':
   name=a[1] if len(a)>1 else ''; c=db(); r=c.execute('select c.*,w.slug from channels c join workspaces w on w.id=c.ws where w.slug||"/"||c.name=?',(name.lstrip('#'),)).fetchone()
   if not r:self.out(':huddle 403 '+name+' :No such channel'); return
   self.joined.append(r['id']); self.joined_names.append(name); hh=c.execute('select head from cursors where ch=?',(r['id'],)).fetchone(); self.last_sent[r['id']]=hh['head'] if hh else 0; self.out(':%s!%s@host JOIN %s'%(self.nick,self.user['username'],name)); self.out(':huddle 353 %s = %s :%s'%(self.nick,name,self.nick)); self.out(':huddle 366 %s %s :End of NAMES list'%(self.nick,name))
  elif cmd=='PRIVMSG' and len(a)>2:
   name=a[1]; body=l.split(':',1)[1] if ':' in l else ' '.join(a[2:]); c=db(); r=c.execute('select c.id,w.slug from channels c join workspaces w on w.id=c.ws where w.slug||"/"||c.name=?',(name.lstrip('#'),)).fetchone()
   if r:
    c.execute('insert into messages(ch,author,body,created) values(?,?,?,?)',(r['id'],self.user['id'],body,now())); z=c.execute('select * from messages where id=last_insert_rowid()').fetchone(); emit(c,r['id'],'message.created',{'message':msgobj(c,z)}); broadcast(name,body,self)
  elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): self.out(':huddle 421 %s %s :supported'%(self.nick,cmd)) if False else None
  else:
   if cmd not in ('USER',''): self.out(':huddle 421 %s %s :Unknown command'%(self.nick or '*',cmd))
def broadcast_web(name,body,u):
 with IRCClient.lock:
  for x in list(IRCClient.clients):
   if x.reg and name in x.joined_names:
    try: x.out(':%s!%s@host PRIVMSG %s :%s'%(u['username'],u['username'],name,body))
    except: pass
def broadcast(name,body,src):
 with IRCClient.lock:
  for x in list(IRCClient.clients):
   if x is not src and name not in x.joined_names: continue
   if x is not src and x.reg:
    try:x.out(':%s!%s@host PRIVMSG %s :%s'%(src.nick,src.user['username'],name,body))
    except:pass
def redis_stub():
 s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',6379)); s.listen()
 while 1:
  x,_=s.accept()
  try:
   x.recv(1024); x.sendall(b'+PONG\r\n')
  except:pass
if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='redis': redis_stub()
 else:
  if PORT==8000: IRC().start()
  ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
