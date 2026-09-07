# -*- coding: utf-8 -*-
"""
赛先知足球（saixz.com）邮箱注册 + 账号密码登录服务
FastAPI + SQLite，单文件实现
- 127.0.0.1:8400，由 nginx 反代 /api/auth/ 前缀
- GET /api/auth/check 供 nginx auth_request 做登录墙
端点：
  POST /api/auth/send-code         {email, purpose}  purpose∈{register, reset}
  POST /api/auth/register          {email, code, password, phone}
  POST /api/auth/login             {email, password, remember}
  POST /api/auth/reset-password    {email, code, password}
  GET  /api/auth/me                当前登录信息（昵称/头像/手机号打码）
  GET  /api/auth/check             nginx auth_request 专用
  POST /api/auth/logout
  GET  /api/auth/profile           个人中心资料
  POST /api/auth/profile/nickname  {nickname}
  POST /api/auth/profile/avatar    {avatar}  data:image/jpeg;base64，≤200KB
  POST /api/auth/profile/password  {old_password, new_password}
  GET  /api/chat/messages?after_id=N   聊天室拉取（公开）
  POST /api/chat/send                  发言（需登录，2 秒 1 条 / 每分钟 20 条）
  GET  /api/support/my?after_id=N      客服会话（用户侧，游客自动签 visitor_id cookie）
  POST /api/support/send               用户发求助消息（每分钟 5 条，首条触发通知邮件）
  GET  /api/support/threads            客服会话列表（管理员/staff）
  GET  /api/support/thread/{tid}/messages  会话消息（管理员/staff，标记已读）
  POST /api/support/thread/{tid}/reply     客服回复（管理员/staff）
  POST /api/support/thread/{tid}/upload    客服发图（管理员/staff，≤30MB）
  POST /api/support/upload               用户/游客客服发图（≤30MB）
  GET  /api/announcement                 当前公告（公开，前端顶部横幅）
  GET  /api/auth/admin/users           用户列表 JSON（仅管理员）
  GET  /api/auth/admin/users-search?q= 用户模糊搜索（email/phone/nickname）
  POST /api/auth/admin/set-role        设置 role=user/staff（仅管理员）
  POST /api/auth/admin/reset-user-password  重置用户密码为随机 10 位（仅展示一次）
  POST /api/auth/admin/disable         {email, disabled} 封号/解封（封号清 session）
  POST /api/auth/admin/delete-user     {email} 注销用户（保留聊天室消息）
  POST /api/auth/admin/ban             {email, banned} 禁言/解禁（仅聊天室）
  GET  /api/auth/admin/chat?page=&size= 聊天室消息倒序分页（仅管理员）
  POST /api/auth/admin/chat/delete     {id} 删聊天消息（图片连文件删）
  GET  /api/auth/admin/words           敏感词列表
  POST /api/auth/admin/words/add       {word} 新增敏感词（立即生效）
  POST /api/auth/admin/words/delete    {word} 删除敏感词
  GET  /api/auth/admin/stats           用户/聊天/客服统计 + nginx 访问量
  GET  /api/auth/admin/data-status     足球数据仓库 mtime + 最新 commit
  POST /api/auth/admin/data-update     git fetch && reset --hard origin/main
  GET  /api/auth/admin/announcements   公告历史列表
  POST /api/auth/admin/announcements/add        {text} 发布公告（其余下架）
  POST /api/auth/admin/announcements/deactivate {id} 下架公告
  GET  /api/auth/admin/users.csv       管理员导出用户 CSV（ADMIN_EMAILS）
环境变量：
  AUTH_DB         SQLite 路径（默认 ./auth.db）
  MAIL_PROVIDER   发信通道 resend(默认) / brevo
  RESEND_API_KEY  Resend API Key（MAIL_PROVIDER=resend 时使用）
  BREVO_API_KEY   Brevo 交易邮件 API Key（MAIL_PROVIDER=brevo 时使用）
                  对应通道的 Key 未设置则 dev 模式，验证码打日志
  MAIL_FROM       发件邮箱（默认 noreply@saixz.com）
  MAIL_FROM_NAME  发件人名（默认 赛先知足球）
  ADMIN_EMAILS    逗号分隔的管理员邮箱（/api/auth/me 返回 is_admin）
  COOKIE_SECURE   "1"(默认) 时 Cookie 带 Secure；本机 http 测试可设 "0"
  AUTH_UPLOAD_DIR 聊天图片存储目录（默认 ./uploads，服务器上为 /var/www/auth/uploads）
  FOOTBALL_REPO_DIR 足球数据 git 仓库（默认 /var/www/football，数据统计/一键更新用）
  NGINX_LOG_DIR    nginx 日志目录（默认 /var/log/nginx，访问量统计读 access.log*）
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import time
from starlette.concurrency import run_in_threadpool
from contextlib import closing
from html import escape

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from banned_words import BANNED_WORDS as _SEED_WORDS

# ---------------- 配置 ----------------
DB_PATH = os.environ.get("AUTH_DB", "./auth.db")
UPLOAD_DIR = os.environ.get("AUTH_UPLOAD_DIR", "./uploads")
FOOTBALL_REPO_DIR = os.environ.get("FOOTBALL_REPO_DIR", "/var/www/football")
NGINX_LOG_DIR = os.environ.get("NGINX_LOG_DIR", "/var/log/nginx")
MAIL_PROVIDER = os.environ.get("MAIL_PROVIDER", "resend").strip().lower()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@saixz.com").strip()
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "赛先知足球").strip()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"

CODE_TTL = 600            # 验证码有效期 10 分钟
CODE_LEN = 6
MAX_ATTEMPTS = 5          # 验证码最多尝试次数
SESSION_TTL = 30 * 86400  # session 30 天
RESEND_INTERVAL = 60      # 同邮箱 60 秒只能发 1 次
EMAIL_DAILY_LIMIT = 5     # 同邮箱每天最多 5 次
IP_DAILY_LIMIT = 10       # 同 IP 每天最多 10 次发码
LOGIN_MAX_FAILS = 5       # 同邮箱连续失败 5 次锁定
LOGIN_LOCK_SECONDS = 600  # 锁定 10 分钟
IP_LOGIN_PER_MIN = 10     # 同 IP 每分钟最多 10 次登录尝试
PWD_ITERATIONS = 100_000  # pbkdf2 迭代次数
CHAT_MSG_TTL = 7 * 86400  # 聊天/客服消息保留 7 天
CHAT_IMG_MAX = 15 * 1024 * 1024     # 聊天室图片最大 15MB
SUPPORT_IMG_MAX = 30 * 1024 * 1024  # 客服图片最大 30MB
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_URL = "https://api.resend.com/emails"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^1\d{10}$")
PURPOSES = {"register", "reset"}

MAIL_SUBJECTS = {
    "register": "【赛先知足球】注册验证码",
    "reset": "【赛先知足球】重置密码验证码",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("auth")

app = FastAPI(title="saixz auth", docs_url=None, redoc_url=None, openapi_url=None)

# ---------------- 数据库 ----------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict) -> None:
    """老库自动迁移：缺列则 ALTER TABLE 补上。"""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in cols.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            log.info("迁移: %s 表新增列 %s", table, name)


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
            CREATE TABLE IF NOT EXISTS send_log(
              email TEXT, ip TEXT, ts INTEGER NOT NULL, day INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_guard(
              email        TEXT PRIMARY KEY,
              fails        INTEGER NOT NULL DEFAULT 0,
              locked_until INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ip_log(
              ip TEXT NOT NULL, ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ip_log ON ip_log(ip, ts);
            CREATE TABLE IF NOT EXISTS chat_messages(
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              email      TEXT NOT NULL,
              nickname   TEXT NOT NULL DEFAULT '',
              avatar     TEXT NOT NULL DEFAULT '',
              text       TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_log(
              ip TEXT NOT NULL, ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_support_log ON support_log(ip, ts);
            CREATE TABLE IF NOT EXISTS support_threads(
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_key   TEXT NOT NULL UNIQUE,
              owner_label TEXT NOT NULL DEFAULT '',
              created_at  INTEGER NOT NULL,
              last_at     INTEGER NOT NULL,
              staff_read  INTEGER NOT NULL DEFAULT 0,
              user_read   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS support_msgs(
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              thread_id   INTEGER NOT NULL,
              sender      TEXT NOT NULL,
              sender_name TEXT NOT NULL DEFAULT '',
              text        TEXT NOT NULL,
              created_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_support_msgs ON support_msgs(thread_id, id);
            CREATE TABLE IF NOT EXISTS banned_words(
              word TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS announcements(
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              text       TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              active     INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        _ensure_columns(conn, "users", {
            "password_hash": "TEXT",
            "phone": "TEXT",
            "updated_at": "INTEGER",
            "nickname": "TEXT",
            "avatar": "TEXT",
            "role": "TEXT NOT NULL DEFAULT 'user'",
            "public_id": "TEXT",
            "banned": "INTEGER NOT NULL DEFAULT 0",
            "disabled": "INTEGER NOT NULL DEFAULT 0",
        })
        _ensure_columns(conn, "codes", {
            "purpose": "TEXT NOT NULL DEFAULT ''",
        })
        _ensure_columns(conn, "chat_messages", {
            "kind": "TEXT NOT NULL DEFAULT 'text'",
        })
        _ensure_columns(conn, "support_msgs", {
            "kind": "TEXT NOT NULL DEFAULT 'text'",
        })
        # 敏感词表种子：表为空则从 banned_words.py 导入初始词表
        if conn.execute("SELECT COUNT(*) FROM banned_words").fetchone()[0] == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO banned_words(word) VALUES(?)",
                [(w,) for w in sorted(_SEED_WORDS)],
            )
            log.info("敏感词表初始化: 导入 %d 个种子词", len(_SEED_WORDS))
        # 迁移 1：老用户昵称若还是「邮箱前缀」自动默认值，置空（对外只显示 public_id 或用户自改昵称）
        conn.execute(
            "UPDATE users SET nickname=NULL"
            " WHERE nickname IS NOT NULL AND nickname = substr(email, 1, instr(email, '@') - 1)"
        )
        # 迁移 2：老用户补随机 public_id
        rows = conn.execute(
            "SELECT email FROM users WHERE public_id IS NULL OR public_id=''"
        ).fetchall()
        for (em,) in rows:
            conn.execute("UPDATE users SET public_id=? WHERE email=?", (_gen_public_id(conn), em))
        conn.commit()


_PID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉易混淆的 0/o/1/l/i


def _gen_public_id(conn: sqlite3.Connection) -> str:
    """8 位字母数字随机公开 ID（如 8f3k2x），带唯一性检查。"""
    for _ in range(20):
        pid = "".join(secrets.choice(_PID_ALPHABET) for _ in range(8))
        if not conn.execute("SELECT 1 FROM users WHERE public_id=?", (pid,)).fetchone():
            return pid
    return secrets.token_hex(8)  # 理论上到不了这里


# ---------------- 敏感词（DB 存储 + 内存缓存） ----------------
_WORDS_CACHE: list = []


def _refresh_words() -> None:
    global _WORDS_CACHE
    with closing(_conn()) as conn:
        _WORDS_CACHE = [r[0] for r in conn.execute("SELECT word FROM banned_words")]


def _filter_sensitive(text: str) -> str:
    """命中敏感词替换为等长 *。词表存 DB（后台可增删），内存缓存加速。"""
    if not text:
        return text
    if not _WORDS_CACHE:
        _refresh_words()
    hit = [w for w in _WORDS_CACHE if w.strip() and w in text]
    for w in hit:
        text = text.replace(w, "*" * len(w))
    return text


# ---------------- 工具 ----------------
def _user_flag(email: str, col: str) -> bool:
    with closing(_conn()) as conn:
        r = conn.execute(f"SELECT {col} FROM users WHERE email=?", (email,)).fetchone()
    return bool(r and r[0])


def _user_banned(email: str) -> bool:
    return _user_flag(email, "banned")


def _user_disabled(email: str) -> bool:
    return _user_flag(email, "disabled")


def _hash_code(email: str, code: str) -> str:
    """验证码哈希：sha256(固定盐 + 邮箱 + 验证码)。"""
    salt = os.environ.get("CODE_HASH_SALT", "saixz-code-salt-v1")
    return hashlib.sha256(f"{salt}|{email}|{code}".encode()).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_password(password: str) -> str:
    """pbkdf2_hmac(sha256, 密码, 随机盐, 100000)，存储格式 salt$hash（hex）。"""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PWD_ITERATIONS).hex()
    return f"{salt}${h}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PWD_ITERATIONS).hex()
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def _valid_password(p: str) -> bool:
    return len(p) >= 8 and bool(re.search(r"[A-Za-z]", p)) and bool(re.search(r"\d", p))


def _mask_phone(phone: str) -> str:
    """138****1234"""
    if phone and len(phone) == 11:
        return phone[:3] + "****" + phone[-4:]
    return ""


def _valid_nickname(n: str) -> bool:
    """2~20 字符，允许中英文数字和常用符号，不允许 <>。"""
    if not (2 <= len(n) <= 20):
        return False
    if "<" in n or ">" in n:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9一-鿿 _\-·.!！?？~@#$%&*()（）+]+", n))


def _day_start(ts: float) -> int:
    return int(ts // 86400) * 86400


def _count_send_today(where: str, val: str) -> int:
    with closing(_conn()) as conn:
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


def _user_row(email: str):
    """返回 (email, password_hash, phone, created_at, nickname, avatar, last_login, role, public_id)"""
    with closing(_conn()) as conn:
        return conn.execute(
            "SELECT email, password_hash, phone, created_at, nickname, avatar, last_login, role, public_id"
            " FROM users WHERE email=?",
            (email,),
        ).fetchone()


def _display_name(email: str, row) -> str:
    """对外显示名：用户自改昵称 > 「用户 public_id」。绝不回退到邮箱。"""
    if row:
        if row[4]:
            return row[4]
        if len(row) > 8 and row[8]:
            return f"用户 {row[8]}"
    return "用户"


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


def _create_session(email: str, remember: bool = True) -> JSONResponse:
    """建 session 并返回带 cookie 的响应。remember=False 时为会话 cookie（不设 Max-Age）。"""
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    with closing(_conn()) as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, email, expires_at, created_at) VALUES(?,?,?,?)",
            (_hash_token(token), email, now + SESSION_TTL, now),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(
        key="fb_token", value=token,
        max_age=SESSION_TTL if remember else None,
        path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax",
    )
    return resp


def _verify_code(email: str, code: str, purpose: str):
    """校验验证码。成功返回 None（并消费验证码），失败返回 JSONResponse。"""
    now = int(time.time())
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT code_hash, expires_at, attempts, purpose FROM codes WHERE email=?",
            (email,),
        ).fetchone()

    if not row:
        return JSONResponse({"ok": False, "error": "请先获取验证码"}, status_code=400)

    code_hash, expires_at, attempts, code_purpose = row
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

    if code_purpose != purpose or _hash_code(email, code) != code_hash:
        attempts += 1
        with closing(_conn()) as conn:
            if attempts >= MAX_ATTEMPTS:
                conn.execute("DELETE FROM codes WHERE email=?", (email,))
            else:
                conn.execute("UPDATE codes SET attempts=? WHERE email=?", (attempts, email))
            conn.commit()
        log.warning("验证码错误 email=%s purpose=%s attempts=%d", email, purpose, attempts)
        remaining = MAX_ATTEMPTS - attempts
        msg = "验证码错误" + (f"，还可尝试 {remaining} 次" if remaining > 0 else "，验证码已作废")
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    with closing(_conn()) as conn:
        conn.execute("DELETE FROM codes WHERE email=?", (email,))
        conn.commit()
    return None


# ---------------- 请求模型 ----------------
def _email_validator(v: str) -> str:
    v = v.strip().lower()
    if len(v) > 254 or not EMAIL_RE.match(v):
        raise ValueError("邮箱格式不正确")
    return v


def _code_validator(v: str) -> str:
    v = v.strip()
    if not re.fullmatch(r"\d{6}", v):
        raise ValueError("验证码为 6 位数字")
    return v


def _password_validator(v: str) -> str:
    if not _valid_password(v):
        raise ValueError("密码至少 8 位，且需同时包含字母和数字")
    return v


def _phone_validator(v: str) -> str:
    v = v.strip()
    if not PHONE_RE.match(v):
        raise ValueError("请输入正确的 11 位手机号")
    return v


class SendCodeReq(BaseModel):
    email: str
    purpose: str = "register"

    @field_validator("email")
    @classmethod
    def _v_email(cls, v): return _email_validator(v)

    @field_validator("purpose")
    @classmethod
    def _v_purpose(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in PURPOSES:
            raise ValueError("purpose 必须为 register 或 reset")
        return v


class RegisterReq(BaseModel):
    email: str
    code: str
    password: str
    phone: str

    @field_validator("email")
    @classmethod
    def _v_email(cls, v): return _email_validator(v)

    @field_validator("code")
    @classmethod
    def _v_code(cls, v): return _code_validator(v)

    @field_validator("password")
    @classmethod
    def _v_pwd(cls, v): return _password_validator(v)

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v): return _phone_validator(v)


class LoginReq(BaseModel):
    email: str
    password: str
    remember: bool = True

    @field_validator("email")
    @classmethod
    def _v_email(cls, v):
        # 登录支持两种：完整邮箱 或 纯用户名（内部映射到 @saixz.local，如管理员账号）
        v = (v or "").strip().lower()
        if "@" in v:
            return _email_validator(v)
        if re.fullmatch(r"[a-z0-9_]{3,32}", v):
            return v
        raise ValueError("请输入正确的邮箱或用户名")

    @field_validator("password")
    @classmethod
    def _v_pwd(cls, v: str) -> str:
        if not v:
            raise ValueError("请输入密码")
        return v


class ResetReq(BaseModel):
    email: str
    code: str
    password: str

    @field_validator("email")
    @classmethod
    def _v_email(cls, v): return _email_validator(v)

    @field_validator("code")
    @classmethod
    def _v_code(cls, v): return _code_validator(v)

    @field_validator("password")
    @classmethod
    def _v_pwd(cls, v): return _password_validator(v)


# ---------------- 邮件 ----------------
def _mail_html(code: str, purpose: str) -> str:
    action = "注册" if purpose == "register" else "重置密码"
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#071510;font-family:'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#0e1f1a;border:1px solid rgba(212,175,55,.25);border-radius:14px;padding:36px 32px;">
    <div style="color:#d4af37;font-size:20px;font-weight:700;letter-spacing:2px;">赛先知足球</div>
    <div style="color:#8fa39b;font-size:12px;margin-top:4px;">saixz.com · {action}验证码</div>
    <div style="margin:28px 0 8px;color:#e9e7de;font-size:15px;">您好，您的{action}验证码是：</div>
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


async def _deliver_mail(to_email: str, subject: str, html: str) -> bool:
    """通用发信。通道由 MAIL_PROVIDER 决定：resend(默认) / brevo；
    对应 Key 未配置则 dev 模式打日志并返回 True。"""
    if MAIL_PROVIDER == "brevo":
        if not BREVO_API_KEY:
            log.info("[DEV brevo 无 Key] 邮件 to=%s subject=%s html=%s", to_email, subject, html[:2000])
            return True
        payload = {
            "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html,
        }
        headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
        url, provider = BREVO_URL, "Brevo"
    else:  # resend（默认）
        if not RESEND_API_KEY:
            log.info("[DEV resend 无 Key] 邮件 to=%s subject=%s html=%s", to_email, subject, html[:2000])
            return True
        payload = {
            "from": f"{MAIL_FROM_NAME} <{MAIL_FROM}>",
            "to": [to_email],
            "subject": subject,
            "html": html,
        }
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        url, provider = RESEND_URL, "Resend"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers, json=payload)
        if provider == "Resend":
            ok = (r.status_code == 200 and bool(r.json().get("id"))) \
                if r.headers.get("content-type", "").startswith("application/json") else False
        else:
            ok = 200 <= r.status_code < 300
        if ok:
            log.info("%s 邮件发送成功 to=%s subject=%s", provider, to_email, subject)
            return True
        log.error("%s 发送失败 status=%s body=%s", provider, r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("%s 请求异常 to=%s err=%s", provider, to_email, e)
        return False


async def send_mail(email: str, code: str, purpose: str) -> bool:
    """验证码邮件。返回 True 表示发送成功（或 dev 模式）。"""
    if MAIL_PROVIDER == "brevo" and not BREVO_API_KEY:
        log.info("[DEV brevo 无 Key] 验证码 purpose=%s email=%s code=%s", purpose, email, code)
        return True
    if MAIL_PROVIDER != "brevo" and not RESEND_API_KEY:
        log.info("[DEV resend 无 Key] 验证码 purpose=%s email=%s code=%s", purpose, email, code)
        return True
    return await _deliver_mail(email, MAIL_SUBJECTS[purpose], _mail_html(code, purpose))


# ---------------- 密码防暴力（登录 / 改密码共用） ----------------
def _lock_check(email: str):
    """账号锁定期内返回 423 JSONResponse，否则返回 None。"""
    now = int(time.time())
    with closing(_conn()) as conn:
        guard = conn.execute(
            "SELECT locked_until FROM login_guard WHERE email=?", (email,)
        ).fetchone()
    if guard and guard[0] > now:
        remain = (guard[0] - now + 59) // 60
        return JSONResponse(
            {"ok": False, "error": f"连续失败次数过多，账号已锁定，请 {remain} 分钟后再试"},
            status_code=423,
        )
    return None


def _record_pwd_fail(email: str):
    """记录一次密码失败，返回 (fails, locked_until)；达到上限即锁定并清零计数。"""
    now = int(time.time())
    with closing(_conn()) as conn:
        guard = conn.execute(
            "SELECT fails FROM login_guard WHERE email=?", (email,)
        ).fetchone()
        fails = (guard[0] if guard else 0) + 1
        locked_until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_MAX_FAILS else 0
        conn.execute(
            "INSERT INTO login_guard(email, fails, locked_until) VALUES(?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET fails=excluded.fails,"
            " locked_until=excluded.locked_until",
            (email, 0 if locked_until else fails, locked_until),
        )
        conn.commit()
    return fails, locked_until


# ---------------- API ----------------
@app.on_event("startup")
def _startup() -> None:
    init_db()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    _cleanup_chat()
    if MAIL_PROVIDER == "brevo":
        dev_mode = not BREVO_API_KEY
    else:
        dev_mode = not RESEND_API_KEY
    log.info("auth server 启动 db=%s provider=%s dev_mode=%s uploads=%s",
             DB_PATH, MAIL_PROVIDER, dev_mode, UPLOAD_DIR)


@app.post("/api/auth/send-code")
async def send_code(req: SendCodeReq, request: Request) -> JSONResponse:
    email, ip, purpose = req.email, _client_ip(request), req.purpose
    now = int(time.time())

    # 注册：已注册邮箱明确提示去登录
    if purpose == "register" and _user_row(email):
        return JSONResponse(
            {"ok": False, "error": "该邮箱已注册，请直接登录", "already": True},
            status_code=409,
        )

    # 重置密码：未注册也返回 ok（防枚举），但不发码只记日志
    if purpose == "reset" and not _user_row(email):
        log.info("reset 发码请求：邮箱未注册，不发码 email=%s ip=%s", email, ip)
        return JSONResponse({"ok": True})

    with closing(_conn()) as conn:
        row = conn.execute("SELECT created_at FROM codes WHERE email=?", (email,)).fetchone()
    if row and now - row[0] < RESEND_INTERVAL:
        return JSONResponse({"ok": False, "error": "发送太频繁，请 60 秒后再试"}, status_code=429)

    if _count_send_today("email", email) >= EMAIL_DAILY_LIMIT:
        return JSONResponse({"ok": False, "error": "该邮箱今日发送次数已达上限"}, status_code=429)
    if _count_send_today("ip", ip) >= IP_DAILY_LIMIT:
        return JSONResponse({"ok": False, "error": "请求过于频繁，请明天再试"}, status_code=429)

    code = "".join(secrets.choice("0123456789") for _ in range(CODE_LEN))
    if not await send_mail(email, code, purpose):
        return JSONResponse({"ok": False, "error": "邮件发送失败，请稍后重试"}, status_code=500)

    with closing(_conn()) as conn:
        conn.execute(
            "REPLACE INTO codes(email, code_hash, expires_at, attempts, created_at, purpose)"
            " VALUES(?,?,?,?,?,?)",
            (email, _hash_code(email, code), now + CODE_TTL, 0, now, purpose),
        )
        conn.commit()
    _record_send(email, ip)
    log.info("验证码已签发 purpose=%s email=%s ip=%s", purpose, email, ip)
    return JSONResponse({"ok": True})


@app.post("/api/auth/register")
async def register(req: RegisterReq) -> JSONResponse:
    email = req.email
    if _user_row(email):
        return JSONResponse(
            {"ok": False, "error": "该邮箱已注册，请直接登录", "already": True},
            status_code=409,
        )

    err = _verify_code(email, req.code, "register")
    if err:
        return err

    now = int(time.time())
    with closing(_conn()) as conn:
        conn.execute(
            "INSERT INTO users(email, password_hash, phone, created_at, last_login, updated_at,"
            " nickname, public_id) VALUES(?,?,?,?,?,?,?,?)",
            (email, _hash_password(req.password), req.phone, now, now, now,
             None, _gen_public_id(conn)),
        )
        conn.commit()
    log.info("注册成功 email=%s", email)
    return _create_session(email, remember=True)


@app.post("/api/auth/login")
async def login(req: LoginReq, request: Request) -> JSONResponse:
    email, ip = req.email, _client_ip(request)
    # 纯用户名映射为内部邮箱（管理员专用账号体系）
    if "@" not in email:
        email = f"{email}@saixz.local"
    now = int(time.time())

    # 同 IP 每分钟最多 10 次尝试
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM ip_log WHERE ts < ?", (now - 3600,))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM ip_log WHERE ip=? AND ts > ?", (ip, now - 60)
        ).fetchone()[0]
        if cnt >= IP_LOGIN_PER_MIN:
            conn.commit()
            return JSONResponse({"ok": False, "error": "操作过于频繁，请 1 分钟后再试"}, status_code=429)
        conn.execute("INSERT INTO ip_log(ip, ts) VALUES(?,?)", (ip, now))
        conn.commit()

    # 同邮箱锁定检查
    lock_err = _lock_check(email)
    if lock_err:
        return lock_err

    row = _user_row(email)
    if not row or not row[1] or not _check_password(req.password, row[1]):
        fails, locked_until = _record_pwd_fail(email)
        log.warning("登录失败 email=%s ip=%s fails=%d", email, ip, fails)
        if locked_until:
            return JSONResponse(
                {"ok": False, "error": "连续失败次数过多，账号已锁定 10 分钟"},
                status_code=423,
            )
        remaining = LOGIN_MAX_FAILS - fails
        return JSONResponse(
            {"ok": False, "error": f"账号或密码不正确（还可尝试 {remaining} 次）"},
            status_code=400,
        )

    # 封号检查（密码正确后才提示，防枚举）
    if _user_disabled(email):
        log.warning("封号用户尝试登录 email=%s ip=%s", email, ip)
        return JSONResponse({"ok": False, "error": "该账号已被封禁，请联系客服"}, status_code=403)

    # 登录成功：清失败计数、更新 last_login
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM login_guard WHERE email=?", (email,))
        conn.execute("UPDATE users SET last_login=? WHERE email=?", (now, email))
        conn.commit()
    log.info("登录成功 email=%s remember=%s", email, req.remember)
    return _create_session(email, remember=req.remember)


@app.post("/api/auth/reset-password")
async def reset_password(req: ResetReq) -> JSONResponse:
    email = req.email
    err = _verify_code(email, req.code, "reset")
    if err:
        return err

    now = int(time.time())
    with closing(_conn()) as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE email=?",
            (_hash_password(req.password), now, email),
        )
        if cur.rowcount == 0:
            conn.commit()
            return JSONResponse({"ok": False, "error": "该邮箱未注册"}, status_code=400)
        # 重置后作废旧 session、清锁定
        conn.execute("DELETE FROM sessions WHERE email=?", (email,))
        conn.execute("DELETE FROM login_guard WHERE email=?", (email,))
        conn.commit()
    log.info("密码重置成功 email=%s", email)
    return _create_session(email, remember=True)


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
    row = _user_row(email)
    return JSONResponse({
        "ok": True,
        "email": email,
        "nickname": _display_name(email, row),
        "public_id": (row[8] or "") if row else "",
        "avatar": (row[5] or "") if row else "",
        "phone": _mask_phone(row[2] or "") if row else "",
        "is_admin": email in ADMIN_EMAILS or (bool(row) and (row[7] or "user") == "admin"),
        "role": (row[7] or "user") if row else "user",
        "created_at": row[3] if row else None,
    })


# ---------------- 个人中心 ----------------
def _require_user(request: Request):
    """返回 (email, row)；未登录返回 (None, JSONResponse 401)。"""
    email = _session_from_cookie(request)
    if not email:
        return None, JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    row = _user_row(email)
    if not row:
        return None, JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    return email, row


class NicknameReq(BaseModel):
    nickname: str

    @field_validator("nickname")
    @classmethod
    def _v_nick(cls, v: str) -> str:
        v = v.strip()
        if not _valid_nickname(v):
            raise ValueError("昵称需 2~20 字符，仅限中英文、数字和常用符号")
        return v


class AvatarReq(BaseModel):
    avatar: str

    @field_validator("avatar")
    @classmethod
    def _v_avatar(cls, v: str) -> str:
        v = v.strip()
        prefix = "data:image/jpeg;base64,"
        if not v.startswith(prefix):
            raise ValueError("头像格式不正确（需 JPEG）")
        b64 = v[len(prefix):]
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            raise ValueError("头像数据损坏")
        if len(raw) > 200 * 1024:
            raise ValueError("头像过大（超过 200KB）")
        if len(raw) < 100:
            raise ValueError("头像数据无效")
        return v


class PwdChangeReq(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _v_pwd(cls, v): return _password_validator(v)

    @field_validator("old_password")
    @classmethod
    def _v_old(cls, v: str) -> str:
        if not v:
            raise ValueError("请输入原密码")
        return v


@app.get("/api/auth/profile")
async def profile(request: Request) -> JSONResponse:
    email, row = _require_user(request)
    if not email:
        return row
    return JSONResponse({
        "ok": True,
        "email": email,
        "nickname": _display_name(email, row),
        "public_id": row[8] or "",
        "avatar": row[5] or "",
        "phone": _mask_phone(row[2] or ""),
        "created_at": row[3],
        "last_login": row[6],
        "is_admin": email in ADMIN_EMAILS or (row[7] or "user") == "admin",
        "role": row[7] or "user",
    })


@app.post("/api/auth/profile/nickname")
async def change_nickname(req: NicknameReq, request: Request) -> JSONResponse:
    email, row = _require_user(request)
    if not email:
        return row
    now = int(time.time())
    with closing(_conn()) as conn:
        conn.execute(
            "UPDATE users SET nickname=?, updated_at=? WHERE email=?",
            (req.nickname, now, email),
        )
        conn.commit()
    log.info("昵称修改 email=%s nickname=%s", email, req.nickname)
    return JSONResponse({"ok": True, "nickname": req.nickname})


@app.post("/api/auth/profile/avatar")
async def change_avatar(req: AvatarReq, request: Request) -> JSONResponse:
    email, row = _require_user(request)
    if not email:
        return row
    now = int(time.time())
    with closing(_conn()) as conn:
        conn.execute(
            "UPDATE users SET avatar=?, updated_at=? WHERE email=?",
            (req.avatar, now, email),
        )
        conn.commit()
    log.info("头像修改 email=%s size=%d", email, len(req.avatar))
    return JSONResponse({"ok": True, "avatar": req.avatar})


@app.post("/api/auth/profile/password")
async def change_password(req: PwdChangeReq, request: Request) -> JSONResponse:
    email, row = _require_user(request)
    if not email:
        return row

    lock_err = _lock_check(email)
    if lock_err:
        return lock_err

    # row[1] = password_hash；旧密码错误计入防暴力锁定
    if not row[1] or not _check_password(req.old_password, row[1]):
        fails, locked_until = _record_pwd_fail(email)
        log.warning("改密码旧密码错误 email=%s fails=%d", email, fails)
        if locked_until:
            return JSONResponse(
                {"ok": False, "error": "连续失败次数过多，账号已锁定 10 分钟"},
                status_code=423,
            )
        remaining = LOGIN_MAX_FAILS - fails
        return JSONResponse(
            {"ok": False, "error": f"原密码不正确（还可尝试 {remaining} 次）"},
            status_code=400,
        )

    now = int(time.time())
    current_token = request.cookies.get("fb_token", "")
    with closing(_conn()) as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE email=?",
            (_hash_password(req.new_password), now, email),
        )
        # 作废该用户其他 session，保留当前
        conn.execute(
            "DELETE FROM sessions WHERE email=? AND token_hash != ?",
            (email, _hash_token(current_token)),
        )
        conn.execute("DELETE FROM login_guard WHERE email=?", (email,))
        conn.commit()
    log.info("密码修改成功 email=%s", email)
    return JSONResponse({"ok": True})


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


# ---------------- 聊天室 ----------------
CHAT_MAX_LEN = 200
CHAT_INTERVAL = 2       # 同用户 2 秒 1 条
CHAT_PER_MIN = 20       # 同用户每分钟 20 条
CHAT_PAGE = 50


class ChatSendReq(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _v_text(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= CHAT_MAX_LEN):
            raise ValueError("消息需 1~200 字")
        return v


@app.get("/api/chat/messages")
async def chat_messages(after_id: int = 0) -> JSONResponse:
    """公开访问。after_id>0 返回 id>after_id 的最新 50 条（正序）；否则返回最新 50 条。"""
    _cleanup_chat()
    with closing(_conn()) as conn:
        if after_id > 0:
            rows = conn.execute(
                "SELECT id, email, nickname, text, created_at, kind FROM chat_messages"
                " WHERE id > ? ORDER BY id ASC LIMIT ?",
                (after_id, CHAT_PAGE),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, email, nickname, text, created_at, kind FROM chat_messages"
                " ORDER BY id DESC LIMIT ?",
                (CHAT_PAGE,),
            ).fetchall()
            rows.reverse()
        # 显示名实时从 users 表解析（自改昵称 > 用户 public_id），历史快照里的邮箱/旧昵称不外泄
        emails = {r[1] for r in rows if r[1]}
        name_map = {}
        if emails:
            placeholders = ",".join("?" for _ in emails)
            users = conn.execute(
                "SELECT email, password_hash, phone, created_at, nickname, avatar, last_login, role, public_id"
                f" FROM users WHERE email IN ({placeholders})", tuple(emails),
            ).fetchall()
            name_map = {u[0]: _display_name(u[0], u) for u in users}
            for em in emails:
                name_map.setdefault(em, "已注销用户")
    return JSONResponse({
        "ok": True,
        "messages": [
            {"id": r[0], "nickname": name_map.get(r[1]) or "游客",
             "text": r[3], "created_at": r[4], "kind": r[5]}
            for r in rows
        ],
    })


_last_chat_cleanup = 0.0

def _cleanup_chat() -> None:
    """删除 7 天前的聊天/客服消息；图片消息连文件一起删。启动时和读/发消息时顺带执行。"""
    global _last_chat_cleanup
    now = time.monotonic()
    if now - _last_chat_cleanup < 60:
        return
    cutoff = int(time.time()) - CHAT_MSG_TTL
    try:
        with closing(_conn()) as conn:
            imgs = conn.execute(
                "SELECT text FROM chat_messages WHERE kind='image' AND created_at < ?",
                (cutoff,),
            ).fetchall()
            imgs += conn.execute(
                "SELECT text FROM support_msgs WHERE kind='image' AND created_at < ?",
                (cutoff,),
            ).fetchall()
            cur = conn.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))
            cur2 = conn.execute("DELETE FROM support_msgs WHERE created_at < ?", (cutoff,))
            conn.commit()
        _last_chat_cleanup = now
        if cur.rowcount or cur2.rowcount:
            log.info("消息清理: 聊天 %d 条, 客服 %d 条", cur.rowcount, cur2.rowcount)
        for (url,) in imgs:
            fname = url.rsplit("/", 1)[-1]
            if re.fullmatch(r"[A-Za-z0-9_\-]{8,}\.(jpg|png|webp|gif)", fname):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fname))
                except OSError:
                    pass
    except Exception as e:
        log.warning("消息清理异常: %s", e)


def _chat_rate_check(conn, email: str, now: int):
    """发言限流：同用户 2 秒 1 条、每分钟 20 条。返回 None 或 429 响应。"""
    last = conn.execute(
        "SELECT MAX(created_at) FROM chat_messages WHERE email=?", (email,)
    ).fetchone()[0]
    if last and now - last < CHAT_INTERVAL:
        return JSONResponse({"ok": False, "error": "发送太快了，请稍等 2 秒"}, status_code=429)
    cnt = conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE email=? AND created_at > ?",
        (email, now - 60),
    ).fetchone()[0]
    if cnt >= CHAT_PER_MIN:
        return JSONResponse({"ok": False, "error": "发言过于频繁，请 1 分钟后再试"}, status_code=429)
    return None


def _chat_insert(conn, email: str, kind: str, text: str, now: int):
    """写入一条聊天消息（带显示名快照），返回消息 dict。"""
    row = _user_row(email)
    nickname = _display_name(email, row)
    cur = conn.execute(
        "INSERT INTO chat_messages(email, nickname, avatar, text, created_at, kind)"
        " VALUES(?,?,?,?,?,?)",
        (email, nickname, "", text, now, kind),
    )
    return {"id": cur.lastrowid, "email": email, "nickname": nickname,
            "text": text, "created_at": now, "kind": kind}


@app.post("/api/chat/send")
async def chat_send(req: ChatSendReq, request: Request) -> JSONResponse:
    email = _session_from_cookie(request)
    if not email:
        return JSONResponse(
            {"ok": False, "error": "请先登录", "need_login": True}, status_code=401
        )
    if _user_banned(email):
        return JSONResponse({"ok": False, "error": "你已被禁言，暂时无法发言"}, status_code=403)
    now = int(time.time())
    _cleanup_chat()
    with closing(_conn()) as conn:
        err = _chat_rate_check(conn, email, now)
        if err:
            return err
        # 敏感词过滤：命中词替换为等长 *
        text = _filter_sensitive(req.text)
        msg = _chat_insert(conn, email, "text", text, now)
        conn.commit()
    return JSONResponse({"ok": True, "message": msg})


# ---- 聊天图片 ----
_IMG_MAGIC = {
    "jpg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
}


def _detect_img(raw: bytes):
    """按魔数识别图片类型，返回扩展名或 None。"""
    for ext, magics in _IMG_MAGIC.items():
        if any(raw.startswith(m) for m in magics):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _save_image(data_url: str, max_size: int):
    """解析 data URL → 校验大小/魔数 → 存文件。成功返回 (url, None)，失败返回 (None, JSONResponse)。"""
    try:
        b64 = data_url.split(",", 1)[1]
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None, JSONResponse({"ok": False, "error": "图片数据损坏"}, status_code=400)
    limit_mb = max_size // (1024 * 1024)
    if len(raw) > max_size:
        return None, JSONResponse({"ok": False, "error": f"图片过大（超过 {limit_mb}MB）"}, status_code=400)
    ext = _detect_img(raw)
    if not ext:
        return None, JSONResponse({"ok": False, "error": "仅支持 jpg/png/webp/gif 图片"}, status_code=400)
    fname = secrets.token_urlsafe(16) + "." + ext
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
        f.write(raw)
    return f"/api/chat/img/{fname}", None


class ChatUploadReq(BaseModel):
    image: str

    @field_validator("image")
    @classmethod
    def _v_img(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("data:image/"):
            raise ValueError("图片格式不正确")
        return v


@app.post("/api/chat/upload")
async def chat_upload(req: ChatUploadReq, request: Request) -> JSONResponse:
    """聊天发图：base64 data URL，解码 ≤15MB，魔数校验，直接生成 kind=image 消息。"""
    email = _session_from_cookie(request)
    if not email:
        return JSONResponse(
            {"ok": False, "error": "请先登录", "need_login": True}, status_code=401
        )
    if _user_banned(email):
        return JSONResponse({"ok": False, "error": "你已被禁言，暂时无法发言"}, status_code=403)
    url, err = _save_image(req.image, CHAT_IMG_MAX)
    if not url:
        return err

    now = int(time.time())
    _cleanup_chat()
    with closing(_conn()) as conn:
        rate_err = _chat_rate_check(conn, email, now)
        if rate_err:
            return rate_err
        msg = _chat_insert(conn, email, "image", url, now)
        conn.commit()
    log.info("聊天图片 email=%s url=%s", email, url)
    return JSONResponse({"ok": True, "message": msg})


@app.get("/api/chat/img/{fname}")
async def chat_img(fname: str) -> Response:
    """聊天图片访问（走 /api/ 反代，无需新增 nginx 配置）。"""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,}\.(jpg|png|webp|gif)", fname):
        return Response(status_code=404)
    path = os.path.join(UPLOAD_DIR, fname)
    if not os.path.isfile(path):
        return Response(status_code=404)
    media = {"jpg": "image/jpeg", "png": "image/png",
             "webp": "image/webp", "gif": "image/gif"}[fname.rsplit(".", 1)[1]]
    return FileResponse(
        path, media_type=media,
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


# ---------------- 客服求助（双向聊天） ----------------
SUPPORT_MAX_LEN = 500
SUPPORT_PER_MIN = 5       # 每个用户/游客每分钟 5 条
VISITOR_COOKIE = "fb_visitor"
VISITOR_TTL = 365 * 86400


def _support_owner(request: Request):
    """返回 (owner_key, owner_label, response_patch)：
    登录用户 owner_key=email；游客用 visitor_id cookie，没有则签发（由 response_patch 写回）。"""
    email = _session_from_cookie(request)
    if email:
        row = _user_row(email)
        return f"u:{email}", _display_name(email, row), None
    vid = request.cookies.get(VISITOR_COOKIE, "").strip()
    if vid and len(vid) <= 64 and re.fullmatch(r"[A-Za-z0-9_\-]+", vid):
        return f"v:{vid}", f"游客 {vid[:6]}", None
    vid = secrets.token_urlsafe(12)
    def patch(resp: JSONResponse) -> None:
        resp.set_cookie(
            key=VISITOR_COOKIE, value=vid, max_age=VISITOR_TTL, path="/",
            httponly=True, secure=COOKIE_SECURE, samesite="lax",
        )
    return f"v:{vid}", f"游客 {vid[:6]}", patch


def _get_or_create_thread(owner_key: str, owner_label: str):
    now = int(time.time())
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT id, user_read FROM support_threads WHERE owner_key=?", (owner_key,)
        ).fetchone()
        if row:
            return row[0], row[1]
        cur = conn.execute(
            "INSERT INTO support_threads(owner_key, owner_label, created_at, last_at)"
            " VALUES(?,?,?,?)",
            (owner_key, owner_label, now, now),
        )
        conn.commit()
        return cur.lastrowid, 0


def _thread_msgs(thread_id: int, after_id: int = 0):
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT id, sender, sender_name, text, created_at, kind FROM support_msgs"
            " WHERE thread_id=? AND id>? ORDER BY id ASC",
            (thread_id, after_id),
        ).fetchall()
    return [
        {"id": r[0], "sender": r[1], "sender_name": r[2], "text": r[3],
         "created_at": r[4], "kind": r[5]}
        for r in rows
    ]


class SupportSendReq(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _v_text(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= SUPPORT_MAX_LEN):
            raise ValueError("消息需 1~500 字")
        return v


async def _notify_new_thread(owner_label: str, text: str) -> None:
    """thread 第一条消息：给管理员发一封「新求助」通知邮件。"""
    admin = sorted(ADMIN_EMAILS)[0] if ADMIN_EMAILS else ""
    if not admin:
        log.warning("新求助 thread 但 ADMIN_EMAILS 未配置")
        return
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#071510;font-family:'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:520px;margin:0 auto;background:#0e1f1a;border:1px solid rgba(212,175,55,.25);border-radius:14px;padding:32px;">
    <div style="color:#d4af37;font-size:18px;font-weight:700;">新用户求助</div>
    <div style="margin-top:16px;color:#e9e7de;font-size:14px;line-height:1.8;white-space:pre-wrap;background:rgba(5,13,10,.6);border-radius:8px;padding:14px;">{escape(text)}</div>
    <div style="margin-top:16px;color:#8fa39b;font-size:13px;line-height:2;">
      来自：{escape(owner_label)}<br>
      时间：{time_str}<br>
      请前往管理后台 · 客服工作台回复。
    </div>
  </div>
</body></html>"""
    await _deliver_mail(admin, "【赛先知足球】新用户求助", html)


