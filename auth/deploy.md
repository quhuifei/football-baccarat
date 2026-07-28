# 赛析足球登录系统部署步骤

目标服务器：AlmaLinux 9，nginx + systemd，python3.9+，可 pip 装包。
部署目标：未登录访客访问 saixz.com 任意页面 → 302 到 /login.html → 邮箱验证码登录 → 30 天免登录。

## 1. 上传文件

| 本地文件 | 服务器位置 |
|---|---|
| `auth_server.py` | `/var/www/auth/auth_server.py` |
| `login.html` | `/var/www/football/login.html` |
| `football-auth.service` | `/etc/systemd/system/football-auth.service` |
| `nginx-auth-snippet.conf` | 参考用，内容合并进 football.conf（见第 4 步） |

```bash
mkdir -p /var/www/auth
```

## 2. 安装 Python 依赖

```bash
pip3 install fastapi uvicorn httpx
# 确认 uvicorn 可执行文件路径与 service 里 ExecStart 一致：
which uvicorn   # 若不是 /usr/local/bin/uvicorn，修改 football-auth.service
```

## 3. 创建环境变量文件

```bash
cat > /etc/football-auth.env <<'EOF'
MAIL_PROVIDER=resend
RESEND_API_KEY=<真实 Resend API Key，部署时填写>
MAIL_FROM=noreply@saixz.com
MAIL_FROM_NAME=赛析足球
AUTH_DB=/var/www/auth/auth.db
ADMIN_EMAILS=<管理员邮箱，多个用逗号分隔>
EOF
chmod 600 /etc/football-auth.env
```

> 发信通道：`MAIL_PROVIDER=resend`（默认）用 `RESEND_API_KEY`；`MAIL_PROVIDER=brevo` 用 `BREVO_API_KEY`，只需配置所选通道的 Key。
> 所选通道的 Key 未设置时服务进入 dev 模式，验证码只打印到 journal 日志，可先用 dev 模式联调 nginx，再补 Key。
> Resend 侧要求：发件域名 `saixz.com` 已在 Resend 后台验证（DKIM/SPF），否则发信被拒。
> `COOKIE_SECURE` 默认 1（生产 HTTPS 必须）。若临时用 http 测试，可加 `COOKIE_SECURE=0`，上线前务必移除。

## 4. 修改 nginx（football.conf）

上架时把 server 块的 `root` 从 `/var/www/offline` 切回 `/var/www/football`，并加入 `nginx-auth-snippet.conf` 中的三个 location 规则：

- `location /api/auth/` — 反代到 127.0.0.1:8400（不做 auth_request，否则死循环）
- `location = /login.html` — 登录页（不做 auth_request，否则死循环）
- `location /` — 加 `auth_request /api/auth/check;` 和 `error_page 401 =302 https://$host/login.html;`

```bash
nginx -t && systemctl reload nginx
```

## 5. 启动服务

```bash
systemctl daemon-reload
systemctl enable --now football-auth
systemctl status football-auth
journalctl -u football-auth -f   # 看日志
```

## 6. 验证

```bash
# 健康：未登录应 302 到登录页
curl -I https://saixz.com/
# 登录页 200
curl -I https://saixz.com/login.html
# 发验证码（dev 模式下 journalctl 里能看到 6 位验证码）
curl -X POST https://saixz.com/api/auth/send-code \
  -H 'Content-Type: application/json' -d '{"email":"you@example.com"}'
# 验证登录（拿到 fb_token Cookie）
curl -c /tmp/ck.txt -X POST https://saixz.com/api/auth/verify \
  -H 'Content-Type: application/json' -d '{"email":"you@example.com","code":"123456"}'
# 带 Cookie 访问首页应 200
curl -b /tmp/ck.txt -I https://saixz.com/
```

## 故障排查

- `systemctl status football-auth` / `journalctl -u football-auth -e`：后端是否起来、依赖是否装全
- `curl -v http://127.0.0.1:8400/api/auth/check`：本机直连后端应 401
- 邮件发不出：确认 `MAIL_PROVIDER` 与对应 Key（`RESEND_API_KEY` 或 `BREVO_API_KEY`）配置正确，且发件域名 `saixz.com` 已在所选平台验证（SPF/DKIM）
- 无限跳转登录页：确认 `/login.html` 和 `/api/auth/` 两个 location **没有**加 auth_request
