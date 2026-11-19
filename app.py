"""เว็บแอปแจ้งเตือนไข้เลือดออก SRRT — login, รายการเคส, /healthz (สเปกข้อ 8).

`create_app` สร้าง Flask app แล้วลงทะเบียนเส้นทางผ่านฟังก์ชันย่อย (`_register_*`)
เพื่อให้ task ถัดไป (หน้าเคส แผนที่ ตัวตั้งเวลา /check) เพิ่มเส้นทางได้โดยไม่ชนกัน.
ของที่ใช้ร่วมกันเก็บใน `app.config`: SETTINGS, STORE, SOURCE, SENDER, CYCLE_STATE
และ `app.extensions["login_throttle"]`.
"""
from __future__ import annotations

import logging
import re
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import alerts
from auth import (MAX_USERNAME_LEN, DengueSessionInterface, LoginThrottle, client_key,
                  form_csrf_ok, form_csrf_token, has_role, is_lan_client, login_required,
                  safe_next)
from case_views import register_case_routes
from facility_service import FacilityService
from facility_views import register_facility_routes
from check_views import register_check_routes
from disease_views import current_rule, register_disease_context
from config import Settings, load_settings, secret_key_problem
from map_views import register_map_routes
from settings_store import FILE_NAME as SETTINGS_FILE_NAME, effective_env
from settings_views import default_restart_hook, register_settings_routes
from store import Store
from trend_service import TrendService
from trend_views import register_trend_routes
from webfmt import (ROLE_LABELS, area_pill, area_text, icd_dot, pin_pill, sex_text,
                    thai_date, thai_date_long, thai_datetime)

log = logging.getLogger(__name__)

APP_NAME = "SRRT ไข้เลือดออก"
DEFAULT_SINCE_DAYS = 14
SINCE_PRESETS = (7, 14, 30, 90)
STATUS_CHOICES = ("active", "cancelled", "all")
AREA_CHOICES = ("in", "out", "all")
LOGIN_MESSAGE = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"
ERROR_MAX_CHARS = 120
ALERT_BAR_ENDPOINTS = ("index", "map_view")  # หน้าที่แสดงแถบ "ส่ง LINE ไม่สำเร็จ"


