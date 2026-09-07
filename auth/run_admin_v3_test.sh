#!/bin/bash
# 管理后台大扩充 v3 测试：禁言/封号/注销/重置密码/搜索/敏感词/聊天管理/
# 统计(nginx)/数据状态+更新(git)/公告/客服图片双向/超限拒绝
cd "$(dirname "$0")"
DB=/tmp/auth-test7.db
UP=/tmp/auth-uploads-test7
NGX=/tmp/auth-ngx-test7
REPO=/tmp/fb-repo-test7
ORIGIN=/tmp/fb-origin-test7.git
rm -f ${DB}*; rm -rf $UP $NGX $REPO $ORIGIN /tmp/fb-push-test7
mkdir -p $NGX

export AUTH_DB=$DB
export AUTH_UPLOAD_DIR=$UP
export NGINX_LOG_DIR=$NGX
export FOOTBALL_REPO_DIR=$REPO
export COOKIE_SECURE=0
export ADMIN_EMAILS="admin@saixz.com"
unset BREVO_API_KEY RESEND_API_KEY MAIL_PROVIDER

# 造一个可离线 fetch/reset 的 git 仓库（bare origin + 工作克隆）
git init -q --bare $ORIGIN
git clone -q $ORIGIN $REPO 2>/dev/null
( cd $REPO && echo '{"v":1}' > data.json && git add . \
  && git -c user.email=t@t.t -c user.name=t commit -qm "init data" \
  && git branch -M main && git push -q -u origin main )

.venv/bin/uvicorn auth_server:app --host 127.0.0.1 --port 8400 > /tmp/auth-test7.log 2>&1 &
UVPID=$!
trap "kill $UVPID 2>/dev/null; rm -f ${DB}*; rm -rf $UP $NGX $REPO $ORIGIN /tmp/fb-push-test7" EXIT

for i in $(seq 1 20); do
  curl -s -o /dev/null http://127.0.0.1:8400/api/auth/check && break
  sleep 0.5
done

B=http://127.0.0.1:8400/api
J='Content-Type: application/json'
getcode() { grep -o "email=$1 code=[0-9]\{6\}" /tmp/auth-test7.log | tail -1 | grep -o '[0-9]\{6\}$'; }
reg_user() {
  curl -s -X POST $B/auth/send-code -H "$J" -d "{\"email\":\"$1\",\"purpose\":\"register\"}" > /dev/null
  sleep 1
  curl -s -c "$2" -X POST $B/auth/register -H "$J" \
    -d "{\"email\":\"$1\",\"code\":\"$(getcode $1)\",\"password\":\"$3\",\"phone\":\"13911112222\"}" > /dev/null
}
sql() { .venv/bin/python -c "import sqlite3;c=sqlite3.connect('$DB');print(c.execute(\"$1\").fetchall());c.commit();c.close()"; }
PASS=0; FAIL=0
ck() { if [ "$2" = "$3" ]; then echo "  ✓ $1"; PASS=$((PASS+1)); else echo "  ✗ $1 （期望[$3] 实际[$2]）"; FAIL=$((FAIL+1)); fi; }

echo "═══════ 0) 准备账号 ═══════"
reg_user admin@saixz.com /tmp/ck7-admin.txt adminpass1
reg_user u1@example.com /tmp/ck7-u1.txt userpass1
reg_user u2@example.com /tmp/ck7-u2.txt userpass2
echo "admin/u1/u2 已注册"

echo "═══════ 1) 用户搜索 ═══════"
R=$(curl -s -b /tmp/ck7-admin.txt "$B/auth/admin/users-search")
echo "$R" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['ok'] and len(d['users'])>=3; print('  ✓ 无 q 全量返回', len(d['users']), '个用户')" && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
R=$(curl -s -b /tmp/ck7-admin.txt "$B/auth/admin/users-search?q=u1@")
ck "q=u1@ 命中 1 个" "$(echo $R | python3 -c "import json,sys; print(len(json.load(sys.stdin)['users']))")" "1"
R=$(curl -s -b /tmp/ck7-admin.txt "$B/auth/admin/users-search?q=%E4%B8%8D%E5%AD%98%E5%9C%A8xyz")
ck "无匹配返回空" "$(echo "$R" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['users']))")" "0"
R=$(curl -s -b /tmp/ck7-u1.txt "$B/auth/admin/users-search" -o /dev/null -w "%{http_code}")
ck "普通用户访问管理接口被拒" "$R" "403"

