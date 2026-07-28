# -*- coding: utf-8 -*-
"""
赛析足球（saixz.com）邮箱验证码登录服务
FastAPI + SQLite，单文件实现
- 127.0.0.1:8400，由 nginx 反代 /api/auth/ 前缀
- GET /api/auth/check 供 nginx auth_request 做登录墙
环境变量：
  AUTH_DB         SQLite 路径（默认 ./auth.db）
  MAIL_PROVIDER   发信通道 resend(默认) / brevo
  RESEND_API_KEY  Resend API Key（MAIL_PROVIDER=resend 时使用）
  BREVO_API_KEY   Brevo 交易邮件 API Key（MAIL_PROVIDER=brevo 时使用）
                  对应通道的 Key 未设置则 dev 模式，验证码打日志
  MAIL_FROM       发件邮箱（默认 noreply@saixz.com）
  MAIL_FROM_NAME  发件人名（默认 赛析足球）
  ADMIN_EMAILS    逗号分隔的管理员邮箱（/api/auth/me 返回 is_admin）
  COOKIE_SECURE   "1"(默认) 时 Cookie 带 Secure；本机 http 测试可设 "0"
"""
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from html import escape

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------- 配置 ----------------
DB_PATH = os.environ.get("AUTH_DB", "./auth.db")
MAIL_PROVIDER = os.environ.get("MAIL_PROVIDER", "resend").strip().lower()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@saixz.com").strip()
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "赛析足球").strip()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"

CODE_TTL = 600            # 验证码有效期 10 分钟
CODE_LEN = 6
MAX_ATTEMPTS = 5          # 验证码最多尝试次数
SESSION_TTL = 30 * 86400  # session 30 天
RESEND_INTERVAL = 60      # 同邮箱 60 秒只能发 1 次
EMAIL_DAILY_LIMIT = 5     # 同邮箱每天最多 5 次
IP_DAILY_LIMIT = 10       # 同 IP 每天最多 10 次
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("auth")

app = FastAPI(title="saixz auth", docs_url=None, redoc_url=None, openapi_url=None)

# ---------------- 数据库 ----------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              email      TEXT PRIMARY KEY,
              created_at INTEGER NOT NULL,
              last_login INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codes(
              email      TEXT NOT NULL,
              code_hash  TEXT NOT NULL,
              expires_at INTEGER NOT NULL,
              attempts   INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              PRIMARY KEY(email)
            );
            CREATE TABLE IF NOT EXISTS sessions(
              token_hash TEXT PRIMARY KEY,
              email      TEXT NOT NULL,
              expires_at INTEGER NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);
            """
        )
        conn.commit()


# ---------------- 工具 ----------------
def _hash_code(email: str, code: str) -> str:
    """验证码哈希：sha256(固定盐 + 邮箱 + 验证码)。固定盐在服务端环境内，DB 泄露也无法直接爆破。"""
    salt = os.environ.get("CODE_HASH_SALT", "saixz-code-salt-v1")
    return hashlib.sha256(f"{salt}|{email}|{code}".encode()).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _day_start(ts: float) -> int:
    return int(ts // 86400) * 86400


def _count_send_today(where: str, val: str) -> int:
    """统计今日发送次数。发送记录在 send_log 表（懒建）。"""
    with closing(_conn()) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS send_log("
            "email TEXT, ip TEXT, ts INTEGER NOT NULL, day INTEGER NOT NULL)"
        )
        row = conn.execute(
            f"SELECT COUNT(*) FROM send_log WHERE {where}=? AND day=?",
            (val, _day_start(time.time())),
        ).fetchone()
        return row[0]


def _record_send(email: str, ip: str) -> None:
    now = int(time.time())
    with closing(_conn()) as conn:
        conn.execute(
            "INSERT INTO send_log(email, ip, ts, day) VALUES(?,?,?,?)",
            (email, ip, now, _day_start(now)),
        )
        conn.execute("DELETE FROM send_log WHERE day < ?", (_day_start(now) - 86400,))
        conn.commit()


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _session_from_cookie(request: Request):
    token = request.cookies.get("fb_token", "")
    if not token:
        return None
    now = int(time.time())
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT email, expires_at FROM sessions WHERE token_hash=?",
            (_hash_token(token),),
        ).fetchone()
        if row and row[1] > now:
            return row[0]
        if row:  # 过期顺手清理
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
            conn.commit()
    return None


# ---------------- 请求模型 ----------------
class SendCodeReq(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) > 254 or not EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v


class VerifyReq(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v

    @field_validator("code")
    @classmethod
    def _valid_code(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"\d{6}", v):
            raise ValueError("验证码为 6 位数字")
        return v


# ---------------- 邮件 ----------------
def _mail_html(code: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#071510;font-family:'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#0e1f1a;border:1px solid rgba(212,175,55,.25);border-radius:14px;padding:36px 32px;">
    <div style="color:#d4af37;font-size:20px;font-weight:700;letter-spacing:2px;">赛析足球</div>
    <div style="color:#8fa39b;font-size:12px;margin-top:4px;">saixz.com · 登录验证码</div>
    <div style="margin:28px 0 8px;color:#e9e7de;font-size:15px;">您好，您的登录验证码是：</div>
    <div style="font-size:40px;font-weight:700;letter-spacing:12px;color:#f0d57a;background:rgba(212,175,55,.08);border:1px dashed rgba(212,175,55,.4);border-radius:10px;padding:16px 0;text-align:center;">{escape(code)}</div>
    <div style="margin-top:20px;color:#8fa39b;font-size:13px;line-height:1.8;">
      · 验证码 <b style="color:#e9e7de;">10 分钟</b> 内有效，请勿泄露给他人。<br>
      · 若非本人操作，请忽略本邮件，您的账号不受影响。
    </div>
    <div style="margin-top:28px;padding-top:16px;border-top:1px solid rgba(212,175,55,.15);color:#5c6f68;font-size:11px;">
      本邮件由系统自动发送，请勿直接回复。数据仅供参考，不构成投资建议。
    </div>
  </div>
</body></html>"""


