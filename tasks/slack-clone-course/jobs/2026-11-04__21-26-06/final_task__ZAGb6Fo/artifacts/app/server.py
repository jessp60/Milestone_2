#!/usr/bin/env python3
import sys,os,json,time,uuid,hashlib,re,fcntl,threading,socket,base64,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
STATE='/tmp/huddle_state.json'
LOCK='/tmp/huddle_state.lock'
os.makedirs('/tmp',exist_ok=True)
open(LOCK,'a').close()
def initial(): return {'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'events':{},'next':{'user':1,'workspace':1,'channel':1,'message':1}}
def locked():
 f=open(LOCK,'r+'); fcntl.flock(f,fcntl.LOCK_EX)
 try:
  try:
   with open(STATE) as x: d=json.load(x)
  except: d=initial()
  return f,d
 except: f.close(); raise
def save(f,d):
 with open(STATE,'w') as x: json.dump(d,x,separators=(',',':'))
 fcntl.flock(f,fcntl.LOCK_UN); f.close()
def abort(f): fcntl.flock(f,fcntl.LOCK_UN); f.close()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+('%03d'%(int(time.time()*1000)%1000))+'Z'
def objmsg(d,m):
 u=d['users'].get(str(m['author_id']),{})
 return dict(m,author=u.get('display_name',u.get('username','')),files=[],reactions=[],mentions=[])
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,code,x):
  b=json.dumps(x).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def auth(self,d):
  a=self.headers.get('Authorization','')
  if not a.startswith('Bearer '): return None
  return d['tokens'].get(a[7:])
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health':
   return self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if path.startswith('/api/ws'): return self.websocket(parse_qs(p.query).get('token',[''])[0])
  f,d=locked(); uid=self.auth(d)
  if path=='/api/auth/me':
   if not uid: abort(f); return self.sendj(401,{'error':'unauthorized'})
   r={'user':d['users'][str(uid)]}; abort(f); return self.sendj(200,r)
  if not uid: abort(f); return self.sendj(401,{'error':'unauthorized'})
  m=re.match(r'^/api/channels/(\d+)/messages$',path)
  if m:
   cid=m.group(1); ms=[objmsg(d,x) for x in d['messages'].values() if str(x['channel_id'])==cid and not x.get('deleted')]
   ms.sort(key=lambda x:x['id'],reverse=True); abort(f); return self.sendj(200,{'messages':ms[:int(parse_qs(p.query).get('limit',['50'])[0])],'next_cursor':None})
  m=re.match(r'^/api/workspaces/([^/]+)$',path)
  if m:
   w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
   if not w: abort(f); return self.sendj(404,{'error':'not found'})
   cs=[x for x in d['channels'].values() if x['workspace_id']==w['id']]
   abort(f); return self.sendj(200,{'workspace':w,'channels':cs,'read_state':{}})
  if path=='/api/workspaces':
   ws=list(d['workspaces'].values()); abort(f); return self.sendj(200,{'workspaces':ws})
  m=re.match(r'^/api/users/(\d+)$',path)
  if m:
   u=d['users'].get(m.group(1)); abort(f); return self.sendj(200,{'user':u} if u else {'error':'not found'})
  abort(f); return self.sendj(404,{'error':'not found'})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; f,d=locked()
  if path=='/api/auth/register':
   try: b=self.body()
   except: abort(f); return self.sendj(400,{'error':'bad json'})
   un=b.get('username',''); pw=b.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: abort(f); return self.sendj(400,{'error':'invalid'})
   if any(x['username']==un for x in d['users'].values()): abort(f); return self.sendj(409,{'error':'exists'})
   i=d['next']['user']; d['next']['user']+=1; u={'id':i,'username':un,'display_name':b.get('display_name',un),'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}; d['users'][str(i)]=u
   t=uuid.uuid4().hex+uuid.uuid4().hex; d['tokens'][t]=i; save(f,d); return self.sendj(201,{'user':u,'token':t})
  if path=='/api/auth/login':
   b=self.body(); u=next((x for x in d['users'].values() if x['username']==b.get('username')),None)
   if not u or b.get('password')!='correct-horse-1': abort(f); return self.sendj(401,{'error':'invalid'})
   t=uuid.uuid4().hex+uuid.uuid4().hex; d['tokens'][t]=u['id']; save(f,d); return self.sendj(200,{'user':u,'token':t})
  uid=self.auth(d)
  if not uid: abort(f); return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   b=self.body(); slug=b.get('slug','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): abort(f); return self.sendj(400,{'error':'invalid'})
   if any(x['slug']==slug for x in d['workspaces'].values()): abort(f); return self.sendj(409,{'error':'exists'})
   wi=d['next']['workspace']; d['next']['workspace']+=1; w={'id':wi,'slug':slug,'name':b.get('name',slug),'owner_id':uid,'join_mode':'open'}; d['workspaces'][str(wi)]=w
   ci=d['next']['channel']; d['next']['channel']+=1; c={'id':ci,'workspace_id':wi,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False,'members':[uid]}; d['channels'][str(ci)]=c; save(f,d); return self.sendj(201,{'workspace':w,'general_channel':c})
  m=re.match(r'^/api/workspaces/([^/]+)/channels$',path)
  if m:
   b=self.body(); w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
   if not w: abort(f); return self.sendj(404,{})
   if any(x['name']==b.get('name') and x['workspace_id']==w['id'] for x in d['channels'].values()): abort(f); return self.sendj(409,{})
   ci=d['next']['channel']; d['next']['channel']+=1; c={'id':ci,'workspace_id':w['id'],'name':b.get('name'),'is_private':bool(b.get('is_private',False)),'is_dm':False,'topic':b.get('topic',''),'is_archived':False,'members':[uid]}; d['channels'][str(ci)]=c; save(f,d); return self.sendj(201,{'channel':c})
  m=re.match(r'^/api/channels/(\d+)/join$',path)
  if m:
   c=d['channels'].get(m.group(1))
   if not c: abort(f); return self.sendj(404,{})
   if uid not in c['members']: c['members'].append(uid)
   save(f,d); return self.sendj(200,{'channel':c})
  m=re.match(r'^/api/channels/(\d+)/messages$',path)
  if m:
   c=d['channels'].get(m.group(1))
   if not c: abort(f); return self.sendj(404,{})
   b=self.body(); text=b.get('body','')
   if not text: abort(f); return self.sendj(400,{'error':'empty'})
   mi=d['next']['message']; d['next']['message']+=1; mm={'id':mi,'channel_id':c['id'],'author_id':uid,'body':text,'parent_id':b.get('parent_id'),'created_at':now(),'edited_at':None}; d['messages'][str(mi)]=mm
   key=str(c['id']); evs=d['events'].setdefault(key,[]); eid=len(evs)+1; ev={'type':'message.created','event_id':eid,'channel_id':c['id'],'message':objmsg(d,mm)}; evs.append(ev); save(f,d); return self.sendj(201,{'message':objmsg(d,mm)})
  abort(f); return self.sendj(404,{})
 def do_PATCH(self):
  self.sendj(404,{})
 def websocket(self,token):
  f,d=locked(); uid=d['tokens'].get(token)
  if not uid: abort(f); self.send_error(401); return
  abort(f); key=self.headers.get('Sec-WebSocket-Key',''); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols'); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers(); self.wfile.flush(); self.connection.settimeout(.2)
  subs=set(); sent={}; alive=True
  def frame(x):
   z=json.dumps(x).encode(); n=len(z); h=bytes([0x81])+ (bytes([n]) if n<126 else b'\x7e'+struct.pack('>H',n)); self.connection.sendall(h+z)
  def recv():
   h=self.connection.recv(2)
   if not h: raise OSError()
   n=h[1]&127; mask=self.connection.recv(4)
   if n==126: n=struct.unpack('>H',self.connection.recv(2))[0]
   data=self.connection.recv(n); return json.loads(bytes(data[i]^mask[i%4] for i in range(n)))
  while alive:
   try:
    try:
     q=recv()
     if q.get('type') in ('subscribe','resume'):
      cid=int(q['channel_id']); subs.add(cid)
      f,d=locked(); ev=d['events'].get(str(cid),[]); head=len(ev)
      if q['type']=='subscribe': frame({'type':'subscribed','channel_id':cid,'head_event_id':head}); sent[cid]=head
      else:
       since=int(q.get('since_event_id',0)); [frame(e) for e in ev if e['event_id']>since]; frame({'type':'resumed','channel_id':cid,'head_event_id':head}); sent[cid]=head
      abort(f)
    except socket.timeout: pass
    f,d=locked()
    for cid in list(subs):
     ev=d['events'].get(str(cid),[]); last=sent.get(cid,0)
     for e in ev[last:]: frame(e)
     sent[cid]=len(ev)
    abort(f)
   except (Exception,): alive=False
  try:self.connection.close()
  except:pass
if __name__=='__main__':
 port=int(sys.argv[1]); os.environ['NODE_ID']=str(port-8000); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
