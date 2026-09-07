#!/bin/bash
# 角色体系 + 双向客服 测试：dev 模式 → curl → 关进程删库
cd "$(dirname "$0")"
DB=/tmp/auth-test5.db
rm -f ${DB}*
export AUTH_DB=$DB
export COOKIE_SECURE=0
export ADMIN_EMAILS="admin@saixz.com"
unset BREVO_API_KEY RESEND_API_KEY MAIL_PROVIDER

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test5.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null; rm -f ${DB}*" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api
J='Content-Type: application/json'
getcode() { grep -o "email=$1 code=[0-9]\{6\}" /tmp/auth-test5.log | tail -1 | grep -o '[0-9]\{6\}$'; }
reg_user() {
  curl -s -X POST $B/auth/send-code -H "$J" -d "{\"email\":\"$1\",\"purpose\":\"register\"}" > /dev/null
  sleep 1
  curl -s -c "$2" -X POST $B/auth/register -H "$J" \
    -d "{\"email\":\"$1\",\"code\":\"$(getcode $1)\",\"password\":\"abc12345\",\"phone\":\"13911112222\"}" > /dev/null
}

reg_user staff1@example.com /tmp/ck-staff.txt
reg_user user1@example.com /tmp/ck-user.txt
reg_user admin@saixz.com /tmp/ck-admin.txt

echo "═══════ 1) set-role 角色体系 ═══════"
echo "--- 1a 管理员设 staff1 为客服 ---"
curl -s -b /tmp/ck-admin.txt -X POST $B/auth/admin/set-role -H "$J" -d '{"email":"staff1@example.com","role":"staff"}'; echo
echo "--- 1b staff1 的 me 返回 role=staff ---"
curl -s -b /tmp/ck-staff.txt $B/auth/me | python3 -c "import json,sys; d=json.load(sys.stdin); print('role:', d['role'], '| is_admin:', d['is_admin'])"
echo "--- 1c staff 访问 admin/users → 403；support/threads → 200 ---"
curl -s -o /dev/null -w 'admin/users: %{http_code}\n' -b /tmp/ck-staff.txt $B/auth/admin/users
curl -s -o /dev/null -w 'support/threads: %{http_code}\n' -b /tmp/ck-staff.txt $B/support/threads
echo "--- 1d 普通用户两者都 403 ---"
curl -s -o /dev/null -w 'admin/users: %{http_code}\n' -b /tmp/ck-user.txt $B/auth/admin/users
curl -s -o /dev/null -w 'support/threads: %{http_code}\n' -b /tmp/ck-user.txt $B/support/threads
echo "--- 1e 未登录 401 ---"
curl -s -o /dev/null -w 'threads: %{http_code}\n' $B/support/threads
echo "--- 1f 非法 role 被拒 ---"
curl -s -w ' [%{http_code}]' -b /tmp/ck-admin.txt -X POST $B/auth/admin/set-role -H "$J" -d '{"email":"staff1@example.com","role":"admin"}' | head -c 150; echo
echo "--- 1g admin/users JSON（倒序）---"
curl -s -b /tmp/ck-admin.txt $B/auth/admin/users | python3 -c "
import json,sys
us = json.load(sys.stdin)['users']
for u in us: print(u['email'], u['role'], u['phone'])"