@app.get("/api/support/my")
async def support_my(request: Request, after_id: int = 0) -> JSONResponse:
    """用户侧：自动找/建自己的 thread，返回 thread_id + 消息（可 after_id 增量）。
    顺带标记 user_read。游客自动签发 visitor_id cookie。"""
    owner_key, owner_label, patch = _support_owner(request)
    tid, _ = _get_or_create_thread(owner_key, owner_label)
    msgs = _thread_msgs(tid, after_id)
    with closing(_conn()) as conn:
        max_id = conn.execute(
            "SELECT MAX(id) FROM support_msgs WHERE thread_id=?", (tid,)
        ).fetchone()[0] or 0
        conn.execute("UPDATE support_threads SET user_read=? WHERE id=?", (max_id, tid))
        conn.commit()
    resp = JSONResponse({"ok": True, "thread_id": tid, "messages": msgs})
    if patch:
        patch(resp)
    return resp


@app.post("/api/support/send")
async def support_send(req: SupportSendReq, request: Request) -> JSONResponse:
    owner_key, owner_label, patch = _support_owner(request)
    tid, _ = _get_or_create_thread(owner_key, owner_label)
    now = int(time.time())

    with closing(_conn()) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM support_msgs WHERE thread_id=? AND sender='user'"
            " AND created_at > ?",
            (tid, now - 60),
        ).fetchone()[0]
        if cnt >= SUPPORT_PER_MIN:
            return JSONResponse({"ok": False, "error": "发送太频繁，请稍后再试"}, status_code=429)
        first = conn.execute(
            "SELECT COUNT(*) FROM support_msgs WHERE thread_id=?", (tid,)
        ).fetchone()[0] == 0
        cur = conn.execute(
            "INSERT INTO support_msgs(thread_id, sender, sender_name, text, created_at)"
            " VALUES(?,?,?,?,?)",
            (tid, "user", owner_label, req.text, now),
        )
        mid = cur.lastrowid
        conn.execute(
            "UPDATE support_threads SET last_at=?, user_read=? WHERE id=?",
            (now, mid, tid),
        )
        conn.commit()

    if first:
        await _notify_new_thread(owner_label, req.text)
    resp = JSONResponse({
        "ok": True,
        "message": {"id": mid, "sender": "user", "sender_name": owner_label,
                    "text": req.text, "created_at": now, "kind": "text"},
    })
    if patch:
        patch(resp)
    return resp


