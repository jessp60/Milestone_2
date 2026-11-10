import socket,threading,sqlite3,time,json,os,fcntl
DB='/app/data.db'; clients=[]; nicks=set(); lock=threading.Lock()
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def user(tok):
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close(); return r
def chans(c):
 return c.execute("SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.is_dm=0").fetchall()
def lookup(name):
 c=db(); r=c.execute("SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.is_dm=0 AND '#'||w.slug||'/'||c.name=?",(name,)).fetchone(); c.close(); return r
def prefix(u,nick): return '%s!%s@localhost'%(nick,u['username'])
def gateway(sock):
 sock.settimeout(.5); buf=''; tok=None; u=None; nick=None; registered=False; joined=set(); seen={}
 with lock: clients.append((sock,lambda:joined))
 def line(x): return send(sock,x)
 def register():
  nonlocal registered
  if registered:return
  registered=True
  for n in range(1,6): line(':huddle %03d %s :huddle'% (n,nick))
  line(':huddle 422 %s :MOTD File is missing'%nick)
 try:
  while True:
   c=db()
   for ch in list(joined):
    cid=ch[0]
    rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,seen.get(cid,0))).fetchall()
    for e in rows:
     p=json.loads(e['payload']); body=p.get('message',{}).get('body')
     if body is not None:
      a=p.get('message',{}).get('author',{}); nn=a.get('username','web')
      meta=c.execute('SELECT w.slug,c.name FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone()
      if meta: line(':%s!%s@localhost PRIVMSG #%s/%s :%s'%(nn,nn,meta['slug'],meta['name'],body))
     seen[ch]=e['event_id']
   c.close()
   try:
    d=sock.recv(4096)
    if not d:break
    buf+=d.decode(errors='ignore')
   except socket.timeout: continue
   while '\r\n' in buf or '\n' in buf:
    if '\r\n' in buf: q,buf=buf.split('\r\n',1)
    else:q,buf=buf.split('\n',1)
    if not q:continue
    a=q.split(' ',2); cmd=a[0].upper(); rest=q[len(a[0]):].strip()
    if cmd=='PASS':
     tok=rest; 
     if not user(tok):line(':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     nn=rest.split()[0] if rest else ''
     with lock: collision=nn in nicks
     if collision:line(':huddle 433 * %s :Nickname is already in use'%nn)
     else:
      if nick:
       with lock:nicks.discard(nick)
      nick=nn
      with lock:nicks.add(nick)
    elif cmd=='USER' and tok and user(tok):u=user(tok);register()
    elif cmd=='PING':line(':huddle PONG '+rest.lstrip(':'))
    elif cmd=='FOOBAR':line(':huddle 421 %s FOOBAR :Unknown command'%nick)
    elif cmd=='QUIT':
     try:sock.shutdown(socket.SHUT_RDWR)
     except:pass
     break
    elif cmd=='JOIN' and len(a)>1:
     ch=lookup(a[1])
     if not ch:line(':huddle 403 %s %s :No such channel'%(nick,a[1]));continue
     key=(ch['id'],ch['name']);joined.add(key);seen[ch['id']]=0
     line(':%s JOIN %s'%(prefix(u,nick),a[1])); names=' '.join([nick])
     line(':huddle 353 %s = %s :%s'%(nick,a[1],names));line(':huddle 366 %s %s :End of NAMES list'%(nick,a[1]))
    elif cmd=='PRIVMSG' and len(a)>2:
     ch=lookup(a[1]); body=a[2].lstrip(':') if a[2].startswith(':') else a[2]
     if ch:
      lk=open('/tmp/event.lock','w');fcntl.flock(lk,fcntl.LOCK_EX); c=db();c.execute('INSERT OR IGNORE INTO seq VALUES(?,0)',(ch['id'],));c.execute('UPDATE seq SET n=n+1 WHERE channel_id=?',(ch['id'],));ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone()['n']; c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,datetime("now"),?)',(ch['id'],u['id'],body,None,ev));r=c.execute('SELECT * FROM messages WHERE id=last_insert_rowid()').fetchone();c.commit();lk.close();seen[ch['id']]=ev
      payload={'message':{'id':r['id'],'body':body,'author':{'username':u['username']}}};c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],ev,'message.created',json.dumps(payload)));c.commit();c.close()
    elif cmd=='NAMES':
     line(':huddle 353 %s = %s :%s'%(nick,a[1] if len(a)>1 else '*',' '.join([nick])))
     line(':huddle 366 %s %s :End of NAMES list'%(nick,a[1] if len(a)>1 else '*'))
    elif cmd=='WHO':
     line(':huddle 315 %s %s :End of WHO list'%(nick,a[1] if len(a)>1 else '*'))
    elif cmd=='LIST':
     line(':huddle 323 %s :End of LIST'%(nick))
    elif cmd=='TOPIC':
     line(':huddle 331 %s %s :No topic is set'%(nick,a[1] if len(a)>1 else '*'))
    elif cmd=='MODE':
     line(':huddle 324 %s %s +nt'%(nick,a[1] if len(a)>1 else '*'))
    elif cmd not in ('PASS','USER','NICK'): line(':huddle 421 %s %s :Unknown command'%(nick,cmd))
 finally:
  with lock: clients[:]=[x for x in clients if x[0] is not sock]
  try:
   sock.close()
  except Exception:
   pass
def main():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(50)
 while True:
  x,_=s.accept();threading.Thread(target=gateway,args=(x,),daemon=True).start()
main()
