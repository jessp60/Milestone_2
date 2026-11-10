import socket,threading,time,re,sys,os,fcntl
sys.path.insert(0,'/app')
import app
app.init()
clients=[]; mu=threading.RLock()
def send(c,line):
 try:c.sendall((line+'\r\n').encode())
 except:pass
def find_channel(name):
 m=re.fullmatch(r'#([^/]+)/([^/]+)',name)
 if not m:return None
 return app.q('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(m.group(1),m.group(2)),True)
def broadcast(cid,line):
 with mu:
  for x in list(clients):
   if cid in x['joins']:send(x['sock'],line)
def bridge():
 seen={}
 while True:
  try:
   rows=app.q("SELECT e.*,m.body,u.username,c.name,w.slug FROM channel_events e JOIN messages m ON m.id=e.message_id JOIN users u ON u.id=m.author_id JOIN channels c ON c.id=e.channel_id JOIN workspaces w ON w.id=c.workspace_id WHERE e.kind='message.created' ORDER BY e.id")
   for r in rows:
    if seen.get(r['channel_id'],0)>=r['event_id']:continue
    seen[r['channel_id']]=r['event_id']
    broadcast(r['channel_id'],':%s!%s@localhost PRIVMSG #%s/%s :%s'%(r['username'],r['username'],r['slug'],r['name'],r['body']))
  except:pass
  time.sleep(.25)
def session(sock,addr):
 x={'sock':sock,'token':None,'uid':None,'nick':None,'registered':False,'joins':set()}
 with mu:clients.append(x)
 buf=b''
 try:
  while True:
   d=sock.recv(4096)
   if not d:break
   buf+=d
   while b'\r\n' in buf:
    raw,buf=buf.split(b'\r\n',1); line=raw.decode(errors='ignore'); parts=line.split(); cmd=parts[0].upper() if parts else ''
    if cmd=='PASS':
     tok=line[5:].strip(); x['token']=tok
     if not app.q('SELECT user_id FROM tokens WHERE token=?',(tok,),True):send(sock,':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     nick=parts[1] if len(parts)>1 else ''
     if any(z.get('nick')==nick and z is not x and z.get('registered') for z in clients):send(sock,':huddle 433 * %s :Nickname in use'%nick);continue
     x['nick']=nick
     if x['uid'] and not x['registered']:
      x['registered']=True
      for n in ('001','002','003','004','005'):send(sock,':huddle %s %s :Welcome'%(n,x['nick']))
      send(sock,':huddle 422 %s :MOTD File is missing'%x['nick'])
    elif cmd=='USER':
     if x['token'] and not x['uid']:
      r=app.q('SELECT user_id FROM tokens WHERE token=?',(x['token'],),True)
      if r:x['uid']=r['user_id']
     if x['uid'] and x['nick'] and not x['registered'] and not any(z is not x and z.get('registered') and z.get('nick')==x['nick'] for z in clients):
      x['registered']=True
      for n in ('001','002','003','004','005'):send(sock,':huddle %s %s :Welcome'%(n,x['nick']))
      send(sock,':huddle 422 %s :MOTD File is missing'%x['nick'])
    elif not x['registered'] and cmd not in ('PASS','NICK','USER'):send(sock,':huddle 451 * :You have not registered')
    elif cmd=='PING':send(sock,':huddle PONG huddle :'+line.split(':',1)[-1])
    elif cmd=='PONG':pass
    elif cmd=='JOIN':
     name=parts[1] if len(parts)>1 else ''; ch=find_channel(name)
     if not ch:send(sock,':huddle 403 %s %s :No such channel'%(x['nick'],name));continue
     x['joins'].add(ch['id']);send(sock,':%s!%s@localhost JOIN :%s'%(x['nick'],x['nick'],name))
     names=[z['username'] for z in app.q('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))]
     send(sock,':huddle 353 %s = %s :%s'%(x['nick'],name,' '.join(names)));send(sock,':huddle 366 %s %s :End of /NAMES list.'%(x['nick'],name))
    elif cmd=='PRIVMSG':
     name=parts[1] if len(parts)>1 else ''; body=line.split(' :',1)[1] if ' :' in line else ''
     ch=find_channel(name)
     if ch and body:
      lf=open('/app/event.lock','w');fcntl.flock(lf,fcntl.LOCK_EX)
      try:
       eid=app.q('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(ch['id'],),True)['n']
       mid=app.run('INSERT INTO messages(channel_id,author_id,body,created_at,event_id) VALUES(?,?,?,?,?)',(ch['id'],x['uid'],body,app.now(),eid))
       app.run('INSERT INTO channel_events(channel_id,event_id,kind,message_id,payload) VALUES(?,?,?,?,?)',(ch['id'],eid,'message.created',mid,''))
      finally:
       fcntl.flock(lf,fcntl.LOCK_UN);lf.close()
    elif cmd=='QUIT':
     return
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     target=parts[1] if len(parts)>1 else '*'
     if cmd=='NAMES':send(sock,':huddle 366 %s %s :End of /NAMES list.'%(x['nick'],target))
     elif cmd=='TOPIC':send(sock,':huddle 331 %s %s :No topic is set'%(x['nick'],target))
     elif cmd=='WHO':send(sock,':huddle 315 %s %s :End of /WHO list.'%(x['nick'],target))
     elif cmd=='LIST':send(sock,':huddle 323 %s :End of /LIST'%(x['nick']))
     elif cmd=='MODE':send(sock,':huddle 324 %s %s +'%(x['nick'],target))
    else:send(sock,':huddle 421 %s %s :Unknown command'%(x['nick'] or '*',cmd))
 finally:
  with mu:
   if x in clients:clients.remove(x)
  try:sock.close()
  except:pass
def main():
 threading.Thread(target=bridge,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  c,a=s.accept();threading.Thread(target=session,args=(c,a),daemon=True).start()
if __name__=='__main__':main()
