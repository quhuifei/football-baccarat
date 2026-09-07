#!/bin/bash
# 个人中心功能测试：dev 模式启动 → curl → 关闭进程 → 删测试库
cd "$(dirname "$0")"
DB=/tmp/auth-test3.db
rm -f ${DB}*
export AUTH_DB=$DB
export COOKIE_SECURE=0
unset BREVO_API_KEY RESEND_API_KEY MAIL_PROVIDER

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test3.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null; rm -f ${DB}*" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api/auth
J='Content-Type: application/json'
getcode() { grep -o "email=$1 code=[0-9]\{6\}" /tmp/auth-test3.log | tail -1 | grep -o '[0-9]\{6\}$'; }

echo "═══════ 0) 准备：注册用户 ═══════"
curl -s -X POST $B/send-code -H "$J" -d '{"email":"p1@example.com","purpose":"register"}' > /dev/null
sleep 1; CODE=$(getcode p1@example.com)
curl -s -c /tmp/pck.txt -X POST $B/register -H "$J" \
  -d "{\"email\":\"p1@example.com\",\"code\":\"$CODE\",\"password\":\"abc12345\",\"phone\":\"13998765432\"}"; echo

echo "═══════ 1) profile 默认昵称（邮箱前缀）═══════"
curl -s -b /tmp/pck.txt $B/profile | python3 -m json.tool --no-ensure-ascii 2>/dev/null || curl -s -b /tmp/pck.txt $B/profile; echo

echo "═══════ 2) 改昵称 ═══════"
echo "--- 2a 合法昵称 ---"
curl -s -b /tmp/pck.txt -X POST $B/profile/nickname -H "$J" -d '{"nickname":"金色前锋-7号"}'; echo
echo "--- 2b 非法昵称：含 <> ---"
curl -s -w ' [%{http_code}]' -b /tmp/pck.txt -X POST $B/profile/nickname -H "$J" -d '{"nickname":"<script>x</script>"}' | head -c 220; echo
echo "--- 2c 非法昵称：太短 ---"
curl -s -w ' [%{http_code}]' -b /tmp/pck.txt -X POST $B/profile/nickname -H "$J" -d '{"nickname":"甲"}' | head -c 220; echo
echo "--- 2d 未登录 401 ---"
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/profile/nickname -H "$J" -d '{"nickname":"abc"}'
curl -s -o /dev/null -w 'profile GET 未登录: %{http_code}\n' $B/profile

echo "═══════ 3) 头像 ═══════"
AV=$(python3 -c "
import base64, io
from PIL import Image
img = Image.new('RGB', (128,128), (201,162,39))
buf = io.BytesIO(); img.save(buf, 'JPEG', quality=85)
print('data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode())
")
echo "小头像大小: $(echo -n $AV | wc -c | tr -d ' ') 字符"
echo "--- 3a 合法小头像 ---"
python3 -c "import json;print(json.dumps({'avatar':'$AV'}))" > /tmp/av.json
curl -s -b /tmp/pck.txt -X POST $B/profile/avatar -H "$J" -d @/tmp/av.json | head -c 120; echo
echo "--- 3b 超大头像（>200KB 解码后）---"
python3 -c "
import json, base64, os
big = 'data:image/jpeg;base64,' + base64.b64encode(os.urandom(250*1024)).decode()
print(json.dumps({'avatar': big}))
" > /tmp/avbig.json
curl -s -w ' [%{http_code}]' -b /tmp/pck.txt -X POST $B/profile/avatar -H "$J" -d @/tmp/avbig.json | head -c 220; echo
echo "--- 3c 错误前缀 ---"
curl -s -w ' [%{http_code}]' -b /tmp/pck.txt -X POST $B/profile/avatar -H "$J" -d '{"avatar":"data:image/png;base64,iVBORw0KGgo="}' | head -c 220; echo

echo "═══════ 4) 改密码 + session 作废 ═══════"
echo "--- 4a 旧密码错误被拒（计锁定）---"
curl -s -w ' [%{http_code}]' -b /tmp/pck.txt -X POST $B/profile/password -H "$J" \
  -d '{"old_password":"wrongold1","new_password":"newpass99"}'; echo
echo "--- 4b 第二个 session（另一设备）---"
curl -s -c /tmp/pck2.txt -X POST $B/login -H "$J" \
  -d '{"email":"p1@example.com","password":"abc12345"}' > /dev/null
echo "--- 4c 用 session1 改密码成功 ---"
curl -s -b /tmp/pck.txt -X POST $B/profile/password -H "$J" \
  -d '{"old_password":"abc12345","new_password":"newpass99"}'; echo
echo "--- 4d session2 应失效(401)，session1 保留(200) ---"
echo "session2 check: $(curl -s -o /dev/null -w '%{http_code}' -b /tmp/pck2.txt $B/check)"
echo "session1 check: $(curl -s -o /dev/null -w '%{http_code}' -b /tmp/pck.txt $B/check)"
echo "--- 4e 新密码能登录 ---"
curl -s -X POST $B/login -H "$J" -d '{"email":"p1@example.com","password":"newpass99"}'; echo

echo "═══════ 5) me 返回 nickname + avatar ═══════"
curl -s -b /tmp/pck.txt $B/me | python3 -c "
import json,sys
d = json.load(sys.stdin)
print('nickname:', d.get('nickname'))
print('avatar 前缀:', (d.get('avatar') or '')[:30], '长度:', len(d.get('avatar') or ''))
print('phone:', d.get('phone'))
"

kill $UVPID 2>/dev/null
trap - EXIT
rm -f ${DB}* /tmp/pck.txt /tmp/pck2.txt /tmp/av.json /tmp/avbig.json
echo "═══════ DONE：进程已关闭、测试库已删除 ═══════"
