"""ตัวช่วย auth ของเว็บ: จำกัดการเดารหัส, next ที่ปลอดภัย, decorator สิทธิ์.

ไม่ import app/webfmt (กัน circular import).
"""
from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from collections import defaultdict
from functools import wraps
from typing import Callable
from urllib.parse import urlparse

from flask import (current_app, jsonify, redirect, render_template, request, session,
                   url_for)
from flask.sessions import SecureCookieSessionInterface

MAX_USERNAME_LEN = 64
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "dengue"

# ลำดับสิทธิ์ viewer < srrt < admin: บทบาทสูงกว่าทำได้ทุกอย่างของบทบาทต่ำกว่า
ROLE_RANK = {"viewer": 0, "srrt": 1, "admin": 2}


def has_role(role: str | None, needed: str) -> bool:
    """True เมื่ออันดับของ role >= needed; role เป็น None/ไม่รู้จัก หรือ needed ไม่รู้จัก = False."""
    have, need = ROLE_RANK.get(role), ROLE_RANK.get(needed)
    return have is not None and need is not None and have >= need


class DengueSessionInterface(SecureCookieSessionInterface):
    """คุกกี้ session ที่ตั้งตามวิธีที่คำขอเข้ามาจริง (R24/I4):

    - Secure เมื่อคำขอเป็น https (หลัง Apache = X-Forwarded-Proto ที่ PrefixMiddleware แปลงเป็น url_scheme)
      เข้าตรง http://<LAN>:6800 ไม่มี Secure จึงยังล็อกอินได้
    - Path = sub-path ของแอป (X-Forwarded-Prefix เช่น /dengue) ไม่ใช่ "/" — เบราว์เซอร์ไม่ส่งคุกกี้นี้
      ไปยังแอปอื่นบน wnkhos.go.th"""

    def get_cookie_secure(self, app) -> bool:
        return request.is_secure

    def get_cookie_path(self, app) -> str:
        return request.script_root or "/"


class LoginThrottle:
    """จำกัดการเดารหัสผ่านในหน่วยความจำ: ผิด 5 ครั้งใน 10 นาที (ต่อ username+ไคลเอนต์) -> ล็อก 10 นาที."""

    MAX_FAILS = 5
    WINDOW = 600
    LOCK = 600
    MAX_KEYS = 5000

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._locked_until: dict[str, float] = {}

    def retry_after(self, key: str) -> int:
        """วินาทีที่ยังถูกล็อก (0 = ไม่ถูกล็อก)."""
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return 0
            left = until - self.clock()
            if left <= 0:
                self._locked_until.pop(key, None)
                self._fails.pop(key, None)
                return 0
            return int(left) + 1

    def fail(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            if len(self._fails) > self.MAX_KEYS:
                self._purge(now)
            recent = [t for t in self._fails[key] if now - t < self.WINDOW]
            recent.append(now)
            self._fails[key] = recent
            if len(recent) >= self.MAX_FAILS:
                self._locked_until[key] = now + self.LOCK

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)

    def _purge(self, now: float) -> None:
        for k in [k for k, ts in self._fails.items()
                  if not ts or now - ts[-1] >= self.WINDOW]:
            self._fails.pop(k, None)
        for k in [k for k, u in self._locked_until.items() if u <= now]:
            self._locked_until.pop(k, None)


def _last_forwarded(req) -> str | None:
    """รายการท้ายสุดของ X-Forwarded-For (ที่ Apache ต่อท้าย): None = ไม่มีหัวนี้,
    "" = มีหัวแต่ไม่มีรายการ."""
    xff = req.headers.get("X-Forwarded-For")
    if xff is None:
        return None
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    return parts[-1] if parts else ""


def client_key() -> str:
    """ท้ายสุดของ X-Forwarded-For (ใส่โดย proxy ของเรา) ไม่มีก็ใช้ remote_addr."""
    return _last_forwarded(request) or (request.remote_addr or "-")


def _client_ip(req) -> str | None:
    """ที่อยู่ที่ Apache เห็น: รายการท้ายสุดของ X-Forwarded-For ถ้ามีหัวนี้ ไม่งั้น remote_addr.

    คืน None ถ้าไม่ใช่ IP ที่อ่านได้ (หรือมีหัวแต่ไม่มีรายการ) — ผู้เรียกต้องถือว่าไม่ใช่ LAN."""
    last = _last_forwarded(req)
    raw = last if last is not None else (req.remote_addr or "")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def is_lan_client(req, prefixes) -> bool:
    """True เฉพาะเมื่อที่อยู่ไคลเอนต์ขึ้นต้นด้วยหนึ่งใน prefixes (เช่น "192.168.").

    แอปอยู่หลัง Apache บนเครื่องเดียวกัน (remote_addr = 127.0.0.1) คำขอจากสาธารณะจึงมี
    X-Forwarded-For เสมอ และรายการท้ายสุดคือที่อยู่ที่ Apache เห็น (รายการก่อนหน้าปลอมได้ จึงไม่ใช้).
    prefixes ว่าง = ปฏิเสธทุกคำขอ."""
    ip = _client_ip(req)
    if ip is None:
        return False
    dotted = ip + "."   # เทียบทั้งเลขชุด: prefix "10." ไม่ตรง 100.x และที่อยู่เต็มไม่ตรงเลขที่ยาวกว่า
    return any(p and dotted.startswith(p) for p in (prefixes or ()))


