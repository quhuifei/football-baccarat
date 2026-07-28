#!/bin/bash
# 本机一次性测试：dev 模式启动 → curl 全接口 → 关闭进程
cd "$(dirname "$0")"
rm -f /tmp/auth-test.db*
export AUTH_DB=/tmp/auth-test.db
export COOKIE_SECURE=0          # 本机 http 测试
export ADMIN_EMAILS="admin@saixz.com"
unset BREVO_API_KEY             # dev 模式：验证码打日志
unset RESEND_API_KEY            # dev 模式（MAIL_PROVIDER 默认 resend）
unset MAIL_PROVIDER

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api/auth
echo "=== 1) send-code 正常 ==="
curl -s -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"test@example.com"}'; echo
sleep 1
CODE=$(grep -o 'code=[0-9]\{6\}' /tmp/auth-test.log | tail -1 | cut -d= -f2)
echo "日志中读到的验证码: $CODE"

echo "=== 2) 60 秒内重复发送被拒 ==="
curl -s -w ' [HTTP %{http_code}]' -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"test@example.com"}'; echo

echo "=== 3) 邮箱格式校验 ==="
curl -s -w ' [HTTP %{http_code}]' -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"not-an-email"}'; echo

echo "=== 4) verify 错误码 ==="
WRONG=$(( (CODE + 1) % 1000000 ))
curl -s -w ' [HTTP %{http_code}]' -X POST $B/verify -H 'Content-Type: application/json' -d "{\"email\":\"test@example.com\",\"code\":\"$(printf %06d $WRONG)\"}"; echo

echo "=== 5) verify 正确码 → cookie ==="
curl -s -c /tmp/auth-ck.txt -X POST $B/verify -H 'Content-Type: application/json' -d "{\"email\":\"test@example.com\",\"code\":\"$CODE\"}"; echo
grep fb_token /tmp/auth-ck.txt | awk '{print "cookie:", $6, substr($7,1,20)"..."}'

echo "=== 6) check 带 cookie (期望 200) ==="
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/auth-ck.txt $B/check

echo "=== 7) check 不带 cookie (期望 401) ==="
curl -s -o /dev/null -w '%{http_code}\n' $B/check

echo "=== 8) me ==="
curl -s -b /tmp/auth-ck.txt $B/me; echo
curl -s -c /tmp/auth-ck2.txt -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"admin@saixz.com"}' > /dev/null
sleep 1
CODE2=$(grep -o 'code=[0-9]\{6\}' /tmp/auth-test.log | tail -1 | cut -d= -f2)
curl -s -c /tmp/auth-ck2.txt -X POST $B/verify -H 'Content-Type: application/json' -d "{\"email\":\"admin@saixz.com\",\"code\":\"$CODE2\"}" > /dev/null
echo "admin me: $(curl -s -b /tmp/auth-ck2.txt $B/me)"

echo "=== 9) logout 后 check (期望 401) ==="
curl -s -b /tmp/auth-ck.txt -c /tmp/auth-ck.txt -X POST $B/logout; echo
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/auth-ck.txt $B/check

echo "=== 10) 错误 5 次验证码作废 ==="
curl -s -X POST $B/send-code -H 'Content-Type: application/json' -d '{"email":"brute@example.com"}' > /dev/null
sleep 1
for i in 1 2 3 4 5; do
  curl -s -X POST $B/verify -H 'Content-Type: application/json' -d '{"email":"brute@example.com","code":"000001"}' | head -c 120; echo
done
echo "--- 作废后即使猜到也拿不到码(无码记录) ---"
curl -s -w ' [HTTP %{http_code}]' -X POST $B/verify -H 'Content-Type: application/json' -d '{"email":"brute@example.com","code":"000001"}'; echo

kill $UVPID 2>/dev/null
trap - EXIT
echo "=== DONE, 进程已关闭 ==="