echo "═══════ 2) 禁言：聊天 403 / 客服正常 ═══════"
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/ban -H "$J" -d '{"email":"u1@example.com","banned":true}' > /dev/null
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/chat/send -H "$J" -d '{"text":"还能说话吗"}' -w '\n%{http_code}')
ck "禁言后聊天室发言 403" "$(echo "$R" | tail -1)" "403"
echo "$R" | head -1 | grep -q "已被禁言" && { echo "  ✓ 提示语正确"; PASS=$((PASS+1)); } || { echo "  ✗ 提示语不对"; FAIL=$((FAIL+1)); }
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/support/send -H "$J" -d '{"text":"禁言后还能找客服"}')
ck "禁言后客服通道正常" "$(echo $R | python3 -c "import json,sys; print(json.load(sys.stdin)['ok'])")" "True"
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/ban -H "$J" -d '{"email":"u1@example.com","banned":false}' > /dev/null
sleep 2.5
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/chat/send -H "$J" -d '{"text":"解禁了"}' -o /dev/null -w "%{http_code}")
ck "解禁后聊天恢复" "$R" "200"

echo "═══════ 3) 封号：旧 session 死 + 登录 403 ═══════"
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/disable -H "$J" -d '{"email":"u1@example.com","disabled":true}' > /dev/null
R=$(curl -s -b /tmp/ck7-u1.txt $B/auth/me -o /dev/null -w "%{http_code}")
ck "封号后旧 session 立即失效" "$R" "401"
R=$(curl -s -X POST $B/auth/login -H "$J" -d '{"email":"u1@example.com","password":"userpass1"}' -w '\n%{http_code}')
ck "封号后登录 403" "$(echo "$R" | tail -1)" "403"
echo "$R" | head -1 | grep -q "已被封禁" && { echo "  ✓ 提示语正确"; PASS=$((PASS+1)); } || { echo "  ✗ 提示语不对"; FAIL=$((FAIL+1)); }
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/disable -H "$J" -d '{"email":"u1@example.com","disabled":false}' > /dev/null
curl -s -c /tmp/ck7-u1.txt -X POST $B/auth/login -H "$J" -d '{"email":"u1@example.com","password":"userpass1"}' > /dev/null
R=$(curl -s -b /tmp/ck7-u1.txt $B/auth/me -o /dev/null -w "%{http_code}")
ck "解封后可重新登录" "$R" "200"

echo "═══════ 4) 重置密码 ═══════"
R=$(curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/reset-user-password -H "$J" -d '{"email":"u1@example.com"}')
NEWPW=$(echo $R | python3 -c "import json,sys; print(json.load(sys.stdin).get('password',''))")
ck "返回 10 位新密码" "${#NEWPW}" "10"
R=$(curl -s -b /tmp/ck7-u1.txt $B/auth/me -o /dev/null -w "%{http_code}")
ck "重置后旧 session 失效" "$R" "401"
R=$(curl -s -X POST $B/auth/login -H "$J" -d '{"email":"u1@example.com","password":"userpass1"}' -o /dev/null -w "%{http_code}")
ck "旧密码登录失败" "$R" "400"
curl -s -c /tmp/ck7-u1.txt -X POST $B/auth/login -H "$J" -d "{\"email\":\"u1@example.com\",\"password\":\"$NEWPW\"}" > /dev/null
R=$(curl -s -b /tmp/ck7-u1.txt $B/auth/me -o /dev/null -w "%{http_code}")
ck "新密码登录成功" "$R" "200"

echo "═══════ 5) 管理员账号保护 ═══════"
for ep in reset-user-password disable delete-user ban; do
  case $ep in
    disable) BODY='{"email":"admin@saixz.com","disabled":true}';;
    ban)     BODY='{"email":"admin@saixz.com","banned":true}';;
    *)       BODY='{"email":"admin@saixz.com"}';;
  esac
  R=$(curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/$ep -H "$J" -d "$BODY" -o /dev/null -w "%{http_code}")
  ck "$ep 拒操作管理员" "$R" "400"
done

