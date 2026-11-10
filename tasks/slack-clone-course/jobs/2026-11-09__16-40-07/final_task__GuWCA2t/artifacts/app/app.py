import os,sys,json,re,sqlite3,secrets,base64,hashlib,struct,threading,datetime,urllib.parse,socket,time,time
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/huddle.db'; PORT=int(sys.argv[1]); NODE=PORT-8000; clients=[]; irc_clients=[]; ACTIVE='/tmp/huddle_ws_active'
def db():
 c=sqlite3.connect(DB,timeout=20); c.row_factory=sqlite3.Row; return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def user(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def who(t):
 if not t:return None
 with db() as c:return c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone()
def body(h):
 try:return json.loads(h.rfile.read(int(h.headers.get('Content-Length','0')) or 0) or b'{}')
 except:return None
def send(h,n,x,typ='application/json'):
 b=x if isinstance(x,bytes) else json.dumps(x,separators=(',',':')).encode();h.send_response(n);h.send_header('Content-Type',typ);h.send_header('Content-Length',str(len(b)));h.end_headers();h.wfile.write(b)
def bad(h,n='400',x='invalid'):send(h,int(n),{'error':x})
def mobj(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rx=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)).fetchall()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(r['id'],)).fetchall()],'mentions':[x['user_id'] for x in c.execute('SELECT user_id FROM message_mentions WHERE message_id=?',(r['id'],)).fetchall()],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rx]}
def durable(c,ch,typ,mid=None):
 c.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(ch,))
 ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone()['n']
 c.execute('INSERT OR REPLACE INTO events(channel_id,event_id,type,message_id) VALUES(?,?,?,?)',(ch,ev,typ,mid)); c.commit()
 return ev