@app.post("/api/support/upload")
async def support_upload(req: ChatUploadReq, request: Request) -> JSONResponse:
    """用户/游客客服发图：≤30MB，生成 kind=image 消息。"""
    owner_key, owner_label, patch = _support_owner(request)
    tid, _ = _get_or_create_thread(owner_key, owner_label)
    url, err = _save_image(req.image, SUPPORT_IMG_MAX)
    if not url:
        return err
    now = int(time.time())

    with closing(_conn()) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM support_msgs WHERE thread_id=? AND sender='user'"
            " AND created_at > ?",
            (tid, now - 60),
        ).fetchone()[0]
        if cnt >= SUPPORT_PER_MIN:
            return JSONResponse({"ok": False, "error": "发送太频繁，请稍后再试"}, status_code=429)
        first = conn.execute(
            "SELECT COUNT(*) FROM support_msgs WHERE thread_id=?", (tid,)
        ).fetchone()[0] == 0
        cur = conn.execute(
            "INSERT INTO support_msgs(thread_id, sender, sender_name, text, created_at, kind)"
            " VALUES(?,?,?,?,?,?)",
            (tid, "user", owner_label, url, now, "image"),
        )
        mid = cur.lastrowid
        conn.execute(
            "UPDATE support_threads SET last_at=?, user_read=? WHERE id=?",
            (now, mid, tid),
        )
        conn.commit()

    if first:
        await _notify_new_thread(owner_label, "[图片]")
    resp = JSONResponse({
        "ok": True,
        "message": {"id": mid, "sender": "user", "sender_name": owner_label,
                    "text": url, "created_at": now, "kind": "image"},
    })
    if patch:
        patch(resp)
    return resp


