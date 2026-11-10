#!/usr/bin/env python3
import os,sys,socket,json,re,time,uuid,hashlib,secrets,sqlite3,threading,base64,struct,urllib.parse,datetime
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data.sqlite3'; LOCK=threading.RLock(); OPEN_WS=set()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
    c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; return c
def init():
    for attempt in range(20):
      try:
       with LOCK:
        c=conn()
        c.executescript('''PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT,mention_count INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
      CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
      CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
      CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
      CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
      CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
      CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
      CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
      CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
      CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
      CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
      CREATE TABLE IF NOT EXISTS open_ws(user_id INTEGER PRIMARY KEY,seen_at REAL);
      CREATE TABLE IF NOT EXISTS user_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
        '''); c.commit(); c.close(); return
      except sqlite3.OperationalError as e:
       if 'locked' not in str(e): raise
       time.sleep(0.15)
    raise RuntimeError('database initialization locked')
def uid(): return secrets.token_hex(16)
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channelobj(r):
    d=dict(r)
    for k in ('is_private','is_dm','is_archived'): d[k]=bool(d.get(k))
    return d
def fileobj(r):
 return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def msgobj(c,r):
 if not r:return None
 a=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rs=c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)).fetchall()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fileobj(x) for x in c.execute('select * from files where id in (select file_id from attachments where message_id=?)',(r['id'],))],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[z['user_id'] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]} for x in rs],'mentions':mention_ids(c,r)}
def groupobj(r):
    return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
def mention_ids(c,r):
    if not r: return []
    text=r['body'] or ''; out=[]
    for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',text):
        if name=='here':
            for z in c.execute('select user_id from cmembers where channel_id=?',(r['channel_id'],)).fetchall():
                if c.execute('select 1 from open_ws where user_id=?',(z['user_id'],)).fetchone() and z['user_id']!=r['author_id'] and z['user_id'] not in out: out.append(z['user_id'])
            continue
        if name=='channel': continue
        gr=c.execute('select user_ids from groups_ where handle=? and workspace_id=(select workspace_id from channels where id=?)',(name,r['channel_id'])).fetchone()
        if gr:
            members=json.loads(gr['user_ids'] or '[]')
            for z in c.execute('select user_id from cmembers where channel_id=?',(r['channel_id'],)).fetchall():
                if z['user_id'] in members and z['user_id']!=r['author_id'] and z['user_id'] not in out: out.append(z['user_id'])
            continue
        z=c.execute('select u.id from users u join cmembers m on m.user_id=u.id where m.channel_id=? and u.username=?',(r['channel_id'],name)).fetchone()
        if z and z['id']!=r['author_id'] and z['id'] not in out: out.append(z['id'])
    if '@channel' in text:
        for z in c.execute('select user_id from cmembers where channel_id=?',(r['channel_id'],)).fetchall():
            if z['user_id']!=r['author_id'] and z['user_id'] not in out: out.append(z['user_id'])
    return out
def reaction_list(c,mid):
    out=[]
    for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)):
        ids=[z['user_id'] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,x['emoji']))]
        out.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
    return out
