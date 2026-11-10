import socket,threading,sqlite3,time,os,re
DB='/app/huddle.db'; clients=[]; nickset=set(); claimed=set(); mu=threading.RLock()
def conn():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False);c.row_factory=sqlite3.Row;c.execute('pragma busy_timeout=30000');return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def lookup(name):
 c=conn(); x=c.execute("select c.*,w.slug,w.name as wsname from channels c join workspaces w on w.id=c.workspace_id where w.slug||'/'||c.name=?",(name.lstrip('#'),)).fetchone();c.close();return x
def user_for(tok):
 c=conn();x=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone();c.close();return x
def broadcast(cid,body,nick,uid):
 with mu:
  for x in list(clients):
   if cid in x['channels']:
    send(x['sock'],':%s!%s@host PRIVMSG %s :%s'%(nick,nick,x['names'][cid],body))
def bridge():
 seen={}
 while True:
  try:
   c=conn()
   for ch in c.execute('select id from channels where is_dm=0'):
    cid=ch['id']; last=seen.get(cid,0)
    for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last)):
     seen[cid]=e['event_id']
     if e['kind'] not in ('message.created','message.reply'):continue
     import json
     p=json.loads(e['payload']); m=c.execute('select m.body,u.username from messages m join users u on u.id=m.author_id where m.id=?',(p.get('id'),)).fetchone()
     if m:broadcast(cid,m['body'],m['username'],0)
   c.close()
  except:pass
  time.sleep(.03)
def client(sock,addr):
 st={'sock':sock,'channels':{},'names':{},'user':None,'nick':None,'registered':False,'user_seen':False}; buf=b''
 with mu: clients.append(st)
 try:
  while True:
   data=sock.recv(4096)
   if not data: break
   buf+=data
   while b'\r\n' in buf:
    raw,buf=buf.split(b'\r\n',1); line=raw.decode(errors='ignore')
    parts=line.split(' ',2); cmd=parts[0].upper(); arg=parts[1] if len(parts)>1 else ''; rest=parts[2] if len(parts)>2 else ''
    if cmd=='PASS':
     st['user']=user_for(arg)
     if not st['user']: send(sock,':irc 464 * :Password incorrect')
    elif cmd=='NICK':
     nick=arg
     with mu: busy=nick in claimed and nick!=st['nick']
     if busy: send(sock,':irc 433 * %s :Nickname in use'%nick); continue
     with mu:
      if st['nick']: nickset.discard(st['nick'])
      st['nick']=nick; nickset.add(nick); claimed.add(nick)
     if st['user'] and st['user_seen'] and not st['registered']:
      st['registered']=True; n=st['nick']
      for code,text in [('001','Welcome'),('002','Your host'),('003','Server created'),('004','irc irc 1'),('005','CHANTYPES=#'),('422','MOTD File is missing')]:
       send(sock,':irc %s %s %s :%s'%(code,n,n,text))
    elif cmd=='USER':
     st['user_seen']=True
     if st['user'] and st['nick'] and not st['registered']:
      st['registered']=True; n=st['nick']
      for code,text in [('001','Welcome'),('002','Your host'),('003','Server created'),('004','irc irc 1'),('005','CHANTYPES=#'),('422','MOTD File is missing')]:
       send(sock,':irc %s %s %s :%s'%(code,n,n,text))
    elif cmd=='PING':
     tok=(arg+' '+rest).strip(); tok=tok[1:] if tok.startswith(':') else tok
     send(sock,':irc PONG irc :'+tok)
    elif cmd=='JOIN':
     name=arg; ch=lookup(name)
     if not ch: send(sock,':irc 403 %s %s :No such channel'%(st['nick'],name)); continue
     st['channels'][ch['id']]=name; st['names'][ch['id']]=name
     send(sock,':%s!%s@host JOIN %s'%(st['nick'],st['nick'],name))
     send(sock,':irc 353 %s = %s :%s'%(st['nick'],name,st['nick']))
     send(sock,':irc 366 %s %s :End of NAMES list'%(st['nick'],name))
    elif cmd=='PRIVMSG':
     name=arg; body=rest[1:] if rest.startswith(':') else rest; ch=lookup(name)
     if ch and ch['id'] in st['channels'] and body:
      import datetime,json
      c=conn(); ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
      c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch['id'],st['user']['id'],body,ts))
      mid=c.execute('select last_insert_rowid()').fetchone()[0]; e=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch['id'],)).fetchone()[0]
      payload=json.dumps({'id':mid,'channel_id':ch['id'],'author_id':st['user']['id'],'body':body})
      c.execute('insert into events values(?,?,?,?)',(ch['id'],e,'message.created',payload)); c.commit(); c.close()
    elif cmd=='QUIT':
     try: sock.shutdown(socket.SHUT_RDWR)
     except: pass
     break
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','CAP'): pass
    elif cmd not in ('PASS','NICK'): send(sock,':irc 421 %s %s :Unknown command'%(st['nick'] or '*',cmd))
 finally:
  with mu:
   if st in clients: clients.remove(st)
   if st['nick']: nickset.discard(st['nick'])
  try: sock.close()
  except: pass
def main():
 threading.Thread(target=bridge,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  x,a=s.accept();threading.Thread(target=client,args=(x,a),daemon=True).start()
main()