def lan_only(view):
    """เปิดเฉพาะไคลเอนต์ใน LAN (ตาม settings.lan_prefixes); นอกนั้น 403 หน้าไทยที่ไม่มีข้อมูล."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        prefixes = current_app.config["SETTINGS"].lan_prefixes
        if not is_lan_client(request, prefixes):
            if _is_api():
                return jsonify(error="หน้านี้เปิดเฉพาะเครือข่ายภายในโรงพยาบาล"), 403
            return render_template(
                "message.html", page_title="เข้าถึงไม่ได้",
                message="หน้านี้เปิดเฉพาะเครือข่ายภายในโรงพยาบาลเท่านั้น"), 403
        return view(*args, **kwargs)
    return wrapped


FORM_CSRF_FIELD = "csrf"


def form_csrf_token() -> str:
    """โทเคน CSRF ของฟอร์ม HTML (login / logout) เก็บใน session — สร้างครั้งแรกที่เรียก.

    ฟอร์มสองตัวนี้ส่งแบบ form post ธรรมดา (ไม่ผ่าน api_write_guard) จึงต้องมีโทเคนเอง:
    กันเว็บอื่นพาเบราว์เซอร์มา login ด้วยบัญชีของผู้โจมตี (login CSRF) หรือสั่ง logout."""
    token = session.get(FORM_CSRF_FIELD)
    if not token:
        token = session[FORM_CSRF_FIELD] = secrets.token_urlsafe(32)
    return token


def form_csrf_ok() -> bool:
    """True เมื่อโทเคนในฟอร์มตรงกับใน session (session ใหม่ที่ไม่มีโทเคน = ไม่ผ่าน)."""
    want = session.get(FORM_CSRF_FIELD) or ""
    sent = request.form.get(FORM_CSRF_FIELD) or ""
    return bool(want) and secrets.compare_digest(sent.encode(), want.encode())


def safe_next(target: str | None) -> str | None:
    """รับเฉพาะ path ภายในแอป (ขึ้นต้น / ไม่ใช่ // ไม่มี scheme/host)."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(ord(c) < 32 for c in target):
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


def _is_api() -> bool:
    return request.path.startswith("/api/")


def _unauthenticated():
    """ยังไม่ login: /api/ -> JSON 401; เบราว์เซอร์ GET -> ไปหน้า login (พก next);
    คำขอที่ไม่ใช่ GET -> หน้า/JSON 401 (ไม่ redirect ไป next ที่จะติด 405)."""
    if _is_api():
        return jsonify(error="กรุณาเข้าสู่ระบบ"), 401
    if request.method not in ("GET", "HEAD"):
        return render_template(
            "message.html", page_title="กรุณาเข้าสู่ระบบ",
            message="เซสชันหมดอายุหรือยังไม่ได้เข้าสู่ระบบ กรุณาเข้าสู่ระบบแล้วลองใหม่"), 401
    target = request.script_root + request.full_path.rstrip("?")
    return redirect(url_for("login", next=target))


def current_role() -> str | None:
    """บทบาทของผู้ใช้ที่ล็อกอินอยู่ โดยอ่านจากฐานทุกคำขอ (หนึ่ง SELECT): ผู้ใช้ที่ถูกลบ = ล้าง session
    แล้วคืน None (ถือว่าไม่ได้ล็อกอิน); บทบาทที่เปลี่ยนในฐาน (เช่นลดสิทธิ์) มีผลคำขอถัดไปทันที."""
    user = session.get("user")
    if not user:
        return None
    role = current_app.config["STORE"].user_role(user)
    if role is None:
        session.clear()
        return None
    if session.get("role") != role:
        session["role"] = role
    return role


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_role() is None:
            return _unauthenticated()
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles: str):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            role = session.get("role")   # login_required เพิ่งซิงก์ค่านี้จากฐานในคำขอนี้
            if not any(has_role(role, r) for r in roles):
                if _is_api():
                    return jsonify(error="ไม่มีสิทธิ์ใช้งานส่วนนี้"), 403
                return render_template(
                    "message.html", page_title="ไม่มีสิทธิ์",
                    message="บัญชีนี้ไม่มีสิทธิ์ใช้งานหน้านี้"), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def api_write_guard(view):
    """ครอบเส้นทางเขียนข้อมูล (POST/PUT/DELETE): ต้อง login (JSON 401),
    Content-Type เป็น application/json (415) และมีหัว X-Requested-With: dengue (403 csrf)
    — คำขอแบบ non-simple ที่ฟอร์มข้ามเว็บปลอมไม่ได้ (เสริม SameSite=Lax)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_role() is None:
            return jsonify(error="กรุณาเข้าสู่ระบบ"), 401
        if request.mimetype != "application/json":
            return jsonify(error="ต้องส่งเป็น application/json"), 415
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return jsonify(error="csrf"), 403
        return view(*args, **kwargs)
    return wrapped