# ---- 客服侧（真管理员或 role=staff）----
def _require_staff(request: Request):
    """返回 (email, None)；无权限返回 (None, JSONResponse)。"""
    email = _session_from_cookie(request)
    if not email:
        return None, JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if email in ADMIN_EMAILS:
        return email, None
    row = _user_row(email)
    if row and (row[7] or "user") in ("staff", "admin"):
        return email, None
    return None, JSONResponse({"ok": False, "error": "无权限"}, status_code=403)


@app.get("/api/support/threads")
async def support_threads(request: Request) -> JSONResponse:
    email, err = _require_staff(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.owner_label, t.last_at, t.staff_read,
              (SELECT text FROM support_msgs WHERE thread_id=t.id ORDER BY id DESC LIMIT 1),
              (SELECT COUNT(*) FROM support_msgs
                WHERE thread_id=t.id AND sender='user' AND id > t.staff_read)
            FROM support_threads t
            WHERE EXISTS(SELECT 1 FROM support_msgs WHERE thread_id=t.id)
            ORDER BY t.last_at DESC
            """
        ).fetchall()
    return JSONResponse({
        "ok": True,
        "threads": [
            {"thread_id": r[0], "owner_label": r[1], "last_at": r[2],
             "last_text": r[4] or "", "unread": r[5]}
            for r in rows
        ],
    })


@app.get("/api/support/thread/{tid}/messages")
async def support_thread_msgs(tid: int, request: Request, after_id: int = 0) -> JSONResponse:
    email, err = _require_staff(request)
    if not email:
        return err
    msgs = _thread_msgs(tid, after_id)
    with closing(_conn()) as conn:
        max_id = conn.execute(
            "SELECT MAX(id) FROM support_msgs WHERE thread_id=?", (tid,)
        ).fetchone()[0] or 0
        conn.execute("UPDATE support_threads SET staff_read=? WHERE id=?", (max_id, tid))
        conn.commit()
    return JSONResponse({"ok": True, "thread_id": tid, "messages": msgs})


@app.post("/api/support/thread/{tid}/reply")
async def support_reply(tid: int, req: SupportSendReq, request: Request) -> JSONResponse:
    email, err = _require_staff(request)
    if not email:
        return err
    now = int(time.time())
    with closing(_conn()) as conn:
        if not conn.execute("SELECT 1 FROM support_threads WHERE id=?", (tid,)).fetchone():
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
        staff_name = "客服"
        row = _user_row(email)
        if row:
            staff_name = _display_name(email, row)
        cur = conn.execute(
            "INSERT INTO support_msgs(thread_id, sender, sender_name, text, created_at)"
            " VALUES(?,?,?,?,?)",
            (tid, "staff", staff_name, req.text, now),
        )
        mid = cur.lastrowid
        conn.execute(
            "UPDATE support_threads SET last_at=?, staff_read=? WHERE id=?",
            (now, mid, tid),
        )
        conn.commit()
    log.info("客服回复 tid=%d staff=%s", tid, email)
    return JSONResponse({
        "ok": True,
        "message": {"id": mid, "sender": "staff", "sender_name": staff_name,
                    "text": req.text, "created_at": now, "kind": "text"},
    })


@app.post("/api/support/thread/{tid}/upload")
async def support_staff_upload(tid: int, req: ChatUploadReq, request: Request) -> JSONResponse:
    """客服侧发图：≤30MB，生成 kind=image 的 staff 消息。"""
    email, err = _require_staff(request)
    if not email:
        return err
    url, img_err = _save_image(req.image, SUPPORT_IMG_MAX)
    if not url:
        return img_err
    now = int(time.time())
    with closing(_conn()) as conn:
        if not conn.execute("SELECT 1 FROM support_threads WHERE id=?", (tid,)).fetchone():
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
        staff_name = "客服"
        row = _user_row(email)
        if row:
            staff_name = _display_name(email, row)
        cur = conn.execute(
            "INSERT INTO support_msgs(thread_id, sender, sender_name, text, created_at, kind)"
            " VALUES(?,?,?,?,?,?)",
            (tid, "staff", staff_name, url, now, "image"),
        )
        mid = cur.lastrowid
        conn.execute(
            "UPDATE support_threads SET last_at=?, staff_read=? WHERE id=?",
            (now, mid, tid),
        )
        conn.commit()
    log.info("客服发图 tid=%d staff=%s", tid, email)
    return JSONResponse({
        "ok": True,
        "message": {"id": mid, "sender": "staff", "sender_name": staff_name,
                    "text": url, "created_at": now, "kind": "image"},
    })


# ---------------- 管理员后台 ----------------
def _require_admin(request: Request):
    """仅真管理员（ADMIN_EMAILS 或 users.role='admin'）。返回 (email, None) 或 (None, JSONResponse)。"""
    email = _session_from_cookie(request)
    if not email:
        return None, JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if email in ADMIN_EMAILS:
        return email, None
    row = _user_row(email)
    if row and (row[7] or "user") == "admin":
        return email, None
    return None, JSONResponse({"ok": False, "error": "无权限"}, status_code=403)


@app.get("/api/auth/admin/users")
async def admin_users(request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT email, nickname, phone, role, created_at, last_login, public_id"
            " FROM users ORDER BY created_at DESC"
        ).fetchall()
    return JSONResponse({
        "ok": True,
        "users": [
            {"email": r[0], "nickname": r[1] or "", "phone": r[2] or "",
             "role": r[3] or "user", "created_at": r[4], "last_login": r[5],
             "public_id": r[6] or ""}
            for r in rows
        ],
    })


class SetRoleReq(BaseModel):
    email: str
    role: str

    @field_validator("email")
    @classmethod
    def _v_email(cls, v): return _email_validator(v)

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"user", "staff"}:
            raise ValueError("role 必须为 user 或 staff")
        return v


@app.post("/api/auth/admin/set-role")
async def admin_set_role(req: SetRoleReq, request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        cur = conn.execute(
            "UPDATE users SET role=?, updated_at=? WHERE email=?",
            (req.role, int(time.time()), req.email),
        )
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
    log.info("角色变更 by=%s target=%s role=%s", email, req.email, req.role)
    return JSONResponse({"ok": True})


# ---- 用户管理 ----
class AdminEmailReq(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _v_email(cls, v): return _email_validator(v)


class AdminDisableReq(AdminEmailReq):
    disabled: bool = True


class AdminBanReq(AdminEmailReq):
    banned: bool = True


class AdminIdReq(BaseModel):
    id: int


class WordReq(BaseModel):
    word: str

    @field_validator("word")
    @classmethod
    def _v_word(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= 30):
            raise ValueError("词条需 1~30 字")
        return v


class AnnAddReq(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _v_text(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= 500):
            raise ValueError("公告需 1~500 字")
        return v


def _guard_target(target: str, operator: str):
    """管理员账号保护：不允许操作 ADMIN_EMAILS 成员或自己。"""
    if target in ADMIN_EMAILS or target == operator:
        return JSONResponse({"ok": False, "error": "不能操作管理员账号"}, status_code=400)
    return None


@app.get("/api/auth/admin/users-search")
async def admin_users_search(request: Request, q: str = "") -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        if q.strip():
            like = f"%{q.strip()}%"
            rows = conn.execute(
                "SELECT email, nickname, phone, role, created_at, last_login, public_id, banned, disabled"
                " FROM users WHERE email LIKE ? OR phone LIKE ? OR nickname LIKE ?"
                " ORDER BY created_at DESC LIMIT 100",
                (like, like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT email, nickname, phone, role, created_at, last_login, public_id, banned, disabled"
                " FROM users ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
    return JSONResponse({
        "ok": True,
        "users": [
            {"email": r[0], "nickname": r[1] or "", "phone": r[2] or "",
             "role": r[3] or "user", "created_at": r[4], "last_login": r[5],
             "public_id": r[6] or "", "banned": bool(r[7]), "disabled": bool(r[8])}
            for r in rows
        ],
    })


@app.post("/api/auth/admin/reset-user-password")
async def admin_reset_user_password(req: AdminEmailReq, request: Request) -> JSONResponse:
    """重置用户密码：生成随机 10 位新密码（仅展示一次），并清掉该用户全部 session。"""
    email, err = _require_admin(request)
    if not email:
        return err
    guard = _guard_target(req.email, email)
    if guard:
        return guard
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    new_pw = "".join(secrets.choice(alphabet) for _ in range(10))
    with closing(_conn()) as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE email=?",
            (_hash_password(new_pw), int(time.time()), req.email),
        )
        if cur.rowcount == 0:
            conn.commit()
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        conn.execute("DELETE FROM sessions WHERE email=?", (req.email,))
        conn.execute("DELETE FROM login_guard WHERE email=?", (req.email,))
        conn.commit()
    log.info("管理员重置密码 by=%s target=%s", email, req.email)
    return JSONResponse({"ok": True, "password": new_pw})


@app.post("/api/auth/admin/disable")
async def admin_disable(req: AdminDisableReq, request: Request) -> JSONResponse:
    """封号/解封。封号时清掉全部 session，之后登录返回「该账号已被封禁」。"""
    email, err = _require_admin(request)
    if not email:
        return err
    guard = _guard_target(req.email, email)
    if guard:
        return guard
    with closing(_conn()) as conn:
        cur = conn.execute(
            "UPDATE users SET disabled=? WHERE email=?", (int(req.disabled), req.email)
        )
        if cur.rowcount == 0:
            conn.commit()
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        if req.disabled:
            conn.execute("DELETE FROM sessions WHERE email=?", (req.email,))
        conn.commit()
    log.info("管理员%s by=%s target=%s", "封号" if req.disabled else "解封", email, req.email)
    return JSONResponse({"ok": True})


@app.post("/api/auth/admin/delete-user")
async def admin_delete_user(req: AdminEmailReq, request: Request) -> JSONResponse:
    """注销用户：删账号/session/其客服会话；聊天室消息保留（前端显示「已注销用户」）。"""
    email, err = _require_admin(request)
    if not email:
        return err
    guard = _guard_target(req.email, email)
    if guard:
        return guard
    with closing(_conn()) as conn:
        cur = conn.execute("DELETE FROM users WHERE email=?", (req.email,))
        if cur.rowcount == 0:
            conn.commit()
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        conn.execute("DELETE FROM sessions WHERE email=?", (req.email,))
        conn.execute("DELETE FROM codes WHERE email=?", (req.email,))
        conn.execute("DELETE FROM login_guard WHERE email=?", (req.email,))
        tids = [r[0] for r in conn.execute(
            "SELECT id FROM support_threads WHERE owner_key=?", (f"u:{req.email}",)
        ).fetchall()]
        for tid in tids:
            conn.execute("DELETE FROM support_msgs WHERE thread_id=?", (tid,))
            conn.execute("DELETE FROM support_threads WHERE id=?", (tid,))
        conn.commit()
    log.info("管理员注销用户 by=%s target=%s threads=%d", email, req.email, len(tids))
    return JSONResponse({"ok": True})


@app.post("/api/auth/admin/ban")
async def admin_ban(req: AdminBanReq, request: Request) -> JSONResponse:
    """禁言/解禁：禁言后聊天室不能发言，客服通道不受影响。"""
    email, err = _require_admin(request)
    if not email:
        return err
    guard = _guard_target(req.email, email)
    if guard:
        return guard
    with closing(_conn()) as conn:
        cur = conn.execute(
            "UPDATE users SET banned=? WHERE email=?", (int(req.banned), req.email)
        )
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
    log.info("管理员%s by=%s target=%s", "禁言" if req.banned else "解禁", email, req.email)
    return JSONResponse({"ok": True})


# ---- 聊天室管理 ----
@app.get("/api/auth/admin/chat")
async def admin_chat(request: Request, page: int = 1, size: int = 20) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    page = max(1, min(page, 10000))
    size = max(1, min(size, 100))
    with closing(_conn()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        rows = conn.execute(
            "SELECT id, email, nickname, text, kind, created_at FROM chat_messages"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (size, (page - 1) * size),
        ).fetchall()
    return JSONResponse({
        "ok": True, "total": total, "page": page, "size": size,
        "messages": [
            {"id": r[0], "email": r[1], "nickname": r[2] or "", "text": r[3],
             "kind": r[4], "created_at": r[5]}
            for r in rows
        ],
    })


@app.post("/api/auth/admin/chat/delete")
async def admin_chat_delete(req: AdminIdReq, request: Request) -> JSONResponse:
    """删除聊天室消息；图片消息连文件一起删。"""
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT kind, text FROM chat_messages WHERE id=?", (req.id,)
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "消息不存在"}, status_code=404)
        conn.execute("DELETE FROM chat_messages WHERE id=?", (req.id,))
        conn.commit()
    if row[0] == "image":
        m = re.search(r"([A-Za-z0-9_\-]{8,}\.(?:jpg|png|webp|gif))", row[1] or "")
        if m:
            try:
                os.remove(os.path.join(UPLOAD_DIR, m.group(1)))
            except OSError:
                pass
    log.info("管理员删聊天消息 id=%d by=%s", req.id, email)
    return JSONResponse({"ok": True})


# ---- 敏感词管理 ----
@app.get("/api/auth/admin/words")
async def admin_words(request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        rows = conn.execute("SELECT word FROM banned_words ORDER BY word").fetchall()
    return JSONResponse({"ok": True, "words": [r[0] for r in rows]})


@app.post("/api/auth/admin/words/add")
async def admin_words_add(req: WordReq, request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        conn.execute("INSERT OR IGNORE INTO banned_words(word) VALUES(?)", (req.word,))
        conn.commit()
    _refresh_words()
    log.info("敏感词新增 by=%s word=%s", email, req.word)
    return JSONResponse({"ok": True})


@app.post("/api/auth/admin/words/delete")
async def admin_words_delete(req: WordReq, request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM banned_words WHERE word=?", (req.word,))
        conn.commit()
    _refresh_words()
    log.info("敏感词删除 by=%s word=%s", email, req.word)
    return JSONResponse({"ok": True})


# ---- 数据统计（用户/聊天/客服 + nginx 访问量） ----
_NGX_LINE = re.compile(
    r'^(\S+) \S+ \S+ \[(\d{2})/(\w{3})/(\d{4}):[^\]]+\] "GET (\S+) [^"]*" (\d{3}) \d+ "[^"]*" "([^"]*)"')
_NGX_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_BOT_UA = re.compile(
    r"bot|spider|crawl|curl|wget|python|go-http|uptime|monitor|headless|scan|nmap|masscan"
    r"|zgrab|check|pingdom|statuscake|slurp|archiver",
    re.I,
)


_visits_cache = {"expires": 0.0, "value": None}

def _parse_visits() -> dict:
    """解析 nginx access 日志，只统计首页（/ 与 /index.html）真人访问。"""
    if time.monotonic() < _visits_cache["expires"]:
        return _visits_cache["value"]
    import glob
    total = 0
    per_day: dict = {}
    ips: set = set()
    uas: dict = {}
    for fp in sorted(glob.glob(os.path.join(NGINX_LOG_DIR, "access.log*"))):
        if fp.endswith(".gz"):
            continue
        try:
            f = open(fp, "r", errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                m = _NGX_LINE.match(line)
                if not m:
                    continue
                ip, dd, mon, yyyy, path, status, ua = m.groups()
                if path not in ("/", "/index.html") or status not in ("200", "304"):
                    continue
                if not ua or _BOT_UA.search(ua):
                    continue
                mm = _NGX_MONTHS.get(mon)
                if not mm:
                    continue
                total += 1
                ips.add(ip)
                day = f"{yyyy}-{mm:02d}-{dd}"
                per_day[day] = per_day.get(day, 0) + 1
                uas[ua] = uas.get(ua, 0) + 1
    result = {
        "total": total,
        "unique_ips": len(ips),
        "days": [{"day": d, "count": c} for d, c in sorted(per_day.items())][-30:],
        "top_uas": [{"ua": u, "count": c}
                    for u, c in sorted(uas.items(), key=lambda x: -x[1])[:10]],
    }

    _visits_cache.update(expires=time.monotonic() + 60, value=result)
    return result


@app.get("/api/auth/admin/stats")
async def admin_stats(request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = conn.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
        disabled = conn.execute("SELECT COUNT(*) FROM users WHERE disabled=1").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        threads = conn.execute("SELECT COUNT(*) FROM support_threads").fetchone()[0]
        msgs = conn.execute("SELECT COUNT(*) FROM support_msgs").fetchone()[0]
    return JSONResponse({
        "ok": True, "users": users, "banned": banned, "disabled": disabled,
        "chat_messages": chats, "support_threads": threads, "support_msgs": msgs,
        "visits": await run_in_threadpool(_parse_visits),
    })


# ---- 足球数据仓库状态 / 一键更新 ----
@app.get("/api/auth/admin/data-status")
async def admin_data_status(request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    repo = FOOTBALL_REPO_DIR
    if not os.path.isdir(repo):
        return JSONResponse({"ok": True, "exists": False, "repo": repo})
    latest = 0.0
    latest_name = ""
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            if fn.endswith((".js", ".json")):
                try:
                    mt = os.path.getmtime(os.path.join(root, fn))
                except OSError:
                    continue
                if mt > latest:
                    latest, latest_name = mt, fn
    commit = ""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ci | %s"],
            cwd=repo, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            commit = r.stdout.strip()
    except Exception:
        pass
    return JSONResponse({
        "ok": True, "exists": True, "repo": repo, "latest_file": latest_name,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest)) if latest else "",
        "commit": commit,
    })


def _update_football_repo(repo: str) -> tuple[bool, str]:
    """在工作线程中更新仓库；fetch 失败时不执行 reset。"""
    try:
        r1 = subprocess.run(
            ["git", "fetch", "origin"], cwd=repo, capture_output=True, text=True, timeout=120,
        )
        if r1.returncode != 0:
            out = (r1.stdout + r1.stderr).strip()[-2000:]
            return False, f"拉取失败：{out[:500]}"
        r2 = subprocess.run(
            ["git", "reset", "--hard", "origin/main"],
            cwd=repo, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "更新超时（120 秒）"
    out = ((r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip())[-2000:]
    return r2.returncode == 0, out if r2.returncode == 0 else f"更新失败：{out[:500]}"


@app.post("/api/auth/admin/data-update")
async def admin_data_update(request: Request) -> JSONResponse:
    """数据更新：cd 仓库 && git fetch && git reset --hard origin/main。"""
    email, err = _require_admin(request)
    if not email:
        return err
    repo = FOOTBALL_REPO_DIR
    if not os.path.isdir(repo):
        return JSONResponse({"ok": False, "error": f"数据目录不存在：{repo}"}, status_code=400)
    ok, output = await run_in_threadpool(_update_football_repo, repo)
    log.info("管理员数据更新 by=%s ok=%s", email, ok)
    if not ok:
        return JSONResponse({"ok": False, "error": output}, status_code=500)
    return JSONResponse({"ok": True, "output": output})


# ---- 公告 ----
@app.get("/api/announcement")
async def announcement_public() -> JSONResponse:
    """公开：返回最新一条 active 公告（前端顶部横幅）。"""
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT id, text, created_at FROM announcements WHERE active=1"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return JSONResponse({"ok": True, "announcement": None})
    return JSONResponse({
        "ok": True,
        "announcement": {"id": row[0], "text": row[1], "created_at": row[2]},
    })


@app.get("/api/auth/admin/announcements")
async def admin_announcements(request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT id, text, created_at, active FROM announcements ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return JSONResponse({
        "ok": True,
        "announcements": [
            {"id": r[0], "text": r[1], "created_at": r[2], "active": bool(r[3])}
            for r in rows
        ],
    })


@app.post("/api/auth/admin/announcements/add")
async def admin_announcement_add(req: AnnAddReq, request: Request) -> JSONResponse:
    """发布公告：新公告 active=1，其余全部下架（同时只展示一条）。"""
    email, err = _require_admin(request)
    if not email:
        return err
    now = int(time.time())
    with closing(_conn()) as conn:
        conn.execute("UPDATE announcements SET active=0")
        cur = conn.execute(
            "INSERT INTO announcements(text, created_at, active) VALUES(?,?,1)",
            (req.text, now),
        )
        new_id = cur.lastrowid
        conn.commit()
    log.info("发布公告 id=%d by=%s", new_id, email)
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/api/auth/admin/announcements/deactivate")
async def admin_announcement_deactivate(req: AdminIdReq, request: Request) -> JSONResponse:
    email, err = _require_admin(request)
    if not email:
        return err
    with closing(_conn()) as conn:
        cur = conn.execute("UPDATE announcements SET active=0 WHERE id=?", (req.id,))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse({"ok": False, "error": "公告不存在"}, status_code=404)
    log.info("下架公告 id=%d by=%s", req.id, email)
    return JSONResponse({"ok": True})


# ---------------- 管理员导出 ----------------
@app.get("/api/auth/admin/users.csv")
async def admin_users_csv(request: Request) -> Response:
    email = _session_from_cookie(request)
    if not email:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    if email not in ADMIN_EMAILS:
        return JSONResponse({"ok": False, "error": "无权限"}, status_code=403)

    import csv
    import io

    def _iso(ts):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else ""

    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT email, nickname, phone, created_at, last_login FROM users ORDER BY created_at"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "nickname", "phone", "created_at", "last_login"])
    for r in rows:
        w.writerow([r[0], r[1] or "", r[2] or "", _iso(r[3]), _iso(r[4])])
    return Response(
        content="﻿" + buf.getvalue(),  # BOM 让 Excel 正确识别 UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="users.csv"'},
    )
