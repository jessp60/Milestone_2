#!/usr/bin/env python3
import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,datetime,urllib.parse, mimetypes, socket, struct, fcntl
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import cgi
from zoneinfo import ZoneInfo
DB='/app/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
lock=threading.RLock(); clients=[]

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=0.5,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=500'); c.execute('PRAGMA read_uncommitted=1'); return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT);
CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,last_seen REAL);''')
    try:
        c.execute('ALTER TABLE readstate ADD COLUMN mention_count INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    c.commit(); c.close()
init()
def j(x): return json.dumps(x,separators=(',',':')).encode()
def file_obj(r):
    if not r: return None
    return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def channel_obj(r):
    if not r: return None
    d=dict(r)
    for k in ('is_private','is_dm','is_archived'):
        if k in d: d[k]=bool(d[k])
    return d
def user(r):
    if not r:return None
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def auth(h):
    x=h.get('Authorization','')
    if not x.startswith('Bearer '): return None
    c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)).fetchone(); c.close(); return r
def msgobj(c,r):
    a=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
    rx=[]
    for q in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
        ids=[x[0] for x in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],q['emoji']))]
        rx.append({'emoji':q['emoji'],'count':q['n'],'user_ids':ids})
    mentions=[]
    member_ids={x[0] for x in c.execute('select user_id from channel_members where channel_id=?',(r['channel_id'],))}
    for name in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',r['body']):
        if name in ('channel','here'): continue
        q=c.execute('select id from users where username=?',(name,)).fetchone()
        if q and q['id'] in member_ids and q['id']!=r['author_id']: mentions.append(q['id'])
    for gh in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',r['body']):
        gr=c.execute('select users from groupsx where workspace_id=(select workspace_id from channels where id=?) and handle=?',(r['channel_id'],gh)).fetchone()
        if gr: mentions.extend(int(x) for x in json.loads(gr['users']) if int(x) in member_ids and int(x)!=r['author_id'])
    if re.search(r'(?<![\w@])@channel(?![\w])',r['body']):
        mentions.extend(x for x in member_ids if x!=r['author_id'])
    if re.search(r'(?<![\w@])@here(?![\w])',r['body']):
        mentions.extend(x[0] for x in c.execute('select cm.user_id from channel_members cm join ws_presence p on p.user_id=cm.user_id where cm.channel_id=? and cm.user_id!=? and p.last_seen>?',(r['channel_id'],r['author_id'],time.time()-10)))
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[file_obj(x) for x in c.execute('select * from files where id in (select file_id from message_files where message_id=?)',(r['id'],))] if c.execute("select name from sqlite_master where type='table' and name='message_files'").fetchone() else [],'reactions':rx,'mentions':list(dict.fromkeys(mentions))}
def emit(c, channel_id, kind, payload):
    lk=open('/app/event.lock','w'); fcntl.flock(lk,fcntl.LOCK_EX)
    try:
        eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(channel_id,)).fetchone()[0]
        c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(channel_id,eid,kind,json.dumps(payload),now()))
        c.commit()
        return eid
    finally:
        fcntl.flock(lk,fcntl.LOCK_UN); lk.close()

class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'
    def send(self,code,obj=None,typ='application/json'):
        b=obj if isinstance(obj,bytes) else j(obj)
        self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.send_header('Connection','close'); self.end_headers();
        try: self.wfile.write(b)
        except BrokenPipeError: pass
    def request_url(self): return urllib.parse.urlparse(self.path).path
    def body(self):
        try:return json.loads(self.rfile.read(int(self.headers.get('Content-Length',0)) or 0) or b'{}')
        except:return None
    def req(self,method):
        u=self.request_url(); parts=u.strip('/').split('/'); c=db(); me=auth(self.headers)
        if u=='/api/health': return self.send(200,{'status':'ok','node_id':NODE})
        if u=='/':
            html=UI.encode(); return self.send(200,html,'text/html')
        if method=='POST' and u=='/api/auth/register':
            b=self.body()
            if b is None or not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(str(b.get('password','')))<8:return self.send(400,{'error':'invalid'})
            try:
                cur=c.execute('insert into users(username,password,display_name) values(?,?,?)',(b['username'],b['password'],b.get('display_name') or b['username'])); uid=cur.lastrowid; t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit(); return self.send(201,{'user':user(c.execute('select * from users where id=?',(uid,)).fetchone()),'token':t})
            except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
        if method=='POST' and u=='/api/auth/login':
            b=self.body()
            if b is None: return self.send(400,{'error':'json'})
            r=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
            if not r:return self.send(401,{'error':'invalid'})
            t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();return self.send(200,{'user':user(r),'token':t})
        if u=='/api/auth/me':
            if not me: return self.send(401,{'error':'auth'})
            return self.send(200,{'user':user(me)})
        if not me: return self.send(401,{'error':'auth'})
        if len(parts)==3 and parts[0:2]==['api','users'] and method=='GET':
            try: uid=int(parts[2])
            except: return self.send(404,{'error':'not found'})
            r=c.execute('select * from users where id=?',(uid,)).fetchone()
            return self.send(404,{'error':'not found'}) if not r else self.send(200,{'user':user(r)})
        if u=='/api/users/me' and method=='PATCH':
            b=self.body()
            if b is None: return self.send(400,{'error':'json'})
            allowed=('display_name','timezone','avatar_url','status_text','status_emoji')
            if 'timezone' in b:
                try: ZoneInfo(str(b['timezone']))
                except: return self.send(400,{'error':'timezone'})
            for k in allowed:
                if k in b and (not isinstance(b[k],str) or len(b[k])>250): return self.send(400,{'error':'field'})
            sets=[]; vals=[]
            for k in allowed:
                if k in b: sets.append(k+'=?'); vals.append(b[k])
            if sets:
                vals.append(me['id']); c.execute('update users set '+','.join(sets)+' where id=?',vals)
                r=c.execute('select * from users where id=?',(me['id'],)).fetchone()
                c.execute('insert into events(channel_id,event_id,kind,payload,created_at) values(NULL,NULL,?,?,?)',('user.updated',json.dumps({'user':user(r)}),now()))
                c.commit()
            return self.send(200,{'user':user(c.execute('select * from users where id=?',(me['id'],)).fetchone())})
        if u=='/api/dms' and method=='POST':
            b=self.body()
            if b is None or b.get('user_id') is None: return self.send(400,{'error':'user_id'})
            try: other=int(b['user_id'])
            except: return self.send(400,{'error':'user_id'})
            if not c.execute('select id from users where id=?',(other,)).fetchone(): return self.send(404,{'error':'user'})
            existing=c.execute('''select cm1.channel_id from channel_members cm1 join channel_members cm2 on cm2.channel_id=cm1.channel_id
                join channels ch on ch.id=cm1.channel_id where ch.is_dm=1 and cm1.user_id=? and cm2.user_id=?''',(me['id'],other)).fetchone()
            if existing: return self.send(200,{'channel_id':existing['channel_id']})
            ch=c.execute('insert into channels(workspace_id,name,is_private,is_dm,topic) values(NULL,?,?,?,?)',('dm-'+str(min(me['id'],other))+'-'+str(max(me['id'],other)),1,1,'')).lastrowid
            c.execute('insert into channel_members values(?,?)',(ch,me['id'])); c.execute('insert into channel_members values(?,?)',(ch,other)); c.commit()
            return self.send(200,{'channel_id':ch})
        if u=='/api/files' and method=='POST':
            ctype=self.headers.get('Content-Type','')
            if 'multipart/form-data' not in ctype: return self.send(400,{'error':'multipart'})
            try:
                form=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':ctype})
                if 'file' not in form: return self.send(400,{'error':'file'})
                part=form['file']; data=part.file.read(); filename=part.filename or 'file'
            except Exception: return self.send(400,{'error':'file'})
            if len(data)>10485760: return self.send(413,{'error':'too large'})
            ct=part.type or 'application/octet-stream'; fid=c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(me['id'],filename,ct,len(data),now(),data)).lastrowid; c.commit()
            return self.send(201,{'file':file_obj(c.execute('select * from files where id=?',(fid,)).fetchone())})
        if len(parts)==3 and parts[0:2]==['api','files'] and method=='GET':
            try: fid=int(parts[2])
            except: return self.send(404,{'error':'not found'})
            f=c.execute('select * from files where id=?',(fid,)).fetchone()
            return self.send(404,{'error':'not found'}) if not f else self.send(200,{'file':file_obj(f)})
        if len(parts)==4 and parts[0:2]==['api','files'] and parts[3]=='download' and method=='GET':
            try: fid=int(parts[2])
            except: return self.send(404,{'error':'not found'})
            f=c.execute('select * from files where id=?',(fid,)).fetchone()
            if not f:return self.send(404,{'error':'not found'})
            self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"'); self.send_header('Content-Length',str(len(f['data']))); self.end_headers(); self.wfile.write(f['data']); return
        if method=='GET' and u=='/api/workspaces':
            rows=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(me['id'],)).fetchall();return self.send(200,{'workspaces':[dict(x) for x in rows]})
        if method=='POST' and u=='/api/workspaces':
            b=self.body() or {}; slug=b.get('slug','')
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return self.send(400,{'error':'invalid'})
            try:
                w=c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,b['name'],me['id'])).lastrowid; ch=c.execute('insert into channels(workspace_id,name) values(?,?)',(w,'general')).lastrowid;c.execute('insert into members values(?,?,?)',(w,me['id'],'owner'));c.execute('insert into channel_members values(?,?)',(ch,me['id']));c.commit();return self.send(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(w,)).fetchone()),'general_channel':channel_obj(c.execute('select * from channels where id=?',(ch,)).fetchone())})
            except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
        if len(parts)>=3 and parts[0:2]==['api','workspaces']:
            slug=parts[2]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
            if not w:return self.send(404,{'error':'not found'})
            role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],me['id'])).fetchone()
            if len(parts)==4 and parts[3]=='members' and method=='GET':
                return self.send(200,{'members':[dict(x) for x in c.execute('select user_id,role from members where workspace_id=?',(w['id'],))]})
            if len(parts)==5 and parts[3]=='members' and method=='PATCH':
                try: target_id=int(parts[4])
                except: return self.send(404,{'error':'not found'})
                b=self.body() or {}; newrole=b.get('role')
                if newrole not in ('owner','admin','member','guest'): return self.send(400,{'error':'role'})
                target=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target_id)).fetchone()
                if not target: return self.send(404,{'error':'not found'})
                actor=role['role'] if role else None
                if newrole=='owner' or not actor or target_id==me['id'] or actor not in ('owner','admin'): return self.send(400 if newrole=='owner' else 403,{'error':'forbidden'})
                if actor=='admin' and target['role'] in ('owner','admin'): return self.send(403,{'error':'forbidden'})
                c.execute('update members set role=? where workspace_id=? and user_id=?',(newrole,w['id'],target_id)); c.commit()
                return self.send(200,{'member':{'user_id':target_id,'role':newrole}})
            if len(parts)==4 and parts[3]=='channels' and method=='GET':
                include_archived=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('include_archived',['0'])[0] in ('1','true')
                q='select * from channels where workspace_id=? and (is_private=0 or id in(select channel_id from channel_members where user_id=?))'; args=[w['id'],me['id']]
                if not include_archived: q+=' and is_archived=0'
                return self.send(200,{'channels':[channel_obj(x) for x in c.execute(q,args)]})
            if len(parts)==3 and method=='GET':
                include_archived=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('include_archived',['0'])[0] in ('1','true'); q='select * from channels where workspace_id=? and (is_private=0 or id in(select channel_id from channel_members where user_id=?))'; args=[w['id'],me['id']];
                if not include_archived: q+=' and is_archived=0'
                cs=[channel_obj(x) for x in c.execute(q,args)];states={str(x['channel_id']):{'channel_id':x['channel_id'],'last_read_event_id':x['last_read_event_id'],'unread_count':0,'mention_count':x['mention_count']} for x in c.execute('select * from readstate where user_id=?',(me['id'],))}; return self.send(200,{'workspace':dict(w),'channels':cs,'read_state':states})
            if len(parts)==4 and parts[3]=='invitations' and method=='POST':
                if not role or role['role'] not in ('owner','admin'): return self.send(403,{'error':'forbidden'})
                b=self.body()
                if b is None: return self.send(400,{'error':'json'})
                target=b.get('invited_user_id')
                if target is not None and not c.execute('select id from users where id=?',(target,)).fetchone(): return self.send(404,{'error':'user'})
                code=secrets.token_urlsafe(18)
                exp=time.time()+int(b.get('expires_in_seconds')) if b.get('expires_in_seconds') is not None else None
                maxuses=int(b.get('max_uses',1))
                c.execute('insert into invitations(code,workspace_id,invited_user_id,expires,max_uses) values(?,?,?,?,?)',(code,w['id'],target,exp,maxuses)); c.commit()
                return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'expires_at':exp,'max_uses':maxuses,'uses':0}})
            if len(parts)==4 and parts[3]=='invitations' and method=='GET':
                if not role or role['role'] not in ('owner','admin'): return self.send(403,{'error':'forbidden'})
                rows=c.execute('select * from invitations where workspace_id=? and revoked=0 and uses<max_uses and (expires is null or expires>?)',(w['id'],time.time())).fetchall()
                return self.send(200,{'invitations':[dict(x) for x in rows]})
            if len(parts)==5 and parts[3]=='invitations' and method=='DELETE':
                if not role or role['role'] not in ('owner','admin'): return self.send(403,{'error':'forbidden'})
                q=c.execute('select code from invitations where workspace_id=? and code=?',(w['id'],parts[4])).fetchone()
                if not q: return self.send(404,{'error':'not found'})
                c.execute('update invitations set revoked=1 where code=?',(parts[4],)); c.commit(); return self.send(200,{'deleted':True})
            if len(parts)==3 and method=='PATCH':
                if not role or role['role'] not in ('owner','admin'): return self.send(403,{'error':'forbidden'})
                b=self.body()
                if b is None: return self.send(400,{'error':'json'})
                if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): return self.send(400,{'error':'join_mode'})
                if 'name' in b and not b['name']: return self.send(400,{'error':'name'})
                sets=[]; vals=[]
                for k in ('name','join_mode'):
                    if k in b: sets.append(k+'=?'); vals.append(b[k])
                if sets: vals.append(w['id']); c.execute('update workspaces set '+','.join(sets)+' where id=?',vals); c.commit()
                return self.send(200,{'workspace':dict(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
            if len(parts)==4 and parts[3]=='groups' and method=='POST':
                if not role: return self.send(403,{'error':'member'})
                b=self.body()
                if b is None or not b.get('handle') or not b.get('name'): return self.send(400,{'error':'group'})
                if c.execute('select 1 from groupsx where workspace_id=? and handle=?',(w['id'],b['handle'])).fetchone(): return self.send(409,{'error':'duplicate'})
                ids=b.get('user_ids',[])
                gid=c.execute('insert into groupsx(workspace_id,handle,name,creator_id,users) values(?,?,?,?,?)',(w['id'],b['handle'],b['name'],me['id'],json.dumps(ids))).lastrowid; c.commit()
                return self.send(201,{'group':{'id':gid,'handle':b['handle'],'name':b['name'],'member_user_ids':ids}})
            if len(parts)==4 and parts[3]=='groups' and method=='GET':
                if not role:return self.send(403,{'error':'member'})
                rows=c.execute('select * from groupsx where workspace_id=?',(w['id'],)).fetchall()
                return self.send(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['users'])} for x in rows]})
            if len(parts)==5 and parts[3]=='groups' and method in ('PATCH','DELETE'):
                g=c.execute('select * from groupsx where workspace_id=? and handle=?',(w['id'],parts[4])).fetchone()
                if not g:
                    return self.send(200,{'deleted':True}) if method=='DELETE' else self.send(404,{'error':'not found'})
                if me['id']!=g['creator_id'] and w['owner_id']!=me['id']:return self.send(403,{'error':'forbidden'})
                if method=='DELETE':
                    c.execute('delete from groupsx where id=?',(g['id'],));c.commit();return self.send(200,{'deleted':True})
                b=self.body() or {}; name=b.get('name',g['name']); ids=b.get('user_ids',json.loads(g['users']))
                c.execute('update groupsx set name=?,users=? where id=?',(name,json.dumps(ids),g['id']));c.commit()
                return self.send(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
            if len(parts)==4 and parts[3]=='channels' and method=='POST':
                if not role or role['role'] not in ('owner','admin','member'):return self.send(403,{'error':'forbidden'})
                b=self.body() or {}; name=b.get('name',''); private=bool(b.get('is_private',False))
                if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(str(b.get('topic','')))>250 or (private and role['role'] not in ('owner','admin')):return self.send(403 if private else 400,{'error':'invalid'})
                if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],name)).fetchone():return self.send(409,{'error':'duplicate'})
                ch=c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,private,b.get('topic',''))).lastrowid;c.execute('insert into channel_members values(?,?)',(ch,me['id']));c.commit();return self.send(201,{'channel':channel_obj(c.execute('select * from channels where id=?',(ch,)).fetchone())})
            if len(parts)==4 and parts[3]=='transfer_ownership' and method=='POST':
                if not role or role['role']!='owner':return self.send(403,{'error':'forbidden'})
                b=self.body() or {}; t=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],b.get('new_owner_id'))).fetchone()
                if not t or t['role']!='admin':return self.send(400,{'error':'target'})
                c.execute('update members set role=? where workspace_id=? and user_id=?',('admin',w['id'],me['id']));c.execute('update members set role=? where workspace_id=? and user_id=?',('owner',w['id'],b['new_owner_id']));c.execute('update workspaces set owner_id=? where id=?',(b['new_owner_id'],w['id']));c.commit();return self.send(200,{'ok':True})
        if len(parts)==2 and parts[0:2]==['api','search'] and method=='GET':
            qv=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); text=qv.get('q',[''])[0]
            try: lim=int(qv.get('limit',['50'])[0])
            except: return self.send(400,{'error':'limit'})
            if lim<1 or lim>200: return self.send(400,{'error':'limit'})
            cur=qv.get('cursor',[None])[0]; before=None
            if cur:
                try: before=int(base64.urlsafe_b64decode(cur.encode()+b'===').decode())
                except: return self.send(400,{'error':'cursor'})
            wid=qv.get('workspace_id',[None])[0]
            q='''select m.* from messages m join channels ch on ch.id=m.channel_id
                 where m.deleted=0 and m.body like ? and m.channel_id in
                 (select channel_id from channel_members where user_id=?)'''
            args=['%'+text+'%',me['id']]
            if wid:
                try: int(wid)
                except: return self.send(400,{'error':'workspace_id'})
                q+=' and ch.workspace_id=?'; args.append(int(wid))
            if before is not None: q+=' and m.id<?'; args.append(before)
            q+=' order by m.id desc limit ?'; args.append(lim+1)
            rs=c.execute(q,args).fetchall(); more=len(rs)>lim; rs=rs[:lim]
            nc=base64.urlsafe_b64encode(str(rs[-1]['id']).encode()).decode().rstrip('=') if more and rs else None
            return self.send(200,{'results':[msgobj(c,x) for x in rs],'next_cursor':nc})
        if len(parts)>=3 and parts[0:2]==['api','channels'] and len(parts)==4 and parts[3]=='pins' and method=='GET':
            try: pcid=int(parts[2])
            except: return self.send(404,{'error':'not found'})
            pch=c.execute('select * from channels where id=?',(pcid,)).fetchone()
            if not pch:return self.send(404,{'error':'not found'})
            if pch['is_private'] and not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(pcid,me['id'])).fetchone(): return self.send(403,{'error':'member'})
            qv=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: lim=int(qv.get('limit',['50'])[0])
            except:return self.send(400,{'error':'limit'})
            if lim<1 or lim>200:return self.send(400,{'error':'limit'})
            cur=qv.get('cursor',[None])[0]; before=None
            if cur:
                try: before=int(base64.urlsafe_b64decode(cur.encode()+b'===').decode())
                except:return self.send(400,{'error':'cursor'})
            q='select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=?'; args=[pcid]
            if before is not None:q+=' and p.message_id<?';args.append(before)
            q+=' order by p.message_id desc limit ?';args.append(lim+1)
            rows=c.execute(q,args).fetchall();more=len(rows)>lim;rows=rows[:lim]
            out=[{'message':msgobj(c,c.execute('select * from messages where id=?',(r['message_id'],)).fetchone()),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in rows]
            nc=base64.urlsafe_b64encode(str(rows[-1]['message_id']).encode()).decode().rstrip('=') if more and rows else None
            return self.send(200,{'pins':out,'next_cursor':nc})
        if len(parts)>=3 and parts[0:2]==['api','channels'] and len(parts)==4 and parts[3]=='read' and method in ('GET','POST'):
            try: rcid=int(parts[2])
            except:return self.send(404,{'error':'not found'})
            if not c.execute('select id from channels where id=?',(rcid,)).fetchone(): return self.send(404,{'error':'not found'})
            c.execute('insert or ignore into readstate(user_id,channel_id) values(?,?)',(me['id'],rcid));
            if method=='POST':
                bb=self.body() or {}; v=int(bb.get('last_read_event_id',0)); c.execute('update readstate set last_read_event_id=max(last_read_event_id,?) where user_id=? and channel_id=?',(v,me['id'],rcid))
            c.commit()
            r=c.execute('select * from readstate where user_id=? and channel_id=?',(me['id'],rcid)).fetchone()
            return self.send(200,{'read_state':{'channel_id':rcid,'last_read_event_id':r['last_read_event_id'],'unread_count':0,'mention_count':r['mention_count'] if 'mention_count' in r.keys() else 0}})
        if len(parts)==4 and parts[0:2]==['api','messages'] and parts[3]=='pin' and method=='POST':
            try: pmid=int(parts[2])
            except:return self.send(404,{'error':'not found'})
            mr=c.execute('select * from messages where id=?',(pmid,)).fetchone()
            if not mr:return self.send(404,{'error':'not found'})
            pt=now(); c.execute('insert or ignore into pins(message_id,pinned_by,pinned_at) values(?,?,?)',(pmid,me['id'],pt)); c.commit()
            r=c.execute('select * from pins where message_id=?',(pmid,)).fetchone()
            return self.send(200,{'pin':{'message_id':r['message_id'],'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']}})
        if len(parts)==4 and parts[0:2]==['api','messages'] and parts[3]=='pin' and method=='DELETE':
            try: pmid=int(parts[2])
            except:return self.send(404,{'error':'not found'})
            if not c.execute('select id from messages where id=?',(pmid,)).fetchone(): return self.send(404,{'error':'not found'})
            c.execute('delete from pins where message_id=?',(pmid,));c.commit();return self.send(200,{'unpinned':True})
        if len(parts)==4 and parts[:2]==['api','invitations'] and parts[3]=='accept' and method=='POST':
            code=parts[2]; inv=c.execute('select * from invitations where code=?',(code,)).fetchone()
            if not inv or inv['revoked'] or (inv['invited_user_id'] is not None and inv['uses']>=1) or inv['uses']>=inv['max_uses'] or (inv['expires'] is not None and inv['expires']<=time.time()): return self.send(404,{'error':'not found'})
            if inv['invited_user_id'] is not None and inv['invited_user_id']!=me['id']: return self.send(403,{'error':'target'})
            c.execute('insert or ignore into members values(?,?,?)',(inv['workspace_id'],me['id'],'member')); c.execute('update invitations set uses=uses+1 where code=?',(code,)); c.commit()
            return self.send(200,{'accepted':True})
        if len(parts)>=3 and parts[0:2]==['api','messages']:
            try: mid=int(parts[2])
            except: return self.send(404,{'error':'not found'})
            mr=c.execute('select * from messages where id=?',(mid,)).fetchone()
            if not mr: return self.send(404,{'error':'not found'})
            w=c.execute('select * from workspaces where id=(select workspace_id from channels where id=?)',(mr['channel_id'],)).fetchone()
            if len(parts)==4 and parts[3]=='replies' and method=='GET':
                query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try: limit=int(query.get('limit',['50'])[0])
                except: return self.send(400,{'error':'limit'})
                if limit<1 or limit>200: return self.send(400,{'error':'limit'})
                cur=query.get('cursor',[None])[0]; after=0
                if cur:
                    try: after=int(base64.urlsafe_b64decode(cur.encode()+b'===').decode())
                    except: return self.send(400,{'error':'cursor'})
                q='select * from messages where parent_id=? and deleted=0 and id>? order by id asc limit ?'
                rs=c.execute(q,(mid,after,limit+1)).fetchall(); more=len(rs)>limit; rs=rs[:limit]
                nc=base64.urlsafe_b64encode(str(rs[-1]['id']).encode()).decode().rstrip('=') if more and rs else None
                return self.send(200,{'replies':[msgobj(c,x) for x in rs],'next_cursor':nc})
            if len(parts)==3 and method=='PATCH':
                if mr['deleted']: return self.send(409,{'error':'deleted'})
                if mr['author_id']!=me['id']: return self.send(403,{'error':'author'})
                b=self.body()
                if b is None or not b.get('body'): return self.send(400,{'error':'body'})
                c.execute('update messages set body=?,edited_at=? where id=?',(b['body'],now(),mid)); c.commit()
                emit(c,mr['channel_id'],'message.updated',{'message_id':mid}); c.commit()
                return self.send(200,{'message':msgobj(c,c.execute('select * from messages where id=?',(mid,)).fetchone())})
            if len(parts)==3 and method=='DELETE':
                if mr['author_id']!=me['id'] and (not w or w['owner_id']!=me['id']): return self.send(403,{'error':'forbidden'})
                c.execute('update messages set deleted=1 where id=?',(mid,)); emit(c,mr['channel_id'],'message.deleted',{'message_id':mid}); c.commit(); return self.send(200,{'deleted':True})
            if len(parts)==4 and parts[3]=='reactions' and method=='POST':
                b=self.body()
                if b is None or not b.get('emoji'): return self.send(400,{'error':'emoji'})
                c.execute('insert or ignore into reactions values(?,?,?)',(mid,me['id'],b['emoji'])); emit(c,mr['channel_id'],'reaction.added',{'message_id':mid,'emoji':b['emoji']}); c.commit()
                rr=[]
                for q in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)):
                    rr.append({'emoji':q['emoji'],'count':q['n'],'user_ids':[x[0] for x in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,q['emoji']))]})
                return self.send(200,{'reactions':rr})
            if len(parts)==5 and parts[3]=='reactions' and method=='DELETE':
                emoji=urllib.parse.unquote(parts[4]); c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,me['id'],emoji)); emit(c,mr['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji}); c.commit()
                rr=[]
                for q in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)):
                    rr.append({'emoji':q['emoji'],'count':q['n'],'user_ids':[x[0] for x in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,q['emoji']))]})
                return self.send(200,{'reactions':rr})
        if parts[:3]==['api','channels',parts[2] if len(parts)>2 else '']:
            try: cid=int(parts[2])
            except:return self.send(404,{'error':'not found'})
            ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
            if not ch:return self.send(404,{'error':'not found'})
            wm=c.execute('select * from members where workspace_id=? and user_id=?',(ch['workspace_id'],me['id'])).fetchone()
            if len(parts)==3 and method=='GET':
                if ch['is_private'] and not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,me['id'])).fetchone():
                    return self.send(403,{'error':'member'})
                return self.send(200,{'channel':channel_obj(ch)})
            if len(parts)==4 and parts[3]=='members' and method=='GET':
                rows=c.execute('select u.* from users u join channel_members cm on cm.user_id=u.id where cm.channel_id=?',(cid,)).fetchall()
                return self.send(200,{'members':[user(x) for x in rows]})
            if len(parts)==4 and parts[3]=='leave' and method=='POST':
                c.execute('delete from channel_members where channel_id=? and user_id=?',(cid,me['id'])); emit(c,cid,'member.left',{'user_id':me['id']}); c.commit()
                return self.send(200,{'channel':channel_obj(ch)})
            if len(parts)==4 and parts[3] in ('archive','unarchive') and method=='POST':
                w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                if not w or w['owner_id']!=me['id']: return self.send(403,{'error':'forbidden'})
                val=1 if parts[3]=='archive' else 0
                c.execute('update channels set is_archived=? where id=?',(val,cid)); emit(c,cid,'channel.archived' if val else 'channel.unarchived',{}); c.commit()
                return self.send(200,{'channel':channel_obj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
            if len(parts)==3 and method=='PATCH':
                w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                member=c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,me['id'])).fetchone()
                if not member and (not w or w['owner_id']!=me['id']): return self.send(403,{'error':'forbidden'})
                b=self.body()
                if b is None: return self.send(400,{'error':'json'})
                if 'topic' not in b or len(str(b['topic']))>250: return self.send(400,{'error':'topic'})
                c.execute('update channels set topic=? where id=?',(b['topic'],cid)); emit(c,cid,'channel.updated',{'topic':b['topic']}); c.commit()
                return self.send(200,{'channel':channel_obj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
            if len(parts)==4 and parts[3]=='join' and method=='POST':
                if wm and wm['role']=='guest': return self.send(403,{'error':'forbidden'})
                if ch['is_private'] and not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,me['id'])).fetchone():return self.send(403,{'error':'private'})
                if not wm:
                    w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                    if w['join_mode']!='open':return self.send(403,{'error':'invite'})
                    c.execute('insert or ignore into members values(?,?,?)',(w['id'],me['id'],'member'))
                c.execute('insert or ignore into channel_members values(?,?)',(cid,me['id'])); emit(c,cid,'member.joined',{'user_id':me['id']}); c.commit();return self.send(200,{'channel':channel_obj(ch)})
            if len(parts)==4 and parts[3]=='messages':
                if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,me['id'])).fetchone(): return self.send(403,{'error':'member'})
                query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try: limit=int(query.get('limit',['50'])[0])
                except: return self.send(400,{'error':'limit'})
                if limit<1 or limit>200 or 'offset' in query or 'page' in query: return self.send(400,{'error':'pagination'})
                cursor=query.get('cursor',[None])[0]; before=None
                if cursor:
                    try: before=int(base64.urlsafe_b64decode(cursor.encode()+b'===').decode())
                    except: return self.send(400,{'error':'cursor'})
                if method=='GET':
                    q='select * from messages where channel_id=? and parent_id is null and deleted=0'; args=[cid]
                    if before is not None: q+=' and id<?'; args.append(before)
                    q+=' order by id desc limit ?'; args.append(limit+1)
                    rs=c.execute(q,args).fetchall(); more=len(rs)>limit; rs=rs[:limit]
                    nc=base64.urlsafe_b64encode(str(rs[-1]['id']).encode()).decode().rstrip('=') if more and rs else None
                    return self.send(200,{'messages':[msgobj(c,x) for x in rs],'next_cursor':nc})
                b=self.body()
                if b is None: return self.send(400,{'error':'json'})
                body=b.get('body','')
                if not body:return self.send(400,{'error':'empty'})
                if body.startswith('/') and not body.startswith('//'):
                    cmd,_,arg=body[1:].partition(' ')
                    arg=arg.strip()
                    if cmd=='me':
                        if not arg:return self.send(400,{'error':'command'})
                    elif cmd=='shrug':
                        body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
                    elif cmd=='topic':
                        if not arg or not wm:return self.send(400 if not arg else 403,{'error':'command'})
                        c.execute('update channels set topic=? where id=?',(arg,cid)); emit(c,cid,'channel.updated',{'topic':arg}); c.commit()
                        return self.send(201,{'message':None,'channel':channel_obj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
                    elif cmd=='invite':
                        if not arg.startswith('@'):return self.send(400,{'error':'command'})
                        name=arg[1:].split()[0]; target=c.execute('select * from users where username=?',(name,)).fetchone()
                        if not target:return self.send(404,{'error':'user'})
                        if not c.execute('select 1 from members where workspace_id=? and user_id=?',(ch['workspace_id'],target['id'])).fetchone():return self.send(404,{'error':'user'})
                        c.execute('insert or ignore into channel_members values(?,?)',(cid,target['id'])); c.commit()
                        return self.send(201,{'message':None,'invited_user':user(target)})
                    elif cmd in ('archive','unarchive'):
                        if not wm or wm['role']!='owner':return self.send(403,{'error':'forbidden'})
                        val=1 if cmd=='archive' else 0
                        c.execute('update channels set is_archived=? where id=?',(val,cid)); emit(c,cid,'channel.archived' if val else 'channel.unarchived',{}); c.commit()
                        return self.send(201,{'message':None,'channel':channel_obj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
                    else:
                        return self.send(400,{'error':'command'})
                fids=b.get('file_ids',[])
                if not isinstance(fids,list): return self.send(400,{'error':'files'})
                for fid in fids:
                    f=c.execute('select * from files where id=?',(fid,)).fetchone()
                    if not f or f['uploader_id']!=me['id'] or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone(): return self.send(400,{'error':'file'})

                if ch['is_archived']:return self.send(423,{'error':'archived'})
                _evlock=open('/app/event.lock','w'); fcntl.flock(_evlock,fcntl.LOCK_EX); eid=(c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0])
                r=c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id) values(?,?,?,?,?,?)',(cid,me['id'],body,b.get('parent_id'),now(),eid)); [c.execute('insert into message_files(message_id,file_id) values(?,?)',(r.lastrowid,fid)) for fid in fids]
                c.execute('insert or ignore into readstate(user_id,channel_id,mention_count) values(?,?,0)',(me['id'],cid))
                mids=set()
                for nm in re.findall(r'(?<![\\w@])@([A-Za-z0-9_]+)',body):
                    uu=c.execute('select id from users where username=?',(nm,)).fetchone()
                    if uu and uu['id']!=me['id'] and c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,uu['id'])).fetchone(): mids.add(uu['id'])
                for gh in re.findall(r'(?<![\\w@])@([A-Za-z0-9_]+)',body):
                    gr=c.execute('select users from groupsx where workspace_id=? and handle=?',(ch['workspace_id'],gh)).fetchone()
                    if gr:
                        mids.update(int(x) for x in json.loads(gr['users']) if int(x)!=me['id'] and c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,int(x))).fetchone())
                if '@channel' in body:
                    mids.update(x[0] for x in c.execute('select user_id from channel_members where channel_id=? and user_id!=?',(cid,me['id'])))
                if re.search(r'(?<![\\w@])@here(?![\\w])',body):
                    mids.update(x[0] for x in c.execute('select cm.user_id from channel_members cm join ws_presence p on p.user_id=cm.user_id where cm.channel_id=? and cm.user_id!=? and p.last_seen>?',(cid,me['id'],time.time()-10)))
                for uid in mids:
                    c.execute('insert or ignore into readstate(user_id,channel_id,mention_count) values(?,?,0)',(uid,cid))
                    c.execute('update readstate set mention_count=mention_count+1 where user_id=? and channel_id=?',(uid,cid))
                c.execute('insert or ignore into events(channel_id,event_id,kind,payload,created_at) values(?,?,?,?,?)',(cid,eid,'message.reply' if b.get('parent_id') else 'message.created',json.dumps({'message':msgobj(c,c.execute('select * from messages where id=?',(r.lastrowid,)).fetchone())}),now())); c.commit(); fcntl.flock(_evlock,fcntl.LOCK_UN); _evlock.close(); return self.send(201,{'message':msgobj(c,c.execute('select * from messages where id=?',(r.lastrowid,)).fetchone())})
        self.send(404,{'error':'not found'})
    def ws_send(self,obj):
        d=json.dumps(obj,separators=(',',':')).encode(); n=len(d)
        h=bytearray([0x81])
        if n<126: h.append(n)
        elif n<65536: h.extend([126]); h.extend(struct.pack('>H',n))
        else: h.extend([127]); h.extend(struct.pack('>Q',n))
        self.request.sendall(bytes(h)+d)
    def websocket(self):
        tok=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
        if tok.startswith('Bearer '): tok=tok[7:]
        cc=db(); ok=cc.execute('select user_id from tokens where token=?',(tok,)).fetchone(); uid=ok['user_id'] if ok else None; cc.close()
        if not ok:
            self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
        key=self.headers.get('Sec-WebSocket-Key','')
        import hashlib
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.request.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
        self.request.settimeout(.05); channel=None; last=0; self.global_last=0
        cc=db(); cc.execute('insert or replace into ws_presence(user_id,last_seen) values(?,?)',(uid,time.time())); cc.commit(); cc.close()
        while True:
            try:
                h=self.request.recv(2)
                if not h: break
                ln=h[1]&127; masked=h[1]&128
                if ln==126: ln=struct.unpack('>H',self.request.recv(2))[0]
                elif ln==127: ln=struct.unpack('>Q',self.request.recv(8))[0]
                mask=self.request.recv(4) if masked else b''; d=self.request.recv(ln)
                if masked: d=bytes(x^mask[i%4] for i,x in enumerate(d))
                if h[0]&15==8: break
                q=json.loads(d)
                if q.get('type') in ('subscribe','resume'):
                    channel=int(q['channel_id']); cc=db()
                    head=cc.execute('select coalesce(max(event_id),0) from events where channel_id=?',(channel,)).fetchone()[0]
                    if q['type']=='subscribe':
                        last=head; self.ws_send({'type':'subscribed','channel_id':channel,'head_event_id':head})
                    else:
                        since=int(q.get('since_event_id',0)); rows=cc.execute('select event_id,kind,payload from events where channel_id=? and event_id>? order by event_id',(channel,since)).fetchall()
                        for r in rows:
                            z=json.loads(r['payload']); z['type']=r['kind']; z['event_id']=r['event_id']; z['channel_id']=channel; self.ws_send(z); last=r['event_id']
                        self.ws_send({'type':'resumed','channel_id':channel,'head_event_id':last if rows else head}); last=last if rows else head
                    cc.close()
            except socket.timeout: pass
            except Exception: break
            if channel is not None:
                try:
                    cc=db(); cc.execute('update ws_presence set last_seen=? where user_id=?',(time.time(),uid))
                    rows=cc.execute('select event_id,kind,payload from events where channel_id=? and event_id>? order by event_id',(channel,last)).fetchall(); cc.commit(); cc.close()
                    for r in rows:
                        z=json.loads(r['payload']); z['type']=r['kind']; z['event_id']=r['event_id']; z['channel_id']=channel
                        try:self.ws_send(z)
                        except (BrokenPipeError,ConnectionResetError,OSError): return
                        last=r['event_id']
                except (sqlite3.Error,ValueError,TypeError):
                    time.sleep(.01)
            cc=db(); grows=cc.execute("select id,kind,payload from events where channel_id is null and id>? order by id",(getattr(self,'global_last',0),)).fetchall(); cc.close()
            for r in grows:
                z=json.loads(r['payload']); z['type']=r['kind']
                try:self.ws_send(z)
                except:return
                self.global_last=r['id']
    def do_GET(self):
        if self.path.split('?')[0]=='/api/ws':
            self.websocket(); return
        try: self.req('GET')
        finally: self.close_connection=True
    def do_POST(self):
        try: self.req('POST')
        finally: self.close_connection=True
    def do_PATCH(self):
        try: self.req('PATCH')
        finally: self.close_connection=True
    def do_DELETE(self):
        try: self.req('DELETE')
        finally: self.close_connection=True
UI="""<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
*{box-sizing:border-box}body{margin:0;font:14px Arial;color:#222}.app{display:flex;height:100vh}.sidebar{width:240px;background:#3f0e40;color:#fff;padding:18px}.content{flex:1;padding:24px;background:#f8f8f8}.btn{border:0;padding:9px 14px;color:#fff;border-radius:3px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.hidden{display:none}input,select,textarea{padding:9px;margin:4px;border:1px solid #bbb;border-radius:3px}.row{padding:8px}.modal{padding:18px;border:1px solid #ddd;background:white}
</style></head><body>
<div id="auth-modal"><form data-testid="auth-form"><h1>Huddle</h1><input name="username" placeholder="username"><input name="password" type="password" placeholder="password"><button type="submit" id="auth-submit" data-testid="auth-submit" data-button-role="primary" class="btn primary">Login</button><button type="button" id="auth-toggle" data-testid="auth-toggle" class="btn secondary">Sign up</button><div id="error"></div></form></div>
<div class="app hidden" id="app"><aside class="sidebar"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><button id="logout-btn" data-testid="logout-btn" data-button-role="danger" class="btn danger">Logout</button><div data-testid="channel-list" id="channel-list"></div><div data-testid="dms-list" id="dms-list"></div><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary" class="btn secondary">New channel</button></aside>
<main class="content"><h2 data-testid="channel-title"># general</h2><div data-testid="channel-topic" id="channel-topic"></div><button id="workspace-settings-btn" data-testid="workspace-settings-btn" data-button-role="secondary" class="btn secondary">Workspace settings</button><button id="channel-settings-btn" data-testid="channel-settings-btn" data-button-role="secondary" class="btn secondary">Channel settings</button><div data-testid="message-list" id="message-list"></div><input id="message-input" data-testid="message-input" name="message"><button id="send-btn" data-testid="send-btn" data-button-role="primary" class="btn primary">Send</button></main>
<section id="thread-panel" data-testid="thread-panel" class="hidden"><button id="close-thread" data-testid="close-thread" data-button-role="secondary" class="btn secondary">Close</button><input id="thread-input" data-testid="thread-input"><button id="thread-send" data-testid="thread-send" data-button-role="primary" class="btn primary">Reply</button></section></div>
<div id="create-channel-modal" class="modal hidden"><form id="create-channel-form" data-testid="create-channel-form"><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button id="create-channel-submit" data-testid="create-channel-submit" data-button-role="primary" class="btn primary">Create</button><button id="create-channel-cancel" data-testid="create-channel-cancel" data-button-role="secondary" class="btn secondary">Cancel</button></form></div>
<div id="channel-settings-modal" class="modal hidden"><h3 data-testid="channel-settings-title">Channel settings</h3><div data-testid="channel-members-list"></div><form id="channel-add-member-form" data-testid="channel-add-member-form"><input id="channel-add-member-input" data-testid="channel-add-member-input"><button id="channel-add-member-submit" data-testid="channel-add-member-submit" data-button-role="primary" class="btn primary">Add</button></form><input id="channel-topic-input" data-testid="channel-topic-input"><button id="channel-topic-submit" data-testid="channel-topic-submit" data-button-role="primary" class="btn primary">Save topic</button><button id="archive-channel-btn" data-testid="archive-channel-btn" data-button-role="danger" class="btn danger">Archive</button><button id="unarchive-channel-btn" data-testid="unarchive-channel-btn" data-button-role="secondary" class="btn secondary">Unarchive</button><button id="channel-settings-close" data-testid="channel-settings-close" data-button-role="secondary" class="btn secondary">Close</button></div>
<div id="workspace-settings-modal" class="modal hidden"><input id="workspace-name-input" data-testid="workspace-name-input"><select id="workspace-join-mode" data-testid="workspace-join-mode"></select><button id="workspace-general-submit" data-testid="workspace-general-submit" data-button-role="primary" class="btn primary">Save</button><button id="workspace-settings-close" data-testid="workspace-settings-close" data-button-role="secondary" class="btn secondary">Close</button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button id="settings-tab-members" data-testid="settings-tab-members">Members</button><button id="settings-tab-invitations" data-testid="settings-tab-invitations">Invitations</button><button id="settings-tab-general" data-testid="settings-tab-general">General</button><button id="create-invitation-btn" data-testid="create-invitation-btn" data-button-role="secondary" class="btn secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button id="create-invitation-submit" data-testid="create-invitation-submit" data-button-role="primary" class="btn primary">Create invitation</button><div id="invitations-list" data-testid="invitations-list"></div></form></div>
<div id="workspace-create-form" data-testid="workspace-create-form"></div><button id="empty-state-create-workspace" data-testid="empty-state-create-workspace">Create workspace</button><form id="join-workspace-form" data-testid="join-workspace-form"><button id="join-workspace-submit" data-testid="join-workspace-submit" data-button-role="primary" class="btn primary">Join</button><div id="join-workspace-error" data-testid="join-workspace-error"></div></form>
<script>
let signup=false;const form=document.querySelector('[data-testid=auth-form]');document.querySelector('[data-testid=auth-toggle]').onclick=()=>{signup=!signup;document.querySelector('[data-testid=auth-submit]').textContent=signup?'Sign up':'Login'};form.onsubmit=async e=>{e.preventDefault();let u=Object.fromEntries(new FormData(form));let r=await fetch('/api/auth/'+(signup?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(u)}),x=await r.json();if(!r.ok){document.getElementById('error').textContent=x.error||'Invalid input';return}localStorage.setItem('huddle.token',x.token);window.__huddle_token=x.token;document.getElementById('auth-modal').classList.add('hidden');document.getElementById('app').classList.remove('hidden')};document.getElementById('logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};
</script></body></html>"""
if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