echo "═══════ 6) 敏感词增删立即生效 ═══════"
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/words/add -H "$J" -d '{"word":"测试违禁词"}' > /dev/null
sleep 2.5
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/chat/send -H "$J" -d '{"text":"这句话含测试违禁词哦"}')
echo "$R" | python3 -c "
import json,sys
t = json.load(sys.stdin)['message']['text']
assert '测试违禁词' not in t and '*****' in t, t
print('  ✓ 新词立即过滤:', t)" && PASS=$((PASS+1)) || { echo "  ✗ 新词未过滤: $R"; FAIL=$((FAIL+1)); }
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/words/delete -H "$J" -d '{"word":"测试违禁词"}' > /dev/null
sleep 2.5
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/chat/send -H "$J" -d '{"text":"再提测试违禁词"}')
echo "$R" | python3 -c "
import json,sys
t = json.load(sys.stdin)['message']['text']
assert '测试违禁词' in t, t
print('  ✓ 删词后恢复:', t)" && PASS=$((PASS+1)) || { echo "  ✗ 删词未生效: $R"; FAIL=$((FAIL+1)); }
R=$(curl -s -b /tmp/ck7-admin.txt $B/auth/admin/words | python3 -c "import json,sys; print(len(json.load(sys.stdin)['words']))")
echo "  当前词库 $R 个词（含种子）"

echo "═══════ 7) 聊天室管理：分页 + 删图连文件 ═══════"
python3 -c "
import base64, io, json
from PIL import Image
img = Image.new('RGB', (120, 80), (30, 70, 50))
buf = io.BytesIO(); img.save(buf, 'JPEG')
print(json.dumps({'image': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()}))
" > /tmp/img7.json
sleep 2.5
IMGMSG=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/chat/upload -H "$J" -d @/tmp/img7.json)
IMGURL=$(echo $IMGMSG | python3 -c "import json,sys; print(json.load(sys.stdin)['message']['text'])")
IMGF=$(basename $IMGURL)
test -f "$UP/$IMGF" && { echo "  ✓ 图片文件已存: $IMGF"; PASS=$((PASS+1)); } || { echo "  ✗ 图片文件缺失"; FAIL=$((FAIL+1)); }
R=$(curl -s -b /tmp/ck7-admin.txt "$B/auth/admin/chat?page=1&size=2")
echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin)
assert d['ok'] and d['total'] >= 3 and len(d['messages']) == 2, d
print('  ✓ 分页正常 total=%d size=2 返回 %d 条（含 email 字段: %s）' % (d['total'], len(d['messages']), d['messages'][0]['email']))" && PASS=$((PASS+1)) || { echo "  ✗ 分页异常: $R"; FAIL=$((FAIL+1)); }
DELID=$(echo $IMGMSG | python3 -c "import json,sys; print(json.load(sys.stdin)['message']['id'])")
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/chat/delete -H "$J" -d "{\"id\":$DELID}" > /dev/null
test -f "$UP/$IMGF" && { echo "  ✗ 删消息后图片文件还在"; FAIL=$((FAIL+1)); } || { echo "  ✓ 删图片消息连文件一起删"; PASS=$((PASS+1)); }

echo "═══════ 8) 数据统计（nginx 访问量解析）═══════"
cat > $NGX/access.log <<'LOGEOF'
1.1.1.1 - - [10/Jan/2026:12:00:01 +0000] "GET / HTTP/1.1" 200 1234 "-" "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
1.1.1.1 - - [10/Jan/2026:12:01:01 +0000] "GET /index.html HTTP/1.1" 304 0 "-" "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"
2.2.2.2 - - [11/Jan/2026:09:00:01 +0000] "GET / HTTP/1.1" 200 1234 "-" "Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari"
9.9.9.9 - - [11/Jan/2026:09:05:01 +0000] "GET / HTTP/1.1" 200 1234 "-" "python-requests/2.31.0"
1.1.1.1 - - [11/Jan/2026:09:10:01 +0000] "GET /data/abc.js HTTP/1.1" 200 999 "-" "Mozilla/5.0"
1.1.1.1 - - [11/Jan/2026:09:11:01 +0000] "GET / HTTP/1.1" 500 0 "-" "Mozilla/5.0"
LOGEOF
R=$(curl -s -b /tmp/ck7-admin.txt $B/auth/admin/stats)
echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin)
v = d['visits']
assert d['users'] == 3, d
assert v['total'] == 3, v           # 只算 / 与 /index.html 的 200/304，排除爬虫
assert v['unique_ips'] == 2, v
assert all('python' not in u['ua'] for u in v['top_uas']), v
print('  ✓ stats: users=3 访问量=3 独立IP=2（bot/非首页/非200 均已排除）')" && PASS=$((PASS+1)) || { echo "  ✗ stats 异常: $R"; FAIL=$((FAIL+1)); }

