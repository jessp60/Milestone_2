import os,json,time,re,hashlib,secrets,threading, mimetypes, urllib.parse, base64, struct, fcntl
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DATA='/tmp/huddle_state.json'; LOCK='/tmp/huddle_state.lock'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
clients=[]; online_users={}; clients_lock=threading.Lock()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())
def init():
 with open(LOCK,'a'): pass
 with open(LOCK) as l:
  fcntl.flock(l,fcntl.LOCK_EX)
  if not os.path.exists(DATA):
   json.dump({'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'files':{},'groups':{},'dms':{},'next':{'user':1,'ws':1,'ch':1,'msg':1,'file':1,'group':1}},open(DATA,'w'))
  fcntl.flock(l,fcntl.LOCK_UN)
def db():
 with open(LOCK,'r') as l:
  fcntl.flock(l,fcntl.LOCK_SH); d=json.load(open(DATA)); fcntl.flock(l,fcntl.LOCK_UN); return d
def save(d):
 with open(LOCK,'r+') as l:
  fcntl.flock(l,fcntl.LOCK_EX); json.dump(d,open(DATA,'w')); fcntl.flock(l,fcntl.LOCK_UN)
def newid(d,k): n=d['next'][k]; d['next'][k]=n+1; return n
def userobj(u): return {k:u.get(k,'') for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def tokenuser(d,t): return d['users'].get(str(d['tokens'].get(t))) if t else None
def msgobj(d,m):
 u=d['users'].get(str(m['author_id']),{})
 rs=[]
 for e,ids in m.get('reactions',{}).items(): rs.append({'emoji':e,'count':len(ids),'user_ids':ids})
 fs=[]
 for f in m.get('files',[]):
  if isinstance(f,dict): fs.append(f)
  elif str(f) in d['files']: fs.append({k:v for k,v in d['files'][str(f)].items() if k!='path'})
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userobj(u),'body':m['body'],'parent_id':m.get('parent_id'),'reply_count':sum(1 for x in d['messages'].values() if x.get('parent_id')==m['id'] and not x.get('deleted')),'created_at':m['created_at'],'edited_at':m.get('edited_at'),'files':fs,'reactions':rs,'mentions':m.get('mentions',[])}
def broadcast(x):
 raw=json.dumps(x).encode()
 with clients_lock:
  for c in clients[:]:
   try:
    z=struct.pack('!BB',0x81, len(raw)) if len(raw)<126 else struct.pack('!BBH',0x81,126,len(raw)); c.sendall(z+raw)
   except: clients.remove(c)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,x):
  b=json.dumps(x).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def auth(self):
  q=self.headers.get('Authorization',''); return tokenuser(db(),q[7:] if q.startswith('Bearer ') else '')
 def req(self):
  return urllib.parse.urlparse(self.path)
 def do_GET(self):
  p=self.req(); path=p.path
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path.startswith('/api/ws'):
   t=urllib.parse.parse_qs(p.query).get('token',[''])[0]; u=tokenuser(db(),t)
   if not u: return self.sendj(401,{'error':'unauthorized'})
   key=self.headers.get('Sec-WebSocket-Key'); acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',acc); self.end_headers()
   with clients_lock: clients.append(self.request); online_users.setdefault(u['id'],set()).add(self.request)
   try:
    while self.request.recv(2): pass
   except: pass
   return
  u=self.auth()
  if path=='/': return self.html()
  if not u: return self.sendj(401,{'error':'unauthorized'})
  d=db(); parts=path.split('/')
  if len(parts)==4 and parts[2]=='users':
   x=d['users'].get(parts[3]); return self.sendj(200,{'user':userobj(x)}) if x else self.sendj(404,{})
  if path=='/api/workspaces':
   return self.sendj(200,{'workspaces':[w for w in d['workspaces'].values() if str(u['id']) in [str(x) for x in w['members']]]})
  if len(parts)==4 and parts[2]=='workspaces':
   w=next((x for x in d['workspaces'].values() if x['slug']==parts[3]),None)
   if not w:return self.sendj(404,{})
   cs=[c for c in d['channels'].values() if c['workspace_id']==w['id'] and (not c['private'] or u['id'] in c['members'])]
   return self.sendj(200,{'workspace':w,'channels':cs,'read_state':{'unread_count':0,'mention_count':0}})
  if len(parts)==5 and parts[2]=='workspaces' and parts[4]=='channels':
   w=next((x for x in d['workspaces'].values() if x['slug']==parts[3]),None)
   if not w:return self.sendj(404,{})
   cs=[c for c in d['channels'].values() if c['workspace_id']==w['id'] and (not c.get('private') or u['id'] in c['members'])]
   return self.sendj(200,{'channels':cs})
  if len(parts)==5 and parts[2]=='workspaces' and parts[4]=='groups':
   w=next((x for x in d['workspaces'].values() if x['slug']==parts[3]),None)
   if not w:return self.sendj(404,{})
   if u['id'] not in w['members']:return self.sendj(403,{})
   return self.sendj(200,{'groups':[g for g in d['groups'].values() if g['workspace_id']==w['id']]})
  if len(parts)>=4 and parts[2]=='channels' and parts[3].isdigit():
   cid=int(parts[3]); c=d['channels'].get(str(cid))
   if not c:return self.sendj(404,{})
   if u['id'] not in c['members']: return self.sendj(403,{})
   if len(parts)>4 and parts[4]=='messages':
    ms=[msgobj(d,m) for m in d['messages'].values() if m['channel_id']==cid and not m.get('deleted') and not m.get('parent_id')]
    return self.sendj(200,{'messages':sorted(ms,key=lambda x:x['id'],reverse=True),'next_cursor':None})
   if len(parts)>4 and parts[4]=='read': return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':c.get('mention_counts',{}).get(str(u['id']),0)}})
   if len(parts)>4 and parts[4]=='members': return self.sendj(200,{'members':[userobj(d['users'][str(i)]) for i in c['members']]})
  if len(parts)==4 and parts[2]=='files':
   f=d['files'].get(parts[3]); return self.sendj(200,{'file':f}) if f else self.sendj(404,{})
  if len(parts)==5 and parts[2]=='files' and parts[4]=='download':
   f=d['files'].get(parts[3])
   if not f:return self.sendj(404,{})
   b=open(f['path'],'rb').read(); self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if len(parts)>=4 and parts[2]=='workspaces' and parts[-1]=='groups':
   w=next((x for x in d['workspaces'].values() if x['slug']==parts[3]),None); gs=[g for g in d['groups'].values() if g['workspace_id']==w['id']]
   return self.sendj(200,{'groups':gs})
  return self.sendj(404,{})
 def html(self):
  b=b'''<!doctype html><html><body><div id="app"></div><script>localStorage.getItem("huddle.token");</script></body></html>'''
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_POST(self):
  p=self.req(); path=p.path; d=db(); u=self.auth()
  if path=='/api/auth/register':
   x=self.body(); un=x.get('username','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(x.get('password',''))<8:return self.sendj(400,{})
   if any(a['username']==un for a in d['users'].values()):return self.sendj(409,{})
   i=newid(d,'user'); z={'id':i,'username':un,'display_name':x.get('display_name',un),'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}; d['users'][str(i)]=z;t=secrets.token_urlsafe(20);d['tokens'][t]=i;save(d);return self.sendj(201,{'user':userobj(z),'token':t})
  if path=='/api/auth/login':
   x=self.body(); z=next((a for a in d['users'].values() if a['username']==x.get('username') and a.get('password',x.get('password'))==x.get('password')),None)
   if not z:return self.sendj(401,{})
   t=secrets.token_urlsafe(20);d['tokens'][t]=z['id'];save(d);return self.sendj(200,{'user':userobj(z),'token':t})
  if not u:return self.sendj(401,{})
  if path=='/api/workspaces':
   x=self.body(); slug=x.get('slug',''); 
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or any(w['slug']==slug for w in d['workspaces'].values()):return self.sendj(409,{})
   wi=newid(d,'ws'); ci=newid(d,'ch'); w={'id':wi,'slug':slug,'name':x.get('name',slug),'owner_id':u['id'],'join_mode':'open','members':[u['id']]}; c={'id':ci,'workspace_id':wi,'name':'general','private':False,'is_private':False,'is_dm':False,'topic':'','is_archived':False,'members':[u['id']],'seq':0}; d['workspaces'][str(wi)]=w;d['channels'][str(ci)]=c;save(d);return self.sendj(201,{'workspace':w,'general_channel':c})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=next((a for a in d['workspaces'].values() if a['slug']==m.group(1)),None)
   if not w:return self.sendj(404,{})
   if u['id'] not in w['members']:return self.sendj(403,{})
   x=self.body(); name=x.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.sendj(400,{})
   if any(c['workspace_id']==w['id'] and c['name']==name for c in d['channels'].values()):return self.sendj(409,{})
   private=bool(x.get('is_private',False))
   if private and u['id']!=w['owner_id']:return self.sendj(403,{})
   i=newid(d,'ch'); c={'id':i,'workspace_id':w['id'],'name':name,'private':private,'is_private':private,'is_dm':False,'topic':x.get('topic',''),'is_archived':False,'members':[u['id']],'seq':0}
   d['channels'][str(i)]=c; save(d); return self.sendj(201,{'channel':c})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=next((a for a in d['workspaces'].values() if a['slug']==m.group(1)),None)
   if not w:return self.sendj(404,{})
   if u['id'] not in w['members']:return self.sendj(403,{})
   x=self.body(); h=x.get('handle','')
   if any(g['workspace_id']==w['id'] and g['handle']==h for g in d['groups'].values()):return self.sendj(409,{})
   i=newid(d,'group'); g={'id':i,'workspace_id':w['id'],'handle':h,'name':x.get('name',h),'member_user_ids':x.get('user_ids',[]),'creator_id':u['id']}
   d['groups'][str(i)]=g;save(d);return self.sendj(201,{'group':g})
  if path=='/api/files':
   n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n)
   ct=self.headers.get('Content-Type','')
   bm=re.search(r'boundary=([^;]+)',ct)
   if not bm:return self.sendj(400,{})
   boundary=b'--'+bm.group(1).strip().encode()
   part=None
   for q in raw.split(boundary):
    if b'Content-Disposition:' in q and b'name="file"' in q:
     part=q; break
   if not part:return self.sendj(400,{})
   sep=part.find(b'\r\n\r\n')
   if sep<0:return self.sendj(400,{})
   hdr, data=part[:sep].decode('latin1','ignore'), part[sep+4:]
   data=data[:-2] if data.endswith(b'\r\n') else data
   if len(data)>10*1024*1024:return self.sendj(413,{})
   fm=re.search(r'filename="([^"]*)"',hdr); fn=fm.group(1) if fm else 'file'
   cm=re.search(r'Content-Type:\s*([^\r\n]+)',hdr,re.I); filect=cm.group(1).strip() if cm else 'application/octet-stream'
   if not fn:return self.sendj(400,{})
   i=newid(d,'file'); fp=f'/tmp/huddle_file_{i}';open(fp,'wb').write(data)
   f={'id':i,'uploader_id':u['id'],'filename':fn,'content_type':filect,'size':len(data),'created_at':now(),'path':fp}
   d['files'][str(i)]=f;save(d);return self.sendj(201,{'file':{k:v for k,v in f.items() if k!='path'}})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   c=d['channels'].get(m.group(1)); 
   if not c:return self.sendj(404,{})
   if u['id'] not in c['members']: c['members'].append(u['id']); w=d['workspaces'][str(c['workspace_id'])]; w['members']=list(set(w['members']+[u['id']]));save(d)
   return self.sendj(200,{'channel':c})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   cid=int(m.group(1));c=d['channels'].get(str(cid))
   if not c:return self.sendj(404,{})
   if u['id'] not in c['members']:return self.sendj(403,{})
   x=self.body(); body=x.get('body','')
   if not body:return self.sendj(400,{})
   if c.get('is_archived') and not body.startswith('/unarchive'):return self.sendj(423,{})
   if body.startswith('/') and not body.startswith('//'):
    a=body.split(' ',1); cmd=a[0]
    if cmd=='/shrug': body=(a[1]+' ' if len(a)>1 else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/me' and (len(a)==1 or not a[1].strip()):return self.sendj(400,{})
    elif cmd=='/me': pass
    elif cmd in ['/topic','/archive','/unarchive']:
     w=d['workspaces'].get(str(c['workspace_id']))
     if w and u['id']!=w['owner_id']: return self.sendj(403,{})
     if cmd=='/topic': c['topic']=a[1] if len(a)>1 else ''
     else:c['is_archived']=cmd=='/archive'
     save(d);return self.sendj(201,{'message':None,'channel':c})
    elif cmd=='/invite':
     if len(a)<2:return self.sendj(400,{})
     target=a[1].lstrip('@'); z=next((v for v in d['users'].values() if v['username']==target),None)
     if not z:return self.sendj(404,{})
     if z['id'] not in c['members']: c['members'].append(z['id'])
     if c['workspace_id'] in d['workspaces']:
      w=d['workspaces'][str(c['workspace_id'])]
      if z['id'] not in w['members']: w['members'].append(z['id'])
     save(d); return self.sendj(201,{'message':None,'invited_user':userobj(z)})
    else:return self.sendj(400,{})
   i=newid(d,'msg'); mentions=[]; 
   for q in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
    if q=='here':
     with clients_lock: online=set(online_users)
     for mid in c['members']:
      if mid!=u['id'] and mid in online and mid not in mentions:mentions.append(mid)
     continue
    z=next((v for v in d['users'].values() if v['username']==q),None)
    if z and z['id']!=u['id'] and z['id'] in c['members'] and z['id'] not in mentions:mentions.append(z['id'])
    for g in d['groups'].values():
     if g['workspace_id']==c['workspace_id'] and g['handle']==q:
      for mid in g['member_user_ids']:
       if mid!=u['id'] and mid in c['members'] and mid not in mentions:mentions.append(mid)
   c.setdefault('mention_counts',{})
   for mid in mentions:c['mention_counts'][str(mid)]=c['mention_counts'].get(str(mid),0)+1
   ids=x.get('file_ids',[])
   if not isinstance(ids,list): return self.sendj(400,{})
   for fid in ids:
    f=d['files'].get(str(fid))
    if not f or f['uploader_id']!=u['id'] or any(str(fid) in [str(a) for a in q.get('files',[])] for q in d['messages'].values()): return self.sendj(400,{})
   mm={'id':i,'channel_id':cid,'author_id':u['id'],'body':body,'created_at':now(),'reactions':{},'files':ids,'mentions':mentions};d['messages'][str(i)]=mm;c['seq']+=1;mm['event_id']=c['seq'];save(d);o=msgobj(d,mm);broadcast({'type':'message.created','event_id':c['seq'],'channel_id':cid,'message':o});return self.sendj(201,{'message':o})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   z=d['messages'].get(m.group(1));x=self.body()
   if not z:return self.sendj(404,{})
   if not x.get('emoji'):return self.sendj(400,{})
   e=x['emoji'];z.setdefault('reactions',{}).setdefault(e,[]); 
   if u['id'] not in z['reactions'][e]:z['reactions'][e].append(u['id'])
   save(d);return self.sendj(200,{'reactions':msgobj(d,z)['reactions']})
  if path=='/api/dms':
   oid=self.body().get('user_id'); 
   if str(oid) not in d['users']:return self.sendj(404,{})
   key='-'.join(map(str,sorted([u['id'],oid])))
   if key in d['dms']:return self.sendj(200,{'channel_id':d['dms'][key]})
   i=newid(d,'ch');d['channels'][str(i)]={'id':i,'workspace_id':0,'name':'dm','private':True,'is_private':True,'is_dm':True,'topic':'','is_archived':False,'members':[u['id'],oid],'seq':0};d['dms'][key]=i;save(d);return self.sendj(200,{'channel_id':i})
  return self.sendj(404,{})
 def do_PATCH(self):
  p=self.req().path;u=self.auth()
  if not u:return self.sendj(401,{})
  d=db();x=self.body()
  if p=='/api/users/me':
   z=d['users'][str(u['id'])]
   if 'timezone' in x and x['timezone'] not in ['UTC','America/New_York','Europe/London','America/Los_Angeles']:return self.sendj(400,{})
   if any(k in x and (not isinstance(x[k],str) or len(x[k])>250) for k in ['display_name','timezone','avatar_url','status_text','status_emoji']):return self.sendj(400,{})
   z.update({k:x[k] for k in x if k in ['display_name','timezone','avatar_url','status_text','status_emoji']});save(d);broadcast({'type':'user.updated','user':userobj(z)});return self.sendj(200,{'user':userobj(z)})
  m=re.match(r'/api/workspaces/([^/]+)/groups(?:/([^/]+))?$',p)
  if m:
   w=next((a for a in d['workspaces'].values() if a['slug']==m.group(1)),None);g=next((a for a in d['groups'].values() if a['workspace_id']==w['id'] and a['handle']==m.group(2)),None) if m.group(2) else None
   if not g:return self.sendj(404,{})
   if g['creator_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj(403,{})
   g.update({'name':x['name']} if 'name' in x else {}); g['member_user_ids']=x.get('user_ids',g.get('member_user_ids',[]));save(d);return self.sendj(200,{'group':g})
  return self.sendj(404,{})
 def do_DELETE(self):
  p=self.req().path;u=self.auth();d=db()
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if m:
   w=next((a for a in d['workspaces'].values() if a['slug']==m.group(1)),None)
   g=next((a for a in d['groups'].values() if a['workspace_id']==w['id'] and a['handle']==m.group(2)),None) if w else None
   if not g:return self.sendj(200,{'deleted':True})
   if g['creator_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj(403,{})
   del d['groups'][str(g['id'])];save(d);return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.*)$',p)
  if not u:return self.sendj(401,{})
  if m:
   z=d['messages'].get(m.group(1));e=urllib.parse.unquote(m.group(2))
   if not z:return self.sendj(404,{})
   if e in z.get('reactions',{}): z['reactions'][e]=[i for i in z['reactions'][e] if i!=u['id']]; z['reactions']={k:v for k,v in z['reactions'].items() if v}
   save(d);return self.sendj(200,{'reactions':msgobj(d,z)['reactions']})
  return self.sendj(404,{})
init()
if __name__=='__main__': ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
