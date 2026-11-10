import json,os,re,sys,time,uuid,hashlib,base64,struct,threading,fcntl,urllib.parse,io,cgi
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
STATE='/tmp/huddle_state.json'; LOCK='/tmp/huddle_state.lock'; EVENTS='/tmp/huddle_events.jsonl'
listeners=[]; active_ws_users=set(); listeners_lock=threading.Lock(); socket_locks={}; event_offset=0
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+('%03d'%(int(time.time()*1000)%1000))+'Z'
def db():
    if not os.path.exists(STATE): return {'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'files':{},'groups':{},'dms':{},'seq':0}
    try:
        with open(STATE) as f:return json.load(f)
    except:return {'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'files':{},'groups':{},'dms':{},'seq':0}
def update(fn):
    os.makedirs('/tmp',exist_ok=True)
    with open(LOCK,'w') as lf:
        fcntl.flock(lf,fcntl.LOCK_EX); d=db(); x=fn(d)
        with open(STATE,'w') as f: json.dump(d,f)
        fcntl.flock(lf,fcntl.LOCK_UN); return x
def read(fn):
    with open(LOCK,'a') as lf:
        fcntl.flock(lf,fcntl.LOCK_SH); x=fn(db()); fcntl.flock(lf,fcntl.LOCK_UN); return x
def userobj(u):
    return {k:u.get(k) for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def msgobj(m,d):
    a=d['users'].get(str(m['author_id']),{})
    rs=[{'emoji':e,'count':len(v),'user_ids':v} for e,v in m.get('reactions',{}).items() if v]
    return {**m,'author':userobj(a),'reactions':rs,'files':[d['files'][str(x)] for x in m.get('file_ids',[])],'mentions':m.get('mentions',[])}
def broadcast(x):
    with listeners_lock:
        for s in list(listeners):
            try: ws_send(s,x)
            except: pass
def event_watcher():
    global event_offset
    while True:
        try:
            with open(EVENTS,'r',encoding='utf-8') as f:
                lines=f.readlines()
            if event_offset > len(lines):
                event_offset=0
            fresh=lines[event_offset:]
            event_offset=len(lines)
            for line in fresh:
                try:
                    x=json.loads(line)
                    if x.get('type')=='user.updated':
                        broadcast(x)
                except Exception:
                    pass
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(0.01)
def ws_send(s,x):
    b=json.dumps(x,separators=(',',':')).encode(); n=len(b)
    if n<126: h=bytes([129,n])
    elif n<65536: h=bytes([129,126])+struct.pack('>H',n)
    else: h=bytes([129,127])+struct.pack('>Q',n)
    lk=socket_locks.setdefault(id(s),threading.Lock())
    with lk:
        s.sendall(h+b)
class H(BaseHTTPRequestHandler):
    server_version='Huddle/1'
    def log_message(self,*a): pass
    def j(self,o,status=200):
        b=json.dumps(o).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def auth(self):
        t=self.headers.get('Authorization','')
        if not t.startswith('Bearer '): return None
        return read(lambda d:d['users'].get(str(d['tokens'].get(t[7:]))))
    def err(self,s,m='error'): self.j({'error':m},s)
    def do_GET(self): self.route('GET')
    def do_POST(self): self.route('POST')
    def do_PATCH(self): self.route('PATCH')
    def do_DELETE(self): self.route('DELETE')
    def route(self,method):
        p=urllib.parse.urlparse(self.path); path=urllib.parse.unquote(p.path); q=urllib.parse.parse_qs(p.query)
        if path=='/api/health': return self.j({'status':'ok','node_id':int(self.server.server_address[1])-8000})
        if path=='/': return self.j({'status':'ok'})
        if path=='/api/ws': return self.websocket(q.get('token',[''])[0])
        if path=='/api/auth/register' and method=='POST':
            x=self.body(); u=x.get('username','')
            if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(x.get('password',''))<8:return self.err(400)
            def f(d):
                if any(v['username']==u for v in d['users'].values()): return None
                i=max([int(k) for k in d['users']] or [0])+1; d['users'][str(i)]={'id':i,'username':u,'display_name':x.get('display_name') or u,'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':'','mention_count':0}
                t=uuid.uuid4().hex+uuid.uuid4().hex; d['tokens'][t]=i; return {'user':userobj(d['users'][str(i)]),'token':t}
            z=update(f); return self.err(409) if z is None else self.j(z,201)
        if path=='/api/auth/login' and method=='POST':
            x=self.body(); z=read(lambda d:next(( (userobj(v),next(k for k,n in d['tokens'].items() if n==v['id'])) for v in d['users'].values() if v['username']==x.get('username')),None))
            if not z:return self.err(401)
            return self.j({'user':z[0],'token':z[1]})
        u=self.auth()
        if not u:return self.err(401)
        if path=='/api/auth/me':return self.j({'user':userobj(u)})
        m=re.fullmatch(r'/api/users/(\d+)',path)
        if m and method=='GET':
            z=read(lambda d:d['users'].get(m.group(1))); return self.err(404) if not z else self.j({'user':userobj(z)})
        if path=='/api/users/me' and method=='PATCH':
            x=self.body(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']
            if 'timezone' in x:
                import zoneinfo
                try: zoneinfo.ZoneInfo(x['timezone'])
                except:return self.err(400)
            if any(k not in allowed or not isinstance(x[k],str) or len(x[k])>250 for k in x):return self.err(400)
            z=update(lambda d:(d['users'][str(u['id'])].update({k:x[k] for k in x}),userobj(d['users'][str(u['id'])]))[1])
            ev={'type':'user.updated','user':z}
            with open(EVENTS,'a') as ef: ef.write(json.dumps(ev)+'\n')
            broadcast(ev)
            for delay in (0.05,0.15,0.30):
                threading.Timer(delay, broadcast, args=(ev,)).start()
            return self.j({'user':z})
        if path=='/api/workspaces' and method=='POST':
            x=self.body()
            def f(d):
                if any(w['slug']==x.get('slug') for w in d['workspaces'].values()):return None
                wid=str(len(d['workspaces'])+1); cid=str(len(d['channels'])+1); w={'id':int(wid),'slug':x['slug'],'name':x.get('name',''),'owner_id':u['id'],'join_mode':'open','members':{str(u['id']):'owner'}}
                d['workspaces'][wid]=w; d['channels'][cid]={'id':int(cid),'workspace_id':int(wid),'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False,'members':[u['id']],'seq':0}
                return {'workspace':w,'general_channel':d['channels'][cid]}
            z=update(f);return self.err(409) if z is None else self.j(z,201)
        if path=='/api/workspaces' and method=='GET':return self.j({'workspaces':read(lambda d:[w for w in d['workspaces'].values() if str(u['id']) in w['members']] )})
        m=re.fullmatch(r'/api/workspaces/([^/]+)',path)
        if m:
            slug=m.group(1); w=read(lambda d:next((v for v in d['workspaces'].values() if v['slug']==slug),None))
            if not w:return self.err(404)
            if method=='GET':return self.j({'workspace':w,'channels':read(lambda d:[c for c in d['channels'].values() if c['workspace_id']==w['id'] and (not c['is_private'] or u['id'] in c['members'])]),'read_state':{}})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',path)
        if m and method=='GET':
            w=read(lambda d:next((v for v in d['workspaces'].values() if v['slug']==m.group(1)),None))
            if not w:return self.err(404)
            return self.j({'channels':read(lambda d:[c for c in d['channels'].values() if c['workspace_id']==w['id'] and (not c['is_private'] or u['id'] in c['members'])])})
        if m and method=='POST':
            x=self.body(); w=read(lambda d:next((v for v in d['workspaces'].values() if v['slug']==m.group(1)),None))
            if not w:return self.err(404)
            if x.get('is_private') and w['members'].get(str(u['id'])) not in ('owner','admin'):return self.err(403)
            def make_channel(d):
                if any(c['workspace_id']==w['id'] and c['name']==x.get('name') for c in d['channels'].values()):
                    return None
                cid=str(len(d['channels'])+1)
                d['channels'][cid]={'id':int(cid),'workspace_id':w['id'],'name':x.get('name'),'is_private':bool(x.get('is_private')),'is_dm':False,'topic':x.get('topic',''),'is_archived':False,'members':[u['id']]}
                return d['channels'][cid]
            z=update(make_channel)
            return self.err(409) if z is None else self.j({'channel':z},201)
        m=re.fullmatch(r'/api/channels/(\d+)/join',path)
        if m and method=='POST':
            def f(d):
                c=d['channels'].get(m.group(1))
                if not c:return None
                if u['id'] not in c['members']:c['members'].append(u['id']); d['workspaces'][str(c['workspace_id'])]['members'][str(u['id'])]='member'
                return c
            z=update(f);return self.err(404) if z is None else self.j({'channel':z})
        m=re.fullmatch(r'/api/channels/(\d+)',path)
        if m and method=='PATCH':
            z=update(lambda d:(d['channels'][m.group(1)].update({'topic':self.body().get('topic','')}),d['channels'][m.group(1)])[1]);return self.j({'channel':z})
        m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
        if m and method in ('GET','POST'):
            cid=m.group(1); c=read(lambda d:d['channels'].get(cid))
            if not c:return self.err(404)
            if u['id'] not in c['members']:return self.err(403)
            if method=='GET':return self.j({'messages':read(lambda d:[msgobj(x,d) for x in d['messages'].values() if str(x['channel_id'])==cid and not x.get('deleted')]) ,'next_cursor':None})
            x=self.body(); body=x.get('body','')
            if not body:return self.err(400)
            file_ids=x.get('file_ids',[])
            def files_ok(d):
                seen=set()
                for raw in file_ids:
                    fid=str(raw)
                    f=d['files'].get(fid)
                    if not f or f.get('uploader_id') != u['id'] or fid in seen:
                        return False
                    seen.add(fid)
                    if any(fid in [str(v) for v in mm.get('file_ids',[])] for mm in d['messages'].values()):
                        return False
                return True
            if file_ids and not read(files_ok): return self.err(400)
            if body.startswith('/') and not body.startswith('//'):
                if body.startswith('/me ') and body[4:].strip():pass
                elif body.startswith('/shrug'):body=body[6:].strip()+' ¯\\_(ツ)_/¯' if body[6:].strip() else '¯\\_(ツ)_/¯'
                elif body.startswith('/topic '):
                    z=update(lambda d:(d['channels'][cid].update({'topic':body[7:].strip()}),d['channels'][cid])[1]);return self.j({'message':None,'channel':z},201)
                elif body in ('/archive','/unarchive'):
                    owner=read(lambda d:str(u['id'])==str(d['workspaces'][str(c['workspace_id'])]['owner_id']))
                    if not owner:return self.err(403)
                    z=update(lambda d:(d['channels'][cid].update({'is_archived':body=='/archive'}),d['channels'][cid])[1]);return self.j({'message':None,'channel':z},201)
                elif body.startswith('/invite '):
                    name=body[8:].strip().lstrip('@')
                    def invite(d):
                        target=next((v for v in d['users'].values() if v['username']==name),None)
                        if not target:return None
                        if target['id'] not in d['channels'][cid]['members']:d['channels'][cid]['members'].append(target['id'])
                        return userobj(target)
                    target=update(invite)
                    return self.err(404) if target is None else self.j({'message':None,'invited_user':target},201)
                else:return self.err(400)
            if c.get('is_archived'):return self.err(423)
            def f(d):
                mid=str(len(d['messages'])+1); mentions=[]; text=body
                for a in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',text):
                    for vv in d['users'].values():
                        if vv['username']==a and vv['id']!=u['id'] and vv['id'] in c['members']:mentions.append(vv['id'])
                    for g in d['groups'].values():
                        if g.get('workspace_id')==c.get('workspace_id') and g.get('handle')==a:
                            mentions.extend(vv for vv in g.get('member_user_ids',[]) if vv!=u['id'] and vv in c['members'])
                if re.search(r'(?<!\w)@here(?!\w)',text):
                    mentions.extend(vv for vv in c['members'] if vv!=u['id'] and vv in active_ws_users)
                mentions=list(dict.fromkeys(mentions))
                for vid in mentions:
                    if str(vid) in d['users']: d['users'][str(vid)]['mention_count']=d['users'][str(vid)].get('mention_count',0)+1
                mm={'id':int(mid),'channel_id':int(cid),'author_id':u['id'],'body':body,'parent_id':x.get('parent_id'),'reply_count':0,'created_at':now(),'edited_at':None,'file_ids':x.get('file_ids',[]),'mentions':mentions,'reactions':{}}
                d['messages'][mid]=mm;c['seq']=c.get('seq',0)+1;return msgobj(mm,d),c['seq']
            z,e=update(f); out={'message':z};broadcast({'type':'message.created','event_id':e,'channel_id':int(cid),'message':z});return self.j(out,201)
        m=re.fullmatch(r'/api/messages/(\d+)/reactions(?:/([^/]+))?',path)
        if m:
            mid,em=m.group(1),m.group(2)
            if not read(lambda d: mid in d['messages']): return self.err(404)
            if method=='POST':
                x=self.body();em=x.get('emoji')
                if not em:return self.err(400)
                z=update(lambda d:(d['messages'][mid].setdefault('reactions',{}).setdefault(em,[]),d['messages'][mid]['reactions'][em].append(u['id']) if u['id'] not in d['messages'][mid]['reactions'][em] else None,d['messages'][mid]['reactions'])[2]);return self.j({'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in z.items()]})
            if method=='DELETE':
                z=update(lambda d:(d['messages'][mid]['reactions'].get(em,[]).remove(u['id']) if u['id'] in d['messages'][mid]['reactions'].get(em,[]) else None,d['messages'][mid]['reactions'])[1]);return self.j({'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in z.items() if v]})
        if path=='/api/dms' and method=='POST':
            x=self.body(); other=str(x.get('user_id'))
            def f(d):
                if other not in d['users']:return None
                key=':'.join(map(str,sorted([u['id'],int(other)])))
                if key not in d['dms']:
                    cid=str(len(d['channels'])+1);d['dms'][key]=cid;d['channels'][cid]={'id':int(cid),'workspace_id':0,'name':'dm','is_private':True,'is_dm':True,'topic':'','is_archived':False,'members':[u['id'],int(other)]}
                return d['dms'][key]
            z=update(f);return self.err(404) if z is None else self.j({'channel_id':int(z)})
        if path=='/api/files' and method=='POST':
            env=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers.get('Content-Type',''),'CONTENT_LENGTH':self.headers.get('Content-Length','')})
            if 'file' not in env:return self.err(400)
            part=env['file']; data=part.file.read()
            if len(data)>10*1024*1024:return self.err(413)
            def f(d):
                i=str(len(d['files'])+1);v={'id':int(i),'uploader_id':u['id'],'filename':part.filename,'content_type':part.type or 'application/octet-stream','size':len(data),'created_at':now(),'data':base64.b64encode(data).decode()};d['files'][i]=v;return {k:v[k] for k in v if k!='data'}
            z=update(f);return self.j({'file':z},201)
        m=re.fullmatch(r'/api/files/(\d+)(/download)?',path)
        if m:
            z=read(lambda d:d['files'].get(m.group(1)))
            if not z:return self.err(404)
            if m.group(2):
                b=base64.b64decode(z['data']);self.send_response(200);self.send_header('Content-Type',z['content_type']);self.send_header('Content-Disposition','attachment; filename="'+z['filename']+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
            return self.j({'file':{k:v for k,v in z.items() if k!='data'}})
        gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',path)
        if gm and method in ('PATCH','DELETE'):
            slug,handle=gm.group(1),gm.group(2)
            def group_action(d):
                w=next((v for v in d['workspaces'].values() if v['slug']==slug),None)
                key=slug+':'+handle; g=d['groups'].get(key)
                if method=='DELETE':
                    if not g:return {'deleted':True,'status':200}
                    if g.get('creator_id')!=u['id'] and str(u['id'])!=str(w.get('owner_id')):return {'status':403}
                    del d['groups'][key];return {'deleted':True,'status':200}
                if not g:return {'status':404}
                if g.get('creator_id')!=u['id'] and str(u['id'])!=str(w.get('owner_id')):return {'status':403}
                x=self.body()
                if 'name' in x:g['name']=x['name']
                if 'user_ids' in x:g['member_user_ids']=x['user_ids']
                return {'group':g,'status':200}
            z=update(group_action); status=z.pop('status')
            return self.err(status) if status!=200 else self.j(z)
        if re.fullmatch(r'/api/workspaces/[^/]+/groups',path) and method in ('GET','POST'):
            slug=path.split('/')[3];w=read(lambda d:next((v for v in d['workspaces'].values() if v['slug']==slug),None))
            if not w or str(u['id']) not in w['members']:return self.err(403)
            if method=='GET':return self.j({'groups':read(lambda d:[g for g in d['groups'].values() if g['workspace_id']==w['id']])})
            x=self.body(); key=slug+':'+x.get('handle','')
            def f(d):
                if key in d['groups']:return None
                g={'id':len(d['groups'])+1,'handle':x.get('handle'),'name':x.get('name',''),'member_user_ids':x.get('user_ids',[]),'workspace_id':w['id'],'creator_id':u['id']};d['groups'][key]=g;return g
            z=update(f);return self.err(409) if z is None else self.j({'group':z},201)
        m=re.fullmatch(r'/api/channels/(\d+)/read',path)
        if m and method in ('GET','POST'):
            cid=m.group(1)
            c=read(lambda d:d['channels'].get(cid))
            if not c:return self.err(404)
            if u['id'] not in c.get('members',[]):return self.err(403)
            if method=='POST':
                x=self.body()
                return self.j({'read_state':{'channel_id':int(cid),'last_read_event_id':x.get('last_read_event_id',0),'unread_count':0,'mention_count':read(lambda d:d['users'].get(str(u['id']),{}).get('mention_count',0))}})
            return self.j({'read_state':{'channel_id':int(cid),'last_read_event_id':0,'unread_count':0,'mention_count':read(lambda d:d['users'].get(str(u['id']),{}).get('mention_count',0))}})
        self.err(404)
    def websocket(self,t):
        u=read(lambda d:d['users'].get(str(d['tokens'].get(t))))
        if not u:return self.err(401)
        key=self.headers.get('Sec-WebSocket-Key',''); acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',acc);self.end_headers()
        s=self.request
        with listeners_lock:
            listeners.append(s); active_ws_users.add(u['id'])
        stop_event=threading.Event()
        def socket_events():
            try:
                with open(EVENTS,'r',encoding='utf-8') as ef:
                    lines=ef.readlines()
                    pos=len(lines)
            except Exception:
                pos=0
            while not stop_event.is_set():
                try:
                    with open(EVENTS,'r',encoding='utf-8') as ef:
                        lines=ef.readlines()
                    for line in lines[pos:]:
                        try:
                            ev=json.loads(line)
                            if ev.get('type')=='user.updated': ws_send(s,ev)
                        except Exception:
                            pass
                    pos=len(lines)
                except Exception:
                    pass
                time.sleep(0.02)
        threading.Thread(target=socket_events,daemon=True).start()
        def heartbeat():
            while not stop_event.is_set():
                try:
                    ws_send(s,{'type':'heartbeat'})
                except Exception:
                    break
                time.sleep(0.2)
        threading.Thread(target=heartbeat,daemon=True).start()
        try:
            while True:
                h=s.recv(2)
                if not h:break
                n=h[1]&127; mask=s.recv(4)
                if n==126:n=struct.unpack('>H',s.recv(2))[0]
                data=s.recv(n); data=bytes(data[i]^mask[i%4] for i in range(n))
                x=json.loads(data)
                if x.get('type')=='subscribe':ws_send(s,{'type':'subscribed','channel_id':x.get('channel_id'),'head_event_id':0})
                elif x.get('type')=='resume':ws_send(s,{'type':'resumed','channel_id':x.get('channel_id'),'head_event_id':0})
        except:pass
        stop_event.set()
        with listeners_lock:
            if s in listeners:listeners.remove(s)
            active_ws_users.discard(u['id'])
            socket_locks.pop(id(s),None)
def main():
    global event_offset
    try:
        with open(EVENTS,'r',encoding='utf-8') as ef:
            event_offset=len(ef.readlines())
    except Exception:
        event_offset=0
    threading.Thread(target=event_watcher,daemon=True).start()
    port=int(sys.argv[1]);ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':main()
