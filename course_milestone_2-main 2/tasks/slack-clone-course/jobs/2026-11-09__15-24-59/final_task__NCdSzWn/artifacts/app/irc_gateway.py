import socketserver, threading, socket, sqlite3, json, time, os, re
from datetime import datetime, timezone
DB='/app/huddle.db'
clients=[]; lock=threading.Lock(); seen=0
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def ts(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def send(c,line):
 try: c.request.sendall((line+'\r\n').encode())
 except: pass
class Client:
 def __init__(self,req):
  self.request=req; self.nick=None; self.user=None; self.uid=None; self.token=None; self.ready=False; self.channels=set()
 def numeric(self,n,*parts):
  text=' '.join(str(x) for x in parts)
  send(self,':huddle %03d %s %s'%(n,self.nick or '*',text))
 def register(self):
  if self.ready:return
  if not self.uid or not self.nick:return
  self.ready=True
  for n in range(1,6): self.numeric(n,'huddle :Welcome')
  self.numeric(422,':No MOTD')
  with lock:
   if self not in clients:clients.append(self)
 def channel(self,name):
  if not name.startswith('#') or '/' not in name:return None
  slug,chname=name[1:].split('/',1); c=db(); r=c.execute('select c.* from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(slug,chname)).fetchone(); c.close(); return r
 def process(self,line):
  a=line.strip().split(' ',2); cmd=a[0].upper() if a else ''
  if cmd=='PASS':
   tok=a[1] if len(a)>1 else ''; c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone(); c.close()
   if not r:self.numeric(464,':Password incorrect')
   else:self.uid=r['id']; self.user=r['username']; self.token=tok
  elif cmd=='NICK':
   nick=a[1] if len(a)>1 else ''
   with lock: used=any(x.nick==nick and x is not self for x in clients)
   if used:self.numeric(433,nick+':Nickname in use')
   else:self.nick=nick; self.register()
  elif cmd=='USER':
   self.user=(a[1] if len(a)>1 else self.user); self.register()
  elif cmd=='PING': send(self,':huddle PONG '+(a[2] if len(a)>2 else (a[1] if len(a)>1 else '')))
  elif cmd=='JOIN':
   name=a[1] if len(a)>1 else ''; ch=self.channel(name)
   if not ch:self.numeric(403,name+':No such channel'); return
   self.channels.add(name); send(self,':%s!%s@host JOIN %s'%(self.nick,self.user,name)); self.numeric(353,'= %s :%s'%(name,self.nick)); self.numeric(366,name,':End of /NAMES list')
  elif cmd=='PRIVMSG':
   if len(a)<3:return
   name=a[1]; body=a[2].lstrip(':'); ch=self.channel(name)
   if not ch or name not in self.channels:return self.numeric(404,name+':Cannot send')
   c=db()
   c.execute('BEGIN IMMEDIATE'); eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch['id'],)).fetchone()[0]
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id,mentions) values(?,?,?,?,?,?,?)',(ch['id'],self.uid,body,None,ts(),eid,'[]')); mid=c.execute('select last_insert_rowid()').fetchone()[0]
   payload={'type':'message.created','event_id':eid,'channel_id':ch['id'],'message':{'id':mid,'channel_id':ch['id'],'author_id':self.uid,'body':body}}
   c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(ch['id'],eid,'message.created',json.dumps(payload),ts())); c.commit(); c.close()
  elif cmd=='QUIT': raise EOFError
  elif cmd=='NAMES': self.numeric(366,a[1] if len(a)>1 else '*',':End of /NAMES list')
  elif cmd=='WHO': self.numeric(315,a[1] if len(a)>1 else '*',':End of /WHO list')
  elif cmd=='LIST': self.numeric(323,':End of /LIST')
  elif cmd=='TOPIC': self.numeric(331,a[1] if len(a)>1 else '*',':No topic')
  elif cmd=='MODE': self.numeric(324,a[1] if len(a)>1 else self.nick,'+')
  else:self.numeric(421,cmd+':Unknown command')
class Handler(socketserver.BaseRequestHandler):
 def handle(self):
  x=Client(self.request)
  try:
   while True:
    line=self.request.recv(4096)
    if not line:break
    for q in line.decode(errors='ignore').splitlines():
     try:x.process(q)
     except EOFError:return
  finally:
   with lock:
    if x in clients:clients.remove(x)
def poll():
 global seen
 while True:
  c=db()
  rows=c.execute("select id,payload from events where id>? order by id",(seen,)).fetchall()
  for r in rows:
   seen=r['id']
   try:
    p=json.loads(r['payload'])
    if p.get('type')!='message.created':continue
    m=p.get('message',{}); ch=c.execute('select w.slug,c.name from channels c join workspaces w on w.id=c.workspace_id where c.id=?',(p['channel_id'],)).fetchone()
    if not ch:continue
    name='#%s/%s'%(ch['slug'],ch['name']); au=c.execute('select username from users where id=?',(m.get('author_id'),)).fetchone(); nick=au['username'] if au else 'user'
    with lock:
     for x in list(clients):
      if name in x.channels and m.get('author_id')!=x.uid:send(x,':%s!%s@host PRIVMSG %s :%s'%(nick,nick,name,m.get('body','')))
   except:pass
  c.close(); time.sleep(.1)
if __name__=='__main__':
 threading.Thread(target=poll,daemon=True).start()
 socketserver.ThreadingTCPServer.allow_reuse_address=True
 socketserver.ThreadingTCPServer(('0.0.0.0',6667),Handler).serve_forever()
