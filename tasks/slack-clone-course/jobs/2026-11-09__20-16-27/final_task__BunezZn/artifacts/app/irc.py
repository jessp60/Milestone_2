#!/usr/bin/env python3
import socket,threading,sqlite3,json,time,os,re
DB='/app/huddle.db'; clients=[]; lock=threading.RLock()
def db():
    c=sqlite3.connect(DB,timeout=2,isolation_level=None); c.row_factory=sqlite3.Row; return c
def send(c,line):
    try:c.s.sendall((line+'\r\n').encode())
    except: pass
def lookup(name):
    c=db(); r=c.execute("select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where '#'||w.slug||'/'||ch.name=?",(name,)).fetchone(); c.close(); return r
class Client:
    def __init__(self,s):
        self.s=s; self.buf=b''; self.token=None; self.uid=None; self.nick=None; self.reg=False; self.joined={}
    def run(self):
        global clients
        with lock: clients.append(self)
        try:
            while True:
                x=self.s.recv(4096)
                if not x: break
                self.buf+=x
                while b'\r\n' in self.buf:
                    line,self.buf=self.buf.split(b'\r\n',1); self.cmd(line.decode(errors='ignore'))
        except: pass
        finally:
            with lock:
                if self in clients: clients.remove(self)
            try:self.s.close()
            except:pass
    def numeric(self,n,text=''):
        send(self,':huddle '+n+' '+(self.nick or '*')+' '+text)
    def cmd(self,line):
        if not line:return
        a=line.split(' ',1); cmd=a[0].upper(); rest=a[1] if len(a)>1 else ''
        if cmd=='PASS':
            self.token=rest.strip(); c=db(); r=c.execute('select user_id from tokens where token=?',(self.token,)).fetchone(); c.close()
            if not r:self.numeric('464','Password incorrect')
            else:self.uid=r['user_id']
        elif cmd=='NICK':
            nick=rest.strip()
            with lock: used=any(x.nick==nick and x is not self for x in clients)
            if used:self.numeric('433',nick+' Nickname in use'); return
            self.nick=nick
            if self.uid and not self.reg:self.finish()
        elif cmd=='USER':
            if self.uid and self.nick and not self.reg:self.finish()
        elif cmd=='PING': send(self,'PONG :'+rest.lstrip(':'))
        elif cmd=='QUIT': self.s.close(); return
        elif not self.reg:
            if cmd not in ('PASS','NICK','USER'): self.numeric('421',cmd+' Unknown command')
        elif cmd=='JOIN':
            name=rest.strip(); ch=lookup(name)
            if not ch:self.numeric('403',name+' No such channel'); return
            self.joined[name]=ch['id']; send(self,':%s!%s@host JOIN %s'%(self.nick,self.uid,name))
            self.numeric('353','= %s :%s'%(name,self.nick)); self.numeric('366',name+' End of NAMES list')
        elif cmd=='PRIVMSG':
            if ' :' not in rest:return
            name,text=rest.split(' :',1); ch=lookup(name.strip())
            if not ch or name.strip() not in self.joined:self.numeric('404',name+' Cannot send'); return
            c=db(); eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch['id'],)).fetchone()[0]
            r=c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id) values(?,?,?,?,?,?)',(ch['id'],self.uid,text,None,time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime()),eid))
            payload={'message_id':r.lastrowid,'body':text,'author_id':self.uid}
            c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(ch['id'],eid,'message.created',json.dumps(payload),time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime()))); c.commit(); c.close()
        elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): send(self,':huddle 200 %s :OK'%self.nick)
        else:self.numeric('421',cmd+' Unknown command')
    def finish(self):
        self.reg=True
        for n in ('001','002','003','004','005'): self.numeric(n,':Welcome to Huddle IRC')
        self.numeric('422',':MOTD File is missing')
def bridge():
    seen={}
    while True:
        try:
            c=db(); rows=c.execute("select e.*,ch.name,w.slug from events e join channels ch on ch.id=e.channel_id join workspaces w on w.id=ch.workspace_id where e.kind='message.created' order by e.id").fetchall(); c.close()
            with lock: cs=list(clients)
            for r in rows:
                for x in cs:
                    if r['channel_id'] not in x.joined.values(): continue
                    if r['id']<=seen.get(r['channel_id'],0): continue
                    p=json.loads(r['payload']); author=p.get('author_id') or p.get('message',{}).get('author_id','user'); name='#%s/%s'%(r['slug'],r['name']); body=p.get('body') or p.get('message',{}).get('body','')
                    send(x,':%s!%s@host PRIVMSG %s :%s'%(name.split('/')[-1],author,name,body))
                seen[r['channel_id']]=max(seen.get(r['channel_id'],0),r['id'])
        except: pass
        time.sleep(.05)
def main():
    threading.Thread(target=bridge,daemon=True).start()
    z=socket.socket(); z.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); z.bind(('0.0.0.0',6667)); z.listen()
    while True:
        s,_=z.accept(); threading.Thread(target=Client(s).run,daemon=True).start()
if __name__=='__main__': main()