echo "═══════ 2) 客服双向聊天 ═══════"
echo "--- 2a 游客发消息（签 visitor_id cookie）---"
curl -s -c /tmp/ck-visitor.txt -X POST $B/support/send -H "$J" -d '{"text":"游客提问：网站怎么打不开了？"}'; echo
grep fb_visitor /tmp/ck-visitor.txt | awk '{print "visitor cookie:", $6, substr($7,1,12)"..."}'
echo "--- 2b 通知邮件（dev 日志，首条触发）---"
sleep 0.5
grep -o "subject=【赛先知足球】新用户求助" /tmp/auth-test5.log | head -1
echo "--- 2c 游客再发一条不重复触发邮件 ---"
CNT1=$(grep -c "新用户求助" /tmp/auth-test5.log)
curl -s -b /tmp/ck-visitor.txt -X POST $B/support/send -H "$J" -d '{"text":"补充：手机上也打不开"}' > /dev/null
sleep 0.5
CNT2=$(grep -c "新用户求助" /tmp/auth-test5.log)
echo "首条前后邮件数: $CNT1 → $CNT2（应相等）"
echo "--- 2d 管理员 threads 列表看到游客会话（带未读数）---"
curl -s -b /tmp/ck-admin.txt $B/support/threads | python3 -m json.tool --no-ensure-ascii
echo "--- 2e 管理员回复 ---"
TID=$(curl -s -b /tmp/ck-admin.txt $B/support/threads | python3 -c "import json,sys; print(json.load(sys.stdin)['threads'][0]['thread_id'])")
curl -s -b /tmp/ck-admin.txt -X POST $B/support/thread/$TID/reply -H "$J" -d '{"text":"您好，已修复，请刷新试试"}'; echo
echo "--- 2f 游客轮询到回复 ---"
curl -s -b /tmp/ck-visitor.txt "$B/support/my" | python3 -c "
import json,sys
ms = json.load(sys.stdin)['messages']
for m in ms: print(m['sender'], '|', m['sender_name'], '|', m['text'])"
echo "--- 2g 回复后 threads 未读清零 ---"
curl -s -b /tmp/ck-admin.txt $B/support/threads | python3 -c "
import json,sys
t = json.load(sys.stdin)['threads'][0]
print('unread:', t['unread'], '| last:', t['last_text'])"
echo "--- 2h 登录用户链路 ---"
curl -s -b /tmp/ck-user.txt -X POST $B/support/send -H "$J" -d '{"text":"登录用户提问：账号能改邮箱吗"}' > /dev/null
curl -s -b /tmp/ck-user.txt "$B/support/my" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print('thread_id:', d['thread_id'], '| 消息数:', len(d['messages']), '| sender:', d['messages'][0]['sender'])"
echo "--- 2i 每分钟 5 条限流（游客已发 2 条，再发 4 条第 4 条应 429）---"
for i in 3 4 5 6; do
  curl -s -w ' [%{http_code}]' -b /tmp/ck-visitor.txt -X POST $B/support/send -H "$J" -d "{\"text\":\"第 $i 条\"}" -o /dev/null; echo " <- 第 $i 条"
done

echo "═══════ 3) 新旧兼容 ═══════"
echo "--- 3a users.csv 仍正常 ---"
curl -s -b /tmp/ck-admin.txt $B/auth/admin/users.csv | head -3
echo "--- 3b 旧 POST /api/support（单向邮件）已删除 → 404/405 ---"
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/support -H "$J" -d '{"message":"x"}'
echo "--- 3c 老库自动加 role 列 ---"
.venv/bin/python - <<'PYEOF'
import sqlite3, os
c = sqlite3.connect('/tmp/auth-old5.db')
c.execute("CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, created_at INTEGER NOT NULL, last_login INTEGER NOT NULL)")
c.execute("INSERT OR IGNORE INTO users VALUES('legacy@x.com', 1700000000, 1700000000)")
c.commit(); c.close()
os.environ['AUTH_DB'] = '/tmp/auth-old5.db'
import importlib, sys
sys.path.insert(0, '.')
import auth_server
auth_server.init_db()
cols = [r[1] for r in sqlite3.connect('/tmp/auth-old5.db').execute('PRAGMA table_info(users)')]
print('迁移后列:', cols)
row = sqlite3.connect('/tmp/auth-old5.db').execute('SELECT email, role FROM users').fetchone()
print('老账号:', row)
PYEOF
rm -f /tmp/auth-old5.db*

kill $UVPID 2>/dev/null
trap - EXIT
rm -f ${DB}* /tmp/ck-staff.txt /tmp/ck-user.txt /tmp/ck-admin.txt /tmp/ck-visitor.txt
echo "═══════ DONE：进程已关闭、测试库已删除 ═══════"
