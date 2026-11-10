import socketserver, socket, threading, sqlite3, json, time, os, re
DB='/app/data/huddle.sqlite3'
clients=set(); nickowners={}; lk=threading.RLock()
def db():
 c=sqlite3.connect(DB,timeout=30,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def q(sql,args=(),one=False):
 with lk:
  c=db(); r=c.execute(sql,args); x=r.fetchone() if one else r.fetchall(); c.close(); return x
def send(c,s):
 try:c.sendall((s+'\r\n').encode())
 except:pass
def user_for(token):
 return q('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token,),True)
def channel(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1)
 return q('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cn),True)
def emit(cid,uid,body):
 with lk:
  c=db()
  r=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone(); eid=(r['n'] if r else 0)+1
  created=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
  c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(cid,uid,body,None,created,eid))
  mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  c.execute('INSERT OR REPLACE INTO seq(channel_id,n) VALUES(?,?)',(cid,eid))
  c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,eid,'message.created',json.dumps({'message_id':mid,'body':body})))
  c.commit(); c.close()
class IRC(socketserver.ThreadingTCPServer):
 allow_reuse_address=True
 def __init__(self,a,h): super().__init__(a,h); self.daemon_threads=True
class H(socketserver.BaseRequestHandler):
 def setup(self):
  self.token=None; self.user=None; self.nick=None; self.reg=False; self.joined=set(); self.last={}; self.alive=True
  with lk: clients.add(self)
 def finish(self):
  self.alive=False
  with lk:
   clients.discard(self)
   if self.nick and nickowners.get(self.nick) is self: del nickowners[self.nick]
 def out(self,s): send(self.request,s)
 def prefix(self): return '%s!%s@localhost'%(self.nick or '*',self.user['username'] if self.user else 'unknown')
 def register(self):
  if self.reg or not self.token or not self.nick or not self.user:return
  self.reg=True
  for n in ('001','002','003','004','005'):self.out(':huddle %s %s :huddle'% (n,self.nick))
  self.out(':huddle 422 %s :MOTD File is missing'%self.nick)
 def poll(self):
  while self.alive:
   time.sleep(.12)
   for cname in list(self.joined):
    c=channel(cname)
    if not c:continue
    last=self.last.get(c['id'],0)
    for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(c['id'],last)):
     self.last[c['id']]=e['event_id']
     if e['kind']!='message.created':continue
     try:p=json.loads(e['payload'])
     except:p={}
     m=q('SELECT m.*,u.username FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(p.get('message_id'),),True) if p.get('message_id') else None
     if not m and p.get('message'):
      pm=p['message']; m={'author_id':pm.get('author_id'),'username':pm.get('author',{}).get('username','user'),'body':pm.get('body','')}
     if m and m['author_id']!=self.user['id']:
      self.out(':%s!%s@localhost PRIVMSG %s :%s'%(m['username'],m['username'],cname,m['body']))
 def handle(self):
  threading.Thread(target=self.poll,daemon=True).start()
  buf=b''
  while self.alive:
   try:
    x=self.request.recv(4096)
    if not x:break
    buf+=x
    while b'\n' in buf:
     line,buf=buf.split(b'\n',1); line=line.decode(errors='replace').strip('\r')
     if not line:continue
     a=line.split(' ',2); cmd=a[0].upper(); rest=line[len(a[0]):].lstrip()
     if cmd=='PASS':
      self.token=rest.split()[0] if rest else ''
      self.user=user_for(self.token)
      if not self.user:self.out(':huddle 464 * :Password incorrect')
     elif cmd=='NICK':
      n=rest.split()[0] if rest else ''
      with lk: used=n in nickowners and nickowners[n] is not self
      if used:self.out(':huddle 433 * %s :Nickname is already in use'%n)
      else:
       if self.nick:
        with lk:nickowners.pop(self.nick,None)
       self.nick=n
       with lk:nickowners[n]=self
       self.register()
     elif cmd=='USER': self.register()
     elif cmd=='PING': self.out(':huddle PONG huddle :'+rest.lstrip(':'))
     elif cmd=='JOIN':
      for cname in rest.split(','):
       c=channel(cname)
       if not c:self.out(':huddle 403 %s %s :No such channel'%(self.nick,cname)); continue
       self.joined.add(cname); self.last[c['id']]=q('SELECT COALESCE(n,0) n FROM seq WHERE channel_id=?',(c['id'],),True)['n']
       self.out(':%s JOIN %s'%(self.prefix(),cname)); self.out(':huddle 353 %s = %s :%s'%(self.nick,cname,self.nick)); self.out(':huddle 366 %s %s :End of NAMES list'%(self.nick,cname))
     elif cmd=='PRIVMSG':
      if ' :' in rest: target,body=rest.split(' :',1)
      else:
       z=rest.split(' ',1); target=z[0]; body=z[1] if len(z)>1 else ''
      c=channel(target)
      if c and self.reg: emit(c['id'],self.user['id'],body)
     elif cmd=='NAMES':
      self.out(':huddle 353 %s = %s :%s'%(self.nick,rest or '*',self.nick)); self.out(':huddle 366 %s %s :End of NAMES list'%(self.nick,rest or '*'))
     elif cmd=='WHO': self.out(':huddle 352 %s %s localhost huddle %s H :0 %s'%(self.nick,rest,self.nick,self.nick))
     elif cmd=='LIST': self.out(':huddle 321 %s Channel :Users Name'%self.nick); self.out(':huddle 323 %s :End of LIST'%self.nick)
     elif cmd=='TOPIC': self.out(':huddle 331 %s %s :No topic'% (self.nick,rest))
     elif cmd=='MODE': self.out(':huddle 324 %s %s +'% (self.nick,rest.split()[0] if rest else '*'))
     elif cmd=='QUIT': return
     elif cmd in ('CAP','PING'): pass
     else:self.out(':huddle 421 %s %s :Unknown command'%(self.nick,cmd))
   except:break
if __name__=='__main__':
 IRC(('0.0.0.0',6667),H).serve_forever()
