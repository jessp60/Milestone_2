import asyncio, json, os, re, secrets, sqlite3, sys, hashlib, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web, WSMsgType
DB='/app/data.db'; FILEDIR='/app/files'
Path(FILEDIR).mkdir(exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=15); c.row_factory=sqlite3.Row; return c
def init():
 c=db(); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT,mention_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT,attached INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS profiles(seq INTEGER PRIMARY KEY,user_id INTEGER,payload TEXT);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,users TEXT);
CREATE TABLE IF NOT EXISTS ws_clients(user_id INTEGER,channel_id INTEGER,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS mention_events(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
'''); c.commit()
 try:
  c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'")
  c.commit()
 except sqlite3.OperationalError:
  pass
 c.close()
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('Authorization','')
 if not h.startswith('Bearer '): return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def js(x): return json.dumps(x,separators=(',',':'))
async def body(req):
 try:return await req.json()
 except:return {}
def out(x,status=200): return web.json_response(x,status=status)
def msgobj(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rs=[]
 for q in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[x[0] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],q['emoji']))]
  rs.append({'emoji':q['emoji'],'count':q['n'],'user_ids':ids})
 fs=[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE attached=?',(r['id'],))]
 mentions=json.loads(r['mentions'] or '[]') if 'mentions' in r.keys() else []
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rs,'mentions':mentions}
def members(c,ch): return [x[0] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(ch,))]
def cdict(r):
 d=dict(r); d['is_private']=bool(d.get('is_private')); d['is_dm']=bool(d.get('is_dm')); d['is_archived']=bool(d.get('is_archived')); return d
def channel(c,id): return c.execute('SELECT * FROM channels WHERE id=?',(id,)).fetchone()
def allowed(c,u,ch):
 r=channel(c,ch)
 return r and (c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch,u)).fetchone() if (r['is_private'] or r['is_dm']) else True)
def event(c,ch,typ,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
 c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch,n,typ,js(payload))); return n
async def health(req): return out({'status':'ok','node_id':int(req.app['port'])-8000})
async def register(req):
 d=await body(req); un=d.get('username',''); pw=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return out({'error':'invalid'},400)
 c=db()
 try:
  c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(un,pw,d.get('display_name') or un,'UTC','','','')); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 except sqlite3.IntegrityError:return out({'error':'duplicate'},409)
 tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 return out({'user':userobj(r),'token':tok},201)
async def login(req):
 d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
 if not r:c.close(); return out({'error':'unauthorized'},401)
 t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return out({'user':userobj(r),'token':t})
async def me(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 return out({'user':userobj(u)})
async def profile(req):
 u=auth(req)
 if not u:return out({},401)
 c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({},404)
 c.close();return out({'user':userobj(r)})
async def patchme(req):
 u=auth(req)
 if not u:return out({},401)
 d=await body(req); fields=['display_name','timezone','avatar_url','status_text','status_emoji']
 if 'timezone' in d:
  try: import zoneinfo; zoneinfo.ZoneInfo(d['timezone'])
  except:return out({},400)
 if 'display_name' in d and len(str(d['display_name']))>200:return out({},400)
 c=db(); vals=[];args=[]
 for f in fields:
  if f in d: vals.append(f+'=?');args.append(d[f])
 if vals:c.execute('UPDATE users SET '+','.join(vals)+' WHERE id=?',args+[u['id']]); r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.execute('INSERT INTO profiles(user_id,payload) VALUES(?,?)',(u['id'],js(userobj(r))))
 c.commit();c.close();return out({'user':userobj(r if vals else u)})
async def workspace(req):
 u=auth(req)
 if not u:return out({},401)
 c=db(); slug=req.match_info.get('slug')
 if req.method=='POST' and not slug:
  d=await body(req)
  try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id']))
  except:return out({},409)
  wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));c.commit();c.close();return out({'workspace':{'id':wid,'slug':d['slug'],'name':d['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':{'id':ch}},201)
 if not slug:
  rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return out({'workspaces':[dict(x) for x in rows]})
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:c.close();return out({},404)
 if req.method=='GET':
  rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close();return out({'workspace':dict(w),'channels':[cdict(x) for x in rows],'read_state':{}})
 if req.path.endswith('/channels'):
  d=await body(req); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
  if not role:return out({},403)
  try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(d.get('is_private',False)),d.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return out({'channel':dict(r)},201)
  except:return out({},409)
 c.close();return out({},400)
async def join(req):
 u=auth(req)
 if not u:return out({},401)
 c=db(); ch=channel(c,int(req.match_info['id']))
 if not ch:c.close();return out({},404)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if ch['is_private'] and not mem:c.close();return out({},403)
 if not mem:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.commit();c.close();return out({'channel':cdict(ch)})
async def channels(req):
 u=auth(req)
 if not u:return out({},401)
 c=db(); ch=channel(c,int(req.match_info['id']))
 if not ch:c.close();return out({},404)
 if req.method=='GET':
  if not allowed(c,u['id'],ch['id']):c.close();return out({},403)
  if not req.path.endswith('/messages'): c.close(); return out({'channel':cdict(ch)})
  rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(ch['id'],)).fetchall(); z=[msgobj(c,r) for r in rows];c.close();return out({'messages':z,'next_cursor':None})
 d=await body(req)
 if req.method=='PATCH':
  if len(d.get('topic',''))>250:c.close();return out({},400)
  c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),ch['id']));c.commit();r=channel(c,ch['id']);c.close();return out({'channel':cdict(r)})
 if req.method=='POST' and req.path.endswith('/messages'):
  if not allowed(c,u['id'],ch['id']):c.close();return out({},403)
  text=d.get('body','')
  if not isinstance(text,str) or not text.strip():c.close();return out({},400)
  if ch['is_archived'] and text.strip().split()[0:1] != ['/unarchive']:c.close();return out({},423)
  if text.startswith('/') and not text.startswith('//'):
   p=text.split(' ',1); cmd=p[0]; arg=p[1].strip() if len(p)>1 else ''
   if cmd=='/me' and not arg:c.close();return out({},400)
   if cmd=='/shrug': text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
   elif cmd=='/me': pass
   elif cmd in ('/topic','/archive','/unarchive'):
    if u['id']!=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()[0]:c.close();return out({},403)
    if cmd=='/topic':c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']))
    else:c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,ch['id']))
    c.commit();r=channel(c,ch['id']);c.close();return out({'message':None,'channel':cdict(r)},201)
   elif cmd=='/invite':
    name=arg.lstrip('@'); target=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
    if not target:c.close();return out({},404)
    c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],target['id']));c.commit();c.close();return out({'message':None,'invited_user':userobj(target)},201)
   else:c.close();return out({},400)
  fids=d.get('file_ids',[])
  for fid in fids:
   f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=? AND attached=0',(fid,u['id'])).fetchone()
   if not f:c.close();return out({},400)
  c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],text,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  for fid in fids:c.execute('UPDATE files SET attached=? WHERE id=?',(mid,fid))
  ids=[]
  channel_members=members(c,ch['id'])
  for x in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',text):
   if x=='channel': ids += [i for i in channel_members if i!=u['id']]
   elif x=='here':
    ids += [i for i in channel_members if i!=u['id'] and c.execute('SELECT 1 FROM ws_clients WHERE user_id=?',(i,)).fetchone()]
   else:
    rr=c.execute('SELECT id FROM users WHERE username=?',(x,)).fetchone()
    if rr and rr[0]!=u['id'] and rr[0] in channel_members: ids.append(rr[0])
    for g in c.execute('SELECT users FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],x)):
     ids += [i for i in json.loads(g['users']) if i in channel_members and i!=u['id']]
  ids=list(dict.fromkeys(ids))
  c.execute('UPDATE messages SET mentions=? WHERE id=?',(js(ids),mid))
  for i in ids:
   c.execute('INSERT OR IGNORE INTO mention_events VALUES(?,?)',(mid,i))
   c.execute('UPDATE users SET mention_count=mention_count+1 WHERE id=?',(i,))
  n=event(c,ch['id'],'message.created',{'message_id':mid});c.execute('UPDATE messages SET event_id=? WHERE id=?',(n,mid));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();z=msgobj(c,r);c.close();return out({'message':z},201)
 c.close();return out({},405)
async def readstate(req):
 u=auth(req)
 if not u:return out({},401)
 c=db(); cid=int(req.match_info['id'])
 if not allowed(c,u['id'],cid): c.close(); return out({},403)
 n=c.execute('SELECT COUNT(*) FROM mention_events me JOIN messages m ON m.id=me.message_id WHERE me.user_id=? AND m.channel_id=? AND m.deleted=0',(u['id'],cid)).fetchone()[0]
 c.close(); return out({'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':n}})
async def reactions(req):
 u=auth(req)
 if not u:return out({},401)
 c=db();m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
 if not m:c.close();return out({},404)
 if req.method=='POST':
  d=await body(req);em=d.get('emoji')
  if not em:c.close();return out({},400)
  c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(m['id'],u['id'],em))
 else:
  c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],req.match_info['emoji']))
 c.commit(); rs=[]
 for q in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(m['id'],)):
  rs.append({'emoji':q[0],'count':q[1],'user_ids':[x[0] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],q[0]))]})
 c.close();return out({'reactions':rs})
async def dms(req):
 u=auth(req)
 if not u:return out({},401)
 d=await body(req); other=int(d.get('user_id',0));c=db()
 if not c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone():c.close();return out({},404)
 a,b=sorted((u['id'],other));r=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,b)).fetchone()
 if r:cid=r[0]
 else:
  c.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{b}',));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,a,cid,b))
 c.commit();c.close();return out({'channel_id':cid})
async def files(req):
 u=auth(req)
 if not u:return out({},401)
 if req.method=='GET':
  c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
  return out({'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}} if r else {},200 if r else 404)
 rd=await req.multipart(); part=await rd.next()
 if not part or part.name!='file':return out({},400)
 data=await part.read(decode=False)
 if len(data)>10*1024*1024:return out({},413)
 c=db(); fn=part.filename or 'file'; typ=part.headers.get('Content-Type','application/octet-stream');path=f'{FILEDIR}/{secrets.token_hex(12)}';open(path,'wb').write(data)
 c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),now(),path));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return out({'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}},201)
async def download(req):
 u=auth(req)
 if not u:return out({},401)
 c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not r:return out({},404)
 return web.FileResponse(r['path'],headers={'Content-Type':r['content_type'],'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
async def groups(req):
 u=auth(req)
 if not u:return out({},401)
 slug=req.match_info['slug'];c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
 if not w or not mem:c.close();return out({},403 if w else 404)
 if req.method=='GET':
  rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall();z=[{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['users'])} for r in rs];c.close();return out({'groups':z})
 d=await body(req); handle=req.match_info.get('handle')
 if req.method=='POST':
  if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],d.get('handle'))).fetchone():c.close();return out({},409)
  c.execute('DELETE FROM groups_ WHERE handle=? AND workspace_id<>?',(d.get('handle'),w['id']))
  c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,users) VALUES(?,?,?,?,?)',(w['id'],d['handle'],d.get('name',''),u['id'],js(d.get('user_ids',[]))));c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();c.close();return out({'group':{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['users'])}},201)
 r=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone() if handle else None
 if not r:
  c.close(); return out({'deleted':True}) if req.method=='DELETE' else out({},404)
 role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if u['id']!=r['creator_id'] and (not role or role['role'] not in ('owner','admin')):c.close();return out({},403)
 if req.method=='DELETE':c.execute('DELETE FROM groups_ WHERE id=?',(r['id'],));c.commit();c.close();return out({'deleted':True})
 if 'name' in d:c.execute('UPDATE groups_ SET name=? WHERE id=?',(d['name'],r['id']))
 if 'user_ids' in d:c.execute('UPDATE groups_ SET users=? WHERE id=?',(js(d['user_ids']),r['id']))
 c.commit();r=c.execute('SELECT * FROM groups_ WHERE id=?',(r['id'],)).fetchone();c.close();return out({'group':{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['users'])}})
async def ws(req):
 tok=req.query.get('token','');c=db();u=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone();c.close()
 if not u:return web.Response(status=401)
 w=web.WebSocketResponse();await w.prepare(req); last=0; subscribed=set(); c=db(); c.execute('INSERT OR IGNORE INTO ws_clients(user_id,channel_id) VALUES(?,0)',(u[0],)); c.commit(); c.close()
 async def poll():
  nonlocal last
  while not w.closed:
   c=db()
   if subscribed:
    for r in c.execute('SELECT * FROM events WHERE event_id>? ORDER BY rowid',(last,)):
     if r['channel_id'] in subscribed:
      p=json.loads(r['payload']);p.update(type=r['type'],event_id=r['event_id'],channel_id=r['channel_id']);await w.send_json(p);last=max(last,r['event_id'])
   for r in c.execute('SELECT * FROM profiles WHERE seq>? ORDER BY seq',(last,)):
    if r['user_id']!=u[0]:await w.send_json({'type':'user.updated','user':json.loads(r['payload'])});last=max(last,r['seq'])
   c.close();await asyncio.sleep(.15)
 task=asyncio.create_task(poll())
 async for m in w:
  if m.type==WSMsgType.TEXT:
   d=json.loads(m.data)
   if d.get('type')=='subscribe':
    subscribed.add(int(d['channel_id']));c=db();h=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(d['channel_id'],)).fetchone()[0];c.close();await w.send_json({'type':'subscribed','channel_id':d['channel_id'],'head_event_id':h})
   elif d.get('type')=='resume':await w.send_json({'type':'resumed','channel_id':d['channel_id'],'head_event_id':0})
 task.cancel(); c=db(); c.execute('DELETE FROM ws_clients WHERE user_id=?',(u[0],)); c.commit(); c.close(); return w
def main():
 init();port=int(sys.argv[1]);app=web.Application();app['port']=port
 app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me);app.router.add_get('/api/users/{id}',profile);app.router.add_patch('/api/users/me',patchme)
 app.router.add_post('/api/workspaces',workspace);app.router.add_get('/api/workspaces',workspace);app.router.add_route('*','/api/workspaces/{slug}',workspace);app.router.add_route('*','/api/workspaces/{slug}/channels',workspace)
 app.router.add_post('/api/channels/{id}/join',join);app.router.add_get('/api/channels/{id}/read',readstate);app.router.add_route('*','/api/channels/{id}/messages',channels);app.router.add_route('*','/api/channels/{id}',channels);app.router.add_route('*','/api/messages/{id}/reactions',reactions);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',reactions);app.router.add_post('/api/dms',dms);app.router.add_post('/api/files',files);app.router.add_get('/api/files/{id}',files);app.router.add_get('/api/files/{id}/download',download);app.router.add_route('*','/api/workspaces/{slug}/groups',groups);app.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',groups);app.router.add_get('/api/ws',ws)
 web.run_app(app,host='127.0.0.1',port=port)
if __name__=='__main__':main()