def err(h,code,text): h.sendj(code,{'error':text})
def event(c,ch,typ,payload):
 n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch,)).fetchone()[0]
 c.execute('insert into events values(?,?,?,?,?)',(ch,n,typ,json.dumps(payload),now())); return n
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,code,obj,headers={}):
  b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  for k,v in headers.items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'))
  try:
   self.bad_json=False
   return json.loads(self.rfile.read(n) or b'{}')
  except (ValueError, TypeError):
   self.bad_json=True
   return {}
 def auth(self,required=True):
  x=self.headers.get('Authorization','')
  if not x.startswith('Bearer '): return None
  with LOCK:
   c=conn(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)).fetchone(); c.close()
  return r
 def path(self): return urllib.parse.urlparse(self.path).path
 def do_GET(self):
  p=urllib.parse.urlparse(self.path).path; q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
  if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(os.getenv('NODE_ID','0'))})
  if p=='/' or p.startswith('/static'): return self.ui()
  if p=='/api/ws':
   tok=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
   with LOCK:
    cc=conn(); uu=cc.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(tok,)).fetchone();cc.close()
   if not uu:self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
   key=self.headers.get('Sec-WebSocket-Key',''); import hashlib as _h
   accept=base64.b64encode(_h.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
   with LOCK:
    pc=conn(); pc.execute('insert or replace into open_ws values(?,?)',(uu['id'],time.time())); pc.commit(); pc.close()
   self.connection.settimeout(0.2); sub=None; since=0; user_since=0
   def wf(o):
    z=json.dumps(o).encode(); n=len(z); h=bytes([129,n]) if n<126 else bytes([129,126])+struct.pack('>H',n); self.connection.sendall(h+z)
   try:
    while True:
     try:
      h=self.connection.recv(2)
      if not h: break
      n=h[1]&127; mask=self.connection.recv(4); data=self.connection.recv(n); data=bytes(data[i]^mask[i%4] for i in range(n)); ob=json.loads(data)
      if ob.get('type') in ('subscribe','resume'):
       sub=int(ob['channel_id']); since=int(ob.get('since_event_id',0)); head=conn().execute('select coalesce(max(event_id),0) from events where channel_id=?',(sub,)).fetchone()[0]
       if ob['type']=='subscribe':
        wf({'type':'subscribed','channel_id':sub,'head_event_id':head}); since=head
       else:
        cc=conn(); missed=cc.execute('select * from events where channel_id=? and event_id>? order by event_id',(sub,since)).fetchall()
        for x in missed:
         frame=json.loads(x['payload']); frame.setdefault('event_id',x['event_id']); frame.setdefault('channel_id',x['channel_id']); wf(frame); since=x['event_id']
        cc.close(); wf({'type':'resumed','channel_id':sub,'head_event_id':head})
     except socket.timeout: pass
     cc=conn()
     grows=cc.execute('select * from user_events where id>? order by id',(user_since,)).fetchall()
     for gx in grows:
      wf(json.loads(gx['payload'])); user_since=gx['id']
     cc.close()
     if sub:
      cc=conn(); rows=cc.execute('select * from events where channel_id=? and event_id>? order by event_id',(sub,since)).fetchall()
      for x in rows:
       frame=json.loads(x['payload']); frame.setdefault('event_id',x['event_id']); frame.setdefault('channel_id',x['channel_id']); wf(frame); since=x['event_id']
      cc.close()
   except Exception: pass
   finally:
    with LOCK:
     pc=conn(); pc.execute('update open_ws set seen_at=? where user_id=?',(time.time(),uu['id'])); pc.commit(); pc.close()
    OPEN_WS.discard(uu['id'])
   return
  u=self.auth()
  if p=='/api/auth/me':
   return self.sendj(200,{'user':userobj(u)}) if u else err(self,401,'unauthorized')
  if not u:return err(self,401,'unauthorized')
  with LOCK:
   c=conn()
   m=re.match(r'/api/files/(\d+)(/download)?$',p)
   if m:
    r=c.execute('select * from files where id=?',(m.group(1),)).fetchone()
    if not r:c.close();return err(self,404,'not found')
    if m.group(2):
     b=r['data']; c.close(); self.send_response(200); self.send_header('Content-Type',r['content_type']); self.send_header('Content-Disposition','attachment; filename="'+r['filename'].replace('"','')+'"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
    out={'file':fileobj(r)};c.close();return self.sendj(200,out)
   if p=='/api/search':
    term=q.get('q',[''])[0]; lim=int(q.get('limit',['50'])[0]); off=int(q.get('cursor',['0'])[0] or 0)
    if lim<1 or lim>200:c.close();return err(self,400,'limit')
    wsfilter=q.get('workspace_id',[''])[0]
    if wsfilter:
     rows=c.execute('select m.* from messages m join channels ch on ch.id=m.channel_id join members wm on wm.workspace_id=ch.workspace_id and wm.user_id=? where m.deleted=0 and m.body like ? and ch.workspace_id=? and (ch.is_private=0 or exists (select 1 from cmembers px where px.channel_id=ch.id and px.user_id=?)) order by m.id desc limit ? offset ?',(u['id'],'%'+term+'%',wsfilter,u['id'],lim+1,off)).fetchall()
    else:
     rows=c.execute('select m.* from messages m join channels ch on ch.id=m.channel_id join members wm on wm.workspace_id=ch.workspace_id and wm.user_id=? where m.deleted=0 and m.body like ? and (ch.is_private=0 or exists (select 1 from cmembers px where px.channel_id=ch.id and px.user_id=?)) order by m.id desc limit ? offset ?',(u['id'],'%'+term+'%',u['id'],lim+1,off)).fetchall()
    more=len(rows)>lim; rows=rows[:lim]; c.close(); return self.sendj(200,{'results':[msgobj(conn(),x) for x in rows],'next_cursor':str(off+lim) if more else None})
   m=re.match(r'/api/users/(\d+)$',p)
   if m:
    r=c.execute('select * from users where id=?',(m.group(1),)).fetchone(); c.close(); return self.sendj(200,{'user':userobj(r)}) if r else err(self,404,'not found')
   if p=='/api/workspaces':
    rows=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall(); c.close(); return self.sendj(200,{'workspaces':[dict(x) for x in rows]})
   m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    chans=c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
    chans=[x for x in chans if (not x['is_private'] or c.execute('select 1 from cmembers where channel_id=? and user_id=?',(x['id'],u['id'])).fetchone())]
    c.close();return self.sendj(200,{'channels':[channelobj(x) for x in chans]})
   m=re.match(r'/api/workspaces/([^/]+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    chans=c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
    chans=[x for x in chans if (not x['is_private'] or c.execute('select 1 from cmembers where channel_id=? and user_id=?',(x['id'],u['id'])).fetchone())]
    rs=[dict(x) for x in c.execute('select channel_id,last_event_id as last_read_event_id from reads where user_id=? and channel_id in (select id from channels where workspace_id=?)',(u['id'],w['id']))];c.close();return self.sendj(200,{'workspace':dict(w),'channels':[channelobj(x) for x in chans],'read_state':rs})
   m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():c.close();return err(self,403,'forbidden')
    rows=c.execute('select * from groups_ where workspace_id=?',(w['id'],)).fetchall();c.close();return self.sendj(200,{'groups':[groupobj(x) for x in rows]})
   m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role'] not in ('owner','admin'):c.close();return err(self,403,'forbidden')
    rows=c.execute('select * from invitations where workspace_id=? and revoked=0 and uses<max_uses and (expires_at is null or expires_at>?)',(w['id'],time.time())).fetchall()
    out=[dict(x) for x in rows];c.close();return self.sendj(200,{'invitations':out})
   m=re.match(r'/api/workspaces/([^/]+)/members$',p)
   if m:
    w0=c.execute('select id from workspaces where slug=?',(m.group(1),)).fetchone();
    if not w0:c.close();return err(self,404,'not found')
    rows=c.execute('select m.*,u.username,u.display_name from members m join users u on u.id=m.user_id where m.workspace_id=?',(w0['id'],)).fetchall();c.close();return self.sendj(200,{'members':[dict(x) for x in rows]})
   m=re.match(r'/api/channels/(\d+)/read$',p)
   if m:
    ch=int(m.group(1)); cr=c.execute('select id from channels where id=?',(ch,)).fetchone()
    if not cr:c.close();return err(self,404,'not found')
    rs=c.execute('select last_event_id from reads where user_id=? and channel_id=?',(u['id'],ch)).fetchone(); last=rs['last_event_id'] if rs else 0
    mc=sum(u['id'] in mention_ids(c,x) for x in c.execute('select * from messages where channel_id=? and deleted=0',(ch,))); st={'channel_id':ch,'last_read_event_id':last,'unread_count':0,'mention_count':mc};c.close()
    if self.command=='POST':
     d=self.body(); last=max(last,int(d.get('last_read_event_id',0))); c.execute('insert or replace into reads values(?,?,?)',(u['id'],ch,last)); c.commit(); mc=sum(u['id'] in mention_ids(c,x) for x in c.execute('select * from messages where channel_id=? and deleted=0',(ch,))); c.close(); return self.sendj(200,{'read_state':{'channel_id':ch,'last_read_event_id':last,'unread_count':0,'mention_count':mc}})
    return self.sendj(200,{'read_state':st})
   m=re.match(r'/api/channels/(\d+)/members$',p)
   if m:
    if not c.execute('select id from channels where id=?',(m.group(1),)).fetchone(): c.close(); return err(self,404,'not found')
    rows=c.execute('select u.* from cmembers m join users u on u.id=m.user_id where m.channel_id=?',(m.group(1),)).fetchall();c.close();return self.sendj(200,{'members':[userobj(x) for x in rows]})
   m=re.match(r'/api/channels/(\d+)/messages$',p)
   if m:
    ch=int(m.group(1));
    if not c.execute('select id from channels where id=?',(ch,)).fetchone(): c.close(); return err(self,404,'not found')
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,u['id'])).fetchone(): c.close(); return err(self,403,'forbidden')
    lim=int(q.get('limit',['50'])[0]); cur=q.get('cursor',[None])[0]
    if lim<1 or lim>200:return err(self,400,'limit')
    last=None
    if cur:
     try: last=int(base64.urlsafe_b64decode(cur+'=='))
     except: c.close();return err(self,400,'cursor')
    if last is None: rows=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc limit ?',(ch,lim+1)).fetchall()
    else: rows=c.execute('select * from messages where channel_id=? and id<? and deleted=0 and parent_id is null order by id desc limit ?',(ch,last,lim+1)).fetchall()
    more=len(rows)>lim; rows=rows[:lim]; nxt=base64.urlsafe_b64encode(str(rows[-1]['id']).encode()).decode().rstrip('=') if more and rows else None; out={'messages':[msgobj(c,x) for x in rows],'next_cursor':nxt};c.close();return self.sendj(200,out)
   m=re.match(r'/api/messages/(\d+)/replies$',p)
   if m:
    if not c.execute('select id from messages where id=?',(m.group(1),)).fetchone(): c.close(); return err(self,404,'not found')
    lim=int(q.get('limit',['50'])[0]); off=int(q.get('cursor',['0'])[0] or 0); rows=c.execute('select * from messages where parent_id=? and deleted=0 order by id limit ? offset ?',(m.group(1),lim+1,off)).fetchall();more=len(rows)>lim;rows=rows[:lim];o={'replies':[msgobj(c,x) for x in rows],'next_cursor':str(off+lim) if more else None};c.close();return self.sendj(200,o)
   m=re.match(r'/api/channels/(\d+)/pins',p)
   if m:
    ch=int(m.group(1)); cr=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not cr:c.close();return err(self,404,'not found')
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,u['id'])).fetchone():c.close();return err(self,403,'forbidden')
    lim=int(q.get('limit',['50'])[0]); off=int(q.get('cursor',['0'])[0] or 0)
    rows=c.execute('select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=? order by p.message_id desc limit ? offset ?',(ch,lim+1,off)).fetchall();more=len(rows)>lim;rows=rows[:lim]
    out=[{'message':msgobj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows];c.close();return self.sendj(200,{'pins':out,'next_cursor':str(off+lim) if more else None})
   c.close()
  return err(self,404,'not found')
 def do_POST(self):
  p=urllib.parse.urlparse(self.path).path; u=self.auth()
  if p=='/api/auth/register' or p=='/api/auth/login':
   d=self.body()
   if getattr(self,'bad_json',False): return err(self,400,'invalid json')
   name=d.get('username','')
   if p.endswith('register'):
    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(d.get('password',''))<8:return err(self,400,'invalid')
    with LOCK:
     c=conn()
     try:c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(name,d['password'],d.get('display_name',name),'UTC','','','')); c.commit()
     except sqlite3.IntegrityError:c.close();return err(self,409,'duplicate')
     uid_=c.execute('select last_insert_rowid()').fetchone()[0];tok=uid();c.execute('insert into tokens values(?,?)',(tok,uid_));c.commit();r=c.execute('select * from users where id=?',(uid_,)).fetchone();c.close();return self.sendj(201,{'user':userobj(r),'token':tok})
   with LOCK:
    c=conn();r=c.execute('select * from users where username=? and password=?',(name,d.get('password'))).fetchone()
    if not r:c.close();return err(self,401,'invalid')
    tok=uid();c.execute('insert into tokens values(?,?)',(tok,r['id']));c.commit();c.close();return self.sendj(200,{'user':userobj(r),'token':tok})
  if not u:return err(self,401,'unauthorized')
  if p=='/api/files':
   raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); ct=self.headers.get('Content-Type','')
   m=re.search(r'boundary=([^;]+)',ct)
   if not m:return err(self,400,'file')
   boundary=('--'+m.group(1).strip('"')).encode(); part=next((x for x in raw.split(boundary) if b'filename=' in x),None)
   if not part:return err(self,400,'file')
   hm, sep, data=part.partition(b'\r\n\r\n'); data=data[:-2] if data.endswith(b'\r\n') else data; fn=re.search(br'filename="([^"]*)"',hm); typ=re.search(br'Content-Type:\s*([^\r\n]+)',hm,re.I)
   if not fn:return err(self,400,'file')
   if len(data)>10*1024*1024:return err(self,413,'too large')
   filename=fn.group(1).decode(errors='replace'); content_type=(typ.group(1).decode().strip() if typ else 'application/octet-stream')
   with LOCK:
    c=conn(); c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(u['id'],filename,content_type,len(data),data,now())); c.commit(); r=c.execute('select * from files where id=?',(c.execute('select last_insert_rowid()').fetchone()[0],)).fetchone(); c.close()
   return self.sendj(201,{'file':fileobj(r)})
  d=self.body()
  if getattr(self,'bad_json',False): return err(self,400,'invalid json')
  with LOCK:
   c=conn()
   m=re.match(r'/api/workspaces/([^/]+)/invitations$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role'] not in ('owner','admin'):c.close();return err(self,403,'forbidden')
    code=secrets.token_urlsafe(12); target=d.get('invited_user_id')
    if not target and d.get('invited_username'):
     z=c.execute('select id from users where username=?',(d['invited_username'],)).fetchone(); target=z['id'] if z else None
    exp=time.time()+int(d.get('expires_in_seconds',604800)); mx=1 if target else max(1,int(d.get('max_uses',1)))
    c.execute('insert into invitations values(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],target,exp,mx,0,0));c.commit();r=dict(c.execute('select * from invitations where code=?',(code,)).fetchone());c.close();return self.sendj(201,{'invitation':r})
   m=re.match(r'/api/invitations/([^/]+)/accept$',p)
   if m:
    inv=c.execute('select * from invitations where code=?',(m.group(1),)).fetchone()
    if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires_at'] and inv['expires_at']<time.time()):c.close();return err(self,404,'not found')
    if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:c.close();return err(self,403,'forbidden')
    c.execute('insert or replace into members values(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.execute('update invitations set uses=uses+1 where code=?',(inv['code'],));c.commit();c.close();return self.sendj(200,{'accepted':True})
   m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    if w['owner_id']!=u['id']:c.close();return err(self,403,'forbidden')
    target=c.execute('select * from members where workspace_id=? and user_id=?',(w['id'],d.get('new_owner_id'))).fetchone()
    if not target or target['role']!='admin':c.close();return err(self,400,'target')
    c.execute('update workspaces set owner_id=? where id=?',(d['new_owner_id'],w['id']))
    c.execute('update members set role=? where workspace_id=? and user_id=?',('admin',w['id'],u['id']))
    c.execute('update members set role=? where workspace_id=? and user_id=?',('owner',w['id'],d['new_owner_id']))
    c.commit();c.close();return self.sendj(200,{'transferred':True})
   if p=='/api/dms':
    other=c.execute('select id from users where id=?',(d.get('user_id'),)).fetchone()
    if not other:c.close();return err(self,404,'not found')
    existing=c.execute('select ch.id from channels ch join cmembers a on a.channel_id=ch.id join cmembers b on b.channel_id=ch.id where ch.is_dm=1 and a.user_id=? and b.user_id=?',(u['id'],other['id'])).fetchone()
    if existing:c.close();return self.sendj(200,{'channel_id':existing['id']})
    c.execute('insert into channels(workspace_id,name,is_dm,is_private) values(?,?,1,1)',(0,'dm-'+str(min(u['id'],other['id']))+'-'+str(max(u['id'],other['id'])))); cid=c.execute('select last_insert_rowid()').fetchone()[0]
    c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.execute('insert into cmembers values(?,?)',(cid,other['id']));c.commit();c.close();return self.sendj(200,{'channel_id':cid})
   m=re.match(r'/api/workspaces/([^/]+)/groups$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    mem=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:c.close();return err(self,404,'not found')
    if not mem:c.close();return err(self,403,'forbidden')
    if not d.get('handle') or not d.get('name'):c.close();return err(self,400,'missing')
    if c.execute('select 1 from groups_ where workspace_id=? and handle=?',(w['id'],d['handle'])).fetchone():c.close();return err(self,409,'duplicate')
    ids=d.get('user_ids') or []; c.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],d['handle'],d['name'],u['id'],json.dumps(ids)));c.commit();r=c.execute('select * from groups_ where id=?',(c.execute('select last_insert_rowid()').fetchone()[0],)).fetchone();c.close();return self.sendj(201,{'group':groupobj(r)})
   if p=='/api/workspaces':
    if not d.get('slug') or not d.get('name'):c.close();return err(self,400,'missing')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):c.close();return err(self,400,'slug')
    try:c.execute('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(d['slug'],d['name'],u['id'],'open')); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));ch=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(ch,u['id']));c.commit();w=dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone());g=dict(c.execute('select * from channels where id=?',(ch,)).fetchone());c.close();return self.sendj(201,{'workspace':w,'general_channel':g})
    except sqlite3.IntegrityError:c.close();return err(self,409,'duplicate')
   m=re.match(r'/api/workspaces/([^/]+)/channels$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone(); mem=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:c.close();return err(self,404,'not found')
    if not mem:c.close();return err(self,403,'forbidden')
    if mem['role']=='guest' or (d.get('is_private') and mem['role'] not in ('owner','admin')): c.close(); return err(self,403,'forbidden')
    if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],d.get('name',''))).fetchone(): c.close(); return err(self,409,'duplicate')
    name=d.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(d.get('topic',''))>250:c.close();return err(self,400,'invalid')
    try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.commit();r=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'channel':channelobj(r)})
    except sqlite3.IntegrityError:c.close();return err(self,409,'duplicate')
   m=re.match(r'/api/channels/(\d+)/join$',p)
   if m:
    ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone();w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone() if ch else None
    if not ch:c.close();return err(self,404,'not found')
    wjoin=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone(); existing=c.execute('select 1 from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    role0=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    if role0 and role0['role']=='guest':c.close();return err(self,403,'forbidden')
    if wjoin['join_mode']=='invite_only' and not existing:c.close();return err(self,403,'forbidden')
    if ch['is_private'] and not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone(): c.close();return err(self,403,'forbidden')
    c.execute('insert or ignore into members values(?,?,?)',(w['id'],u['id'],'member'));c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],u['id']))
    eid=event(c,ch['id'],'member.joined',{'type':'member.joined','channel_id':ch['id'],'user_id':u['id']})
    c.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'type':'member.joined','channel_id':ch['id'],'user_id':u['id'],'event_id':eid}),ch['id'],eid))
    c.commit();c.close();return self.sendj(200,{'channel':channelobj(ch)})
   m=re.match(r'/api/channels/(\d+)/(leave|archive|unarchive)$',p)
   if m:
    ch=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
    if not ch:c.close();return err(self,404,'not found')
    w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
    if m.group(2)=='leave':
     c.execute('delete from cmembers where channel_id=? and user_id=?',(ch['id'],u['id']))
    else:
     if w['owner_id']!=u['id']:c.close();return err(self,403,'forbidden')
     c.execute('update channels set is_archived=? where id=?',(1 if m.group(2)=='archive' else 0,ch['id']))
    typ='member.left' if m.group(2)=='leave' else ('channel.archived' if m.group(2)=='archive' else 'channel.unarchived')
    eid=event(c,ch['id'],typ,{'type':typ,'channel_id':ch['id'],'event_id':0})
    c.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'type':typ,'channel_id':ch['id'],'event_id':eid}),ch['id'],eid))
    c.commit(); r=c.execute('select * from channels where id=?',(ch['id'],)).fetchone(); c.close(); return self.sendj(200,{'channel':channelobj(r)})
   m=re.match(r'/api/channels/(\d+)/messages$',p)
   if m:
    ch=int(m.group(1)); cr=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not cr:c.close();return err(self,404,'not found')
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,u['id'])).fetchone(): c.close();return err(self,403,'forbidden')
    if cr['is_archived'] and not (isinstance(d.get('body'),str) and d.get('body').strip()=='/unarchive'):c.close();return err(self,423,'archived')
    if not d.get('body'):c.close();return err(self,400,'empty')
    body=d['body']
    if body.startswith('/') and not body.startswith('//'):
     parts=body.split(None,1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
     if cmd=='/me' and not arg: c.close();return err(self,400,'text required')
     if cmd not in ('/me','/shrug','/topic','/invite','/archive','/unarchive'):
      c.close();return err(self,400,'unknown command')
     if cmd=='/topic':
      if not arg or len(arg)>250:c.close();return err(self,400,'topic')
      c.execute('update channels set topic=? where id=?',(arg,ch));c.commit(); cr2=dict(c.execute('select * from channels where id=?',(ch,)).fetchone());c.close();return self.sendj(201,{'message':None,'channel':channelobj(cr2)})
     if cmd in ('/archive','/unarchive'):
      ow=c.execute('select owner_id from workspaces where id=?',(cr['workspace_id'],)).fetchone()
      if not ow or ow['owner_id']!=u['id']:c.close();return err(self,403,'forbidden')
      val=1 if cmd=='/archive' else 0;c.execute('update channels set is_archived=? where id=?',(val,ch));c.commit();cr2=dict(c.execute('select * from channels where id=?',(ch,)).fetchone());c.close();return self.sendj(201,{'message':None,'channel':channelobj(cr2)})
     if cmd=='/invite':
      if not arg.startswith('@'):c.close();return err(self,400,'user')
      target=c.execute('select * from users where username=?',(arg[1:].strip(),)).fetchone()
      if not target:c.close();return err(self,404,'user')
      c.execute('insert or ignore into cmembers values(?,?)',(ch,target['id']));c.execute('insert or ignore into members values(?,?,?)',(cr['workspace_id'],target['id'],'member'));c.commit();c.close();return self.sendj(201,{'message':None,'invited_user':userobj(target)})
    file_ids=d.get('file_ids') or []
    for fid in file_ids:
     fr=c.execute('select * from files where id=?',(fid,)).fetchone()
     if not fr or fr['uploader_id']!=u['id'] or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone(): c.close();return err(self,400,'file')
    if body.startswith('/shrug'):body=(body[6:].strip()+' ¯\\_(ツ)_/¯').strip()
    c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,u['id'],body,d.get('parent_id'),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0]; c.executemany('insert into attachments(message_id,file_id) values(?,?)',[(mid,int(fid)) for fid in file_ids]); mo=msgobj(c,c.execute('select * from messages where id=?',(mid,)).fetchone());
    for mentioned in mo['mentions']: c.execute('update users set mention_count=mention_count+1 where id=?',(mentioned,))
    typ='message.reply' if d.get('parent_id') else 'message.created';eid=event(c,ch,typ,{'type':typ,'channel_id':ch,'message':mo,'event_id':eid if False else 0}); c.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'type':typ,'channel_id':ch,'message':mo,'event_id':eid}),ch,eid));c.commit();c.close();return self.sendj(201,{'message':mo})
   m=re.match(r'/api/channels/(\d+)/read$',p)
   if m:
    ch=int(m.group(1)); cr=c.execute('select id from channels where id=?',(ch,)).fetchone()
    if not cr:c.close();return err(self,404,'not found')
    dval=int(d.get('last_read_event_id',0)); old=c.execute('select last_event_id from reads where user_id=? and channel_id=?',(u['id'],ch)).fetchone()
    last=max(dval,old['last_event_id'] if old else 0); c.execute('insert or replace into reads values(?,?,?)',(u['id'],ch,last));c.commit();mc=sum(u['id'] in mention_ids(c,x) for x in c.execute('select * from messages where channel_id=? and deleted=0',(ch,)));c.close();return self.sendj(200,{'read_state':{'channel_id':ch,'last_read_event_id':last,'unread_count':0,'mention_count':mc}})
   m=re.match(r'/api/messages/(\d+)/pin$',p)
   if m:
    r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
    if not r:c.close();return err(self,404,'not found')
    c.execute('insert or ignore into pins values(?,?,?)',(r['id'],u['id'],now()));c.commit();x=c.execute('select * from pins where message_id=?',(r['id'],)).fetchone();c.close();return self.sendj(200,{'pin':dict(x)})
   m=re.match(r'/api/messages/(\d+)/reactions$',p)
   if m:
    if not c.execute('select id from messages where id=? and deleted=0',(m.group(1),)).fetchone(): c.close();return err(self,404,'not found')
    if not d.get('emoji'): c.close();return err(self,400,'emoji')
    c.execute('insert or ignore into reactions values(?,?,?)',(m.group(1),u['id'],d['emoji']))
    rr=c.execute('select channel_id from messages where id=?',(m.group(1),)).fetchone()
    eid=event(c,rr['channel_id'],'reaction.added',{'type':'reaction.added','channel_id':rr['channel_id'],'message_id':int(m.group(1))})
    c.commit(); o=reaction_list(c,m.group(1)); c.close(); return self.sendj(200,{'reactions':o})
   c.close()
  return err(self,404,'not found')
 def do_PATCH(self):
  p=urllib.parse.urlparse(self.path).path;u=self.auth()
  if not u:return err(self,401,'unauthorized')
  d=self.body()
  with LOCK:
   c=conn()
   m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone(); target=c.execute('select * from members where workspace_id=? and user_id=?',(w['id'],m.group(2))) if w else None; target=target.fetchone() if target else None
    if not w or not target:c.close();return err(self,404,'not found')
    actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    role=d.get('role')
    if role=='owner':c.close();return err(self,400,'role')
    if not role or role not in ('admin','member','guest'):c.close();return err(self,400,'role')
    if not actor or actor['role'] not in ('owner','admin') or target['role']=='owner' or (actor['role']=='admin' and target['role']=='admin') or int(m.group(2))==w['owner_id']:c.close();return err(self,403,'forbidden')
    c.execute('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],m.group(2)));c.commit();r=dict(c.execute('select * from members where workspace_id=? and user_id=?',(w['id'],m.group(2))).fetchone());c.close();return self.sendj(200,{'member':r})
   m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone();g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not w or not g:c.close();return err(self,404,'not found')
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):c.close();return err(self,403,'forbidden')
    c.execute('update groups_ set name=coalesce(?,name),user_ids=coalesce(?,user_ids) where id=?',(d.get('name'),json.dumps(d['user_ids']) if 'user_ids' in d else None,g['id']));c.commit();r=c.execute('select * from groups_ where id=?',(g['id'],)).fetchone();c.close();return self.sendj(200,{'group':groupobj(r)})
   m=re.match(r'/api/workspaces/([^/]+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:c.close();return err(self,404,'not found')
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role'] not in ('owner','admin'):c.close();return err(self,403,'forbidden')
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):c.close();return err(self,400,'join_mode')
    c.execute('update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit();r=dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone());c.close();return self.sendj(200,{'workspace':r})
   if p=='/api/users/me':
    if 'timezone' in d and d['timezone'] not in ('UTC','America/New_York','Europe/London'):c.close();return err(self,400,'timezone')
    if len(d.get('display_name',''))>200:c.close();return err(self,400,'length')
    for k in ('display_name','timezone','avatar_url','status_text','status_emoji'):
     if k in d:c.execute('update users set '+k+'=? where id=?',(d[k],u['id']))
    c.commit();r=c.execute('select * from users where id=?',(u['id'],)).fetchone();c.execute('insert into user_events(payload) values(?)',(json.dumps({'type':'user.updated','user':userobj(r)}),));c.commit();c.close();return self.sendj(200,{'user':userobj(r)})
   m=re.match(r'/api/messages/(\d+)$',p)
   if m:
    r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
    if not r:c.close();return err(self,404,'not found')
    if r['deleted']:c.close();return err(self,409,'deleted')
    if r['author_id']!=u['id']:c.close();return err(self,403,'forbidden')
    if not isinstance(d.get('body'),str) or not d['body'].strip():c.close();return err(self,400,'empty')
    c.execute('update messages set body=?,edited_at=? where id=?',(d['body'],now(),r['id']))
    x=msgobj(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone())
    eid=event(c,r['channel_id'],'message.updated',{'type':'message.updated','channel_id':r['channel_id'],'message':x,'event_id':0})
    c.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'type':'message.updated','channel_id':r['channel_id'],'message':x,'event_id':eid}),r['channel_id'],eid))
    c.commit();c.close();return self.sendj(200,{'message':x})
   m=re.match(r'/api/channels/(\d+)$',p)
   if m:
    r=c.execute('select * from channels where id=?',(m.group(1),)).fetchone()
    if not r:c.close();return err(self,404,'not found')
    w=c.execute('select * from workspaces where id=?',(r['workspace_id'],)).fetchone()
    mem=c.execute('select role from members where workspace_id=? and user_id=?',(r['workspace_id'],u['id'])).fetchone()
    if len(d.get('topic',''))>250:c.close();return err(self,400,'topic')
    if (not mem or not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(r['id'],u['id'])).fetchone()) and w['owner_id']!=u['id']:c.close();return err(self,403,'forbidden')
    c.execute('update channels set topic=? where id=?',(d.get('topic',''),m.group(1)))
    eid=event(c,r['id'],'channel.updated',{'type':'channel.updated','channel_id':r['id'],'event_id':0})
    c.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'type':'channel.updated','channel_id':r['id'],'event_id':eid}),r['id'],eid))
    c.commit();r=c.execute('select * from channels where id=?',(m.group(1),)).fetchone();c.close();return self.sendj(200,{'channel':channelobj(r)})
   c.close()
  return err(self,404,'not found')
 def do_DELETE(self):
  p=urllib.parse.urlparse(self.path).path;u=self.auth()
  if not u:return err(self,401,'unauthorized')
  with LOCK:
   c=conn()
   m=re.match(r'/api/messages/(\d+)/pin$',p)
   if m:
    if not c.execute('select id from messages where id=?',(m.group(1),)).fetchone(): c.close(); return err(self,404,'not found')
    if not c.execute('select 1 from pins where message_id=?',(m.group(1),)).fetchone(): c.close(); return self.sendj(200,{'unpinned':True})
    c.execute('delete from pins where message_id=?',(m.group(1),));c.commit();c.close();return self.sendj(200,{'unpinned':True})
   m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone();g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],m.group(2))).fetchone() if w else None
    if not w or not g:c.close();return self.sendj(200,{'deleted':True})
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if g['creator_id']!=u['id'] and (not role or role['role']!='owner'):c.close();return err(self,403,'forbidden')
    c.execute('delete from groups_ where id=?',(g['id'],));c.commit();c.close();return self.sendj(200,{'deleted':True})
   m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    inv=c.execute('select * from invitations where code=? and workspace_id=?',(m.group(2),w['id'] if w else -1)).fetchone() if w else None
    role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w or not inv:c.close();return err(self,404,'not found')
    if not role or role['role'] not in ('owner','admin'):c.close();return err(self,403,'forbidden')
    c.execute('update invitations set revoked=1 where code=?',(inv['code'],));c.commit();c.close();return self.sendj(200,{'deleted':True})
   m=re.match(r'/api/messages/(\d+)$',p)
   if m:
    r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
    if not r:c.close();return err(self,404,'not found')
    w=c.execute('select owner_id from workspaces where id=(select workspace_id from channels where id=?)',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):c.close();return err(self,403,'forbidden')
    c.execute('update messages set deleted=1 where id=?',(r['id'],))
    eid=event(c,r['channel_id'],'message.deleted',{'type':'message.deleted','channel_id':r['channel_id'],'message_id':r['id'],'event_id':0})
    c.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'type':'message.deleted','channel_id':r['channel_id'],'message_id':r['id'],'event_id':eid}),r['channel_id'],eid))
    c.commit();c.close();return self.sendj(200,{'deleted':True})
   m=re.match(r'/api/messages/(\d+)/reactions/(.*)$',p)
   if m:
    c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(m.group(1),u['id'],urllib.parse.unquote(m.group(2))))
    rr=c.execute('select channel_id from messages where id=?',(m.group(1),)).fetchone()
    if not rr:c.close();return err(self,404,'not found')
    eid=event(c,rr['channel_id'],'reaction.removed',{'type':'reaction.removed','channel_id':rr['channel_id'],'message_id':int(m.group(1))})
    c.commit(); o=reaction_list(c,m.group(1)); c.close(); return self.sendj(200,{'reactions':o})
   c.close()
  return err(self,404,'not found')
 def ui(self):
  b='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;background:#f8f8f8;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:4px}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f40;color:white;padding:18px}.main{flex:1;padding:30px}.modal{padding:20px;background:white}.hidden{display:none}</style></head><body><div id=app class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><button data-testid=logout-btn data-button-role=danger>Logout</button><div data-testid=channel-list></div></aside><main class=main><h1 data-testid=channel-title>#general</h1><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input name=message><button data-testid=send-btn data-button-role=primary>Send</button></main></div><div id=auth-modal><form data-testid=auth-form><input name=username><input name=password type=password><button data-testid=auth-submit data-button-role=primary>Login</button><button type=button data-testid=auth-toggle>Sign up</button><div id=auth-error></div></form></div><script>localStorage.getItem('huddle.token');</script><section id=static-hooks style="display:none">