async def send_mail(email: str, code: str) -> bool:
    """返回 True 表示发送成功（或 dev 模式）。失败返回 False。
    通道由 MAIL_PROVIDER 决定：resend(默认) / brevo；对应 Key 未配置则 dev 模式打日志。"""
    subject = "【赛析足球】登录验证码"
    if MAIL_PROVIDER == "brevo":
        if not BREVO_API_KEY:
            log.info("[DEV brevo 无 Key] 验证码 email=%s code=%s", email, code)
            return True
        payload = {
            "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM},
            "to": [{"email": email}],
            "subject": subject,
            "htmlContent": _mail_html(code),
        }
        headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
        url, provider = BREVO_URL, "Brevo"
    else:  # resend（默认）
        if not RESEND_API_KEY:
            log.info("[DEV resend 无 Key] 验证码 email=%s code=%s", email, code)
            return True
        payload = {
            "from": f"{MAIL_FROM_NAME} <{MAIL_FROM}>",
            "to": [email],
            "subject": subject,
            "html": _mail_html(code),
        }
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        url, provider = RESEND_URL, "Resend"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers, json=payload)
        if provider == "Resend":
            ok = r.status_code == 200 and bool(r.json().get("id")) if r.headers.get("content-type", "").startswith("application/json") else False
        else:
            ok = 200 <= r.status_code < 300
        if ok:
            log.info("%s 邮件发送成功 email=%s", provider, email)
            return True
        log.error("%s 发送失败 status=%s body=%s", provider, r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("%s 请求异常 email=%s err=%s", provider, email, e)
        return False


# ---------------- API ----------------
@app.on_event("startup")
def _startup() -> None:
    init_db()
    if MAIL_PROVIDER == "brevo":
        dev_mode = not BREVO_API_KEY
    else:
        dev_mode = not RESEND_API_KEY
    log.info("auth server 启动 db=%s provider=%s dev_mode=%s", DB_PATH, MAIL_PROVIDER, dev_mode)


@app.post("/api/auth/send-code")
async def send_code(req: SendCodeReq, request: Request) -> JSONResponse:
    email, ip = req.email, _client_ip(request)
    now = int(time.time())

    with closing(_conn()) as conn:
        row = conn.execute("SELECT created_at FROM codes WHERE email=?", (email,)).fetchone()
    if row and now - row[0] < RESEND_INTERVAL:
        return JSONResponse({"ok": False, "error": "发送太频繁，请 60 秒后再试"}, status_code=429)

    if _count_send_today("email", email) >= EMAIL_DAILY_LIMIT:
        return JSONResponse({"ok": False, "error": "该邮箱今日发送次数已达上限"}, status_code=429)
    if _count_send_today("ip", ip) >= IP_DAILY_LIMIT:
        return JSONResponse({"ok": False, "error": "请求过于频繁，请明天再试"}, status_code=429)

    code = "".join(secrets.choice("0123456789") for _ in range(CODE_LEN))
    if not await send_mail(email, code):
        return JSONResponse({"ok": False, "error": "邮件发送失败，请稍后重试"}, status_code=500)

    with closing(_conn()) as conn:
        conn.execute(
            "REPLACE INTO codes(email, code_hash, expires_at, attempts, created_at)"
            " VALUES(?,?,?,?,?)",
            (email, _hash_code(email, code), now + CODE_TTL, 0, now),
        )
        conn.commit()
    _record_send(email, ip)
    log.info("验证码已签发 email=%s ip=%s", email, ip)
    # 无论邮箱是否已注册都返回 ok，防枚举
    return JSONResponse({"ok": True})


@app.post("/api/auth/verify")
async def verify(req: VerifyReq, request: Request) -> JSONResponse:
    email, code = req.email, req.code
    now = int(time.time())

    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT code_hash, expires_at, attempts FROM codes WHERE email=?", (email,)
        ).fetchone()

    if not row:
        return JSONResponse({"ok": False, "error": "请先获取验证码"}, status_code=400)

    code_hash, expires_at, attempts = row
    if expires_at <= now:
        with closing(_conn()) as conn:
            conn.execute("DELETE FROM codes WHERE email=?", (email,))
            conn.commit()
        return JSONResponse({"ok": False, "error": "验证码已过期，请重新获取"}, status_code=400)

    if attempts >= MAX_ATTEMPTS:
        with closing(_conn()) as conn:
            conn.execute("DELETE FROM codes WHERE email=?", (email,))
            conn.commit()
        return JSONResponse({"ok": False, "error": "错误次数过多，验证码已作废"}, status_code=400)

    if _hash_code(email, code) != code_hash:
        attempts += 1
        with closing(_conn()) as conn:
            if attempts >= MAX_ATTEMPTS:
                conn.execute("DELETE FROM codes WHERE email=?", (email,))
            else:
                conn.execute("UPDATE codes SET attempts=? WHERE email=?", (attempts, email))
            conn.commit()
        log.warning("验证码错误 email=%s attempts=%d", email, attempts)
        remaining = MAX_ATTEMPTS - attempts
        msg = "验证码错误" + (f"，还可尝试 {remaining} 次" if remaining > 0 else "，验证码已作废")
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    # 验证成功：删验证码、建用户/更新登录时间、建 session
    token = secrets.token_urlsafe(32)
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM codes WHERE email=?", (email,))
        conn.execute(
            "INSERT INTO users(email, created_at, last_login) VALUES(?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET last_login=excluded.last_login",
            (email, now, now),
        )
        conn.execute(
            "INSERT INTO sessions(token_hash, email, expires_at, created_at) VALUES(?,?,?,?)",
            (_hash_token(token), email, now + SESSION_TTL, now),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()

    log.info("登录成功 email=%s", email)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(
        key="fb_token", value=token, max_age=SESSION_TTL, path="/",
        httponly=True, secure=COOKIE_SECURE, samesite="lax",
    )
    return resp


@app.get("/api/auth/check")
async def check(request: Request) -> Response:
    """nginx auth_request 专用：有效 200，无效 401。"""
    if _session_from_cookie(request):
        return Response(status_code=200)
    return Response(status_code=401)


@app.get("/api/auth/me")
async def me(request: Request) -> JSONResponse:
    email = _session_from_cookie(request)
    if not email:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    return JSONResponse({"ok": True, "email": email, "is_admin": email in ADMIN_EMAILS})


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get("fb_token", "")
    if token:
        with closing(_conn()) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
            conn.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="fb_token", path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")
    return resp
