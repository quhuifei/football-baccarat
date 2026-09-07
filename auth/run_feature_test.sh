#!/bin/bash
# 聊天室 + 客服求助 + 管理员导出 测试：dev 模式 → curl → 关进程删库
cd "$(dirname "$0")"
DB=/tmp/auth-test4.db
rm -f ${DB}*
export AUTH_DB=$DB
export COOKIE_SECURE=0
export ADMIN_EMAILS="admin@saixz.com"
unset BREVO_API_KEY RESEND_API_KEY MAIL_PROVIDER

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test4.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null; rm -f ${DB}*" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api
J='Content-Type: application/json'
getcode() { grep -o "email=$1 code=[0-9]\{6\}" /tmp/auth-test4.log | tail -1 | grep -o '[0-9]\{6\}$'; }
reg_user() {
  curl -s -X POST $B/auth/send-code -H "$J" -d "{\"email\":\"$1\",\"purpose\":\"register\"}" > /dev/null
  sleep 1
  curl -s -c "$2" -X POST $B/auth/register -H "$J" \
    -d "{\"email\":\"$1\",\"code\":\"$(getcode $1)\",\"password\":\"abc12345\",\"phone\":\"13911112222\"}" > /dev/null
}

reg_user c1@example.com /tmp/ck-c1.txt
reg_user admin@saixz.com /tmp/ck-admin.txt

echo "═══════ 1) 聊天室 ═══════"
echo "--- 1a 游客 GET 消息（公开 200）---"
curl -s -w ' [%{http_code}]' "$B/chat/messages"; echo
echo "--- 1b 游客 POST 发言（401 need_login）---"
curl -s -w ' [%{http_code}]' -X POST $B/chat/send -H "$J" -d '{"text":"大家好"}'; echo
echo "--- 1c 登录用户发言 + 2 秒限流 ---"
curl -s -b /tmp/ck-c1.txt -X POST $B/chat/send -H "$J" -d '{"text":"第一条：今晚这场看好主队"}'; echo
curl -s -w ' [%{http_code}]' -b /tmp/ck-c1.txt -X POST $B/chat/send -H "$J" -d '{"text":"马上第二条"}'; echo
echo "等 2.5 秒…"; sleep 2.5
curl -s -b /tmp/ck-c1.txt -X POST $B/chat/send -H "$J" -d '{"text":"第二条来了"}' | head -c 150; echo
echo "--- 1d after_id 增量只返回新消息 ---"
curl -s -b /tmp/ck-c1.txt -X POST $B/chat/send -H "$J" -d '{"text":"等下用于增量测试"}' > /dev/null
sleep 2.5
LAST_ID=$(curl -s "$B/chat/messages" | python3 -c "import json,sys; print(json.load(sys.stdin)['messages'][-1]['id'])")
curl -s -b /tmp/ck-c1.txt -X POST $B/chat/send -H "$J" -d '{"text":"这是增量新消息"}' > /dev/null
echo "after_id=$LAST_ID 返回:"
curl -s "$B/chat/messages?after_id=$LAST_ID" | python3 -m json.tool --no-ensure-ascii
echo "--- 1e 消息含 nickname 快照（改昵称后历史消息仍是旧昵称）---"
curl -s -b /tmp/ck-c1.txt -X POST $B/auth/profile/nickname -H "$J" -d '{"nickname":"金牌射手"}' > /dev/null
curl -s "$B/chat/messages" | python3 -c "
import json,sys
ms = json.load(sys.stdin)['messages']
print('历史消息昵称快照:', [m['nickname'] for m in ms])"
echo "--- 1f 超长消息被拒 ---"
LONG=$(python3 -c "print('x'*201)")
curl -s -w ' [%{http_code}]' -b /tmp/ck-c1.txt -X POST $B/chat/send -H "$J" -d "{\"text\":\"$LONG\"}" | head -c 150; echo

echo "═══════ 2) 客服求助 ═══════"
for i in 1 2 3; do
  curl -s -X POST $B/support -H "$J" -d "{\"message\":\"第 $i 条求助：页面打不开了\",\"contact\":\"微信 test$i\"}"; echo
done
echo "--- 同 IP 第 4 条被拒 ---"
curl -s -w ' [%{http_code}]' -X POST $B/support -H "$J" -d '{"message":"第 4 条"}'; echo
echo "--- 日志中的求助邮件内容（dev 模式最后一条）---"
grep "用户求助" /tmp/auth-test4.log | tail -1 | head -c 400; echo
echo "--- 空消息被拒 ---"
curl -s -w ' [%{http_code}]' -X POST $B/support -H "$J" -d '{"message":"  "}' | head -c 150; echo

echo "═══════ 3) 管理员导出 ═══════"
echo "--- 3a 未登录 401 ---"
curl -s -o /dev/null -w '%{http_code}\n' $B/auth/admin/users.csv
echo "--- 3b 普通用户 403 ---"
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/ck-c1.txt $B/auth/admin/users.csv
echo "--- 3c 管理员 CSV（手机号完整）---"
curl -s -b /tmp/ck-admin.txt -D /tmp/csv-hdr.txt $B/auth/admin/users.csv
grep -i 'content-disposition' /tmp/csv-hdr.txt

kill $UVPID 2>/dev/null
trap - EXIT
rm -f ${DB}* /tmp/ck-c1.txt /tmp/ck-admin.txt /tmp/csv-hdr.txt
echo "═══════ DONE：进程已关闭、测试库已删除 ═══════"
