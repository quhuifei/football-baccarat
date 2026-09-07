#!/bin/bash
# 注册+密码登录全流程测试：dev 模式启动 → curl → 关闭进程 → 删测试库
cd "$(dirname "$0")"
DB=/tmp/auth-test2.db
rm -f ${DB}*
export AUTH_DB=$DB
export COOKIE_SECURE=0
export ADMIN_EMAILS="admin@saixz.com"
unset BREVO_API_KEY RESEND_API_KEY MAIL_PROVIDER

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test2.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null; rm -f ${DB}*" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api/auth
getcode() { grep -o "email=$1 code=[0-9]\{6\}" /tmp/auth-test2.log | tail -1 | grep -o '[0-9]\{6\}$'; }
sql() { .venv/bin/python -c "import sqlite3;c=sqlite3.connect('$DB');print(c.execute(\"$1\").fetchall());c.commit();c.close()"; }

echo "═══════ 1) 注册全流程 ═══════"
echo "--- 1a 发码 purpose=register ---"
curl -s -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"u1@example.com","purpose":"register"}'; echo
sleep 1; CODE=$(getcode u1@example.com); echo "日志验证码: $CODE"
echo "--- 1b 错误验证码注册被拒 ---"
curl -s -w ' [%{http_code}]' -X POST $B/register -H 'Content-Type: application/json' \
  -d '{"email":"u1@example.com","code":"999999","password":"abc12345","phone":"13812341234"}'; echo
echo "--- 1c 弱密码被拒: 太短 ---"
curl -s -w ' [%{http_code}]' -X POST $B/register -H 'Content-Type: application/json' \
  -d "{\"email\":\"u1@example.com\",\"code\":\"$CODE\",\"password\":\"a1b2\",\"phone\":\"13812341234\"}" | head -c 200; echo
echo "--- 1d 弱密码被拒: 纯数字 ---"
curl -s -w ' [%{http_code}]' -X POST $B/register -H 'Content-Type: application/json' \
  -d "{\"email\":\"u1@example.com\",\"code\":\"$CODE\",\"password\":\"12345678\",\"phone\":\"13812341234\"}" | head -c 200; echo
echo "--- 1e 手机号非法被拒 ---"
curl -s -w ' [%{http_code}]' -X POST $B/register -H 'Content-Type: application/json' \
  -d "{\"email\":\"u1@example.com\",\"code\":\"$CODE\",\"password\":\"abc12345\",\"phone\":\"2381234123\"}" | head -c 200; echo
echo "--- 1f 合规注册成功 → cookie ---"
curl -s -c /tmp/ck1.txt -X POST $B/register -H 'Content-Type: application/json' \
  -d "{\"email\":\"u1@example.com\",\"code\":\"$CODE\",\"password\":\"abc12345\",\"phone\":\"13812341234\"}"; echo
grep fb_token /tmp/ck1.txt | awk '{print "cookie:", $6, substr($7,1,16)"..."}'
echo "users 表: $(sql "SELECT email,phone,password_hash IS NOT NULL,created_at FROM users")"

echo "═══════ 2) 重复注册 ═══════"
echo "--- 2a 已注册邮箱发码 ---"
curl -s -w ' [%{http_code}]' -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"u1@example.com","purpose":"register"}'; echo

echo "═══════ 3) 登录防暴力 ═══════"
for i in 1 2 3 4 5; do
  curl -s -w ' [%{http_code}]' -X POST $B/login -H 'Content-Type: application/json' \
    -d '{"email":"u1@example.com","password":"wrongpass1"}'; echo
done
echo "--- 3b 锁定后即使密码正确也被拒 ---"
curl -s -w ' [%{http_code}]' -X POST $B/login -H 'Content-Type: application/json' \
  -d '{"email":"u1@example.com","password":"abc12345"}'; echo
