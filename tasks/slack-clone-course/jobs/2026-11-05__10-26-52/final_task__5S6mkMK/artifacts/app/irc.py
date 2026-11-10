import socket,threading,sqlite3,json,time,re,os
DB='/tmp/huddle.sqlite3'; clients=[]; lock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=20);c.row_factory=sqlite3.Row;return c
def send(c,line):
 try:c.sock.sendall((line+'\r\n').encode())
 except: c.alive=False
def lookup(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,ch=name[1:].split('/',1); c=db()
 r=c.execute('select w.slug,ch.* from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,ch)).fetchone();c.close();return r
def announce(ch,row,skip=None):
 with lock:
  for c in list(clients):
   if c is not skip and c.alive and ch in c.joined:
    author=row.get('author') or {}; nick=author.get('username','web')
    send(c,':%s!%s@host PRIVMSG %s :%s'%(nick,nick,ch,row.get('body','')))
def poll():
 last=0
 while True:
  try:
   c=db()
   for e in c.execute("select e.*,w.slug,ch.name as cname from events e join channels ch on ch.id=e.channel_id join workspaces w on w.id=ch.workspace_id where e.id>? and e.kind='message.created' order by e.id",(last,)):
    p=json.loads(e['payload']); last=e['id']; announce('#'+e['slug']+'/'+e['cname'],p)
   c.close()
  except:pass
  time.sleep(.08)
class Client:
 def __init__(self,s,a):
  self.sock=s;self.addr=a;self.buf='';self.nick=None;self.user=None;self.token=None;self.uid=None;self.registered=False;self.joined=set();self.alive=True
 def numeric(self,n,text='',extra=None):
  if extra is not None: text=text+' '+extra
  send(self,':huddle %s %s :%s'%(n,self.nick or '*',text))
 def register(self):
  if self.registered or not self.token or not self.nick or not self.user:return
  c=db();r=c.execute('select * from users u join tokens t on t.user_id=u.id where t.token=?',(self.token,)).fetchone();c.close()
  if not r:self.numeric('464','Password incorrect');return
  self.uid=r['id'];self.registered=True
  for n in ('001','002','003','004','005'):self.numeric(n,'huddle')
  self.numeric('422','No MOTD')
 def command(self,line):
  if not line:return
  p=line.split(' ',1);cmd=p[0].upper(); rest=p[1] if len(p)>1 else ''
  if cmd=='PASS':self.token=rest.strip();return
  if cmd=='NICK':
   n=rest.strip().split()[0] if rest.strip() else ''
   with lock: collision=any(x is not self and x.nick==n for x in clients)
   if collision:self.numeric('433','Nickname in use');return
   self.nick=n;self.register();return
  if cmd=='USER':self.user=rest.split()[0] if rest else self.nick;self.register();return
  if cmd=='PING':send(self,'PONG '+rest);return
  if cmd=='QUIT':self.alive=False;return
  if not self.registered:self.numeric('451','You have not registered');return
  if cmd=='JOIN':
   name=rest.split()[0]; r=lookup(name)
   if not r:self.numeric('403','No such channel');return
   self.joined.add(name);send(self,':%s!%s@host JOIN %s'%(self.nick,self.nick,name));self.numeric('353','= %s :%s'%(name,self.nick));self.numeric('366',name);return
  if cmd=='PRIVMSG':
   if ' :' not in rest:return
   name,text=rest.split(' :',1);r=lookup(name)
   if not r:self.numeric('403','No such channel');return
   c=db(); body=text
   c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(r['id'],self.uid,body,None,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')))
   mid=c.execute('select last_insert_rowid()').fetchone()[0]
   mr=dict(c.execute('select * from messages where id=?',(mid,)).fetchone()); ev=max(0,c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(r['id'],)).fetchone()[0])+1
   mr['author']={'username':self.nick};mr['_irc']=r['slug']+'/'+r['name']
   c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(r['id'],ev,'message.created',json.dumps(mr),time.strftime('%Y-%m-%dT%H:%M:%SZ')));c.commit();c.close();return
  if cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):return
  self.numeric('421',cmd+' Unknown command')
def serve(s,a):
 x=Client(s,a)
 with lock:clients.append(x)
 try:
  while x.alive:
   d=s.recv(4096)
   if not d:break
   x.buf+=d.decode(errors='ignore')
   while '\n' in x.buf:
    line,x.buf=x.buf.split('\n',1);x.command(line.rstrip('\r'))
 finally:
  x.alive=False
  with lock:
   if x in clients:clients.remove(x)
  try:s.close()
  except:pass
def main():
 threading.Thread(target=poll,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  c,a=s.accept();threading.Thread(target=serve,args=(c,a),daemon=True).start()
main()
