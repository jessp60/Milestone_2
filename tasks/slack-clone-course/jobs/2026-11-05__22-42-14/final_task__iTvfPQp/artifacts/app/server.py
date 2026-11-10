#!/usr/bin/env python3
import os, re, json, uuid, hashlib, secrets, sqlite3, threading, time, mimetypes, urllib.parse, base64, struct, socket, select, cgi
from zoneinfo import available_timezones
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DB='/tmp/huddle.sqlite3'
NODE=int(os.environ.get('NODE_ID','0'))
PORT=int(os.environ.get('PORT',8000))
WS_LOCK=threading.Lock(); WS=[]

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=1,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=750')
    return c
def init():
    c=db()
    try: c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'")
    except sqlite3.OperationalError: pass
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS online_ws(user_id INTEGER PRIMARY KEY,updated_at INTEGER);
    '''); c.commit(); c.close()
def user_obj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def ws_obj(r): return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}
def ch_obj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def get_user(c,uid): return c.execute('select * from users where id=?',(uid,)).fetchone()
def emit(c,channel_id,kind,payload):
    head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(channel_id,)).fetchone()[0]
    eid=int(head)+1
    data=dict(payload or {}); data['type']=kind
    if channel_id != 0:
        data.update({'channel_id':channel_id,'event_id':eid})
    c.execute('insert into events values(?,?,?,?)',(channel_id,eid,kind,json.dumps(data)))
    return eid
def auth(h):
    x=h.headers.get('Authorization','')
    if not x.startswith('Bearer '): return None
    c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)).fetchone(); c.close()
    return r
def msg_obj(c,r):
    a=get_user(c,r['author_id'])
    rx=[]
    for z in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
        ids=[x[0] for x in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],z['emoji']))]
        rx.append({'emoji':z['emoji'],'count':z['n'],'user_ids':ids})
    fs=[dict(x) for x in c.execute('select id,uploader_id,filename,content_type,size,created_at from files where attached=?',(r['id'],))]
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rx,'mentions':json.loads(r['mentions']) if r['mentions'] else []}
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def sendj(self,code,obj,headers=None):
        b=json.dumps(obj,separators=(',',':')).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); 
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
    def js(self):
        try:
            self._bad_json=False
            return json.loads(self.body() or b'{}')
        except Exception:
            self._bad_json=True
            return {}
    def route(self):
        return urllib.parse.urlparse(self.path)
    def req(self):
        p=self.route(); return p.path,urllib.parse.parse_qs(p.query)
    def need(self):
        u=auth(self)
        if not u: self.sendj(401,{'error':'unauthorized'})
        return u
    def do_GET(self):
        p,q=self.req()
        if p=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
        if p=='/' or p.startswith('/static'): return self.page()
        if p=='/api/ws': return self.websocket()
        u=self.need()
        if not u:return
        c=db()
        try:
            if p=='/api/auth/me': return self.sendj(200,{'user':user_obj(u)})
            m=re.fullmatch(r'/api/users/(\d+)',p)
            if m:
                r=get_user(c,int(m.group(1))); return self.sendj(200,{'user':user_obj(r)}) if r else self.sendj(404,{})
            if p=='/api/workspaces':
                a=[] 
                for r in c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)): a.append(ws_obj(r))
                return self.sendj(200,{'workspaces':a})
            m=re.fullmatch(r'/api/workspaces/([^/]+)(?:/(.*))?',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                tail=m.group(2)
                if tail=='members': return self.sendj(200,{'members':[dict(x) for x in c.execute('select user_id,role from members where workspace_id=?',(w['id'],))]})
                if tail=='invitations':
                    actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                    if not actor or actor['role'] not in ('owner','admin'): return self.sendj(403,{})
                    rows=c.execute('select * from invitations where workspace_id=? and max_uses>uses and (expires=0 or expires>?)',(w['id'],int(time.time()))).fetchall()
                    return self.sendj(200,{'invitations':[dict(x) for x in rows]})
                if tail=='channels' or tail is None:
                    include=q.get('include_archived',['0'])[0] in ('1','true')
                    chans=[]
                    for x in c.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'],include)):
                        if not x['is_private'] or c.execute('select 1 from channel_members where channel_id=? and user_id=?',(x['id'],u['id'])).fetchone() or w['owner_id']==u['id']:
                            chans.append(ch_obj(x))
                    if tail is None:
                        states={}
                        for xx in chans:
                            rr=c.execute('select last_read_event_id from reads where user_id=? and channel_id=?',(u['id'],xx['id'])).fetchone()
                            states[str(xx['id'])]={'channel_id':xx['id'],'last_read_event_id':int(rr['last_read_event_id']) if rr else 0,'unread_count':0,'mention_count':0}
                        return self.sendj(200,{'workspace':ws_obj(w),'channels':chans,'read_state':states})
                    return self.sendj(200,{'channels':chans})
                if tail=='groups': return self.sendj(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in c.execute('select * from groups_ where workspace_id=?',(w['id'],))]})
            m=re.fullmatch(r'/api/channels/(\d+)(?:/(.*))?',p)
            if m:
                ch=c.execute('select * from channels where id=?',(int(m.group(1)),)).fetchone()
                if not ch:return self.sendj(404,{})
                if ch['is_dm'] and not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone(): return self.sendj(403,{})
                tail=m.group(2) or ''
                if tail=='members': 
                    return self.sendj(200,{'members':[user_obj(x) for x in c.execute('select u.* from users u join channel_members cm on cm.user_id=u.id where cm.channel_id=?',(ch['id'],))]})
                if tail=='messages':
                    try:
                        limit=int(q.get('limit',['50'])[0])
                    except Exception:
                        return self.sendj(400,{})
                    if limit<1 or limit>200 or 'page' in q or 'offset' in q:return self.sendj(400,{})
                    cursor=q.get('cursor',[None])[0]
                    before=None
                    if cursor is not None:
                        if not cursor.isdigit():return self.sendj(400,{})
                        before=int(cursor)
                    sql='select * from messages where channel_id=? and deleted=0 and parent_id is null'
                    args=[ch['id']]
                    if before is not None:sql+=' and id<?';args.append(before)
                    sql+=f' order by id desc limit {limit+1}'
                    rows=c.execute(sql,args).fetchall()
                    more=len(rows)>limit; rows=rows[:limit]
                    nxt=str(rows[-1]['id']) if more and rows else None
                    return self.sendj(200,{'messages':[msg_obj(c,x) for x in rows],'next_cursor':nxt})
                if tail=='pins':
                    if ch['is_private'] and not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.sendj(403,{})
                    try: limit=int(q.get('limit',['50'])[0])
                    except:return self.sendj(400,{})
                    if limit<1 or limit>200:return self.sendj(400,{})
                    cur=q.get('cursor',[None])[0]; args=[ch['id']]
                    sql='select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=?'
                    if cur is not None:
                        if not cur.isdigit():return self.sendj(400,{})
                        sql+=' and p.message_id<?';args.append(int(cur))
                    sql+=f' order by p.message_id desc limit {limit+1}'
                    rows=c.execute(sql,args).fetchall(); more=len(rows)>limit; rows=rows[:limit]
                    out=[{'message':msg_obj(c,c.execute('select * from messages where id=?',(x['message_id'],)).fetchone()),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows]
                    return self.sendj(200,{'pins':out,'next_cursor':str(rows[-1]['message_id']) if more and rows else None})
                if tail=='read':
                    row=c.execute('select last_read_event_id from reads where user_id=? and channel_id=?',(u['id'],ch['id'])).fetchone()
                    last=int(row['last_read_event_id']) if row else 0
                    total=c.execute('select count(*) from messages where channel_id=? and deleted=0 and id>?',(ch['id'],last)).fetchone()[0]
                    mention=0
                    for mr in c.execute('select mentions from messages where channel_id=? and deleted=0 and id>?',(ch['id'],last)):
                        try:
                            if u['id'] in json.loads(mr['mentions'] or '[]'): mention+=1
                        except Exception: pass
                    return self.sendj(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':last,'unread_count':total,'mention_count':mention}})
                return self.sendj(200,{'channel':ch_obj(ch)})
            m=re.fullmatch(r'/api/messages/(\d+)/replies',p)
            if m:
                if not c.execute('select 1 from messages where id=?',(int(m.group(1)),)).fetchone(): return self.sendj(404,{})
                try:limit=int(q.get('limit',['50'])[0])
                except Exception:return self.sendj(400,{})
                if limit<1 or limit>200:return self.sendj(400,{})
                cursor=q.get('cursor',[None])[0]; after=None
                if cursor is not None:
                    if not cursor.isdigit():return self.sendj(400,{})
                    after=int(cursor)
                sql='select * from messages where parent_id=? and deleted=0'
                args=[int(m.group(1))]
                if after is not None:sql+=' and id>?';args.append(after)
                sql+=f' order by id asc limit {limit+1}'
                rows=c.execute(sql,args).fetchall(); more=len(rows)>limit; rows=rows[:limit]
                nxt=str(rows[-1]['id']) if more and rows else None
                return self.sendj(200,{'replies':[msg_obj(c,x) for x in rows],'next_cursor':nxt})
            m=re.fullmatch(r'/api/files/(\d+)(/download)?',p)
            if m:
                f=c.execute('select * from files where id=?',(int(m.group(1)),)).fetchone()
                if not f:return self.sendj(404,{})
                if m.group(2):
                    b=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
                return self.sendj(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
            if p=='/api/search':
                try: limit=int(q.get('limit',['50'])[0])
                except Exception:return self.sendj(400,{})
                if limit<1 or limit>200:return self.sendj(400,{})
                cur=q.get('cursor',[None])[0]; before=None
                if cur is not None:
                    if not cur.isdigit():return self.sendj(400,{})
                    before=int(cur)
                term=q.get('q',[''])[0]; args=[u['id'],'%'+term+'%']; ws=q.get('workspace_id',[None])[0]
                sql='select distinct m.* from messages m join channel_members cm on cm.channel_id=m.channel_id and cm.user_id=? join channels ch on ch.id=m.channel_id where m.body like ? and m.deleted=0'
                if ws is not None:
                    try:int(ws)
                    except:return self.sendj(400,{})
                    sql+=' and ch.workspace_id=?';args.append(int(ws))
                if before is not None:sql+=' and m.id<?';args.append(before)
                sql+=f' order by m.id desc limit {limit+1}'
                rows=c.execute(sql,args).fetchall(); more=len(rows)>limit; rows=rows[:limit]
                return self.sendj(200,{'results':[msg_obj(c,x) for x in rows],'next_cursor':str(rows[-1]['id']) if more and rows else None})
            return self.sendj(404,{})
        finally:c.close()
    def do_POST(self):
        p,q=self.req()
        if p in ('/api/auth/register','/api/auth/login'):
            c=db(); d=self.js()
            try:
                if getattr(self,'_bad_json',False): return self.sendj(400,{'error':'malformed json'})
                if p.endswith('register'):
                    name=str(d.get('username','')); pw=str(d.get('password',''))
                    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8:
                        return self.sendj(400,{'error':'invalid registration'})
                    if c.execute('select 1 from users where username=?',(name,)).fetchone():
                        return self.sendj(409,{'error':'username exists'})
                    h=hashlib.sha256(pw.encode()).hexdigest()
                    c.execute('insert into users(username,password,display_name) values(?,?,?)',(name,h,d.get('display_name',name)))
                    uid=c.execute('select last_insert_rowid()').fetchone()[0]
                    tok=secrets.token_urlsafe(32); c.execute('insert into tokens values(?,?)',(tok,uid)); c.commit()
                    return self.sendj(201,{'user':user_obj(get_user(c,uid)),'token':tok})
                name=str(d.get('username','')); pw=str(d.get('password',''))
                r=c.execute('select * from users where username=? and password=?',(name,hashlib.sha256(pw.encode()).hexdigest())).fetchone()
                if not r:return self.sendj(401,{'error':'invalid credentials'})
                tok=secrets.token_urlsafe(32); c.execute('insert into tokens values(?,?)',(tok,r['id'])); c.commit()
                return self.sendj(200,{'user':user_obj(r),'token':tok})
            finally:c.close()
        u=self.need()
        if not u:return
        c=db()
        if p=='/api/files':
            try:
                if not self.headers.get('Content-Type','').startswith('multipart/form-data'):
                    return self.sendj(400,{})
                form=cgi.FieldStorage(fp=self.rfile,headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers.get('Content-Type','')},keep_blank_values=True)
                if 'file' not in form or not getattr(form['file'],'file',None): return self.sendj(400,{})
                part=form['file']; data=part.file.read()
                if len(data)>10*1024*1024:return self.sendj(413,{})
                filename=part.filename or 'file'; ctype=part.type or 'application/octet-stream'
                c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),now(),data))
                fid=c.execute('select last_insert_rowid()').fetchone()[0]; c.commit()
                r=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone()
                return self.sendj(201,{'file':dict(r)})
            finally:c.close()
        d=self.js()
        try:
            m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                mr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not mr:return self.sendj(403,{})
                handle=str(d.get('handle','')); name=str(d.get('name',''))
                if not re.fullmatch(r'[a-z0-9_-]{1,32}',handle) or not name:return self.sendj(400,{})
                if c.execute('select 1 from groups_ where workspace_id=? and handle=?',(w['id'],handle)).fetchone():return self.sendj(409,{})
                ids=d.get('user_ids',[])
                if not isinstance(ids,list):return self.sendj(400,{})
                ids=[int(x) for x in ids if get_user(c,int(x))]
                c.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],handle,name,u['id'],json.dumps(ids)));c.commit()
                g=c.execute('select * from groups_ where id=?',(c.execute('select last_insert_rowid()').fetchone()[0],)).fetchone()
                return self.sendj(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{})
                target=d.get('invited_user_id')
                if target is not None and not get_user(c,int(target)):return self.sendj(404,{})
                code=secrets.token_urlsafe(18); exp=(int(time.time())+int(d['expires_in_seconds'])) if d.get('expires_in_seconds') is not None else 0
                mx=int(d.get('max_uses',1) or 1)
                c.execute('insert into invitations values(?,?,?,?,?,0)',(code,w['id'],target,exp,mx));c.commit()
                return self.sendj(201,{'invitation':dict(c.execute('select * from invitations where code=?',(code,)).fetchone())})
            m=re.fullmatch(r'/api/invitations/([^/]+)/accept',p)
            if m:
                inv=c.execute('select * from invitations where code=?',(m.group(1),)).fetchone()
                if not inv or inv['uses']>=inv['max_uses'] or (inv['expires'] and inv['expires']<=int(time.time())):return self.sendj(404,{})
                if inv['invited_user_id'] is not None and inv['invited_user_id']!=u['id']:return self.sendj(403,{})
                c.execute('insert or ignore into members values(?,?,?)',(inv['workspace_id'],u['id'],'member'))
                c.execute('update invitations set uses=uses+1,max_uses=case when invited_user_id is not null then 1 else max_uses end where code=?',(inv['code'],));c.commit()
                return self.sendj(200,{'accepted':True})
            if p=='/api/workspaces':
                if not d.get('name') or not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))):return self.sendj(400,{})
                if c.execute('select 1 from workspaces where slug=?',(d['slug'],)).fetchone():return self.sendj(409,{})
                c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d.get('name',''),u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0]
                c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.commit()
                return self.sendj(201,{'workspace':ws_obj(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':ch_obj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                mr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not mr or mr['role']!='owner':return self.sendj(403,{})
                target=int(d.get('new_owner_id',0) or 0)
                tr=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
                if not tr or tr['role']!='admin':return self.sendj(400,{})
                c.execute('update members set role=? where workspace_id=? and user_id=?',('admin',w['id'],u['id']))
                c.execute('update members set role=? where workspace_id=? and user_id=?',('owner',w['id'],target))
                c.execute('update workspaces set owner_id=? where id=?',(target,w['id']));c.commit()
                return self.sendj(200,{'workspace':ws_obj(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                target=int(m.group(2)); old=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
                new=d.get('role')
                if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{})
                if new not in ('admin','member','guest'):return self.sendj(400,{})
                if not old:return self.sendj(404,{})
                if target==u['id'] or old['role']=='owner':return self.sendj(403,{})
                if actor['role']=='admin' and old['role']=='admin':return self.sendj(403,{})
                c.execute('update members set role=? where workspace_id=? and user_id=?',(new,w['id'],target));c.commit()
                return self.sendj(200,{'member':{'user_id':target,'role':new}})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not role:return self.sendj(403,{})
                if role['role']=='guest':return self.sendj(403,{})
                if d.get('is_private') and role['role'] not in ('owner','admin'):return self.sendj(403,{})
                if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],d.get('name'))).fetchone():return self.sendj(409,{})
                if not re.fullmatch(r'[a-z0-9-]{1,32}',str(d.get('name',''))) or len(d.get('topic',''))>250:return self.sendj(400,{})
                c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')))
                cid=c.execute('select last_insert_rowid()').fetchone()[0]
                c.execute('insert into channel_members values(?,?)',(cid,u['id']))
                c.commit()
                return self.sendj(201,{'channel':ch_obj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
            m=re.fullmatch(r'/api/channels/(\d+)/join',p)
            if m:
                ch=c.execute('select * from channels where id=?',(int(m.group(1)),)).fetchone()
                if not ch:return self.sendj(404,{})
                mr=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
                wr=c.execute('select join_mode from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                if mr and mr['role']=='guest':return self.sendj(403,{})
                if not mr and wr and wr['join_mode']=='invite_only':return self.sendj(403,{})
                if ch['is_private'] and not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.sendj(403,{})
                c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member')); c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],u['id'])); emit(c,ch['id'],'member.joined',{'user_id':u['id']}); c.commit(); return self.sendj(200,{'channel':ch_obj(ch)})
            m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
            if m:
                ch=c.execute('select * from channels where id=?',(int(m.group(1)),)).fetchone()
                if not ch:return self.sendj(404,{})
                w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                action=m.group(2)
                if action=='leave':
                    c.execute('delete from channel_members where channel_id=? and user_id=?',(ch['id'],u['id']));emit(c,ch['id'],'member.left',{'user_id':u['id']});c.commit()
                    return self.sendj(200,{'channel':ch_obj(ch)})
                if w['owner_id']!=u['id']:return self.sendj(403,{})
                archived=1 if action=='archive' else 0
                c.execute('update channels set is_archived=? where id=?',(archived,ch['id']))
                emit(c,ch['id'],'channel.archived' if archived else 'channel.unarchived',{})
                c.commit()
                return self.sendj(200,{'channel':ch_obj(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())})
            m=re.fullmatch(r'/api/channels/(\d+)/read',p)
            if m:
                ch=c.execute('select * from channels where id=?',(int(m.group(1)),)).fetchone()
                if not ch:return self.sendj(404,{})
                try: requested=int(d.get('last_read_event_id',0))
                except:return self.sendj(400,{})
                old=c.execute('select last_read_event_id from reads where user_id=? and channel_id=?',(u['id'],ch['id'])).fetchone()
                last=max(requested,int(old['last_read_event_id']) if old else 0)
                c.execute('insert or replace into reads(user_id,channel_id,last_read_event_id) values(?,?,?)',(u['id'],ch['id'],last));c.commit()
                return self.sendj(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':last,'unread_count':0,'mention_count':0}})
            m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
            if m:
                ch=c.execute('select * from channels where id=?',(int(m.group(1)),)).fetchone()
                if not ch:return self.sendj(404,{})
                if not d.get('body','').strip() and not d.get('file_ids'):return self.sendj(400,{})
                if ch['is_archived'] and str(d.get('body','')).strip().split(None,1)[0].lower() not in ('/unarchive',):return self.sendj(423,{})
                if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.sendj(403,{})
                body=str(d.get('body',''))
                if body.startswith('/') and not body.startswith('//'):
                    parts=body.split(None,1); cmd=parts[0].lower(); arg=parts[1] if len(parts)>1 else ''
                    if cmd=='/me':
                        if not arg.strip(): return self.sendj(400,{})
                    elif cmd=='/shrug':
                        body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
                    elif cmd=='/topic':
                        if not arg.strip() or len(arg)>250:return self.sendj(400,{})
                        c.execute('update channels set topic=? where id=?',(arg,ch['id'])); emit(c,ch['id'],'channel.updated',{'topic':arg}); c.commit()
                        return self.sendj(201,{'message':None,'channel':ch_obj(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())})
                    elif cmd=='/invite':
                        uname=arg.strip().lstrip('@'); target=c.execute('select * from users where username=?',(uname,)).fetchone()
                        if not target:return self.sendj(404,{})
                        c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],target['id'])); c.commit()
                        return self.sendj(201,{'message':None,'invited_user':user_obj(target)})
                    elif cmd in ('/archive','/unarchive'):
                        w=c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                        if not w or w['owner_id']!=u['id']:return self.sendj(403,{})
                        val=1 if cmd=='/archive' else 0
                        c.execute('update channels set is_archived=? where id=?',(val,ch['id'])); emit(c,ch['id'],'channel.archived' if val else 'channel.unarchived',{}); c.commit()
                        return self.sendj(201,{'message':None,'channel':ch_obj(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())})
                    else:
                        return self.sendj(400,{})
                mentions=[]
                for z in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
                    if z in ('channel','here'):
                        sql='select user_id from channel_members where channel_id=? and user_id<>?'
                        args=[ch['id'],u['id']]
                        if z=='here':
                            sql+=' and user_id in (select user_id from online_ws where updated_at>?)';args.append(int(time.time())-60)
                        mentions += [x[0] for x in c.execute(sql,args)]
                    else:
                        x=c.execute('select id from users where username=?',(z,)).fetchone()
                        g=c.execute('select user_ids from groups_ where workspace_id=? and handle=?',(ch['workspace_id'],z)).fetchone()
                        if x and x[0]!=u['id'] and c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],x[0])).fetchone(): mentions.append(x[0])
                        if g:
                            mentions += [int(x[0]) for x in c.execute('select value from json_each(?) where value in (select user_id from channel_members where channel_id=?)',(g['user_ids'],ch['id'])) if int(x[0])!=u['id']]
                mentions=list(dict.fromkeys(mentions))
                file_ids=d.get('file_ids',[])
                if not isinstance(file_ids,list):return self.sendj(400,{})
                for fid in file_ids:
                    f=c.execute('select * from files where id=?',(fid,)).fetchone()
                    if not f or f['uploader_id']!=u['id'] or f['attached']:return self.sendj(400,{})
                c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id,mentions) values(?,?,?,?,?,?,?)',(ch['id'],u['id'],body,d.get('parent_id'),now(),0,json.dumps(mentions))); mid=c.execute('select last_insert_rowid()').fetchone()[0]
                for fid in file_ids: c.execute('update files set attached=? where id=?',(mid,fid))
                created=msg_obj(c,c.execute('select * from messages where id=?',(mid,)).fetchone()); emit(c,ch['id'],'message.reply' if d.get('parent_id') else 'message.created',{'message_id':mid,'message':created}); c.commit(); return self.sendj(201,{'message':created})
            m=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
            if m:
                r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
                if not r:return self.sendj(404,{})
                if not d.get('emoji'):return self.sendj(400,{})
                c.execute('insert or ignore into reactions values(?,?,?)',(r['id'],u['id'],d['emoji']));emit(c,r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':d['emoji']});c.commit(); return self.sendj(200,{'reactions':msg_obj(c,r)['reactions']})
            m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
            if m:
                r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
                if not r:return self.sendj(404,{})
                c.execute('insert or ignore into pins values(?,?,?)',(int(m.group(1)),u['id'],now()));c.commit();return self.sendj(200,{'pin':{'message_id':int(m.group(1)),'pinned_by':u['id'],'pinned_at':now()}})
            if p=='/api/dms':
                other=get_user(c,d.get('user_id')); 
                if not other:return self.sendj(404,{})
                a,b=sorted((u['id'],other['id'])); key=f'dm-{a}-{b}'; ch=c.execute('select * from channels where name=?',(key,)).fetchone()
                if not ch:
                    c.execute('insert into channels(name,is_dm) values(?,1)',(key,)); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into channel_members values(?,?),(?,?)',(cid,a,cid,b));c.commit()
                else:cid=ch['id']
                return self.sendj(200,{'channel_id':cid})
            return self.sendj(404,{})
        finally:c.close()
    def do_PATCH(self):
        p,q=self.req();u=self.need()
        if not u:return
        c=db();d=self.js()
        try:
            m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                target=int(m.group(2)); old=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],target)).fetchone()
                new=d.get('role')
                if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{})
                if new=='owner':return self.sendj(400,{})
                if new not in ('admin','member','guest'):return self.sendj(400,{})
                if not old:return self.sendj(404,{})
                if target==u['id'] or old['role']=='owner':return self.sendj(403,{})
                if actor['role']=='admin' and old['role']=='admin':return self.sendj(403,{})
                c.execute('update members set role=? where workspace_id=? and user_id=?',(new,w['id'],target));c.commit()
                return self.sendj(200,{'member':{'user_id':target,'role':new}})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],m.group(2))) if w else None
                g=g.fetchone() if g else None
                if not w or not g:return self.sendj(404,{})
                actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not actor or (g['creator_id']!=u['id'] and w['owner_id']!=u['id']):return self.sendj(403,{})
                name=d.get('name',g['name']); ids=d.get('user_ids',json.loads(g['user_ids']))
                if not isinstance(ids,list):return self.sendj(400,{})
                c.execute('update groups_ set name=?,user_ids=? where id=?',(name,json.dumps([int(x) for x in ids]),g['id']));c.commit()
                return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':[int(x) for x in ids]}})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{})
                if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return self.sendj(400,{})
                vals=[];args=[]
                for k in ('name','join_mode'):
                    if k in d: vals.append(k+'=?');args.append(d[k])
                if vals:c.execute('update workspaces set '+','.join(vals)+' where id=?',(*args,w['id']));c.commit()
                return self.sendj(200,{'workspace':ws_obj(c.execute('select * from workspaces where id=?',(w['id'],)).fetchone())})
            if p=='/api/users/me':
                allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
                if 'timezone' in d and d['timezone'] not in available_timezones(): return self.sendj(400,{})
                if any(k in d and len(str(d[k]))>250 for k in allowed): return self.sendj(400,{})
                sets=[k+'=?' for k in d if k in allowed]
                if sets:
                    c.execute('update users set '+','.join(sets)+' where id=?',tuple(d[k] for k in d if k in allowed)+(u['id'],))
                    fresh=get_user(c,u['id'])
                    ev={'type':'user.updated','user':user_obj(fresh)}
                    emit(c,0,'user.updated',{'user':user_obj(fresh)})
                    c.commit()
                    with open('/tmp/huddle-global.jsonl','a') as gf: gf.write(json.dumps(ev)+'\\n')
                return self.sendj(200,{'user':user_obj(get_user(c,u['id']))})
            m=re.fullmatch(r'/api/messages/(\d+)',p)
            if m:
                r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
                if not r:return self.sendj(404,{})
                if r['deleted']:return self.sendj(409,{})
                if r['author_id']!=u['id']:return self.sendj(403,{})
                c.execute('update messages set body=?,edited_at=? where id=?',(d.get('body',''),now(),r['id']));emit(c,r['channel_id'],'message.updated',{'message_id':r['id']});c.commit();return self.sendj(200,{'message':msg_obj(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone())})
            m=re.fullmatch(r'/api/channels/(\d+)',p)
            if m:
                ch=c.execute('select * from channels where id=?',(int(m.group(1)),)).fetchone()
                if not ch:return self.sendj(404,{})
                if 'topic' not in d or len(str(d.get('topic','')))>250:return self.sendj(400,{})
                w=c.execute('select * from workspaces where id=?',(ch['workspace_id'],)).fetchone()
                member=c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone()
                if not member and w['owner_id']!=u['id']:return self.sendj(403,{})
                c.execute('update channels set topic=? where id=?',(d['topic'],ch['id']));emit(c,ch['id'],'channel.updated',{'topic':d['topic']});c.commit()
                return self.sendj(200,{'channel':ch_obj(c.execute('select * from channels where id=?',(ch['id'],)).fetchone())})
            return self.sendj(404,{})
        finally:c.close()
    def do_DELETE(self):
        p,q=self.req();u=self.need()
        if not u:return
        c=db()
        try:
            m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],m.group(2))) if w else None
                g=g.fetchone() if g else None
                if not g:return self.sendj(200,{'deleted':True}) if w else self.sendj(404,{})
                if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return self.sendj(403,{})
                c.execute('delete from groups_ where id=?',(g['id'],));c.commit();return self.sendj(200,{'deleted':True})
            m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',p)
            if m:
                w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
                if not w:return self.sendj(404,{})
                actor=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
                if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{})
                inv=c.execute('select code from invitations where code=? and workspace_id=?',(m.group(2),w['id'])).fetchone()
                if not inv:return self.sendj(404,{})
                c.execute('update invitations set max_uses=uses where code=?',(m.group(2),));c.commit()
                return self.sendj(200,{'deleted':True})
            m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
            if m:
                if not c.execute('select 1 from messages where id=?',(int(m.group(1)),)).fetchone():return self.sendj(404,{})
                c.execute('delete from pins where message_id=?',(int(m.group(1)),));c.commit()
                return self.sendj(200,{'unpinned':True})
            m=re.fullmatch(r'/api/messages/(\d+)',p)
            if m:
                r=c.execute('select m.*,w.owner_id from messages m join channels ch on ch.id=m.channel_id join workspaces w on w.id=ch.workspace_id where m.id=?',(int(m.group(1)),)).fetchone()
                if not r:return self.sendj(404,{})
                if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return self.sendj(403,{})
                c.execute('update messages set deleted=1 where id=?',(r['id'],));emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']});c.commit();return self.sendj(200,{'deleted':True})
            m=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',p)
            if m:
                r=c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone()
                if not r:return self.sendj(404,{})
                emoji=urllib.parse.unquote(m.group(2))
                c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(int(m.group(1)),u['id'],emoji))
                emit(c,r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':emoji})
                c.commit()
                return self.sendj(200,{'reactions':msg_obj(c,c.execute('select * from messages where id=?',(int(m.group(1)),)).fetchone())['reactions']})
            return self.sendj(404,{})
        finally:c.close()
    def page(self):
        html='''<!doctype html><html><head><title>Huddle</title><style>body{font:16px Arial;margin:0;background:#f8f8f8}button{border:0;padding:9px 14px;color:#fff;background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}</style></head><body><div id="app"><h1>Huddle</h1><form data-testid="auth-form" id="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary">Log in</button></form><button data-testid="auth-toggle" class="secondary">Sign up</button><div data-testid="channel-list"></div><main data-testid="message-list"></main></div></body></html>'''
        b=html.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def websocket(self):
        tok=urllib.parse.parse_qs(self.route().query).get('token',[''])[0]
        c=db(); ok=c.execute('select user_id from tokens where token=?',(tok,)).fetchone();c.close()
        if not ok:return self.sendj(401,{'error':'unauthorized'})
        wc=db(); wc.execute('insert or replace into online_ws values(?,?)',(ok['user_id'],int(time.time()))); wc.commit(); wc.close()
        key=self.headers.get('Sec-WebSocket-Key','')
        if not key:return self.sendj(400,{'error':'bad websocket'})
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade')
        self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1(key.encode()+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest()).decode());self.end_headers()
        self.close_connection=True
        self.connection.settimeout(0.2)
        def frame(payload):
            b=json.dumps(payload,separators=(',',':')).encode(); n=len(b)
            if n<126: h=bytes([0x81,n])
            elif n<65536:h=bytes([0x81,126])+struct.pack('!H',n)
            else:h=bytes([0x81,127])+struct.pack('!Q',n)
            self.wfile.write(h+b);self.wfile.flush()
        def read():
            h=self.rfile.read(2)
            if len(h)<2:return None
            ln=h[1]&127; masked=h[1]&128
            if ln==126:ln=struct.unpack('!H',self.rfile.read(2))[0]
            elif ln==127:ln=struct.unpack('!Q',self.rfile.read(8))[0]
            mask=self.rfile.read(4) if masked else b''
            b=self.rfile.read(ln)
            if masked:b=bytes(x^mask[i%4] for i,x in enumerate(b))
            if (h[0]&15)==8:return None
            try:return json.loads(b)
            except:return {}
        try:
            subs={}
            global_last=0
            while True:
                d=None
                try:
                    ready,_,_=select.select([self.connection],[],[],0.15)
                    if ready: d=read()
                except (socket.timeout, BlockingIOError): d=None
                if d:
                    if d.get('type') in ('subscribe','resume'):
                        cid=int(d.get('channel_id',0)); since=int(d.get('since_event_id',0)); subs[cid]=since
                        c=db(); head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0] or 0
                        if d.get('type')=='subscribe':
                            frame({'type':'subscribed','channel_id':cid,'head_event_id':head})
                        else:
                            missed=c.execute('select event_id,payload from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
                            for row in missed:
                                frame(json.loads(row['payload']))
                            subs[cid]=head
                            frame({'type':'resumed','channel_id':cid,'head_event_id':head})
                        c.close()
                    elif d.get('type')=='close': break
                time.sleep(0.05)
                c=db()
                grows=c.execute('select event_id,payload from events where channel_id=0 and event_id>? order by event_id',(global_last,)).fetchall()
                for row in grows: frame(json.loads(row['payload'])); global_last=row['event_id']
                for cid,last in list(subs.items()):
                    rows=c.execute('select event_id,payload from events where channel_id=? and event_id>? order by event_id',(cid,last)).fetchall()
                    for row in rows: frame(json.loads(row['payload'])); subs[cid]=row['event_id']
                c.close()
        except Exception: pass

init()
ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