<form data-testid=workspace-create-form></form><button data-testid=empty-state-create-workspace></button>
<form data-testid=join-workspace-form></form><button data-testid=join-workspace-submit data-button-role=primary></button>
<div data-testid=dms-list></div><button data-testid=new-channel-btn data-button-role=secondary></button>
<button data-testid=workspace-settings-btn data-button-role=secondary></button><button data-testid=workspace-settings-close data-button-role=secondary></button>
<input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary></button>
<button data-testid=channel-settings-btn data-button-role=secondary></button><h2 data-testid=channel-settings-title></h2><button data-testid=channel-settings-close data-button-role=secondary></button>
<div data-testid=channel-members-list></div><form data-testid=channel-add-member-form></form><input data-testid=channel-add-member-input><button data-testid=channel-add-member-submit data-button-role=primary></button>
<input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary></button>
<button data-testid=archive-channel-btn data-button-role=danger></button><button data-testid=unarchive-channel-btn data-button-role=secondary></button>
<form data-testid=create-channel-form></form><input data-testid=create-channel-private type=checkbox><button data-testid=create-channel-submit data-button-role=primary></button><button data-testid=create-channel-cancel data-button-role=secondary></button>
<aside data-testid=thread-panel></aside><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary></button><button data-testid=close-thread data-button-role=secondary></button>
<button data-testid=settings-tab-members></button><button data-testid=settings-tab-invitations></button><button data-testid=settings-tab-general></button>
<div data-testid=settings-pane-members></div><div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div>
<button data-testid=create-invitation-btn data-button-role=secondary></button><form id=create-invitation-form data-testid=create-invitation-form></form><button data-testid=create-invitation-submit data-button-role=primary></button><div data-testid=invitations-list></div>
<div id=create-channel-modal></div><div id=channel-settings-modal></div><div id=workspace-settings-modal></div>
</section></body></html>'''
  x=b.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(x)));self.end_headers();self.wfile.write(x)
if __name__=='__main__':
 init(); port=int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
