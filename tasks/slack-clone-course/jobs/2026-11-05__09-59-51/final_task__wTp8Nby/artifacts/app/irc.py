import socket,threading,sqlite3,json,os,time,re,fcntl,datetime
DB='/app/huddle.db'; LOCK='/app/huddle.event.lock'; PORT=6667
clients=[]; mu=threading.RLock(); nicks=set()
def db():
 c=sqlite3.connect(DB,timeout=30,isolation_level=None,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('pragma busy_timeout=30000'); return c
def ts(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def send(c,line):
 try:c.sock.sendall((line+'\r\n').encode())
 except: c.alive=False
def find_channel(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1); return db().execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cn)).fetchone()
def token_user(tok):
 return db().execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone()
def event(ch,kind,msg):
 with open(LOCK,'w') as lk:
  fcntl.flock(lk,fcntl.LOCK_EX)
  c=db(); x=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(ch,)).fetchone()['n']
  o={'type':kind,'channel_id':ch,'event_id':x,'message':msg}
  c.execute('insert into events values(?,?,?,?,?)',(ch,x,kind,msg['id'],json.dumps(o)))
  os.makedirs('/tmp/huddle_events',exist_ok=True); tmp='/tmp/huddle_events/.%s.%s.%s'%(ch,x,os.getpid())
  open(tmp,'w').write(json.dumps(o)); os.replace(tmp,'/tmp/huddle_events/%s.%s'%(ch,x))
 return o
def message(c,ch,uid,body):
 r=c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,uid,body,None,ts()))
 mid=r.lastrowid; c.commit()
 u=c.execute('select * from users where id=?',(uid,)).fetchone()
 o={'id':mid,'channel_id':ch,'author_id':uid,'author':{k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')},'body':body,'parent_id':None,'reply_count':0,'created_at':ts(),'edited_at':None,'files':[],'reactions':[],'mentions':[]}
 return o
class Client:
 def __init__(self,s):
  self.sock=s;self.alive=True;self.token=None;self.user=None;self.nick=None;self.joined={}
 def register(self):
  if self.user and self.nick and self.nick not in nicks:
   with mu:nicks.add(self.nick)
   for n in ('001','002','003','004','005'):send(self,':irc '+n+' '+self.nick+' :Welcome')
   send(self,':irc 422 '+self.nick+' :No MOTD')
 def run(self):
  buf=b''
  try:
   while self.alive:
    d=self.sock.recv(4096)
    if not d:break
    buf+=d
    while b'\n' in buf:
     line,buf=buf.split(b'\n',1); z=line.decode(errors='ignore').strip('\r'); self.cmd(z)
  except:pass
  finally:
   if self.nick:
    with mu:nicks.discard(self.nick)
   self.alive=False
   try:self.sock.close()
   except:pass
   with mu:
    if self in clients:clients.remove(self)
 def cmd(self,line):
  if not line:return
  a=line.split(); cmd=a[0].upper(); rest=line[len(a[0]):].lstrip()
  if cmd=='PASS':
   tok=rest.split()[0] if rest else ''; self.token=tok; self.user=token_user(tok)
   if not self.user:send(self,':irc 464 * :Password incorrect')
  elif cmd=='NICK':
   nick=a[1] if len(a)>1 else ''
   with mu: busy=nick in nicks and nick!=self.nick
   if busy:send(self,':irc 433 * '+nick+' :Nickname in use');return
   self.nick=nick;self.register()
  elif cmd=='USER':
   self.register()
  elif cmd=='PING':send(self,'PONG '+rest)
  elif cmd=='QUIT':self.alive=False
  elif cmd=='JOIN':
   name=a[1] if len(a)>1 else ''; x=find_channel(name)
   if not x:send(self,':irc 403 '+str(self.nick or '*')+' '+name+' :No such channel');return
   self.joined[name]=x['id'];send(self,':'+self.nick+'!'+self.user['username']+'@host JOIN '+name);send(self,':irc 353 '+self.nick+' = '+name+' :'+self.nick);send(self,':irc 366 '+self.nick+' '+name+' :End of NAMES')
  elif cmd=='PRIVMSG':
   if len(a)<2 or ':' not in rest:return
   name=a[1]; body=rest.split(':',1)[1]; ch=self.joined.get(name); x=find_channel(name)
   if not ch or not x:return
   c=db(); mo=message(c,ch,self.user['id'],body);event(ch,'message.created',mo)
  elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
   if cmd=='NAMES':send(self,':irc 353 '+str(self.nick)+' = '+(a[1] if len(a)>1 else '*')+' :'+str(self.nick));send(self,':irc 366 '+str(self.nick)+' '+(a[1] if len(a)>1 else '*')+' :End')
  else:send(self,':irc 421 '+str(self.nick or '*')+' '+cmd+' :Unknown command')
def bridge():
 seen=set(os.listdir('/tmp/huddle_events')) if os.path.isdir('/tmp/huddle_events') else set()
 while True:
  try:
   for fn in os.listdir('/tmp/huddle_events'):
    if fn in seen or fn.startswith('.'):continue
    try:
     o=json.load(open('/tmp/huddle_events/'+fn)); x=find_by_id(o['channel_id'])
     if not x:continue
     name='#'+x['slug']+'/'+x['name']; m=o.get('message') or {}; body=m.get('body',''); author=m.get('author') or {}
     with mu:
      for c in list(clients):
       if name in c.joined and c.nick:send(c,':'+author.get('username','user')+'!user@host PRIVMSG '+name+' :'+body)
     seen.add(fn)
    except:pass
  except:pass
  time.sleep(.02)
def find_by_id(ch): return db().execute('select c.*,w.slug from channels c join workspaces w on w.id=c.workspace_id where c.id=?',(ch,)).fetchone()
def main():
 threading.Thread(target=bridge,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',PORT));s.listen()
 while True:
  x,_=s.accept(); c=Client(x)
  with mu:clients.append(c)
  threading.Thread(target=c.run,daemon=True).start()
main()
