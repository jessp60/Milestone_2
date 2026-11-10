import socket,threading,sqlite3,json,time,re,datetime,os
DB='/app/huddle.db'; clients=[]; nicks=set(); lk=threading.RLock()
def db():
 c=sqlite3.connect(DB,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def q(sql,a=(),one=False):
 with lk:
  c=db(); x=c.execute(sql,a); r=x.fetchone() if one else x.fetchall(); c.commit(); c.close(); return r
def ts(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def send(c,x):
 try:c.sendall((x+'\r\n').encode())
 except: pass
def channel(name):
 m=re.match(r'^#([^/]+)/([^/]+)$',name)
 if not m:return None
 return q('select c.* from channels c join workspaces w on w.id=c.ws where w.slug=? and c.name=?',(m.group(1),m.group(2)),True)
def cname(ch):
 w=q('select slug from workspaces where id=?',(ch['ws'],),True); return '#%s/%s'%(w['slug'],ch['name'])
def event(ch,kind,mid):
 e=q('select coalesce(max(event),0)+1 n from events where ch=?',(ch,),True)['n']; q('insert into events(ch,event,kind,mid) values(?,?,?,?)',(ch,e,kind,mid)); q('update messages set event=? where id=?',(e,mid))
def client(s,addr):
 global nicks
 nick=None; uid=None; registered=False; user_seen=False; joined=set(); last=0
 with lk:
  for box in clients:
   if box[0] is s: joined=box[1]; break
 def prefix(): return '%s!%s@localhost'%(nick or '*',uid or 'user')
 while True:
  try:
   line=s.recv(4096)
   if not line:return
   for raw in line.decode(errors='ignore').splitlines():
    z=raw.strip(); parts=z.split(); cmd=parts[0].upper() if parts else ''
    if cmd=='PASS':
     t=parts[1] if len(parts)>1 else ''; u=q('select uid from tokens where token=?',(t,),True)
     if not u: send(s,':irc 464 * :Password incorrect'); continue
     uid=u['uid']
    elif cmd=='NICK':
     n=parts[1] if len(parts)>1 else ''
     if n in nicks: send(s,':irc 433 * %s :Nickname in use'%n); continue
     nick=n
     with lk:
      nicks.add(n)
     with lk:
      for box in clients:
       if box[0] is s:
        if len(box)<3: box.append(n)
        else: box[2]=n
        break
     if uid and user_seen and not registered:
      registered=True
      for code in ('001','002','003','004','005'): send(s,':irc %s %s :Huddle'%(code,nick))
      send(s,':irc 422 %s :MOTD File is missing'%nick)
    elif cmd=='USER':
     user_seen=True
     if uid and nick and not registered:
      registered=True
      for n in ('001','002','003','004','005'): send(s,':irc %s %s :Huddle'% (n,nick))
      send(s,':irc 422 %s :MOTD File is missing'%nick)
    elif cmd=='PING': send(s,'PONG :'+(z.split(':',1)[1] if ':' in z else (parts[1] if len(parts)>1 else '')))
    elif cmd=='JOIN':
     ch=channel(parts[1] if len(parts)>1 else '')
     if not ch: send(s,':irc 403 %s %s :No such channel'%(nick,parts[1] if len(parts)>1 else '')); continue
     joined.add(ch['id']); send(s,':%s JOIN %s'%(prefix(),cname(ch)))
     names=[q('select username from users where id=?',(x['uid'],),True)['username'] for x in q('select uid from cmembers where ch=?',(ch['id'],))]
     if nick not in names:names.append(nick)
     send(s,':irc 353 %s = %s :%s'%(nick,cname(ch),' '.join(names))); send(s,':irc 366 %s %s :End of /NAMES list.'%(nick,cname(ch)))
    elif cmd=='PRIVMSG' and len(parts)>=3:
     ch=channel(parts[1]); body=z.split(':',1)[1] if ':' in z else ' '.join(parts[2:])
     if ch and ch['id'] in joined:
      q('insert into messages(ch,uid,body,created,parent,fileids,event,mentions) values(?,?,?,?,?,?,?,?)',(ch['id'],uid,body,ts(),None,'[]',0,'[]'))
      m=q('select * from messages where ch=? and uid=? order by id desc limit 1',(ch['id'],uid),True); event(ch['id'],'message.created',m['id'])
    elif cmd=='QUIT':
     try:s.shutdown(socket.SHUT_RDWR)
     except:pass
     try:s.close()
     except:pass
     return
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
     if cmd=='NAMES' and len(parts)>1:
      ch=channel(parts[1]); send(s,':irc 353 %s = %s :%s'%(nick,parts[1],nick)); send(s,':irc 366 %s %s :End of /NAMES list.'%(nick,parts[1]))
    elif cmd and cmd not in ('USER',):
     send(s,':irc 421 %s %s :Unknown command'%(nick,cmd))
  except:return
 try:s.close()
 except:pass
 with lk:
  for x in list(clients):
   if x[0] is s:clients.remove(x)
  if nick:nicks.discard(nick)
def broadcaster():
 last=0
 while True:
  try:
   for e in q('select rowid,* from events where rowid>? order by rowid',(last,)):
    last=e['rowid']; ch=q('select * from channels where id=?',(e['ch'],),True)
    if not ch:continue
    body=q('select body,uid from messages where id=?',(e['mid'],),True)
    if not body:continue
    u=q('select username from users where id=?',(body['uid'],),True); line=':%s!%s@localhost PRIVMSG %s :%s'%(u['username'],u['username'],cname(ch),body['body'])
    for box in list(clients):
     if ch['id'] in box[1]:send(box[0],line)
  except:pass
  time.sleep(.03)
def main():
 srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen(100)
 threading.Thread(target=broadcaster,daemon=True).start()
 while True:
  s,a=srv.accept(); box=[s,set()]
  with lk: clients.append(box)
  # wrapper tracks joins by sharing box
  threading.Thread(target=client,args=(s,a),daemon=True).start()
if __name__=='__main__':main()
