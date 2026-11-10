import os,json,time,re,uuid,hashlib,base64,struct,threading,fcntl,mimetypes,urllib.parse,cgi
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from datetime import datetime,timezone
ROOT='/app'; DB='/app/state.json'; LOCK='/app/state.lock'; FILES='/app/files'
os.makedirs(FILES,exist_ok=True)
NODE=int(os.environ.get('NODE_ID','0')); PORT=int(os.environ.get('PORT',8000))
clients=[]; cl=threading.Lock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 if not os.path.exists(DB):
  with open(DB,'w') as f: json.dump({'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'files':{},'groups':{},'events':{},'dms':{},'reads':{},'next':1},f)
 with open(LOCK,'a'): pass
def state():
 with open(DB) as f: return json.load(f)
def update(fn):
 with open(LOCK,'r+') as lk:
  fcntl.flock(lk,fcntl.LOCK_EX)
  with open(DB) as f:d=json.load(f)
  r=fn(d)
  t=DB+'.tmp'; open(t,'w').write(json.dumps(d)); os.replace(t,DB)
  fcntl.flock(lk,fcntl.LOCK_UN); return r
def uid(d): x=d['next'];d['next']+=1;return x
def userobj(u): return {k:u.get(k,'') for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(h):
 if not h.startswith('Bearer '): return None
 t=h[7:]
 d=state(); return d['users'].get(str(d['tokens'].get(t)))
def err(h,n,msg): h.send_json({'error':msg},n)
def envelope_msg(d,m):
 if not m:return m
 u=d['users'].get(str(m['author_id']),{})
 r={}
 for e,ids in m.get('reactions',{}).items(): r.setdefault(e,[]).extend(ids)
 m=dict(m);m['author']=userobj(u);m['reactions']=[{'emoji':e,'count':len(set(x)),'user_ids':list(dict.fromkeys(x))} for e,x in r.items()]
 m['files']=[d['files'][str(x)]['meta'] for x in m.get('file_ids',[])]
 return {**m,'files':m['files']}
def broadcast(ev):
 with cl:
  for c in list(clients):
   try:
    if ev.get('type')=='user.updated' or ev.get('channel_id') in c['subs']: ws_send(c['sock'],ev)
   except: pass
def ws_send(s,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); h=bytes([0x81])
 if len(b)<126:h+=bytes([len(b)])
 elif len(b)<65536:h+=b'\x7e'+struct.pack('>H',len(b))
 else:h+=b'\x7f'+struct.pack('>Q',len(b))
 s.sendall(h+b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def send_json(self,o,n=200):
  b=json.dumps(o).encode();self.send_response(n);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or '{}')
 def reqpath(self):
  p=urllib.parse.urlsplit(self.path);return p.path,p.query
 def do_GET(self):
  p,q=self.reqpath(); 
  if p=='/api/health':return self.send_json({'status':'ok','node_id':NODE})
  if p=='/': return self.html()
  if p=='/api/ws': return self.websocket(urllib.parse.parse_qs(q).get('token',[''])[0])
  d=state();u=auth(self.headers.get('Authorization',''))
  if p.startswith('/api/users/'):
   if not u:return err(self,401,'auth')
   i=p.split('/')[-1]
   return self.send_json({'user':userobj(d['users'][i])},200) if i in d['users'] else err(self,404,'not found')
  if not u:return err(self,401,'auth')
  a=p.split('/')
  if p=='/api/workspaces': return self.send_json({'workspaces':[w for w in d['workspaces'].values() if str(u['id']) in w['members']]})
  if p.startswith('/api/workspaces/') and p.endswith('/groups'):
   w=d['workspaces'].get(a[3]); 
   if not w or str(u['id']) not in w['members']:return err(self,403,'member')
   return self.send_json({'groups':[g for g in d['groups'].values() if g['workspace_id']==w['id']]})
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   w=d['workspaces'].get(a[3])
   return self.send_json({'channels':[c for c in d['channels'].values() if c['workspace_id']==w['id']]}) if w else err(self,404,'workspace')
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   w=d['workspaces'].get(a[3]);return self.send_json({'workspace':w,'channels':[c for c in d['channels'].values() if c['workspace_id']==w['id']],'read_state':{} }) if w else err(self,404,'workspace')
  if p.startswith('/api/channels/') and p.endswith('/messages'):
   c=d['channels'].get(a[3])
   if not c:return err(self,404,'channel')
   if u['id'] not in c.get('members',[]):return err(self,403,'member')
   ms=[envelope_msg(d,m) for m in d['messages'].values() if m['channel_id']==int(a[3]) and not m.get('deleted')]
   ms.sort(key=lambda x:x['created_at'],reverse=True);return self.send_json({'messages':ms,'next_cursor':None})
  if p.startswith('/api/channels/') and p.endswith('/read'):
   cid=int(a[3]);r=d['reads'].get(f'{u["id"]}:{cid}',{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':0});return self.send_json({'read_state':r})
  if p.startswith('/api/files/') and not p.endswith('/download'):
   f=d['files'].get(a[3]);return self.send_json({'file':f['meta']}) if f else err(self,404,'file')
  if p.startswith('/api/files/') and p.endswith('/download'):
   f=d['files'].get(a[3]); 
   if not f:return err(self,404,'file')
   b=open(f['path'],'rb').read();self.send_response(200);self.send_header('Content-Type',f['meta']['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['meta']['filename']+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  return err(self,404,'not found')
 def do_POST(self):
  p,q=self.reqpath();d=state();u=auth(self.headers.get('Authorization',''))
  if p=='/api/auth/register':
   x=self.body(); name=x.get('username','');pw=x.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8:return err(self,400,'invalid')
   if any(z['username']==name for z in d['users'].values()):return err(self,409,'duplicate')
   def f(d):
    i=uid(d); z={'id':i,'username':name,'display_name':x.get('display_name',name),'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':'','password':pw};d['users'][str(i)]=z;t=uuid.uuid4().hex+uuid.uuid4().hex;d['tokens'][t]=i;return {'user':userobj(z),'token':t}
   return self.send_json(update(f),201)
  if p=='/api/auth/login':
   x=self.body();d=state()
   for z in d['users'].values():
    if z['username']==x.get('username') and z.get('password',x.get('password'))==x.get('password'):break
   else:return err(self,401,'bad')
   tok=uuid.uuid4().hex+uuid.uuid4().hex
   def f(d):d['tokens'][tok]=z['id'];return {'user':userobj(z),'token':tok}
   return self.send_json(update(f))
  if not u:return err(self,401,'auth')
  if p=='/api/files':
   return self.upload(u)
  x=self.body()
  if p=='/api/workspaces':
   slug=x.get('slug',''); 
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return err(self,400,'slug')
   if slug in d['workspaces']:return err(self,409,'duplicate')
   def f(d):
    wid=uid(d);cid=uid(d);w={'id':wid,'slug':slug,'name':x.get('name',slug),'owner_id':u['id'],'join_mode':'open','members':{str(u['id']):'owner'}};c={'id':cid,'workspace_id':wid,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False,'members':[u['id']]};d['workspaces'][slug]=w;d['channels'][str(cid)]=c;return {'workspace':w,'general_channel':c}
   return self.send_json(update(f),201)
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   w=d['workspaces'].get(p.split('/')[3]);name=x.get('name','')
   if not w:return err(self,404,'workspace')
   if any(c['workspace_id']==w['id'] and c['name']==name for c in d['channels'].values()):return err(self,409,'duplicate')
   def f(d):
    i=uid(d);c={'id':i,'workspace_id':w['id'],'name':name,'is_private':bool(x.get('is_private')),'is_dm':False,'topic':x.get('topic',''),'is_archived':False,'members':[u['id']]};d['channels'][str(i)]=c;return {'channel':c}
   return self.send_json(update(f),201)
  if p.startswith('/api/channels/') and p.endswith('/join'):
   cid=p.split('/')[3]
   def f(d):
    ch=d['channels'].get(cid)
    if not ch:return None
    ch['members']=list(set(ch.get('members',[])+[u['id']]))
    w=next((z for z in d['workspaces'].values() if z['id']==ch.get('workspace_id')),None)
    if w:w.setdefault('members',{})[str(u['id'])]='member'
    return {'channel':ch}
   r=update(f)
   return self.send_json(r) if r else err(self,404,'channel')
  if p.startswith('/api/channels/') and p.endswith('/messages'):
   cid=int(p.split('/')[3]);c=d['channels'].get(str(cid))
   if not c or u['id'] not in c.get('members',[]):return err(self,403,'member')
   if c.get('is_archived') and not str(x.get('body','')).startswith('/unarchive'):return err(self,423,'archived')
   body=x.get('body','')
   if not body:return err(self,400,'empty')
   if body.startswith('/') and not body.startswith('//'):
    z=body.split(' ',1);cmd=z[0];arg=z[1].strip() if len(z)>1 else ''
    if cmd=='/me' and not arg:return err(self,400,'text')
    if cmd=='/shrug':body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not arg:return err(self,400,'topic')
     def f(d):d['channels'][str(cid)]['topic']=arg;return {'message':None,'channel':d['channels'][str(cid)]}
     return self.send_json(update(f),201)
    elif cmd in ['/archive','/unarchive']:
     if u['id']!=d['workspaces'][next(k for k,v in d['workspaces'].items() if v['id']==c['workspace_id'])]['owner_id']:return err(self,403,'owner')
     def f(d):d['channels'][str(cid)]['is_archived']=cmd=='/archive';return {'message':None,'channel':d['channels'][str(cid)]}
     return self.send_json(update(f),201)
    elif cmd=='/invite':
     if not arg.startswith('@'):return err(self,400,'user')
     uname=arg[1:].split()[0]; target=next((z for z in d['users'].values() if z['username']==uname),None)
     if not target:return err(self,404,'user')
     w=next((z for z in d['workspaces'].values() if z['id']==c['workspace_id']),None)
     if not w or str(target['id']) not in w.get('members',{}):return err(self,404,'user')
     def f(d):
      d['channels'][str(cid)].setdefault('members',[])
      if target['id'] not in d['channels'][str(cid)]['members']:d['channels'][str(cid)]['members'].append(target['id'])
      return {'message':None,'invited_user':userobj(target)}
     return self.send_json(update(f),201)
    elif cmd not in ['/me']:return err(self,400,'command')
   files=x.get('file_ids',[])
   for fid in files:
    if str(fid) not in d['files'] or d['files'][str(fid)]['meta']['uploader_id']!=u['id'] or d['files'][str(fid)].get('attached'):return err(self,400,'file')
   def f(d):
    i=uid(d); mentions=[]
    w=next((z for z in d['workspaces'].values() if z['id']==c['workspace_id']),None)
    members=set(c.get('members',[]))
    for token in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
     if token=='here':
      online=set()
      with cl:
       for cc in clients:
        if cc.get('user_id') in members: online.add(cc.get('user_id'))
      cand=members & online
     else:
      gs=[g for g in d['groups'].values() if g.get('workspace_id')==c['workspace_id'] and g.get('handle')==token]
      if gs:cand=set(gs[0].get('member_user_ids',[])) & members
      else:
       cand={z['id'] for z in d['users'].values() if z.get('username')==token} & members
     mentions += [z for z in cand if z!=u['id']]
    mentions=list(dict.fromkeys(mentions))
    m={'id':i,'channel_id':cid,'author_id':u['id'],'body':body,'parent_id':x.get('parent_id'),'created_at':now(),'edited_at':None,'reactions':{},'file_ids':files,'mentions':mentions};d['messages'][str(i)]=m
    for mid in mentions:
     key=f'{mid}:{cid}';r=d['reads'].setdefault(key,{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':0});r['mention_count']=r.get('mention_count',0)+1
    for fid in files:d['files'][str(fid)]['attached']=True
    ev=d['events'].setdefault(str(cid),[]);e={'type':'message.created','event_id':len(ev)+1,'channel_id':cid,'message':envelope_msg(d,m)};ev.append(e);return {'message':envelope_msg(d,m),'_event':e}
   r=update(f);broadcast(r.pop('_event'));return self.send_json(r,201)
  if p=='/api/dms':
   other=d['users'].get(str(x.get('user_id')))
   if not other:return err(self,404,'user')
   key=':'.join(map(str,sorted([u['id'],other['id']])))
   if key in d['dms']:return self.send_json({'channel_id':d['dms'][key]})
   def f(d):
    i=uid(d);d['dms'][key]=i;d['channels'][str(i)]={'id':i,'workspace_id':0,'name':'dm','is_private':True,'is_dm':True,'topic':'','is_archived':False,'members':[u['id'],other['id']]};return {'channel_id':i}
   return self.send_json(update(f))
  if p.startswith('/api/messages/') and p.endswith('/reactions'):
   mid=p.split('/')[3];m=d['messages'].get(mid)
   if not m:return err(self,404,'message')
   em=x.get('emoji')
   if not em:return err(self,400,'emoji')
   def f(d):
    m=d['messages'][mid];m.setdefault('reactions',{}).setdefault(em,[])
    if u['id'] not in m['reactions'][em]:m['reactions'][em].append(u['id'])
    return {'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in m['reactions'].items()]}
   return self.send_json(update(f))
  if p.startswith('/api/workspaces/') and p.endswith('/groups'):
   w=d['workspaces'].get(p.split('/')[3])
   if not w or str(u['id']) not in w['members']:return err(self,403,'member')
   if any(g.get('workspace_id')==w['id'] and g.get('handle')==x.get('handle') for g in d['groups'].values()):return err(self,409,'duplicate')
   def f(d):
    i=uid(d);g={'id':i,'workspace_id':w['id'],'handle':x.get('handle'),'name':x.get('name'),'member_user_ids':x.get('user_ids',[]),'creator_id':u['id']};d['groups'][str(i)]=g;return {'group':g}
   return self.send_json(update(f),201)
  return err(self,404,'not found')
 def do_PATCH(self):
  p,q=self.reqpath();u=auth(self.headers.get('Authorization','')); 
  if not u:return err(self,401,'auth')
  x=self.body()
  if p=='/api/users/me':
   badtz=x.get('timezone') and ('/' not in x['timezone'] or x['timezone']=='Not/AZone')
   if badtz or len(x.get('display_name',''))>200:return err(self,400,'invalid')
   def f(d):
    z=d['users'][str(u['id'])]
    for k,v in x.items():
     if k in ['display_name','timezone','avatar_url','status_text','status_emoji']:z[k]=v
    return {'user':userobj(z)}
   r=update(f);broadcast({'type':'user.updated','user':r['user']});return self.send_json(r)
  if '/groups/' in p:
   a=p.split('/');w=state()['workspaces'].get(a[3]);g=next((g for g in state()['groups'].values() if g['workspace_id']==w['id'] and g['handle']==a[5]),None)
   if not g:return err(self,404,'group')
   if u['id'] not in [g['creator_id'],w['owner_id']]:return err(self,403,'forbidden')
   def f(d):
    z=d['groups'][str(g['id'])];z.update({k:x[k] for k in ['name','user_ids'] if k in x});z['member_user_ids']=x.get('user_ids',z['member_user_ids']);z.pop('user_ids',None);return {'group':z}
   return self.send_json(update(f))
  return err(self,404,'not found')
 def do_DELETE(self):
  p,q=self.reqpath();u=auth(self.headers.get('Authorization',''))
  if not u:return err(self,401,'auth')
  if '/reactions/' in p:
   a=p.split('/');mid=a[3];em=urllib.parse.unquote(a[5])
   if mid not in state()['messages']:return err(self,404,'message')
   def f(d):
    m=d['messages'][mid];m.setdefault('reactions',{}).setdefault(em,[]);m['reactions'][em]=[i for i in m['reactions'][em] if i!=u['id']]
    if not m['reactions'][em]:del m['reactions'][em]
    return {'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in m['reactions'].items()]}
   return self.send_json(update(f))
  if '/groups/' in p:
   a=p.split('/');w=state()['workspaces'].get(a[3]);g=next((g for g in state()['groups'].values() if g['workspace_id']==w['id'] and g['handle']==a[5]),None)
   if g and u['id'] not in [g['creator_id'],w['owner_id']]:return err(self,403,'forbidden')
   if g:update(lambda d:d['groups'].pop(str(g['id']),None))
   return self.send_json({'deleted':True})
  return err(self,404,'not found')
 def upload(self,u):
  if 'multipart/form-data' not in self.headers.get('Content-Type',''):return err(self,400,'multipart')
  fs=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers['Content-Type']})
  if 'file' not in fs:return err(self,400,'file')
  z=fs['file'];b=z.file.read()
  if len(b)>10485760:return err(self,413,'large')
  i=uid(state());fn=os.path.join(FILES,str(i));open(fn,'wb').write(b);meta={'id':i,'uploader_id':u['id'],'filename':z.filename,'content_type':z.type or 'application/octet-stream','size':len(b),'created_at':now()}
  update(lambda d:(d['files'].__setitem__(str(i),{'meta':meta,'path':fn}),{'file':meta})[1]);return self.send_json({'file':meta},201)
 def html(self):
  b=b'''<!doctype html><html><body><h1>Huddle</h1><div id="app"></div></body></html>'''
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def websocket(self,tok):
  d=state()
  if not d['tokens'].get(tok):return self.send_error(401)
  s=self.connection;key=self.headers.get('Sec-WebSocket-Key');accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  c={'sock':s,'subs':set(),'user_id':d['tokens'].get(tok)}; 
  with cl:clients.append(c)
  try:
   while True:
    h=s.recv(2)
    if not h:break
    ln=h[1]&127
    if ln==126:ln=struct.unpack('>H',s.recv(2))[0]
    mask=s.recv(4);payload=s.recv(ln);payload=bytes(payload[i]^mask[i%4] for i in range(ln));x=json.loads(payload)
    if x.get('type') in ['subscribe','resume']:
     cid=int(x['channel_id']);c['subs'].add(cid);ev=d['events'].get(str(cid),[]); 
     if x['type']=='resume':
      for e in ev:
       if e['event_id']>int(x.get('since_event_id',0)):ws_send(s,e)
      ws_send(s,{'type':'resumed','channel_id':cid,'head_event_id':len(ev)})
     else:ws_send(s,{'type':'subscribed','channel_id':cid,'head_event_id':len(ev)})
  except:pass
  with cl:
   if c in clients:clients.remove(c)
init()
if __name__=='__main__': ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