echo "═══════ 9) 数据状态 + 一键更新 ═══════"
R=$(curl -s -b /tmp/ck7-admin.txt $B/auth/admin/data-status)
echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin)
assert d['ok'] and d['exists'] and 'init data' in d['commit'], d
print('  ✓ data-status:', d['latest_file'], '|', d['commit'])" && PASS=$((PASS+1)) || { echo "  ✗ data-status 异常: $R"; FAIL=$((FAIL+1)); }
# 往 origin 推一个新提交，然后走接口更新
git clone -q $ORIGIN /tmp/fb-push-test7 2>/dev/null
( cd /tmp/fb-push-test7 && git checkout -q main 2>/dev/null; echo '{"v":2}' > data2.json \
  && git add . && git -c user.email=t@t.t -c user.name=t commit -qm "add data2" && git push -q origin main )
R=$(curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/data-update -H "$J" -d '{}')
echo "$R" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['ok'], d; print('  ✓ data-update 返回 ok')" && PASS=$((PASS+1)) || { echo "  ✗ data-update 失败: $R"; FAIL=$((FAIL+1)); }
test -f "$REPO/data2.json" && { echo "  ✓ 新提交已同步到仓库"; PASS=$((PASS+1)); } || { echo "  ✗ 仓库未更新"; FAIL=$((FAIL+1)); }

echo "═══════ 10) 公告 ═══════"
R=$(curl -s $B/announcement)
ck "初始无公告" "$(echo $R | python3 -c "import json,sys; print(json.load(sys.stdin)['announcement'])")" "None"
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/announcements/add -H "$J" -d '{"text":"第一条公告"}' > /dev/null
R=$(curl -s $B/announcement | python3 -c "import json,sys; print(json.load(sys.stdin)['announcement']['text'])")
ck "发布后公开接口可读" "$R" "第一条公告"
ANN1=$(curl -s -b /tmp/ck7-admin.txt $B/auth/admin/announcements | python3 -c "import json,sys; print(json.load(sys.stdin)['announcements'][0]['id'])")
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/announcements/add -H "$J" -d '{"text":"第二条公告"}' > /dev/null
R=$(curl -s $B/announcement | python3 -c "import json,sys; print(json.load(sys.stdin)['announcement']['text'])")
ck "新公告自动顶替旧公告" "$R" "第二条公告"
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/announcements/deactivate -H "$J" -d "{\"id\":$((ANN1+1))}" > /dev/null
R=$(curl -s $B/announcement | python3 -c "import json,sys; print(json.load(sys.stdin)['announcement'])")
ck "下架后公开接口为空" "$R" "None"