echo "--- 3c 清锁定后正确密码登录（remember=false，看 Set-Cookie 无 Max-Age）---"
sql "DELETE FROM login_guard" > /dev/null
curl -s -D - -o /dev/null -c /tmp/ck2.txt -X POST $B/login -H 'Content-Type: application/json' \
  -d '{"email":"u1@example.com","password":"abc12345","remember":false}' | grep -i 'set-cookie'
echo "--- 3d remember=true 有 Max-Age ---"
curl -s -D - -o /dev/null -X POST $B/login -H 'Content-Type: application/json' \
  -d '{"email":"u1@example.com","password":"abc12345","remember":true}' | grep -i 'set-cookie'

echo "═══════ 4) 忘记密码 ═══════"
echo "--- 4a 未注册邮箱 reset 发码 → ok 但不发码 ---"
MARK=$(wc -l < /tmp/auth-test2.log | tr -d ' ')
curl -s -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"ghost@example.com","purpose":"reset"}'; echo
sleep 0.5
tail -n +$MARK /tmp/auth-test2.log | grep -c "未注册，不发码" | xargs echo "日志『未注册，不发码』条数:"
echo "--- 4b 已注册邮箱 reset 全流程 ---"
curl -s -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"u1@example.com","purpose":"reset"}' > /dev/null
sleep 1; RCODE=$(getcode u1@example.com); echo "日志验证码: $RCODE"
curl -s -c /tmp/ck3.txt -X POST $B/reset-password -H 'Content-Type: application/json' \
  -d "{\"email\":\"u1@example.com\",\"code\":\"$RCODE\",\"password\":\"newpass99\"}"; echo
echo "--- 4c 旧密码失效 ---"
curl -s -w ' [%{http_code}]' -X POST $B/login -H 'Content-Type: application/json' \
  -d '{"email":"u1@example.com","password":"abc12345"}'; echo
echo "--- 4d 新密码可登录 ---"
sql "DELETE FROM login_guard" > /dev/null
curl -s -c /tmp/ck4.txt -X POST $B/login -H 'Content-Type: application/json' \
  -d '{"email":"u1@example.com","password":"newpass99"}'; echo

echo "═══════ 5) me / logout ═══════"
curl -s -b /tmp/ck4.txt $B/me; echo
echo "check 带 cookie: $(curl -s -o /dev/null -w '%{http_code}' -b /tmp/ck4.txt $B/check)"
curl -s -b /tmp/ck4.txt -X POST $B/logout > /dev/null
echo "logout 后 check: $(curl -s -o /dev/null -w '%{http_code}' -b /tmp/ck4.txt $B/check)"

echo "═══════ 6) 老库迁移检查（旧 users 无 password_hash 列）═══════"
OLDDB=/tmp/auth-old.db; rm -f ${OLDDB}*
.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect('/tmp/auth-old.db')
c.execute("CREATE TABLE users(email TEXT PRIMARY KEY, created_at INTEGER NOT NULL, last_login INTEGER NOT NULL)")
c.execute("INSERT INTO users VALUES('legacy@example.com', 1700000000, 1700000000)")
c.commit(); c.close()
EOF
AUTH_DB=$OLDDB .venv/bin/python -c "
import os; os.environ['AUTH_DB']='/tmp/auth-old.db'
import auth_server; auth_server.init_db()
import sqlite3
cols = [r[1] for r in sqlite3.connect('/tmp/auth-old.db').execute('PRAGMA table_info(users)')]
print('迁移后 users 列:', cols)
row = sqlite3.connect('/tmp/auth-old.db').execute('SELECT email, password_hash, phone FROM users').fetchone()
print('老账号完好:', row)
"
rm -f ${OLDDB}*

kill $UVPID 2>/dev/null
trap - EXIT
rm -f ${DB}* /tmp/ck1.txt /tmp/ck2.txt /tmp/ck3.txt /tmp/ck4.txt
echo "═══════ DONE：进程已关闭、测试库已删除 ═══════"
