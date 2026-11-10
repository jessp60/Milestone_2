import socket,threading,sqlite3,time,re,os
DB='/app/data.sqlite3'
def db():
 c=sqlite3.connect(DB,timeout=10);c.row_factory=sqlite3.Row;return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
clients=[]; nicks=set(); lock=threading.Lock()
def user_for(tok):
 c=db();r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone();c.close();return r
def chan_for(name):
 if not name.startswith('#') or '/' not in name:return None
 x=name[1:].split('/',1);c=db();r=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(x[0],x[1])).fetchone();c.close();return r
def loop(s,addr):
 tok=None;u=None;nick=None;user_seen=False;registered=False;joined={};alive=True
 with lock: clients.append((s,joined,nick if 'nick' in locals() else None))
 s.settimeout(.25)
 def poll():
  nonlocal alive
  while alive:
   time.sleep(.15)
   for ch,last in list(joined.items()):
    c=db(); rows=c.execute('select e.*,m.author_id,m.body,u.username from events e left join messages m on json_extract(e.payload,"$.message.id")=m.id left join users u on u.id=m.author_id where e.channel_id=? and e.event_id>? order by e.event_id',(ch,last)).fetchall()
    for r in rows:
     joined[ch]=r['event_id']
     if r['type'] in ('message.created','message.reply'):
      try: send(s,':%s!%s@host PRIVMSG #%s/%s :%s'%(r['username'] or 'user',r['username'] or 'user',chan_for_id(ch)[0],chan_for_id(ch)[1],r['body']))
      except:pass
    c.close()
 def chan_for_id(cid):
  c=db();r=c.execute('select w.slug,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(cid,)).fetchone();c.close();return (r['slug'],r['name'])
 threading.Thread(target=poll,daemon=True).start()
 buf=b''
 try:
  while alive:
   try:
    z=s.recv(4096)
    if not z:break
    buf+=z
   except socket.timeout:continue
   while b'\n' in buf:
    raw,buf=buf.split(b'\n',1); line=raw.decode(errors='ignore').strip()
    if not line:continue
    a=line.split(); cmd=a[0].upper(); rest=line.split(' ',1)[1] if ' ' in line else ''
    if cmd=='PASS':
     tok=rest.lstrip(':');u=user_for(tok)
     if not u:send(s,':irc 464 * :Password incorrect')
    elif cmd=='NICK':
     nn=rest.split()[0] if rest else ''
     with lock: collision=nn in nicks or any(x[0] is not s and len(x)>2 and x[2]==nn for x in clients)
     if collision:send(s,':irc 433 * %s :Nickname is already in use'%nn)
     else:
      with lock:
       if nick in nicks:nicks.discard(nick)
       nick=nn;nicks.add(nn)
      for i,x in enumerate(clients):
       if x[0] is s: clients[i]=(s,joined,nick)
      if u and user_seen and not registered and nick:
       registered=True
       for n,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created now'),('004','irc huddle'),('005','CHANTYPES=#'),('422','MOTD File is missing')]:send(s,':irc %s %s :%s'%(n,nick,text))
    elif cmd=='USER':
     user_seen=True
     if u and nick and not registered:
      registered=True
      for n,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created now'),('004','irc huddle'),('005','CHANTYPES=#'),('422','MOTD File is missing')]:send(s,':irc %s %s :%s'%(n,nick,text))
    elif cmd=='PING':send(s,'PONG :'+rest.lstrip(':'))
    elif cmd=='JOIN':
     if not registered:continue
     name=rest.split()[0];ch=chan_for(name)
     if not ch:send(s,':irc 403 %s %s :No such channel'%(nick,name));continue
     joined[ch['id']]=0;send(s,':%s!%s@host JOIN %s'%(nick,u['username'],name));send(s,':irc 353 %s = %s :%s'%(nick,name,nick));send(s,':irc 366 %s %s :End of NAMES list'%(nick,name))
     c=db();head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch['id'],)).fetchone()[0];joined[ch['id']]=head;c.close()
    elif cmd=='PRIVMSG':
     name,body=(rest.split(' :',1)+[''])[:2] if ' :' in rest else (rest.split()[0],'')
     ch=chan_for(name) if name else None
     if ch and body:
      c=db();c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch['id'],u['id'],body,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z')));mid=c.execute('select last_insert_rowid()').fetchone()[0];n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch['id'],)).fetchone()[0];c.execute('insert into events values(?,?,?,?,?)',(ch['id'],n,'message.created','{\"type\":\"message.created\",\"channel_id\":%d,\"message\":{\"id\":%d,\"body\":%s},\"event_id\":%d}'%(ch['id'],mid,__import__('json').dumps(body),n),''));c.commit();c.close()
    elif cmd=='QUIT':alive=False;break
    elif cmd=='NAMES':
     target=rest.split()[0] if rest else '';send(s,':irc 353 %s = %s :%s'%(nick,target,nick));send(s,':irc 366 %s %s :End of NAMES list'%(nick,target))
    elif cmd=='WHO':
     target=rest.split()[0] if rest else '*';send(s,':irc 315 %s %s :End of WHO list'%(nick,target))
    elif cmd=='LIST':
     send(s,':irc 321 %s Channel :Users Name'%nick);send(s,':irc 323 %s :End of LIST'%nick)
    elif cmd=='TOPIC':
     target=rest.split()[0] if rest else '';send(s,':irc 331 %s %s :No topic is set'%(nick,target))
    elif cmd=='MODE':
     target=rest.split()[0] if rest else '';send(s,':irc 324 %s %s +nt'%(nick,target))
    else:send(s,':irc 421 %s %s :Unknown command'%(nick,cmd))
 except:pass
 finally:
  alive=False
  with lock:
   for x in list(clients):
    if x[0] is s:
     clients.remove(x)
     if len(x)>2 and x[2]: nicks.discard(x[2])
  try:s.close()
  except:pass
def main():
 q=socket.socket();q.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);q.bind(('0.0.0.0',6667));q.listen()
 while 1:
  s,a=q.accept();threading.Thread(target=loop,args=(s,a),daemon=True).start()
if __name__=='__main__':main()
