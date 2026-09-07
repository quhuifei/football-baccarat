#!/bin/bash
# 随机ID + 敏感词 + 聊天图片 + 7天清理 测试
cd "$(dirname "$0")"
DB=/tmp/auth-test6.db
UP=/tmp/auth-uploads-test
rm -f ${DB}*; rm -rf $UP
export AUTH_DB=$DB
export AUTH_UPLOAD_DIR=$UP
export COOKIE_SECURE=0
export ADMIN_EMAILS="admin@saixz.com"
unset BREVO_API_KEY RESEND_API_KEY MAIL_PROVIDER

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test6.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null; rm -f ${DB}*; rm -rf $UP" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api
J='Content-Type: application/json'
getcode() { grep -o "email=$1 code=[0-9]\{6\}" /tmp/auth-test6.log | tail -1 | grep -o '[0-9]\{6\}$'; }
reg_user() {
  curl -s -X POST $B/auth/send-code -H "$J" -d "{\"email\":\"$1\",\"purpose\":\"register\"}" > /dev/null
  sleep 1
  curl -s -c "$2" -X POST $B/auth/register -H "$J" \
    -d "{\"email\":\"$1\",\"code\":\"$(getcode $1)\",\"password\":\"abc12345\",\"phone\":\"13911112222\"}" > /dev/null
}
sql() { .venv/bin/python -c "import sqlite3;c=sqlite3.connect('$DB');print(c.execute(\"$1\").fetchall());c.commit();c.close()"; }

echo "═══════ 1) 注册新用户 → 随机 ID ═══════"
reg_user new1@example.com /tmp/ck-n1.txt
curl -s -b /tmp/ck-n1.txt $B/auth/me | python3 -c "
import json,sys
d = json.load(sys.stdin)
print('nickname:', d['nickname'], '| public_id:', d['public_id'], '| phone:', d['phone'])
assert d['nickname'] == '用户 ' + d['public_id'], '显示名应为 用户+ID'
assert len(d['public_id']) == 8
print('✓ 显示名不含邮箱/手机号')"
reg_user new2@example.com /tmp/ck-n2.txt
echo "两个新用户 ID 不重复: $(sql "SELECT public_id FROM users WHERE email LIKE 'new%'")"

echo "═══════ 2) 老数据迁移 ═══════"
OLD=/tmp/auth-old6.db; rm -f ${OLD}*
.venv/bin/python - <<'PYEOF'
import sqlite3, os, sys
c = sqlite3.connect('/tmp/auth-old6.db')
c.execute("CREATE TABLE users(email TEXT PRIMARY KEY, created_at INTEGER NOT NULL, last_login INTEGER NOT NULL, password_hash TEXT, phone TEXT, updated_at INTEGER, nickname TEXT, avatar TEXT, role TEXT NOT NULL DEFAULT 'user')")
c.execute("INSERT INTO users VALUES('old1@example.com', 1700000000, 1700000000, 'x', '13900001111', 1700000000, 'old1', NULL, 'user')")  # 昵称=邮箱前缀（旧默认）
c.execute("INSERT INTO users VALUES('old2@example.com', 1700000000, 1700000000, 'x', '13900002222', 1700000000, '铁杆球迷', NULL, 'user')")  # 用户自改昵称
c.commit(); c.close()
os.environ['AUTH_DB'] = '/tmp/auth-old6.db'
sys.path.insert(0, '.')
import auth_server
auth_server.init_db()
rows = sqlite3.connect('/tmp/auth-old6.db').execute('SELECT email, nickname, public_id FROM users ORDER BY email').fetchall()
for r in rows: print(r)
assert all(r[2] and len(r[2]) == 8 for r in rows), 'public_id 应补齐'
assert rows[0][1] is None, '邮箱前缀昵称应置空'
assert rows[1][1] == '铁杆球迷', '自改昵称应保留'
print('✓ 迁移正确')
PYEOF
rm -f ${OLD}*

echo "═══════ 3) 敏感词过滤 ═══════"
curl -s -b /tmp/ck-n1.txt -X POST $B/chat/send -H "$J" -d '{"text":"你这个傻逼别赌球了，六合彩都是骗局"}' | python3 -c "
import json,sys
m = json.load(sys.stdin)['message']
print('入库文本:', m['text'])
assert '傻逼' not in m['text'] and '赌球' not in m['text'] and '六合彩' not in m['text']
assert '**' in m['text']
print('✓ 敏感词已替换为 *')"
sleep 2.5

echo "═══════ 4) 聊天图片 ═══════"
for fmt in jpeg png gif webp; do
  python3 -c "
