import os,sys,tempfile,time,sqlite3
from unittest.mock import patch
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
 os.environ['AUTH_DB']=d+'/test.db';os.environ['NGINX_LOG_DIR']=d;os.environ['AUTH_UPLOAD_DIR']=d+'/uploads'
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'auth'))
 import auth_server as a
 from fastapi.testclient import TestClient
 with TestClient(a.app) as c:
  with sqlite3.connect(a.DB_PATH) as db:
   db.execute("INSERT INTO users(email,created_at,last_login,nickname,public_id) VALUES(?,?,?,?,?)",('test@example.com',1,1,'测试用户','abcd1234'))
   db.execute("INSERT INTO chat_messages(email,nickname,text,created_at,kind) VALUES(?,?,?,?,?)",('test@example.com','','hello',int(time.time()),'text'))
  r=c.get('/api/chat/messages');assert r.status_code==200,r.text
  m=r.json()['messages'];assert len(m)==1 and m[0]['nickname']=='测试用户',m
  assert 'email' not in m[0]
  assert c.get('/api/chat/messages?after_id='+str(m[0]['id'])).json()['messages']==[]
  assert c.get('/api/auth/me').status_code in (200,401)
  assert c.get('/api/auth/admin/stats').status_code in (401,403)
  one=a._parse_visits();assert a._parse_visits() is one
  # 拉取失败时，不得继续执行会覆盖工作树的 reset。
  with patch.object(a.subprocess, 'run') as run:
   run.return_value = type('R', (), {'returncode': 1, 'stdout': '', 'stderr': 'network failed'})()
   ok, message = a._update_football_repo(d)
   assert not ok and '拉取失败' in message
   assert run.call_count == 1
  print('Backend chat, incremental cursor, privacy, authorization, stats cache passed')