echo "═══════ 11) 客服图片：用户/客服双向 + 超限拒绝 ═══════"
TID=$(sql "SELECT id FROM support_threads WHERE owner_key='u:u1@example.com'" | grep -o '[0-9]\+')
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/support/upload -H "$J" -d @/tmp/img7.json)
echo "$R" | python3 -c "
import json,sys
m = json.load(sys.stdin)['message']
assert m['kind'] == 'image' and m['text'].startswith('/api/chat/img/'), m
print('  ✓ 用户客服发图 ok:', m['text'])" && PASS=$((PASS+1)) || { echo "  ✗ 用户客服发图失败: $R"; FAIL=$((FAIL+1)); }
R=$(curl -s -b /tmp/ck7-admin.txt -X POST $B/support/thread/$TID/upload -H "$J" -d @/tmp/img7.json)
echo "$R" | python3 -c "
import json,sys
m = json.load(sys.stdin)['message']
assert m['kind'] == 'image' and m['sender'] == 'staff', m
print('  ✓ 客服侧发图 ok:', m['sender'], m['text'])" && PASS=$((PASS+1)) || { echo "  ✗ 客服侧发图失败: $R"; FAIL=$((FAIL+1)); }
R=$(curl -s -b /tmp/ck7-u1.txt "$B/support/my")
echo "$R" | python3 -c "
import json,sys
ms = json.load(sys.stdin)['messages']
imgs = [m for m in ms if m.get('kind') == 'image']
assert len(imgs) >= 2 and any(m['sender'] == 'staff' for m in imgs), ms
print('  ✓ 用户侧能拉到双向图片消息', len(imgs), '条')" && PASS=$((PASS+1)) || { echo "  ✗ 图片消息拉取异常: $R"; FAIL=$((FAIL+1)); }
echo "--- 31MB 客服图（JPEG 魔数+填充）被拒 ---"
python3 -c "
import base64, json
raw = b'\xff\xd8\xff\xe0' + b'\x00' * (31 * 1024 * 1024)
print(json.dumps({'image': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()}))
" > /tmp/huge7.json
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/support/upload -H "$J" -d @/tmp/huge7.json -w '\n%{http_code}')
ck "31MB 客服图 400" "$(echo "$R" | tail -1)" "400"
echo "--- 16MB 聊天图被拒 ---"
python3 -c "
import base64, json
raw = b'\xff\xd8\xff\xe0' + b'\x00' * (16 * 1024 * 1024)
print(json.dumps({'image': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()}))
" > /tmp/big7.json
R=$(curl -s -b /tmp/ck7-u1.txt -X POST $B/chat/upload -H "$J" -d @/tmp/big7.json -w '\n%{http_code}')
ck "16MB 聊天图 400" "$(echo "$R" | tail -1)" "400"

echo "═══════ 12) 注销用户 ═══════"
curl -s -b /tmp/ck7-u2.txt -X POST $B/support/send -H "$J" -d '{"text":"我要注销前的留言"}' > /dev/null
curl -s -b /tmp/ck7-admin.txt -X POST $B/auth/admin/delete-user -H "$J" -d '{"email":"u2@example.com"}' > /dev/null
R=$(curl -s -b /tmp/ck7-u2.txt $B/auth/me -o /dev/null -w "%{http_code}")
ck "注销后 session 清除" "$R" "401"
R=$(curl -s -b /tmp/ck7-admin.txt "$B/auth/admin/users-search?q=u2@")
ck "注销后搜索不到" "$(echo $R | python3 -c "import json,sys; print(len(json.load(sys.stdin)['users']))")" "0"
R=$(sql "SELECT COUNT(*) FROM support_threads WHERE owner_key='u:u2@example.com'")
ck "注销后客服会话清除" "$R" "[(0,)]"
R=$(curl -s -X POST $B/auth/login -H "$J" -d '{"email":"u2@example.com","password":"userpass2"}' -o /dev/null -w "%{http_code}")
ck "注销后无法登录" "$R" "400"

echo "═══════ 13) 前端 JS 语法 ═══════"
cd ..
for f in index.html auth/admin.html auth/login.html auth/profile.html; do
  python3 - <<PYEOF
import re
html = open('$f').read()
blocks = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
src = '\n;\n'.join(b for b in blocks if b.strip())
open('/tmp/check7.js', 'w').write(src)
PYEOF
  if node --check /tmp/check7.js 2>/dev/null; then echo "  ✓ $f"; PASS=$((PASS+1)); else echo "  ✗ $f JS 语法错误"; FAIL=$((FAIL+1)); fi
done
cd auth

echo "═══════ 14) 无旧管理邮箱硬编码 ═══════"
if grep -rn "saixianziguanliyuan" auth_server.py admin.html ../index.html 2>/dev/null; then
  echo "  ✗ 发现旧管理邮箱硬编码"; FAIL=$((FAIL+1))
else
  echo "  ✓ 无 saixianziguanliyuan 硬编码"; PASS=$((PASS+1))
fi

echo ""
echo "════════════════════════════════"
echo "  结果：✓ $PASS 项通过，✗ $FAIL 项失败"
echo "════════════════════════════════"

kill $UVPID 2>/dev/null
trap - EXIT
rm -f ${DB}* /tmp/ck7-*.txt /tmp/img7.json /tmp/big7.json /tmp/huge7.json /tmp/check7.js
rm -rf $UP $NGX $REPO $ORIGIN /tmp/fb-push-test7
test $FAIL -eq 0 && echo "DONE：全部通过，进程已关闭、测试数据已清理" || { echo "DONE：存在失败项"; exit 1; }