import base64, io, json
from PIL import Image
img = Image.new('RGB', (300, 200), (20, 60, 40))
buf = io.BytesIO()
img.save(buf, 'WEBP' if '$fmt' == 'webp' else '$fmt'.upper())
print(json.dumps({'image': 'data:image/$fmt;base64,' + base64.b64encode(buf.getvalue()).decode()}))
" > /tmp/img.json
  R=$(curl -s -b /tmp/ck-n1.txt -X POST $B/chat/upload -H "$J" -d @/tmp/img.json)
  echo "$fmt → $(echo $R | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['ok'], d['message']['text'] if d['ok'] else d.get('error'))" 2>/dev/null || echo $R)"
  sleep 2.5
done
echo "--- 4b 文件已存 uploads ---"
ls $UP | head -6
echo "--- 4c 图片可通过接口访问 ---"
FNAME=$(ls $UP | head -1)
curl -s -o /dev/null -w "GET /api/chat/img/$FNAME → %{http_code} (%{content_type})\n" $B/chat/img/$FNAME
echo "--- 4d 伪装图片（文本改后缀）被拒 ---"
python3 -c "import base64, json; print(json.dumps({'image': 'data:image/png;base64,' + base64.b64encode(b'not an image at all').decode()}))" > /tmp/bad.json
curl -s -w ' [%{http_code}]' -b /tmp/ck-n1.txt -X POST $B/chat/upload -H "$J" -d @/tmp/bad.json; echo
echo "--- 4e 超大图片被拒 ---"
python3 -c "
import base64, io, json
from PIL import Image
img = Image.new('RGB', (4000, 3000), (100, 50, 20))
buf = io.BytesIO(); img.save(buf, 'JPEG', quality=95)
print(json.dumps({'image': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()}))
" > /tmp/big.json
echo "大小: $(wc -c < /tmp/big.json | tr -d ' ') 字符"
curl -s -w ' [%{http_code}]' -b /tmp/ck-n1.txt -X POST $B/chat/upload -H "$J" -d @/tmp/big.json | head -c 120; echo
sleep 2.5

echo "═══════ 5) 7 天自动清理 ═══════"
echo "--- 5a 造一条 8 天前的文字消息 + 8 天前的图片消息（含文件）---"
.venv/bin/python - <<PYEOF
import sqlite3, time, os
old_ts = int(time.time()) - 8 * 86400
c = sqlite3.connect('$DB')
c.execute("INSERT INTO chat_messages(email, nickname, avatar, text, created_at, kind) VALUES('new1@example.com','测试','','8天前的老消息',?,'text')", (old_ts,))
open('$UP/oldimg12345.jpg', 'wb').write(b'\xff\xd8\xfffake')
c.execute("INSERT INTO chat_messages(email, nickname, avatar, text, created_at, kind) VALUES('new1@example.com','测试','','/api/chat/img/oldimg12345.jpg',?,'image')", (old_ts,))
c.commit()
print('清理前消息数:', c.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0])
c.close()
PYEOF
ls $UP | grep oldimg && echo "（旧图片文件已就位）"
echo "--- 5b 触发清理（GET messages 顺带清理）---"
curl -s "$B/chat/messages" > /dev/null
sql "SELECT COUNT(*) FROM chat_messages" | xargs echo "清理后消息数:"
test -f $UP/oldimg12345.jpg && echo "✗ 旧图片文件还在" || echo "✓ 旧图片文件已删除"
curl -s "$B/chat/messages" | python3 -c "
import json,sys
ms = json.load(sys.stdin)['messages']
assert not any('8天前' in m['text'] for m in ms), '8天前消息应已删除'
print('✓ 8 天前消息已从接口消失，当前', len(ms), '条')"

echo "═══════ 6) 显示名一致性（客服 + admin）═══════"
curl -s -b /tmp/ck-n1.txt -X POST $B/support/send -H "$J" -d '{"text":"测试显示名"}' > /dev/null
sql "SELECT owner_label FROM support_threads WHERE owner_key='u:new1@example.com'" | xargs echo "客服会话显示名:"
reg_user admin@saixz.com /tmp/ck-admin.txt
curl -s -b /tmp/ck-admin.txt $B/auth/admin/users | python3 -c "
import json,sys
us = json.load(sys.stdin)['users']
u = [x for x in us if x['email'] == 'new1@example.com'][0]
print('admin/users 行:', u['email'], '| public_id:', u['public_id'], '| nickname:', repr(u['nickname']))"

kill $UVPID 2>/dev/null
trap - EXIT
rm -f ${DB}* /tmp/ck-n1.txt /tmp/ck-n2.txt /tmp/ck-admin.txt /tmp/img.json /tmp/bad.json /tmp/big.json
rm -rf $UP
echo "═══════ DONE：进程已关闭、测试库与上传目录已删除 ═══════"