class PrefixMiddleware:
    """รองรับแอปหลัง reverse proxy แบบ sub-path (ดู CLAUDE.md ของโปรเจกต์).

    prefix ที่ไม่ขึ้นต้นด้วย "/" หรือมี "//" / ".." ถูกเมิน; X-Forwarded-Proto ใช้เฉพาะ
    รายการแรก และรับแค่ http/https."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "").strip()
        if prefix.startswith("/") and "//" not in prefix and ".." not in prefix:
            environ["SCRIPT_NAME"] = prefix.rstrip("/")
        proto = environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[0].strip().lower()
        if proto in ("http", "https"):
            environ["wsgi.url_scheme"] = proto
        return self.wsgi_app(environ, start_response)


def parse_filters(args) -> dict:
    """since/status/area จาก query string; ค่าที่ผิดรูปใช้ค่าเริ่มต้น (ไม่ล้ม)."""
    default_since = date.today() - timedelta(days=DEFAULT_SINCE_DAYS)
    try:
        since = date.fromisoformat((args.get("since") or "").strip())
    except ValueError:
        since = default_since
    status = args.get("status") if args.get("status") in STATUS_CHOICES else "active"
    area = args.get("area") if args.get("area") in AREA_CHOICES else "all"
    return {"since": since, "status": status, "area": area}


def same_village_groups(cases: list[dict]) -> list[dict]:
    """หมู่บ้านที่มีเคสใช้งานหมุด 'ประมาณ' ตั้งแต่ 2 รายขึ้นไป."""
    counts: dict[str, int] = defaultdict(int)
    for c in cases:
        if c["status"] == "active" and c["pin_state"] == "estimated" and c["village_code"]:
            counts[c["village_code"]] += 1
    return [{"village_code": v, "count": n, "area": area_text(v)}
            for v, n in sorted(counts.items()) if n >= 2]


# ข้อความที่ "หน้าตาเหมือน" error ของไดรเวอร์/การเชื่อมต่อฐานข้อมูล: มีรหัส MySQL ในวงเล็บ `(1045, ...)`, `'user'@'host'`,
# เลข IP หรือวลีมาตรฐานของ MySQL/MariaDB — ข้อความดิบพวกนี้พา user/host ของฐานข้อมูลออกมาด้วย
_DRIVER_CODE_RE = re.compile(r"\(\s*(\d{4})(?!\d)")
_DRIVER_LIKE_RE = re.compile(
    r"\(\s*\d{4}\s*,"                                   # (1045, "...")
    r"|'[^']*'\s*@\s*'[^']*'"                              # 'user'@'host'
    r"|(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])"       # IPv4
    r"|Access denied|Can't connect|MySQL server|MariaDB server|Lost connection|Unknown (?:MySQL )?database",
    re.I)
_ERROR_TYPE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout))(?![A-Za-z0-9_])")
MSG_DB_ERROR_FALLBACK = "ข้อผิดพลาดการเชื่อมต่อฐานข้อมูล"


def _driver_summary(msg: str) -> str | None:
    """ข้อความแบบ error ของไดรเวอร์ -> "<ชนิด> (<รหัส MySQL>)" เฉพาะชื่อชนิดกับรหัสที่พบ (ไม่มี user/host/IP/ข้อความดิบ);
    ไม่ใช่ข้อความแบบนี้ -> None; ใช่แต่ไม่พบทั้งชนิดและรหัส -> "" """
    if not _DRIVER_LIKE_RE.search(msg):
        return None
    kind = _ERROR_TYPE_RE.search(msg)
    code = _DRIVER_CODE_RE.search(msg)
    parts = [kind.group(1)] if kind else []
    if code:
        parts.append(f"({code.group(1)})")
    return " ".join(parts)


def sanitize_error(err: BaseException | str) -> str:
    """ข้อความสำหรับ /healthz ที่เปิดสาธารณะ: ชื่อชนิด exception + ข้อความไม่เกิน 120 ตัวอักษร
    เลขยาวเกิน 3 หลักถูกแทนด้วย "…" (กันหลุด HN/เลขบัตร) ไม่มีขึ้นบรรทัดใหม่.
    ข้อความที่เป็น error ของไดรเวอร์/การเชื่อมต่อ DB เก็บเฉพาะชื่อชนิดกับรหัส MySQL (เช่น "OperationalError (1045)")
    เพราะข้อความดิบมีชื่อผู้ใช้/โฮสต์/IP ของฐานข้อมูล; ข้อความไทยคงที่ของแอปผ่านออกไปตามเดิม"""
    msg = str(err)
    summary = _driver_summary(msg)
    if summary is not None:
        msg = summary or ("" if isinstance(err, BaseException) else MSG_DB_ERROR_FALLBACK)
    else:
        msg = re.sub(r"\d{4,}", "…", msg)
        msg = " ".join(msg.split())[:ERROR_MAX_CHARS]
    if isinstance(err, BaseException):
        name = type(err).__name__
        return f"{name}: {msg}" if msg else name
    return msg


def set_cycle_error(app: Flask, err: BaseException | str | None) -> None:
    """ตั้ง/ล้าง CYCLE_STATE["last_error"] ด้วยข้อความที่ผ่านการกรองแล้วเท่านั้น (None = ล้าง)."""
    app.config["CYCLE_STATE"]["last_error"] = None if err is None else sanitize_error(err)


def _alert_bars(store: Store, settings: Settings | None = None) -> dict:
    """จำนวนสำหรับแถบเตือนส่ง LINE: retrying = ยังลองใหม่อัตโนมัติ, given_up = เลิกส่งแล้ว
    (แยกกัน เพื่อไม่ให้แถบ "กำลังลองใหม่" ขัดกับแถบ "หยุดส่งแล้ว" สำหรับรายการเดียวกัน)
    แถบเตือนต้องไม่ทำให้หน้าหลักล่ม จึงจับ error ของฐานเป็นศูนย์ (log เฉพาะชื่อชนิด exception)."""
    try:
        h = alerts.health(store, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("อ่านสถานะแจ้งเตือนสำหรับแถบเตือนไม่สำเร็จ (%s)", type(exc).__name__)
        return {"retrying": 0, "given_up": 0}
    return {"retrying": h["failing"] - h["given_up"], "given_up": h["given_up"]}


# ---------- app factory ----------

def _default_env_provider(db_path: str) -> Callable[[], dict]:
    base = {"DENGUE_DB_PATH": db_path}   # ไม่ดึง env ของโปรเซส: ค่าเริ่มต้นนี้มีแต่ไฟล์ settings.json ของแอป
    return lambda: effective_env(base)


def create_app(settings: Settings | None = None, store: Store | None = None,
               source: object | None = None, sender: Callable | None = None,
               start_scheduler: bool = False) -> Flask:
    settings = settings if settings is not None else load_settings(effective_env())
    problem = secret_key_problem(settings.secret_key)
    if problem:  # ไม่ใส่ค่าลงในข้อความ
        raise RuntimeError(f"{problem} (แอปเปิดสู่อินเทอร์เน็ตผ่าน Cloudflare)")
    store = store if store is not None else Store(settings.db_path)

    app = Flask(__name__)
    app.wsgi_app = PrefixMiddleware(app.wsgi_app)
    app.session_interface = DengueSessionInterface()
    app.secret_key = settings.secret_key
    app.permanent_session_lifetime = timedelta(hours=8)
    app.config.update(
        SESSION_COOKIE_NAME="dengue_session",  # ไม่ชนคุกกี้ของแอปอื่นบนโดเมนเดียวกัน
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SETTINGS=settings,
        STORE=store,
        SOURCE=source,
        SENDER=sender,
        CYCLE_STATE={"last_cycle_at": None, "last_error": None},
        # /healthz: True เมื่อ wiring เริ่มตัวตั้งเวลา (ตั้งค่าโดย wiring.build_app); CLOCK ฉีดจากเทสต์ได้
        SCHEDULER_EXPECTED=False,
        # ผลอ่าน settings.json ตอนเริ่ม (wiring ตั้งให้): ข้อความ error (ชื่อคีย์เท่านั้น) หรือ None;
        # SETTINGS_BASE_ENV = env ก่อนซ้อนทับ ใช้ให้หน้า /check บอกแหล่งที่มา (ค่าเริ่มต้นเมื่อไม่ผ่าน wiring)
        SETTINGS_OVERLAY_ERROR=None,
        SETTINGS_BASE_ENV={"DENGUE_DB_PATH": settings.db_path},
        # หน้า /settings: ไฟล์ค่าตั้งอยู่โฟลเดอร์เดียวกับฐานข้อมูล; RESTART_HOOK ฉีดจากเทสต์ได้ (ห้ามฆ่าโปรเซสในเทสต์)
        SETTINGS_PATH=Path(settings.db_path).parent / SETTINGS_FILE_NAME,
        RESTART_HOOK=default_restart_hook,
        RESTART_DELAY=1.5,
        # ปุ่มทดสอบ/ด่านทดสอบ DB ของหน้า /settings: ENV_PROVIDER = ฟังก์ชันไม่มีอาร์กิวเมนต์คืน env ที่มีผลอยู่ตอนนี้
        # (ไม่ปิดความลับ; wiring ตั้งให้เป็น closure) — ค่าเริ่มต้นนี้ใช้เฉพาะตำแหน่ง settings.json ของแอปนี้.
        # CHECK_DEPS ฉีด connect/tables_source/tcp_check/sender จากเทสต์ (ว่าง = ของจริง)
        ENV_PROVIDER=_default_env_provider(settings.db_path),
        CHECK_DEPS={},
        CLOCK=datetime.now,
        # รหัสประจำโปรเซส (สุ่มครั้งเดียวตอนสร้างแอป ไม่ใช่ความลับแต่เดาไม่ได้): ใส่ใน /healthz และบล็อกข้อมูลของหน้า
        # /settings เพื่อให้หน้าเว็บรู้ว่าแอป "เริ่มใหม่แล้วจริง" (boot_id เปลี่ยน) แม้ ok จะเป็น false ด้วยเหตุอื่น
        BOOT_ID=secrets.token_hex(8),
    )
    app.config["STARTED_AT"] = app.config["CLOCK"]()
    # หน้า /trend: ไม่ผูกกับตัวตั้งเวลา; CLOCK เดียวกับแอป (เทสต์ฉีดเวลาได้)
    app.config["TREND"] = (TrendService(store, source, settings, clock=lambda: app.config["CLOCK"]())
                           if source is not None else None)
    # หมุด รพ./รพ.สต.: รายชื่อจาก HOSxP (ถ้ามี source) + พิกัดใน SQLite — สร้างเสมอ (source เป็น None ก็ใช้แถวใน SQLite ได้)
    app.config["FACILITIES"] = FacilityService(store, source, settings, trend=app.config["TREND"],
                                               clock=lambda: app.config["CLOCK"]())
    app.extensions["login_throttle"] = LoginThrottle()

    app.jinja_env.filters.update(
        thai_date=thai_date, thai_datetime=thai_datetime, icd=icd_dot,
        sex=sex_text, area=area_text, pin_pill=pin_pill, area_pill=area_pill)

    @app.context_processor
    def inject_globals():
        role = session.get("role")
        ctx = {}
        if request.endpoint in ALERT_BAR_ENDPOINTS:
            ctx["alert_bars"] = _alert_bars(app.config["STORE"], settings)
        return {
            **ctx,
            "app_name": APP_NAME,
            "audit_needed": not settings.data_audited,
            "today_long": thai_date_long(date.today()),
            "role_label": ROLE_LABELS.get(role, role or ""),
            "form_csrf_token": form_csrf_token,
            # ลิงก์ /check โผล่เฉพาะไคลเอนต์ใน LAN (ผู้ใช้สาธารณะไม่เห็นว่ามีหน้านี้)
            "show_check_link": is_lan_client(request, settings.lan_prefixes),
            # ลิงก์ "ตั้งค่า": ผู้ดูแล (admin) ที่เปิดจาก LAN เท่านั้น (เงื่อนไข LAN เดียวกับลิงก์ /check)
            "show_settings_link": has_role(role, "admin") and is_lan_client(request, settings.lan_prefixes),
        }

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":  # หน้าที่มีข้อมูลผู้ป่วยห้ามค้างใน cache
            resp.headers["Cache-Control"] = "no-store"
        return resp

    register_disease_context(app)
    _register_auth(app)
    _register_cases(app)
    register_case_routes(app)
    register_map_routes(app)
    register_facility_routes(app)
    register_trend_routes(app)
    register_check_routes(app)
    register_settings_routes(app)
    _register_health(app)
    return app


def _register_auth(app: Flask) -> None:
    throttle: LoginThrottle = app.extensions["login_throttle"]

    def render_login(error: str | None = None, status: int = 200):
        return render_template("login.html", page_title="เข้าสู่ระบบ", error=error,
                               next=request.values.get("next", "")), status

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if session.get("user"):
                return redirect(safe_next(request.args.get("next")) or url_for("index"))
            return render_login()

        if not form_csrf_ok():
            # หน้า login เก่า/คุกกี้หาย หรือฟอร์มจากเว็บอื่น — ไม่นับเป็นการเดารหัส
            return render_login("หน้าเข้าสู่ระบบหมดอายุ กรุณาลองใหม่อีกครั้ง", 400)
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        too_long = len(username) > MAX_USERNAME_LEN
        key = f"{username[:MAX_USERNAME_LEN].lower()}|{client_key()}"
        wait = throttle.retry_after(key)
        if wait:
            minutes = max(1, -(-wait // 60))
            return render_login(
                f"พยายามเข้าสู่ระบบผิดหลายครั้ง กรุณาลองใหม่ในอีก {minutes} นาที", 429)
        role = (None if too_long
                else app.config["STORE"].check_user(username, password))
        if role is None:
            throttle.fail(key)
            return render_login(LOGIN_MESSAGE)
        throttle.clear(key)
        session.clear()
        session.permanent = True
        session["user"] = username
        session["role"] = role
        return redirect(safe_next(request.form.get("next")) or url_for("index"))

    @app.post("/logout")
    def logout():
        if session.get("user") and not form_csrf_ok():
            # ฟอร์มจากเว็บอื่นสั่ง logout ไม่ได้ (ยังไม่ได้ login อยู่แล้ว = ไม่มีอะไรให้ป้องกัน)
            return render_template("message.html", page_title="ออกจากระบบไม่สำเร็จ",
                                   message="หน้านี้หมดอายุ กรุณากลับไปหน้าหลักแล้วกดออกจากระบบอีกครั้ง"), 400
        session.clear()
        return redirect(url_for("login"))


def _register_cases(app: Flask) -> None:
    @app.get("/")
    @login_required
    def index():
        store: Store = app.config["STORE"]
        f = parse_filters(request.args)
        rule = current_rule(app.config["SETTINGS"], request.args)
        disease = rule.key
        active = store.list_cases(f["since"], status="active", disease=disease)
        status = None if f["status"] == "all" else f["status"]
        in_area = {"in": True, "out": False}.get(f["area"])
        rows = store.list_cases(f["since"], status=status, in_area=in_area, disease=disease)
        kpis = {
            "active": len(active),
            # โรคกลุ่มก้อนไม่บังคับปักหมุด จึงไม่นับ "รอปักหมุด"
            "pending": sum(c["pin_state"] == "none" for c in active) if rule.mode == "per_case_pin" else 0,
            "confirmed": sum(c["pin_state"] == "confirmed" for c in active),
            "out": sum(not c["in_area"] for c in active),
        }
        today = date.today()
        presets = [(n, (today - timedelta(days=n)).isoformat()) for n in SINCE_PRESETS]
        custom_since = (f["since"].isoformat()
                        if f["since"].isoformat() not in [p[1] for p in presets] else None)
        return render_template(
            "cases.html", page_title="รายการเคส", cases=rows, kpis=kpis, f=f,
            since_iso=f["since"].isoformat(), presets=presets,
            custom_since=custom_since,
            # เตือน "ยังไม่ปักหมุดครบ" เฉพาะโรคที่ต้องปักหมุด (โรคกลุ่มก้อนจัดกลุ่มตามหมู่บ้าน ไม่ต้องปักหมุด)
            groups=same_village_groups(active) if rule.mode == "per_case_pin" else [])


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _scheduler_stale(app: Flask) -> bool:
    """ตัวตรวจจับควรทำงานแต่ไม่มีรอบเลยเกิน 3 x poll_seconds นับจากเริ่มแอป (ยังไม่เคยมีรอบ)
    หรือรอบล่าสุดเก่ากว่า 3 x poll_seconds."""
    if not app.config.get("SCHEDULER_EXPECTED"):
        return False
    limit = timedelta(seconds=3 * max(app.config["SETTINGS"].poll_seconds, 1))
    now = app.config["CLOCK"]()
    last = _as_datetime(app.config["CYCLE_STATE"].get("last_cycle_at"))
    return now - (last or app.config["STARTED_AT"]) > limit


def _register_health(app: Flask) -> None:
    @app.get("/healthz")
    def healthz():
        state = app.config["CYCLE_STATE"]
        error = state.get("last_error")
        store: Store = app.config["STORE"]
        try:
            h = alerts.health(store, app.config["SETTINGS"])
        except Exception:  # ฐานเปิดไม่ได้ ต้องไม่ทำให้ healthz ล่มเงียบ
            h, error = {"failing": 0, "given_up": 0}, error or "store error"
        # เปิดสาธารณะ: ไม่ใส่จำนวนเคส/ข้อมูลผู้ป่วย; ok=false ถ้าตัวตรวจจับไม่ทำงาน (ดู INSTALL.md watchdog)
        # settings_error: ไฟล์ค่าตั้งจากหน้าเว็บเสีย/มีบางค่าถูกทิ้ง (ไม่ใส่ข้อความ — เปิดสาธารณะ)
        settings_error = app.config.get("SETTINGS_OVERLAY_ERROR") is not None
        return jsonify(ok=(not error and h["given_up"] == 0 and not _scheduler_stale(app)
                           and not settings_error),
                       last_cycle_at=state.get("last_cycle_at"), last_error=error,
                       scheduler_expected=bool(app.config.get("SCHEDULER_EXPECTED")),
                       alerts_failing=h["failing"], alerts_given_up=h["given_up"],
                       settings_error=settings_error,
                       # boot_id: สุ่มต่อโปรเซส (ไม่ใช่ข้อมูลผู้ป่วย/ความลับ) — หน้า /settings เทียบกับค่าที่ได้ตอนโหลด
                       # หน้าเพื่อตรวจว่าเริ่มระบบใหม่แล้ว
                       boot_id=app.config["BOOT_ID"])