def event(x,ch=None):
 if x.get('event_id') is not None:return
 raw=json.dumps(x).encode()
 for item in list(clients):
  s,cid=item[0],item[1]
  if cid is None or cid==ch:
   try:
    h=b'\x81'+(bytes([len(raw)]) if len(raw)<126 else b'\x7e'+struct.pack('>H',len(raw)))+raw;s.sendall(h)
   except:pass
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def au(self):
  z=self.headers.get('Authorization','');return who(z[7:] if z.startswith('Bearer ') else '')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path).path;q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
  if p=='/api/health':return send(self,200,{'status':'ok','node_id':NODE})
  if p=='/':return send(self,200,HTML,'text/html')
  if p=='/api/ws':return self.ws(q.get('token',[''])[0])
  u=self.au()
  if p=='/api/auth/me' and not u:return bad(self,401,'authentication required')
  elif not u:return bad(self,401,'authentication required')
  with db() as c:
   if p=='/api/auth/me':return send(self,200,{'user':user(u)})
   z=re.fullmatch(r'/api/users/(\d+)',p)
   if z:
    r=c.execute('SELECT * FROM users WHERE id=?',(z.group(1),)).fetchone();return send(self,200,{'user':user(r)}) if r else bad(self,404,'not found')
   if p=='/api/workspaces':
    r=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();return send(self,200,{'workspaces':[dict(x) for x in r]})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    ar=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return bad(self,404,'not found')
    if not ar or ar['role'] not in ('owner','admin'):return bad(self,403,'forbidden')
    rr=c.execute('SELECT * FROM invites WHERE workspace_id=? AND uses<max_uses',(w['id'],)).fetchall();return send(self,200,{'invitations':[dict(x) for x in rr]})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    mem=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not mem:return bad(self,403,'forbidden')
    rows=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall()
    return send(self,200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rows]})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    cs=c.execute('SELECT * FROM channels WHERE workspace_id=?'+('' if q.get('include_archived',['0'])[0] in ('1','true') else ' AND is_archived=0'),(w['id'],)).fetchall()
    return send(self,200,{'channels':[dict(x,is_private=bool(x['is_private']),is_dm=bool(x['is_dm']),is_archived=bool(x['is_archived'])) for x in cs]})
   z=re.fullmatch(r'/api/workspaces/([^/]+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    include=q.get('include_archived',['0'])[0] in ('1','true'); cs=c.execute('SELECT * FROM channels WHERE workspace_id=?'+('' if include else ' AND is_archived=0'),(w['id'],)).fetchall(); states={}
    for x in cs:
     rr=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone()
     states[str(x['id'])]={'channel_id':x['id'],'last_read_event_id':rr['last_read'] if rr else 0,'unread_count':0,'mention_count':0}
    return send(self,200,{'workspace':dict(w),'channels':[dict(x, is_private=bool(x['is_private']), is_dm=bool(x['is_dm']), is_archived=bool(x['is_archived'])) for x in cs if (not x['is_private'] or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone() or w['owner_id']==u['id'])],'read_state':states})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/members',p)
   if z:
    w=c.execute('SELECT id FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    r=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)).fetchall();return send(self,200,{'members':[dict(x) for x in r]})
   z=re.fullmatch(r'/api/channels/(\d+)/members',p)
   if z:
    if not c.execute('SELECT id FROM channels WHERE id=?',(z.group(1),)).fetchone():return bad(self,404,'not found')
    r=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(z.group(1),)).fetchall();return send(self,200,{'members':[user(x) for x in r]})
   z=re.fullmatch(r'/api/channels/(\d+)/messages',p)
   if z:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(z.group(1),)).fetchone()
    if not ch:return bad(self,404,'not found')
    if ch['is_dm'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return bad(self,403,'forbidden')
    try: lim=int(q.get('limit',['50'])[0])
    except: return bad(self,400,'invalid limit')
    if lim<1 or lim>200:return bad(self,400,'invalid limit')
    cur=q.get('cursor',[None])[0]
    if cur is not None and not cur.isdigit():return bad(self,400,'invalid cursor')
    args=[z.group(1)]; clause=''
    if cur is not None: clause=' AND id < ?';args.append(int(cur))
    args.append(lim+1)
    r=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0'+clause+' ORDER BY id DESC LIMIT ?',args).fetchall()
    nxt=str(r[lim-1]['id']) if len(r)>lim else None
    return send(self,200,{'messages':[mobj(c,x) for x in r[:lim]],'next_cursor':nxt})
   z=re.fullmatch(r'/api/messages/(\d+)/replies',p)
   if z:
    if not c.execute('SELECT id FROM messages WHERE id=?',(z.group(1),)).fetchone():return bad(self,404,'not found')
    try: lim=int(q.get('limit',['50'])[0])
    except: return bad(self,400,'invalid limit')
    if lim<1 or lim>200:return bad(self,400,'invalid limit')
    cur=q.get('cursor',[None])[0]
    if cur is not None and not cur.isdigit():return bad(self,400,'invalid cursor')
    args=[z.group(1)]; clause=''
    if cur is not None: clause=' AND id > ?';args.append(int(cur))
    args.append(lim+1)
    r=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0'+clause+' ORDER BY id LIMIT ?',args).fetchall()
    nxt=str(r[lim-1]['id']) if len(r)>lim else None
    return send(self,200,{'replies':[mobj(c,x) for x in r[:lim]],'next_cursor':nxt})
   z=re.fullmatch(r'/api/channels/(\d+)/pins',p)
   if z:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(z.group(1),)).fetchone()
    if not ch:return bad(self,404,'not found')
    if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return bad(self,403,'forbidden')
    try: lim=int(q.get('limit',['50'])[0])
    except: return bad(self,400,'invalid limit')
    if lim<1 or lim>200:return bad(self,400,'invalid limit')
    cur=q.get('cursor',[None])[0]
    if cur is not None and not cur.isdigit():return bad(self,400,'invalid cursor')
    args=[ch['id']];clause=''
    if cur is not None:clause=' AND p.message_id < ?';args.append(int(cur))
    args.append(lim+1)
    r=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'+clause+' ORDER BY p.message_id DESC LIMIT ?',args).fetchall()
    nxt=str(r[lim-1]['message_id']) if len(r)>lim else None
    return send(self,200,{'pins':[{'message':mobj(c,x),'pinned_by':x['user_id'],'pinned_at':x['pinned_at']} for x in r[:lim]],'next_cursor':nxt})
   z=re.fullmatch(r'/api/channels/(\d+)/read',p)
   if z:
    if not c.execute('SELECT id FROM channels WHERE id=?',(z.group(1),)).fetchone():return bad(self,404,'not found')
    r=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],z.group(1))).fetchone();return send(self,200,{'read_state':{'channel_id':int(z.group(1)),'last_read_event_id':r['last_read'] if r else 0,'unread_count':0,'mention_count':c.execute('SELECT COUNT(*) FROM message_mentions mm JOIN messages m ON m.id=mm.message_id WHERE mm.user_id=? AND m.channel_id=?',(u['id'],z.group(1))).fetchone()[0]}})
   z=re.fullmatch(r'/api/files/(\d+)(/download)?',p)
   if z:
    f=c.execute('SELECT * FROM files WHERE id=?',(z.group(1),)).fetchone()
    if not f:return bad(self,404,'not found')
    if z.group(2):
     self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(f['size']));self.end_headers();self.wfile.write(f['data']);return
    return send(self,200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
   if p.startswith('/api/search'):
    term=q.get('q',[''])[0]
    try: lim=int(q.get('limit',['50'])[0])
    except: return bad(self,400,'invalid limit')
    if lim<1 or lim>200:return bad(self,400,'invalid limit')
    cur=q.get('cursor',[None])[0]
    if cur is not None and not cur.isdigit():return bad(self,400,'invalid cursor')
    wid=q.get('workspace_id',[None])[0]
    params=[f'%{term}%',u['id']]; scope=''
    if wid:scope+=' AND ch.workspace_id=?';params.append(int(wid))
    if cur:scope+=' AND m.id < ?';params.append(int(cur))
    params.append(lim+1)
    r=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? AND EXISTS(SELECT 1 FROM cmembers z WHERE z.channel_id=ch.id AND z.user_id=?)'+scope+' ORDER BY m.id DESC LIMIT ?',params).fetchall()
    nxt=str(r[lim-1]['id']) if len(r)>lim else None
    return send(self,200,{'results':[mobj(c,x) for x in r[:lim]],'next_cursor':nxt})
  return bad(self,404,'not found')
 def do_POST(self):
  p=urllib.parse.urlparse(self.path).path;d=None if p=='/api/files' else body(self)
  if p=='/api/auth/register':
   if not d:return bad(self)
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(str(d.get('password','')))<8:return bad(self)
   try:
    with db() as c:
     cur=c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(d['username'],d['password'],d.get('display_name',d['username']),'UTC','','',''));uid=cur.lastrowid;t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    return send(self,201,{'user':user(r),'token':t})
   except sqlite3.IntegrityError:return bad(self,409,'duplicate')
  if p=='/api/auth/login':
   if not d:return bad(self,400,'invalid json')
   with db() as c:r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
   if not r:return bad(self,401,'invalid credentials')
   t=secrets.token_urlsafe(24)
   with db() as c:c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']))
   return send(self,200,{'user':user(r),'token':t})
  u=self.au()
  if p=='/api/workspaces':
   if not u:return bad(self,401,'authentication required')
   if not d or not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))) or not d.get('name'):return bad(self)
   try:
    with db() as c:
     cur=c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(d['slug'],d['name'],u['id'],'open')); wid=cur.lastrowid
     c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'))
     gc=c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,0,'')",(wid,'general')); gid=gc.lastrowid
     c.execute('INSERT INTO cmembers VALUES(?,?)',(gid,u['id']))
     w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); g=c.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone()
    return send(self,201,{'workspace':dict(w),'general_channel':dict(g)})
   except sqlite3.IntegrityError:return bad(self,409,'duplicate')
  if not u:return bad(self,401,'authentication required')
  if p=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return bad(self,400,'file required')
   import cgi
   form=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':ct,'CONTENT_LENGTH':self.headers.get('Content-Length','0')})
   if 'file' not in form:return bad(self,400,'file required')
   item=form['file']; data=item.file.read()
   if len(data)>10*1024*1024:return bad(self,413,'too large')
   fn=item.filename or 'file'; typ=item.type or 'application/octet-stream'
   with db() as c:
    cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,data,created_at) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),data,now())); fid=cur.lastrowid
   return send(self,201,{'file':{'id':fid,'uploader_id':u['id'],'filename':fn,'content_type':typ,'size':len(data),'created_at':now()}})
  with db() as c:
   z=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return bad(self,403,'forbidden')
    if not d or not d.get('handle') or not d.get('name'):return bad(self,400,'invalid')
    ids=[int(x) for x in d.get('user_ids',[])]
    try:
     cur=c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],d['handle'],d['name'],u['id'],json.dumps(ids)))
     return send(self,201,{'group':{'id':cur.lastrowid,'handle':d['handle'],'name':d['name'],'member_user_ids':ids}})
    except sqlite3.IntegrityError:return bad(self,409,'duplicate')
   z=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone(); a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None; t=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],z.group(2))).fetchone()
    if not w or not t:return bad(self,404,'not found')
    role=(d or {}).get('role')
    if role not in ('owner','admin','member','guest'):return bad(self,400,'invalid role')
    if not a or a['role'] not in ('owner','admin') or role=='owner' or int(z.group(2))==w['owner_id'] or (a['role']=='admin' and t['role']=='admin'):return bad(self,403,'forbidden')
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],z.group(2)));return send(self,200,{'member':{'user_id':int(z.group(2)),'role':role}})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    if w['owner_id']!=u['id']:return bad(self,403,'forbidden')
    tid=int((d or {}).get('new_owner_id',0));t=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],tid)).fetchone()
    if not t or t['role']!='admin':return bad(self,400,'target must be admin')
    c.execute("UPDATE members SET role='admin' WHERE workspace_id=? AND user_id=?",(w['id'],u['id']));c.execute("UPDATE members SET role='owner' WHERE workspace_id=? AND user_id=?",(w['id'],tid));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(tid,w['id']));return send(self,200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone();a=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return bad(self,404,'not found')
    if not a or a['role'] not in ('owner','admin'):return bad(self,403,'forbidden')
    code=secrets.token_urlsafe(12); mx=int((d or {}).get('max_uses',1)); target=(d or {}).get('invited_user_id'); exp=int(time.time())+int((d or {}).get('expires_in_seconds',86400));c.execute('INSERT INTO invites(code,workspace_id,invited_user_id,max_uses,expires) VALUES(?,?,?,?,?)',(code,w['id'],target,1 if target else mx,exp));return send(self,201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0,'expires':exp}})
   z=re.fullmatch(r'/api/invitations/([^/]+)/accept',p)
   if z:
    x=c.execute('SELECT * FROM invites WHERE code=?',(z.group(1),)).fetchone()
    if not x or x['uses']>=x['max_uses'] or x['expires']<int(time.time()):return bad(self,404,'not found')
    if x['invited_user_id'] and x['invited_user_id']!=u['id']:return bad(self,403,'targeted')
    c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(x['code'],));return send(self,200,{'accepted':True})
   z=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
   if z:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(z.group(1),)).fetchone()
    if not ch:return bad(self,404,'not found')
    if z.group(2)=='leave':c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));durable(c,ch['id'],'member.left');return send(self,200,{'channel':dict(ch, is_private=bool(ch['is_private']), is_dm=bool(ch['is_dm']), is_archived=bool(ch['is_archived']))})
    w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if not w or w['owner_id']!=u['id']:return bad(self,403,'forbidden')
    val=1 if z.group(2)=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));durable(c,ch['id'],'channel.archived' if val else 'channel.unarchived');rr=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();return send(self,200,{'channel':dict(rr,is_private=bool(rr['is_private']),is_dm=bool(rr['is_dm']),is_archived=bool(rr['is_archived']))})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
   if z:
    if not d:return bad(self,400,'invalid json')
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not mr or mr['role']=='guest':return bad(self,403,'forbidden')
    if d.get('is_private') and mr['role'] not in ('owner','admin'):return bad(self,403,'forbidden')
    if len(str(d.get('topic','')))>250:return bad(self,400,'topic too long')
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d.get('name'))).fetchone():return bad(self,409,'duplicate')
    if not d or not re.fullmatch(r'[a-z0-9-]{1,32}',str(d.get('name',''))):return bad(self)
    try:
     cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));r=c.execute('SELECT * FROM channels WHERE id=?',(cur.lastrowid,)).fetchone();c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(r['id'],u['id']));return send(self,201,{'channel':dict(r, is_private=bool(r['is_private']), is_dm=bool(r['is_dm']), is_archived=bool(r['is_archived']))})
    except sqlite3.IntegrityError:return bad(self,409,'duplicate')
   z=re.fullmatch(r'/api/channels/(\d+)/join',p)
   if z:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(z.group(1),)).fetchone()
    if not ch:return bad(self,404,'not found')
    if ch['is_private']:return bad(self,403,'private')
    wm=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    wrow=c.execute('SELECT join_mode FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if wm and wm['role']=='guest':return bad(self,403,'forbidden')
    if wrow and wrow['join_mode']=='invite_only' and not wm:return bad(self,403,'invite only')
    c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));durable(c,ch['id'],'member.joined');return send(self,200,{'channel':dict(ch, is_private=bool(ch['is_private']), is_dm=bool(ch['is_dm']), is_archived=bool(ch['is_archived']))})
   z=re.fullmatch(r'/api/channels/(\d+)/messages',p)
   if z:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(z.group(1),)).fetchone()
    if not ch:return bad(self,404,'not found')
    if ch['is_archived'] and str((d or {}).get('body','')).strip()!='/unarchive':return bad(self,423,'archived')
    b=str((d or {}).get('body',''))
    if not b:return bad(self)
    fids=(d or {}).get('file_ids',[])
    for fid in fids:
     f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
     if not f or f['uploader_id']!=u['id'] or f['attached']:return bad(self,400,'invalid file')
    if b.startswith('/') and not b.startswith('//'):
     if b == '/me' or (b.startswith('/me ') and not b[4:].strip()):return bad(self)
     if b.startswith('/shrug'):
      text=b[6:].strip(); b=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
     elif b.startswith('/topic '):
      topic=b[7:].strip()
      if not topic or len(topic)>250:return bad(self)
      member=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(); owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(ch['workspace_id'],u['id'])).fetchone()
      if not member and not owner:return bad(self,403,'forbidden')
      c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));rchan=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();return send(self,201,{'message':None,'channel':dict(rchan,is_private=bool(rchan['is_private']),is_dm=bool(rchan['is_dm']),is_archived=bool(rchan['is_archived']))})
     elif b.startswith('/invite '):
      name=b[8:].strip().lstrip('@'); target=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
      if not target:return bad(self,404,'unknown user')
      owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(ch['workspace_id'],u['id'])).fetchone()
      if not owner:return bad(self,403,'forbidden')
      c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],target['id']));return send(self,201,{'message':None,'invited_user':user(target)})
     elif b in ('/archive','/unarchive'):
      owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(ch['workspace_id'],u['id'])).fetchone()
      if not owner:return bad(self,403,'forbidden')
      val=1 if b=='/archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));rchan=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();return send(self,201,{'message':None,'channel':dict(rchan,is_private=bool(rchan['is_private']),is_dm=bool(rchan['is_dm']),is_archived=bool(rchan['is_archived']))})
     elif not b.startswith('/me '):return bad(self)
    mentions=set()
    members=[x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(ch['id'],)).fetchall()]
    for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',b):
     if name in ('channel','here'):
      mentions.update(x for x in members if x!=u['id'])
     else:
      ur=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
      if ur and ur['id'] in members and ur['id']!=u['id']:mentions.add(ur['id'])
      gr=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],name)).fetchone()
      if gr:
       mentions.update(x for x in json.loads(gr['user_ids']) if x in members and x!=u['id'])
    if '@here' in b:
     try:
      with open(ACTIVE,'a+') as af:
       af.seek(0); active={int(x.strip()) for x in af if x.strip().isdigit()}
     except: active=set()
     mentions.intersection_update(active)
    c.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(ch['id'],));ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch['id'],)).fetchone()['n'];cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],b,(d or {}).get('parent_id'),now(),ev))
    r=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone()
    c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],ev,'message.reply' if r['parent_id'] else 'message.created',r['id']))
    for mid in mentions:c.execute('INSERT INTO message_mentions VALUES(?,?)',(r['id'],mid))
    for fid in fids:
     c.execute('INSERT INTO message_files VALUES(?,?)',(r['id'],fid))
     c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
    o=mobj(c,r);event({'type':'message.reply' if r['parent_id'] else 'message.created','event_id':ev,'channel_id':ch['id'],'message':o},ch['id']);return send(self,201,{'message':o})
   z=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
   if z:
    target=c.execute('SELECT id FROM messages WHERE id=? AND deleted=0',(z.group(1),)).fetchone()
    if not target:return bad(self,404,'not found')
    if not d or not d.get('emoji'):return bad(self,400,'emoji required')
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(z.group(1),u['id'],d.get('emoji'))); mr=c.execute('SELECT channel_id FROM messages WHERE id=?',(z.group(1),)).fetchone(); durable(c,mr['channel_id'],'reaction.added',int(z.group(1)));r=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(z.group(1),)).fetchall();return send(self,200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in r]})
   z=re.fullmatch(r'/api/messages/(\d+)/pin',p)
   if z:
    if not c.execute('SELECT id FROM messages WHERE id=? AND deleted=0',(z.group(1),)).fetchone():return bad(self,404,'not found')
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(z.group(1),u['id'],now()));r=c.execute('SELECT message_id,user_id,pinned_at FROM pins WHERE message_id=?',(z.group(1),)).fetchone();return send(self,200,{'pin':{'message_id':r['message_id'],'pinned_by':r['user_id'],'pinned_at':r['pinned_at']}})
   z=re.fullmatch(r'/api/channels/(\d+)/read',p)
   if z:
    if not c.execute('SELECT id FROM channels WHERE id=?',(z.group(1),)).fetchone():return bad(self,404,'not found')
    try: eid=int((d or {}).get('last_read_event_id'))
    except: return bad(self,400,'invalid read state')
    r=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],z.group(1))).fetchone()
    oldv=r['last_read'] if r else 0
    if eid>oldv:c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u['id'],z.group(1),eid));oldv=eid
    return send(self,200,{'read_state':{'channel_id':int(z.group(1)),'last_read_event_id':oldv,'unread_count':0,'mention_count':c.execute('SELECT COUNT(*) FROM message_mentions mm JOIN messages m ON m.id=mm.message_id WHERE mm.user_id=? AND m.channel_id=?',(u['id'],z.group(1))).fetchone()[0]}})
   if p=='/api/dms':
    try: other=int(d['user_id'])
    except: return bad(self,400,'user_id required')
    if not c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone():return bad(self,404,'unknown user')
    ids=sorted([u['id'],other]);r=c.execute('SELECT c.id FROM channels c JOIN cmembers a ON a.channel_id=c.id JOIN cmembers b ON b.channel_id=c.id WHERE c.is_dm=1 AND a.user_id=? AND b.user_id=?',(ids[0],ids[1])).fetchone()
    if r:return send(self,200,{'channel_id':r['id']})
    cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(0,?,1)',('dm-'+str(ids[0])+'-'+str(ids[1]),));cid=cur.lastrowid
    for x in ids:c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,x))
    return send(self,200,{'channel_id':cid})
  return bad(self,404,'not found')
 def do_PATCH(self):
  p=self.path;u=self.au()
  if not u:return bad(self,401,'authentication required')
  d=body(self)
  with db() as c:
   z=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],z.group(2))).fetchone() if w else None
    if not g:return bad(self,404,'not found')
    owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(w['id'],u['id'])).fetchone()
    if g['creator_id']!=u['id'] and not owner:return bad(self,403,'forbidden')
    name=d.get('name',g['name']);ids=[int(x) for x in d.get('user_ids',json.loads(g['user_ids']))]
    c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(name,json.dumps(ids),g['id']))
    return send(self,200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],z.group(2))).fetchone()
    if not target:return bad(self,404,'not found')
    role=(d or {}).get('role')
    if role not in ('owner','admin','member','guest'):return bad(self,400,'invalid role')
    if role=='owner':return bad(self,400,'invalid role')
    if int(z.group(2))==w['owner_id']:return bad(self,403,'forbidden')
    if not actor or actor['role'] not in ('owner','admin'):return bad(self,403,'forbidden')
    if actor['role']=='admin' and target['role']=='admin':return bad(self,403,'forbidden')
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],z.group(2)))
    return send(self,200,{'member':{'user_id':int(z.group(2)),'role':role}})
   z=re.fullmatch(r'/api/workspaces/([^/]+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    if not w:return bad(self,404,'not found')
    role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role'] not in ('owner','admin'):return bad(self,403,'forbidden')
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return bad(self,400,'invalid join_mode')
    if 'name' in d:c.execute('UPDATE workspaces SET name=? WHERE id=?',(d['name'],w['id']))
    if 'join_mode' in d:c.execute('UPDATE workspaces SET join_mode=? WHERE id=?',(d['join_mode'],w['id']))
    return send(self,200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   z=re.fullmatch(r'/api/channels/(\d+)',p)
   if z:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(z.group(1),)).fetchone()
    if not ch:return bad(self,404,'not found')
    if len(str(d.get('topic','')))>250:return bad(self,400,'topic too long')
    member=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(ch['workspace_id'],u['id'])).fetchone()
    if not member and not owner:return bad(self,403,'forbidden')
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),ch['id']));durable(c,ch['id'],'channel.updated');rr=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();return send(self,200,{'channel':dict(rr,is_private=bool(rr['is_private']),is_dm=bool(rr['is_dm']),is_archived=bool(rr['is_archived']))})
   if p=='/api/users/me':
    if 'timezone' in d and d['timezone'] not in ('UTC','America/New_York','Europe/London'):return bad(self)
    if any(len(str(v))>200 for v in d.values()):return bad(self)
    for k in ('display_name','timezone','avatar_url','status_text','status_emoji'):
     if k in d:c.execute('UPDATE users SET '+k+'=? WHERE id=?',(d[k],u['id']))
    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();event({'type':'user.updated','user':user(r)});return send(self,200,{'user':user(r)})
   z=re.fullmatch(r'/api/messages/(\d+)',p)
   if z:
    r=c.execute('SELECT * FROM messages WHERE id=?',(z.group(1),)).fetchone()
    if not r:return bad(self,404,'not found')
    if r['deleted']:return bad(self,409,'deleted')
    if r['author_id']!=u['id']:return bad(self,403,'forbidden')
    if not d or not str(d.get('body','')).strip():return bad(self,400,'empty body')
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d.get('body',''),now(),r['id']))
    c.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(r['channel_id'],))
    ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
    c.execute('UPDATE messages SET event_id=? WHERE id=?',(ev,r['id']))
    updated=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone(); c.execute('INSERT INTO events VALUES(?,?,?,?)',(r['channel_id'],ev,'message.updated',r['id'])); o=mobj(c,updated)
    event({'type':'message.updated','event_id':ev,'channel_id':r['channel_id'],'message':o},r['channel_id'])
    return send(self,200,{'message':o})
  return bad(self,404,'not found')
 def do_DELETE(self):
  p=urllib.parse.urlparse(self.path).path
  u=self.au()
  if not u:return bad(self,401,'authentication required')
  with db() as c:
   z=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone();g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],z.group(2))).fetchone() if w else None
    if not g:return send(self,200,{'deleted':True})
    owner=c.execute('SELECT 1 FROM workspaces WHERE id=? AND owner_id=?',(w['id'],u['id'])).fetchone()
    if g['creator_id']!=u['id'] and not owner:return bad(self,403,'forbidden')
    c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));return send(self,200,{'deleted':True})
   z=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',p)
   if z:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(z.group(1),)).fetchone()
    ar=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return bad(self,404,'not found')
    if not ar or ar['role'] not in ('owner','admin'):return bad(self,403,'forbidden')
    c.execute('DELETE FROM invites WHERE code=?',(z.group(2),));return send(self,200,{'deleted':True})
   z=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',p)
   if z:
    mid=int(z.group(1)); emoji=urllib.parse.unquote(z.group(2))
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); mr=c.execute('SELECT channel_id FROM messages WHERE id=?',(mid,)).fetchone(); durable(c,mr['channel_id'],'reaction.removed',mid)
    rows=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall()
    return send(self,200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rows]})
   z=re.fullmatch(r'/api/messages/(\d+)/pin',p)
   if z:
    if not c.execute('SELECT id FROM messages WHERE id=?',(z.group(1),)).fetchone():return bad(self,404,'not found')
    c.execute('DELETE FROM pins WHERE message_id=?',(z.group(1),));return send(self,200,{'unpinned':True})
   z=re.fullmatch(r'/api/messages/(\d+)',p)
   if z:
    r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(z.group(1),)).fetchone()
    if not r:return bad(self,404,'not found')
    if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return bad(self,403,'forbidden')
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(z.group(1),))
    c.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(r['channel_id'],))
    ev=c.execute('SELECT n FROM seq WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']; c.execute('UPDATE messages SET event_id=? WHERE id=?',(ev,r['id'])); c.execute('INSERT INTO events VALUES(?,?,?,?)',(r['channel_id'],ev,'message.deleted',r['id']))
    event({'type':'message.deleted','event_id':ev,'channel_id':r['channel_id'],'message_id':r['id']},r['channel_id'])
    return send(self,200,{'deleted':True})
  return bad(self,404,'not found')
 def ws(self,t):
  wsuser=who(t)
  if not wsuser:return bad(self,401,'invalid token')
  try:
   with open(ACTIVE,'a+') as af:
    af.seek(0); vals={x.strip() for x in af if x.strip()}; vals.add(str(wsuser['id'])); af.seek(0); af.truncate(); af.write(chr(10).join(sorted(vals))+chr(10))
  except: pass
  k=self.headers.get('Sec-WebSocket-Key',''); a=base64.b64encode(hashlib.sha1((k+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.request.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+a+'\r\n\r\n').encode())
  clients.append((self.request,None,wsuser['id']))
  cid=None; last=0; self.request.settimeout(.15)
  def frame(x):
   raw=json.dumps(x,separators=(',',':')).encode(); ln=len(raw)
   if ln<126:h=b'\x81'+bytes([ln])
   elif ln<65536:h=b'\x81\x7e'+struct.pack('>H',ln)
   else:h=b'\x81\x7f'+struct.pack('>Q',ln)
   self.request.sendall(h+raw)
  try:
   while True:
    if cid is not None:
     with db() as c:
      rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
      for e in rows:
       r=c.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone() if e['message_id'] else None
       payload={'message_id':e['message_id']} if e['type']=='message.deleted' else ({'message':mobj(c,r)} if r else {})
       frame({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**payload}); last=e['event_id']
    try:
     h=self.request.recv(2)
     if not h:break
    except socket.timeout:continue
    ln=h[1]&127
    if ln==126:ln=struct.unpack('>H',self.request.recv(2))[0]
    elif ln==127:ln=struct.unpack('>Q',self.request.recv(8))[0]
    mask=self.request.recv(4); dat=self.request.recv(ln); dat=bytes(dat[i]^mask[i%4] for i in range(ln))
    try:d=json.loads(dat.decode())
    except:continue
    if d.get('type') in ('subscribe','resume'):
     cid=int(d.get('channel_id')); q=d.get('since_event_id')
     with db() as c:
      headrow=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone(); head=headrow['n'] if headrow else 0
      last=int(q) if d.get('type')=='resume' and q is not None else head
      if d.get('type')=='resume' and q is not None:
       rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
       for e in rows:
        r=c.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone()
        frame({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**({'message_id':e['message_id']} if e['type']=='message.deleted' else {'message':mobj(c,r)})});last=e['event_id']
     frame({'type':'subscribed' if d['type']=='subscribe' else 'resumed','channel_id':cid,'head_event_id':head})
  except:pass
  finally:
   for x in clients[:]:
    if x[0] is self.request:clients.remove(x)

HTML=b"""<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}
button{border:0;padding:8px 12px;border-radius:4px}.shell{display:grid;grid-template-columns:240px 1fr;min-height:700px}.sidebar{background:#3f0e40;color:white;padding:16px}.main{padding:20px}.modal{padding:12px;border:1px solid #ddd}
</style></head><body><div id="app">
<div id="auth-modal" class="modal"><form data-testid="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary" class="primary">Sign in</button></form><button data-testid="auth-toggle">Create account</button><div data-testid="join-workspace-error"></div></div>
<div id="chat-shell" class="shell">
<aside class="sidebar"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><button data-testid="new-channel-btn" data-button-role="secondary" class="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary" class="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger" class="danger">Logout</button><div data-testid="channel-list"></div><div data-testid="dms-list"></div></aside>
<main class="main"><h2 data-testid="channel-title"># general</h2><div data-testid="channel-topic"></div><div data-testid="message-list"></div><form><input data-testid="message-input" name="body"><button data-testid="send-btn" data-button-role="primary" class="primary">Send</button></form><div data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary" class="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary" class="secondary">Close</button></div></main></div>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><input data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary" class="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary" class="secondary">Cancel</button></form></div>
<div id="channel-settings-modal"><h3 data-testid="channel-settings-title"></h3><button data-testid="channel-settings-btn" data-button-role="secondary" class="secondary">Settings</button><button data-testid="channel-settings-close" data-button-role="secondary" class="secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary" class="primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary" class="primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger" class="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary" class="secondary">Unarchive</button></div>
<div id="workspace-settings-modal"><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary" class="primary">Save</button><button data-testid="workspace-settings-close" data-button-role="secondary" class="secondary">Close</button><div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary" class="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary" class="primary">Create invitation</button></form><div data-testid="invitations-list"></div></div>
<form data-testid="workspace-create-form"><input name="slug"><input name="name"><button data-testid="empty-state-create-workspace">Create workspace</button></form><form data-testid="join-workspace-form"><input name="workspace"><button data-testid="join-workspace-submit" data-button-role="primary" class="primary">Join</button></form>
<script>window.__huddle_token=localStorage.getItem("huddle.token")||"";const t=window.__huddle_token;</script></div></body></html>"""

def init():
 with db() as c:c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER,PRIMARY KEY(user_id,channel_id));CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER);CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,message_id INTEGER,PRIMARY KEY(channel_id,event_id));CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT,attached INTEGER DEFAULT 0)''')
if __name__=='__main__':init();ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
