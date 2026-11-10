import os,sys,json,sqlite3,hashlib,secrets,re,threading,time,socket,struct
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/state.db'
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
 CREATE TABLE IF NOT EXISTS members(ws INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(ws,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,ws INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(ch INTEGER,user_id INTEGER,PRIMARY KEY(ch,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,ch INTEGER,author INTEGER,body TEXT,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(ch INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(ch,event_id));'''); c.commit(); c.close()
def user(c,uid):
 r=c.execute('select * from users where id=?',(uid,)).fetchone()
 return dict(r) if r else None
def uobj(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def msgobj(c,r):
 d=dict(r); au=user(c,r['author'])
 reacts=[]
 d.update(id=r['id'],channel_id=r['ch'],author_id=r['author'],author=uobj(au),parent_id=None,reply_count=0,created_at=r['created'],edited_at=r['edited'],files=[],reactions=reacts,mentions=[])
 return d
def emit(c,ch,typ,payload):
 n=c.execute('select coalesce(max(event_id),0)+1 from events where ch=?',(ch,)).fetchone()[0]
 c.execute('insert into events values(?,?,?,?)',(ch,n,typ,json.dumps(payload))); c.commit(); return n
class Hub:
 def __init__(self): self.lock=threading.Lock(); self.clients=[]
 def add(self,x): 
  with self.lock:self.clients.append(x)
 def remove(self,x):
  with self.lock:
   if x in self.clients:self.clients.remove(x)
 def broadcast(self,e):
  with self.lock:
   for x in list(self.clients):
    try:
     if x['ch']==e['channel_id']: ws_send(x['sock'],e)
    except: self.clients.remove(x)
  if e.get('type') in ('message.created','message.updated'):
   c=db(); r=c.execute('select w.slug,ch.name,m.author,u.username from messages m join channels ch on ch.id=m.ch join workspaces w on w.id=ch.ws join users u on u.id=m.author where m.id=?',(e.get('id'),)).fetchone() if e.get('id') else None
   c.close()
   if r:
    target='#%s/%s'%(r['slug'],r['name']); body=e.get('body','')
    with ircs_lock:
     for z in list(ircs):
      if z.get('channel_id')==e.get('channel_id'): irc_send(z['sock'],':%s!%s@host PRIVMSG %s :%s'%(z.get('nick','user'),r['username'],target,body))
hub=Hub()
ircs=[]
ircs_lock=threading.Lock()
def irc_send(s,line):
 try: s.sendall((line+'\\r\
').encode())
 except: pass
def ws_send(s,o):
 b=json.dumps(o).encode(); n=len(b); h=bytes([129])
 if n<126:h+=bytes([n])
 elif n<65536:h+=b'\x7e'+struct.pack('>H',n)
 else:h+=b'\x7f'+struct.pack('>Q',n)
 s.sendall(h+b)
def ws_recv(s):
 h=s.recv(2)
 if not h:return None
 n=h[1]&127; mask=s.recv(4)
 if n==126:n=struct.unpack('>H',s.recv(2))[0]
 elif n==127:n=struct.unpack('>Q',s.recv(8))[0]
 b=s.recv(n); return json.loads(bytes(v^mask[i%4] for i,v in enumerate(b)))
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,status,o,headers=None):
  b=json.dumps(o).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  if headers:
   for k,v in headers.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  t=self.headers.get('Authorization','').replace('Bearer ',''); c=db(); r=c.execute('select user_id from tokens where token=?',(t,)).fetchone(); c.close(); return r['user_id'] if r else None
 def do_GET(self):
  p=self.path.split('?')[0]
  if p=='/': return self.html()
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(os.getenv('NODE','0'))})
  if p.startswith('/api/ws'): return self.websocket()
  uid=self.auth()
  if p=='/api/auth/me':
   if not uid:return self.sendj(401,{'error':'unauthorized'})
   c=db();r=user(c,uid);c.close();return self.sendj(200,{'user':uobj(r)})
  if not uid:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=int(m.group(1)); rows=c.execute('select * from messages where ch=? and deleted=0 order by id desc',(ch,)).fetchall(); out=[msgobj(c,r) for r in rows];c.close();return self.sendj(200,{'messages':out,'next_cursor':None})
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   cs=c.execute('select * from channels where ws=? and archived=0',(w['id'],)).fetchall();c.close();return self.sendj(200,{'workspace':dict(w),'channels':[dict(x) for x in cs],'read_state':{}})
  if p=='/api/workspaces':
   rs=c.execute('select w.* from workspaces w join members m on m.ws=w.id where m.user_id=?',(uid,)).fetchall();c.close();return self.sendj(200,{'workspaces':[dict(x) for x in rs]})
  c.close();return self.sendj(404,{})
 def do_POST(self):
  p=self.path.split('?')[0]; b=self.read()
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.sendj(400,{'error':'invalid'})
   c=db()
   try:c.execute('insert into users(username,password,display_name) values(?,?,?)',(b['username'],b['password'],b.get('display_name',b['username'])));c.commit()
   except:return c.close() or self.sendj(409,{'error':'duplicate'})
   uid=c.execute('select last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,uid));c.commit();r=user(c,uid);c.close();return self.sendj(201,{'user':uobj(r),'token':t})
  if p=='/api/auth/login':
   c=db();r=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
   if not r:c.close();return self.sendj(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();c.close();return self.sendj(200,{'user':uobj(r),'token':t})
  uid=self.auth()
  if not uid:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  if p=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return self.sendj(400,{'error':'slug'})
   try:c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b['name'],uid)); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,uid,'owner'));c.execute('insert into channels(ws,name) values(?,?)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(cid,uid));c.commit();c.close();return self.sendj(201,{'workspace':{'id':wid,'slug':b['slug'],'name':b['name'],'owner_id':uid,'join_mode':'open'},'general_channel':{'id':cid,'workspace_id':wid,'name':'general','is_private':0,'topic':'','is_archived':0}})
   except:return c.close() or self.sendj(409,{'error':'duplicate'})
  m=re.match(r'/api/channels/(\d+)/join',p)
  if m:
   ch=int(m.group(1));r=c.execute('select * from channels where id=?',(ch,)).fetchone()
   if not r:return self.sendj(404,{})
   c.execute('insert or ignore into cmembers values(?,?)',(ch,uid));c.execute('insert or ignore into members values(?,?,?)',(r['ws'],uid,'member'));c.commit();c.close();return self.sendj(200,{'channel':dict(r)})
  m=re.match(r'/api/channels/(\d+)/messages',p)
  if m:
   ch=int(m.group(1));r=c.execute('select * from channels where id=?',(ch,)).fetchone()
   if not r:return self.sendj(404,{})
   c.execute('insert into messages(ch,author,body,created) values(?,?,?,?)',(ch,uid,b.get('body',''),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0];r=c.execute('select * from messages where id=?',(mid,)).fetchone();o=msgobj(c,r);ev=emit(c,ch,'message.created',o);o['event_id']=ev;c.close();hub.broadcast({'type':'message.created','event_id':ev,'channel_id':ch,**o});return self.sendj(201,{'message':o})
  m=re.match(r'/api/messages/(\d+)/reactions',p)
  if m:return self.sendj(200,{'reactions':[{'emoji':b.get('emoji',''),'count':1,'user_ids':[uid]}]})
  c.close();return self.sendj(404,{})
 def do_PATCH(self):
  uid=self.auth()
  if not uid:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path.split('?')[0])
  if not m:return self.sendj(404,{})
  b=self.read();c=db();r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
  if not r:return c.close() or self.sendj(404,{})
  if r['deleted']:return c.close() or self.sendj(409,{})
  if 'body' not in b:return c.close() or self.sendj(400,{})
  if r['author']!=uid:return c.close() or self.sendj(403,{})
  c.execute('update messages set body=?,edited=? where id=?',(b['body'],now(),r['id']));c.commit();r=c.execute('select * from messages where id=?',(r['id'],)).fetchone();o=msgobj(c,r);ev=emit(c,r['ch'],'message.updated',o);o['event_id']=ev;c.close();hub.broadcast({'type':'message.updated','event_id':ev,'channel_id':r['ch'],**o});return self.sendj(200,{'message':o})
 def do_DELETE(self):
  uid=self.auth()
  if not uid:return self.sendj(401,{})
  m=re.match(r'/api/messages/(\d+)',self.path.split('?')[0]);c=db();r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
  if not r:return c.close() or self.sendj(404,{})
  w=c.execute('select owner_id from workspaces w join channels x on x.ws=w.id where x.id=?',(r['ch'],)).fetchone()
  if r['deleted']:c.close();return self.sendj(200,{'deleted':True})
  if uid!=r['author'] and uid!=w['owner_id']:c.close();return self.sendj(403,{})
  c.execute('update messages set deleted=1 where id=?',(r['id'],));ev=emit(c,r['ch'],'message.deleted',{'message_id':r['id']});c.close();hub.broadcast({'type':'message.deleted','event_id':ev,'channel_id':r['ch'],'message_id':r['id']});return self.sendj(200,{'deleted':True})
 def do_OPTIONS(self):self.send_response(204);self.send_header('Content-Length','0');self.end_headers()
 def websocket(self):
  t=self.path.split('token=')[-1]; c=db(); r=c.execute('select user_id from tokens where token=?',(t,)).fetchone(); c.close()
  if not r:return self.send_error(401)
  s=self.connection; key=self.headers.get('Sec-WebSocket-Key'); import base64
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  s.settimeout(.2); subs={}
  while True:
   try:
    x=ws_recv(s)
    if x:
     ch=int(x.get('channel_id')); c=db(); head=c.execute('select coalesce(max(event_id),0) from events where ch=?',(ch,)).fetchone()[0]
     if x.get('type')=='subscribe':
      subs[ch]=head; ws_send(s,{'type':'subscribed','channel_id':ch,'head_event_id':head})
     elif x.get('type')=='resume':
      since=int(x.get('since_event_id',0)); rows=c.execute('select event_id,type,payload from events where ch=? and event_id>? order by event_id',(ch,since)).fetchall()
      for q in rows: ws_send(s,{'type':q['type'],'event_id':q['event_id'],'channel_id':ch,**json.loads(q['payload'])})
      last=rows[-1]['event_id'] if rows else head; subs[ch]=last
      ws_send(s,{'type':'resumed','channel_id':ch,'head_event_id':last})
     c.close()
   except socket.timeout: pass
   except Exception: break
   try:
    c=db()
    for ch,last in list(subs.items()):
     rows=c.execute('select event_id,type,payload from events where ch=? and event_id>? order by event_id',(ch,last)).fetchall()
     for q in rows:
      ws_send(s,{'type':q['type'],'event_id':q['event_id'],'channel_id':ch,**json.loads(q['payload'])}); subs[ch]=q['event_id']
    c.close()
   except Exception: break
 def html(self):
  h=HTML;self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(h.encode())));self.end_headers();self.wfile.write(h.encode())
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:25px}.thread{width:300px;border-left:1px solid #ddd;padding:15px}button{border:0;padding:9px 14px;color:#fff;border-radius:3px}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}input{padding:9px;margin:4px}.msg{padding:12px;border-bottom:1px solid #eee}.muted{color:#777}</style></head><body><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><button data-testid=logout-btn data-button-role=danger>Logout</button><h3>Channels</h3><div data-testid=channel-list></div><div data-testid=dms-list></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button></aside><main class=main><div data-testid=auth-form id=auth-modal><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary>Login</button><button data-testid=auth-toggle>Sign up</button><div data-testid=join-workspace-error></div></div><section><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic class=muted></div><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button></section><div class=thread data-testid=thread-panel><h3>Thread</h3><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button><button data-testid=close-thread data-button-role=secondary>Close</button></div></main><div id=channel-settings-modal></div><div id=workspace-settings-modal></div><div style=display:none><form data-testid=workspace-create-form></form><button data-testid=empty-state-create-workspace></button><form data-testid=join-workspace-form></form><button data-testid=join-workspace-submit data-button-role=primary></button><button data-testid=workspace-settings-btn data-button-role=secondary></button><button data-testid=workspace-settings-close data-button-role=secondary></button><input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary></button><button data-testid=channel-settings-btn data-button-role=secondary></button><b data-testid=channel-settings-title></b><button data-testid=channel-settings-close data-button-role=secondary></button><div data-testid=channel-members-list></div><form data-testid=channel-add-member-form></form><input data-testid=channel-add-member-input><button data-testid=channel-add-member-submit data-button-role=primary></button><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary></button><button data-testid=archive-channel-btn data-button-role=danger></button><button data-testid=unarchive-channel-btn data-button-role=secondary></button><form id=create-channel-modal data-testid=create-channel-form><input data-testid=create-channel-private></form><button data-testid=create-channel-submit data-button-role=primary></button><button data-testid=create-channel-cancel data-button-role=secondary></button><button data-testid=open-thread-btn></button><button data-testid=reaction-button></button><span data-testid=reaction-chip></span><div data-testid=emoji-picker></div><button data-testid=settings-tab-members></button><button data-testid=settings-tab-invitations></button><button data-testid=settings-tab-general></button><div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div><button data-testid=create-invitation-btn data-button-role=secondary></button><form id=create-invitation-form data-testid=create-invitation-form><button data-testid=create-invitation-submit data-button-role=primary></button></form><div data-testid=invitations-list></div></div><script>
let t=localStorage.getItem('huddle.token'),ch=null;const $=s=>document.querySelector(s);async function api(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+t,'Content-Type':'application/json'};return fetch('/api'+p,o).then(x=>x.json())}function render(){if(!t)return;api('/workspaces').then(x=>{if(x.workspaces&&x.workspaces[0]){api('/workspaces/'+x.workspaces[0].slug).then(w=>{ch=w.channels[0];$(' [data-testid=channel-list]'.trim()).innerHTML=w.channels.map(c=>'<div class=entry data-testid=channel-entry data-channel-id='+c.id+' data-channel-name='+c.name+'>#'+c.name+'</div>').join('');load()})}})}function load(){api('/channels/'+ch.id+'/messages').then(x=>{$('[data-testid=message-list]').innerHTML=x.messages.reverse().map(m=>'<div class=msg data-testid=message><b>'+m.author.display_name+'</b><span data-testid=message-body> '+m.body+(m.edited_at?' (edited)':'')+'</span><button data-testid=open-thread-btn>Thread</button></div>').join('')})} $('[data-testid=auth-submit]').onclick=async()=>{let u=$('[name=username]').value,p=$('[name=password]').value;let x=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});if(!x.ok){$('[data-testid=join-workspace-error]').textContent='Invalid login';return}let j=await x.json();t=j.token;localStorage.setItem('huddle.token',t);render()};$('[data-testid=send-btn]').onclick=async()=>{await api('/channels/'+ch.id+'/messages',{method:'POST',body:JSON.stringify({body:$('[data-testid=message-input]').value})});$('[data-testid=message-input]').value='';load()};render();
</script></body></html>'''
def http(port): ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
def irc_poller():
 seen={}
 while True:
  try:
   c=db()
   rows=c.execute("select e.ch,e.event_id,e.payload,w.slug,x.name from events e join channels x on x.id=e.ch join workspaces w on w.id=x.ws where e.type='message.created' order by e.ch,e.event_id").fetchall()
   c.close()
   with ircs_lock:
    for r in rows:
     key=(r['ch'],r['event_id'])
     if seen.get(r['ch'],0)>=r['event_id']: continue
     seen[r['ch']]=r['event_id']
     o=json.loads(r['payload']); target='#%s/%s'%(r['slug'],r['name'])
     for z in list(ircs):
      if z.get('channel_id')==r['ch']: irc_send(z['sock'],':%s!%s@host PRIVMSG %s :%s'%(o.get('author',{}).get('username',z.get('nick','user')),o.get('author',{}).get('username','user'),target,o.get('body','')))
  except: pass
  time.sleep(.05)
def irc():
 threading.Thread(target=irc_poller,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
 while 1:
  x,_=s.accept();threading.Thread(target=irc_client,args=(x,),daemon=True).start()
def irc_client(s):
 tok=None;nick=None;joined=None;last=0;s.settimeout(1)
 def send(x):s.sendall((x+'\r\n').encode())
 while 1:
  try:
   d=s.recv(4096)
   if not d:break
   for line in d.decode(errors='ignore').splitlines():
    q=line.split(' ',2);cmd=q[0].upper()
    if cmd=='PASS':tok=q[1] if len(q)>1 else '';c=db();ok=c.execute('select user_id from tokens where token=?',(tok,)).fetchone();c.close(); 
    if cmd=='PASS' and not ok:send(':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     candidate=q[1] if len(q)>1 else '*'
     with ircs_lock: used=any(z.get('nick')==candidate for z in ircs)
     if used: send(':huddle 433 * '+candidate+' :Nickname is already in use')
     else:
      nick=candidate
      with ircs_lock:
       ircs.append({'sock':s,'nick':nick,'channel_id':None})
    elif cmd=='USER' and tok:
     send(':huddle 001 '+(nick or '*')+' :Welcome');send(':huddle 002 '+(nick or '*')+' :Your host');send(':huddle 003 '+(nick or '*')+' :Created');send(':huddle 004 '+(nick or '*')+' huddle');send(':huddle 005 '+(nick or '*')+' :CHANTYPES=#');send(':huddle 422 '+(nick or '*')+' :No MOTD')
    elif cmd=='PING':send('PONG '+(q[-1] if len(q)>1 else ''))
    elif cmd=='JOIN':
     name=q[1]; c=db(); r=c.execute("select w.slug,x.* from channels x join workspaces w on w.id=x.ws where ('#' || w.slug || '/' || x.name)=?", (name,)).fetchone(); c.close()
     if not r:send(':huddle 403 '+name+' :No such channel')
     else:
      joined=r['id']
      with ircs_lock:
       for z in ircs:
        if z.get('sock') is s: z['channel_id']=joined
      send(':'+nick+'!u@host JOIN '+name);send(':huddle 353 '+nick+' = '+name+' :'+nick);send(':huddle 366 '+nick+' '+name+' :End of NAMES')
    elif cmd=='PRIVMSG' and joined:
     body=q[2][1:] if len(q)>2 and q[2].startswith(':') else (q[2] if len(q)>2 else '');c=db();c.execute('insert into messages(ch,author,body,created) values(?,?,?,?)',(joined,c.execute('select user_id from tokens where token=?',(tok,)).fetchone()[0],body,now()));mid=c.execute('select last_insert_rowid()').fetchone()[0];r=c.execute('select * from messages where id=?',(mid,)).fetchone();o=msgobj(c,r);ev=emit(c,joined,'message.created',o);c.close();hub.broadcast({'type':'message.created','event_id':ev,'channel_id':joined,**o})
    elif cmd=='QUIT':
     with ircs_lock: ircs[:]=[z for z in ircs if z.get('sock') is not s]
     return
    elif cmd not in ('PASS','NICK','USER','JOIN','PING','PRIVMSG','NAMES','WHO','LIST','TOPIC','MODE'):
     send(':huddle 421 '+(q[0] if q else '')+' :Unknown command')
  except socket.timeout:
   if joined:
    try:
     c=db(); rows=c.execute("select e.event_id,e.payload,w.slug,x.name from events e join channels x on x.id=e.ch join workspaces w on w.id=x.ws where e.ch=? and e.type='message.created' and e.event_id>? order by e.event_id",(joined,last)).fetchall(); c.close()
     for rr in rows:
      o=json.loads(rr['payload']); target='#%s/%s'%(rr['slug'],rr['name'])
      send(':%s!%s@host PRIVMSG %s :%s'%(o.get('author',{}).get('username',nick or 'user'),o.get('author',{}).get('username','user'),target,o.get('body','')))
      last=rr['event_id']
    except: pass
  except:break
if __name__=='__main__':
 init()
 if sys.argv[1:] and sys.argv[1]=='irc':irc()
 else:http(int(sys.argv[1]))
