import socket,threading,sqlite3,json,time,re,os
DB='/app/huddle.db'
clients={}; lock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=10);c.row_factory=sqlite3.Row;return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def valid(token):
 c=db();r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone();c.close();return r
def channel(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1);c=db();r=c.execute('SELECT ch.*,w.slug,w.name wname FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone();c.close();return r
def emit_message(ch,user,text):
 c=db();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,datetime("now"))',(ch['id'],user['id'],text,None));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 payload={'id':mid,'channel_id':ch['id'],'author_id':user['id'],'body':text};n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch['id'],)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload,created_at) VALUES(?,?,?,?,datetime("now"))',(ch['id'],n,'message.created',json.dumps(payload)));c.commit();c.close();return n
class Conn(threading.Thread):
 def __init__(self,s,a):
  super().__init__(daemon=True);self.s=s;self.a=a;self.token=None;self.user=None;self.nick=None;self.reg=False;self.joined={};self.seen={}
 def run(self):
  buf=b''
  try:
   self.s.settimeout(.2)
   while True:
    try:
     x=self.s.recv(4096)
     if not x:break
     buf+=x
    except socket.timeout:pass
    while b'\n' in buf:
     line,buf=buf.split(b'\n',1);self.cmd(line.decode(errors='ignore').strip('\r'))
    self.poll()
  except:pass
  finally:
   with lock:
    clients.pop(self.nick,None)
   try:self.s.close()
   except:pass
 def cmd(self,line):
  if not line:return
  parts=line.split(' ',1);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
  if cmd=='PASS':
   self.token=arg.strip();self.user=valid(self.token)
   if not self.user:send(self.s,':huddle 464 * :Password incorrect')
  elif cmd=='NICK':
   n=arg.strip().split()[0] if arg.strip() else ''
   with lock:busy=n in clients and clients[n] is not self
   if busy:send(self.s,':huddle 433 * '+n+' :Nickname is already in use');return
   self.nick=n
   with lock:clients[n]=self
   self.tryreg()
  elif cmd=='USER':
   self.tryreg()
  elif cmd=='PING':send(self.s,'PONG '+arg)
  elif cmd=='QUIT':raise RuntimeError()
  elif cmd=='JOIN':
   if not self.reg:return
   name=arg.split()[0];ch=channel(name)
   if not ch:send(self.s,':huddle 403 '+self.nick+' '+name+' :No such channel');return
   self.joined[ch['id']]={'ch':ch,'name':name};self.seen[ch['id']]=0
   send(self.s,':'+self.nick+'!'+self.user['username']+'@localhost JOIN '+name)
   send(self.s,':huddle 353 '+self.nick+' = '+name+' :'+self.nick)
   send(self.s,':huddle 366 '+self.nick+' '+name+' :End of NAMES list')
  elif cmd=='PRIVMSG':
   if not self.reg:return
   m=re.match(r'(\S+)\s+:(.*)',arg)
   if not m:return
   name,text=m.groups();ch=channel(name)
   if ch:
    emit_message(ch,self.user,text)
  elif cmd=='NAMES':
   send(self.s,':huddle 366 '+(self.nick or '*')+' * :End of NAMES list')
  elif cmd=='WHO':
   send(self.s,':huddle 315 '+(self.nick or '*')+' * :End of WHO list')
  elif cmd=='LIST':
   send(self.s,':huddle 323 '+(self.nick or '*')+' :End of LIST')
  elif cmd=='TOPIC':
   send(self.s,':huddle 331 '+(self.nick or '*')+' * :No topic is set')
  elif cmd=='MODE':
   send(self.s,':huddle 324 '+(self.nick or '*')+' * +')
  else:send(self.s,':huddle 421 '+(self.nick or '*')+' '+cmd+' :Unknown command')
 def tryreg(self):
  if self.reg or not self.user or not self.nick:return
  self.reg=True
  for n,msg in [('001','Welcome'),('002','Your host is huddle'),('003','Server created'),('004','huddle 1.0'),('005','CHANTYPES=#')]:
   send(self.s,':huddle '+n+' '+self.nick+' :'+msg)
  send(self.s,':huddle 422 '+self.nick+' :MOTD File is missing')
 def poll(self):
  if not self.reg:return
  c=db()
  for cid,x in list(self.joined.items()):
   rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,self.seen.get(cid,0))).fetchall()
   for e in rows:
    self.seen[cid]=e['event_id']
    if e['kind']!='message.created':continue
    p=json.loads(e['payload'])
    if p.get('author_id')==self.user['id']:continue
    u=c.execute('SELECT username FROM users WHERE id=?',(p.get('author_id'),)).fetchone()
    send(self.s,':'+(u['username'] if u else 'user')+'!'+(u['username'] if u else 'user')+'@localhost PRIVMSG '+x['name']+' :'+p.get('body',''))
  c.close()
def serve():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  q,a=s.accept();Conn(q,a).start()
if __name__=='__main__':serve()
