import socket,threading,sqlite3,time,sys
DB='/app/huddle.db'; active={}; nicks=set(); lock=threading.RLock()
def db():
 c=sqlite3.connect(DB,timeout=20);c.row_factory=sqlite3.Row;return c
def line(s,x):s.sendall((x+'\r\n').encode())
def user_token(t):
 with db() as c:return c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone()
def lookup(name):
 x=name.lstrip('#').split('/',1)
 if len(x)!=2:return None
 with db() as c:return c.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(x[0],x[1])).fetchone()
def gateway(c):
 nick=None;usr=None;token=None;joined=None;last=0;registered=False
 def poll():
  nonlocal last
  while c.fileno()>=0:
   time.sleep(.12)
   if not joined:continue
   try:
    with db() as d:
     rows=d.execute('SELECT e.*,m.body,m.author_id,u.username FROM events e JOIN messages m ON m.id=e.message_id JOIN users u ON u.id=m.author_id WHERE e.channel_id=? AND e.event_id>? ORDER BY e.event_id',(joined['id'],last)).fetchall()
    for e in rows:
     last=e['event_id']
     if e['type'] in ('message.created','message.reply') and e['author_id']!= (usr['id'] if usr else -1):
      line(c,':'+e['username']+'!user@host PRIVMSG #'+joined['slug']+'/'+joined['name']+' :'+e['body'])
   except: return
 threading.Thread(target=poll,daemon=True).start()
 buf=''
 try:
  while True:
   z=c.recv(4096)
   if not z:break
   buf+=z.decode(errors='ignore')
   while '\n' in buf:
    raw,buf=buf.split('\n',1); raw=raw.rstrip('\r'); a=raw.split(' ',2); cmd=a[0].upper()
    if cmd=='PASS':token=a[1] if len(a)>1 else ''
    elif cmd=='NICK':
     nn=a[1] if len(a)>1 else ''
     with lock:
      if nn in nicks and nn!=nick:line(c,':huddle 433 * '+nn+' :Nickname in use');continue
      if nick:nicks.discard(nick)
      nick=nn;nicks.add(nick)
      if usr and not registered:
       registered=True
       for n in ('001','002','003','004','005'):line(c,':huddle '+n+' '+nick+' :Welcome to Huddle')
       line(c,':huddle 422 '+nick+' :MOTD File is missing')
    elif cmd=='USER':
     usr=user_token(token)
     if not usr:line(c,':huddle 464 * :Password incorrect');continue
     if not nick:continue
     if not registered:
      registered=True
      for n in ('001','002','003','004','005'):line(c,':huddle '+n+' '+nick+' :Welcome to Huddle')
      line(c,':huddle 422 '+nick+' :MOTD File is missing')
    elif cmd=='PING':line(c,'PONG '+(a[-1] if a else ''))
    elif cmd=='JOIN' and len(a)>1:
     ch=lookup(a[1])
     if not ch:line(c,':huddle 403 '+nick+' '+a[1]+' :No such channel')
     else:
      joined=ch
      with db() as d:
       q=d.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone();last=q['n'] if q else 0
      line(c,':'+nick+'!user@host JOIN '+a[1]);line(c,':huddle 353 '+nick+' = '+a[1]+' :'+nick);line(c,':huddle 366 '+nick+' '+a[1]+' :End of NAMES list')
    elif cmd=='PRIVMSG' and joined and len(a)>2:
     text=a[2].lstrip(':')
     with db() as d:
      d.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(joined['id'],));ev=d.execute('SELECT n FROM seq WHERE channel_id=?',(joined['id'],)).fetchone()['n'];cur=d.execute('INSERT INTO messages(channel_id,author_id,body,created_at,event_id) VALUES(?,?,?,?,?)',(joined['id'],usr['id'],text,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'),ev));mid=cur.lastrowid;d.execute('INSERT INTO events VALUES(?,?,?,?)',(joined['id'],ev,'message.created',mid))
    elif cmd=='QUIT':return
    elif cmd=='NAMES':
     line(c,':huddle 353 '+(nick or '*')+' = '+(a[1] if len(a)>1 else '*')+' :'+(nick or ''))
     line(c,':huddle 366 '+(nick or '*')+' '+(a[1] if len(a)>1 else '*')+' :End of NAMES list')
    elif cmd=='WHO':
     line(c,':huddle 315 '+(nick or '*')+' '+(a[1] if len(a)>1 else '*')+' :End of WHO list')
    elif cmd=='LIST':
     line(c,':huddle 323 '+(nick or '*')+' :End of LIST')
    elif cmd=='TOPIC':
     line(c,':huddle 331 '+(nick or '*')+' '+(a[1] if len(a)>1 else '*')+' :No topic')
    elif cmd=='MODE':
     line(c,':huddle 324 '+(nick or '*')+' '+(a[1] if len(a)>1 else '*')+' +')
    else:line(c,':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
 finally:
  with lock:
   if nick:nicks.discard(nick)
  try:c.close()
  except:pass
def main():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
 while True:
  c,_=s.accept();threading.Thread(target=gateway,args=(c,),daemon=True).start()
main()
