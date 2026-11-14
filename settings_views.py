"""หน้า `/settings` และ API บันทึก/ย้อนกลับค่าตั้ง — เฉพาะผู้ดูแล (admin) ในเครือข่าย LAN (สเปก §2, §4-§8).

ส่วนนี้เขียนรหัสผ่านฐานข้อมูลและ key MOPH จึงเข้มงวด:
- ด่านทุกเส้นทาง (นอกสุดก่อน): `lan_only` -> `login_required` -> `role_required("admin")` -> (POST) `api_write_guard`
- เขียนได้เมื่อใส่รหัสผ่านของตัวเองซ้ำ (นับในตัวจำกัด `settings:<user>`: ผิด 5 ครั้งใน 10 นาที -> ล็อก 10 นาที)
- ค่าจากคำขอ **ไม่เคย** ถูก log / ส่งกลับ / ใส่ใน audit / อยู่ในข้อความ exception — log เฉพาะชื่อชนิด exception
  และ audit เก็บเฉพาะ "ชื่อคีย์" (คีย์ที่ไม่รู้จักไม่ถูกสะท้อนกลับ)
- การเขียนไฟล์ถูกเรียงลำดับด้วยล็อกระดับโมดูล (`settings_store` ไม่ปลอดภัยต่อการเขียนพร้อมกัน; production ใช้ workers=1)
- ด่าน "แก้ค่า DB_* ต้องทดสอบก่อน" ทำฝั่งเซิร์ฟเวอร์: บันทึกที่เปลี่ยน DB_* (และไม่ได้ force) จะรัน `run_db_test` กับค่ารวม
  (ค่าปัจจุบันจาก `ENV_PROVIDER` + ค่าที่ตรวจแล้ว) ก่อนถือ lock เขียน — ขั้นใด fail = 409 `untested` ไม่เขียนอะไร;
  ธงจากหน้าเว็บ (`tested_ok`) ไม่ถูกใช้
- `POST /api/settings/test` และ `/api/settings/send-test` ใช้ด่านเดียวกัน (ไม่ต้องใส่รหัสผ่านซ้ำเพราะไม่แก้ค่า) + ตัวจำกัดความถี่
  ต่อผู้ใช้ (10 ครั้ง/นาที และ 3 ครั้ง/นาทีสำหรับส่งจริง); ผลเป็นขั้นตอนที่ปลอดภัยโดยโครงสร้าง (ดู settings_checks)

หลังบันทึก/ย้อนกลับสำเร็จ แอปต้องเริ่มใหม่เพื่อใช้ค่าใหม่: ใต้ gunicorn (ตรวจจาก WSGI environ `SERVER_SOFTWARE`
ของคำขอนั้น) ตั้ง `threading.Timer(RESTART_DELAY, RESTART_HOOK)` ครั้งเดียว (ตอบ "scheduled") — hook เริ่มต้นส่ง SIGTERM
ให้ตัวเอง แล้ว master/systemd ตั้ง worker ใหม่; ไม่ใช่ gunicorn (เช่น `python run.py`) ตอบ "manual" และไม่แตะโปรเซส.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import signal
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from pathlib import Path

from werkzeug.exceptions import RequestEntityTooLarge
from flask import Flask, jsonify, render_template, request, session, url_for

import config
import settings_checks
import settings_store
from auth import api_write_guard, lan_only, login_required, role_required
from diseases import GROUP_LABELS, MODE_LABELS, REGISTRY, env_names
from store import Store
from webfmt import thai_datetime

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 65536  # คำขอจริงไม่กี่ร้อยไบต์ — ปฏิเสธก่อนอ่าน/แปลง JSON (worker เดียวที่ต้องรันตัวตรวจจับด้วย)
MAX_PASSWORD_LEN = 1024
MAX_KEYS = 40
# ช่องที่ตั้งค่าเริ่มต้นไว้ใน config.py — describe() รายงานว่า unset จึงต้องดึงค่าที่แอปใช้จริงจาก Settings
SETTINGS_FALLBACK = ("DENGUE_START_FROM", "DENGUE_BASE_URL", "DENGUE_DATA_AUDITED", "DENGUE_DISEASES")

MSG_PASSWORD = "รหัสผ่านไม่ถูกต้อง"
MSG_UNKNOWN_KEY = "ไม่รู้จักตัวแปรที่ขอตั้ง"
MSG_CLEAR_KEY = "ล้างค่าได้เฉพาะช่องรหัสลับเท่านั้น"
MSG_CLEAR_CONFLICT = "ใส่ค่าใหม่และสั่งล้างค่าช่องเดียวกันพร้อมกันไม่ได้"
MSG_TOO_LARGE = "คำขอใหญ่เกินกำหนด ไม่ได้ประมวลผล"
MSG_SERVER = "เกิดข้อผิดพลาดภายในระบบ ไม่ได้บันทึกค่า กรุณาลองใหม่ หรือแจ้งผู้ดูแลระบบ"
MSG_REVERT_DETAIL = "ย้อนกลับค่าก่อนหน้า"
MSG_RATE_LIMITED = {
    "test": "กดทดสอบถี่เกินไป (ไม่เกิน 10 ครั้งต่อนาที) กรุณารอสักครู่แล้วลองใหม่",
    "send": "ส่งข้อความทดสอบถี่เกินไป (ไม่เกิน 3 ครั้งต่อนาที) กรุณารอสักครู่แล้วลองใหม่",
}
MSG_PASSWORD_REQUIRED = "กรอกรหัสผ่านฐานข้อมูลใหม่เมื่อเปลี่ยนโฮสต์ พอร์ต หรือผู้ใช้"
MSG_SEND_OK = "ส่งข้อความทดสอบสำเร็จ"
MSG_SEND_FAILED = "ส่งข้อความทดสอบไม่สำเร็จ"
TEST_TARGETS = ("db", "moph")
# ข้อความเตือนที่หน้าจอแสดงเมื่อ DENGUE_START_FROM ย้อนหลังและมีกุญแจ MOPH (JS เป็นผู้คำนวณเงื่อนไข จาก server_today)
MSG_CONFLICT = "ค่าถูกเปลี่ยนโดยผู้ดูแลคนอื่นระหว่างบันทึก โหลดหน้าใหม่แล้วลองอีกครั้ง"
# แถบเตือนเมื่อเปิดหน้านี้ผ่าน http ธรรมดาจากเครื่องอื่น (ไม่ใช่ loopback): รหัสผ่านฐานข้อมูล/กุญแจ MOPH/รหัสผ่านผู้ดูแลวิ่งเป็นข้อความธรรมดา
MSG_INSECURE_TRANSPORT = ("การเชื่อมต่อนี้ไม่เข้ารหัส (http) — รหัสผ่านฐานข้อมูลและกุญแจ MOPH จะวิ่งผ่านเครือข่ายเป็นข้อความธรรมดา "
                          "แนะนำให้เปิดผ่าน SSH tunnel (ดูคู่มือข้อ 4.7)")
SCHEDULER_STOP_WAIT_S = 20   # hook รีสตาร์ตรอรอบของตัวตั้งเวลาจบไม่เกินนี้ แล้ว SIGTERM ต่อไม่ว่าจบหรือไม่
MSG_START_FROM_PAST = ("ตั้งวันเริ่มย้อนหลัง — เคสที่สะสมไว้จะถูกส่งเข้ากลุ่มรวดเดียวเมื่อมีกุญแจ MOPH "
                       "(ถ้าเป็นกลุ่มจริง ให้ตั้งเป็นวัน go-live)")

# สลับ settings.json แบบ อ่าน-รวม-เขียน ต้องไม่ซ้อนกัน (หลายคำขอพร้อมกันจะทำให้ค่าของอีกฝ่ายหาย)
_WRITE_LOCK = threading.Lock()


class SlidingWindowLimiter:
    """ตัวจำกัดความถี่แบบหน้าต่างเลื่อนในหน่วยความจำ ต่อ (ชนิด, ผู้ใช้): ไม่เกิน `limits[kind]` ครั้งใน `window` วินาที.
    `clock` ฉีดจากเทสต์ได้ · ล็อกกันเธรดแย่งกัน (gunicorn workers=1 แต่ Flask dev server ใช้หลายเธรด)"""

    LIMITS = {"test": 10, "send": 3}
    WINDOW = 60.0
    MAX_KEYS = 5000

    def __init__(self, clock=time.monotonic, limits: dict | None = None, window: float | None = None):
        self.clock = clock
        self.limits = dict(limits or self.LIMITS)
        self.window = self.WINDOW if window is None else window
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def allow(self, kind: str, user: str) -> bool:
        """True = อนุญาตและนับครั้งนี้; False = เกินแล้ว (ไม่นับ)."""
        limit = self.limits[kind]
        with self._lock:
            now = self.clock()
            if len(self._hits) > self.MAX_KEYS:
                self._purge(now)
            hits = self._hits[(kind, user)]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def _purge(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window]:
            del self._hits[key]


def default_restart_hook(app: Flask | None = None, wait: float = SCHEDULER_STOP_WAIT_S) -> None:
    """หยุดตัวตั้งเวลาอย่างสุภาพแล้วส่ง SIGTERM ให้โปรเซสตัวเอง (gunicorn worker ปิด แล้ว master/systemd ตั้งตัวใหม่ขึ้นมา).

    ส่ง SIGTERM กลางรอบตรวจจับอาจตัดการส่ง LINE ครึ่งทาง (รอบถัดไปส่งซ้ำ = ข้อความซ้ำในกลุ่ม) จึงสั่ง
    `app.config["SCHEDULER_STOP"]` ก่อน แล้ว join `SCHEDULER_THREAD` ไม่เกิน `wait` วินาที (รอบที่กำลังทำจบเอง) —
    รอบที่ค้างเกินกำหนดก็ถูกตัดตามเดิม (ไม่ค้างรีสตาร์ตตลอดไป). ไม่มี app / ไม่มีตัวตั้งเวลา = ส่ง SIGTERM ทันที.
    Timer เรียกพร้อม app (`wants_app`); hook ที่ฉีดจากเทสต์ยังเป็นฟังก์ชันไม่มีอาร์กิวเมนต์"""
    if app is not None:
        stop = app.config.get("SCHEDULER_STOP")
        thread = app.config.get("SCHEDULER_THREAD")
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=wait)
    os.kill(os.getpid(), signal.SIGTERM)


default_restart_hook.wants_app = True


def clean_username(raw) -> str:
    """ชื่อผู้ใช้สำหรับเขียนลงไฟล์/audit: ตัดอักขระควบคุมและ surrogate เดี่ยว (เข้ารหัส UTF-8 ไม่ได้) ยาวไม่เกิน 64"""
    text = "".join(c for c in str(raw)
                   if not (ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF))
    return text[:64] or "unknown"


def _bad_request():
    return jsonify(error="bad_request"), 400


def _invalid(key: str | None, message: str):
    # ไม่สะท้อนชื่อคีย์ที่ผู้ใช้ส่งมาแต่ไม่อยู่ใน ALLOWED (กันสะท้อนข้อความตามใจกลับไป)
    safe_key = key if key in settings_store.ALLOWED or key == "file" else None
    return jsonify(error="invalid", key=safe_key, message=message), 400


def _no_fail(steps: list[dict]) -> bool:
    return not any(st["status"] == settings_checks.FAIL for st in steps)


def _is_bool(value) -> bool:
    return isinstance(value, bool)


def _read_json_body():
    """อ่าน JSON ของคำขอโดยจำกัดขนาด -> (body, None) หรือ (None, response 413).

    ปฏิเสธเมื่อไม่มี Content-Length (เช่น chunked) หรือใหญ่เกิน MAX_BODY_BYTES ก่อนอ่านตัวคำขอ; และตั้ง
    `request.max_content_length` ให้ stream ของ werkzeug ตัดที่ขีดจำกัดเดียวกัน (ไคลเอนต์ที่ส่งเกินที่แจ้งก็ผ่านไม่ได้)
    คำขอที่ถูกปฏิเสธไม่ถูกแปลง JSON ไม่ผ่านตรวจรหัสผ่าน/ตัวจำกัด"""
    too_large = (jsonify(error="too_large", message=MSG_TOO_LARGE), 413)
    length = request.content_length
    if length is None or length > MAX_BODY_BYTES:
        return None, too_large
    request.max_content_length = MAX_BODY_BYTES
    try:
        raw = request.get_data(cache=True)
    except RequestEntityTooLarge:
        return None, too_large
    if len(raw) > MAX_BODY_BYTES:
        return None, too_large
    try:
        return json.loads(raw), None
    except ValueError:  # JSONDecodeError และ UnicodeDecodeError
        return None, None


def _parse_save_body(body):
    """-> (values, clear, password, force) หรือ None เมื่อรูปแบบผิด (ฟิลด์ `tested_ok` เก่าถูกเมิน — ไม่เชื่อธงจากหน้าเว็บ)"""
    if not isinstance(body, dict):
        return None
    values = body.get("values", {})
    clear = body.get("clear", [])
    password = body.get("password", "")
    force = body.get("force", False)
    if (not isinstance(values, dict) or len(values) > MAX_KEYS
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())):
        return None
    if (not isinstance(clear, list) or len(clear) > MAX_KEYS
            or not all(isinstance(k, str) for k in clear)):
        return None
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LEN:
        return None
    if not _is_bool(force):
        return None
    return values, clear, password, force


def _parse_revert_body(body):
    if not isinstance(body, dict):
        return None
    password = body.get("password", "")
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LEN:
        return None
    return password


def _throttle_key(username: str) -> tuple[str, str]:
    """คีย์ตัวจำกัดของหน้านี้: tuple จึงไม่ชนคีย์ข้อความ `user|ip` ของ /login ไม่ว่าชื่อผู้ใช้เป็นอย่างไร"""
    return ("settings", clean_username(username).lower())


def _verify_password(app: Flask, username: str, password: str):
    """ตรวจรหัสผ่านของผู้ใช้ที่ล็อกอินอยู่ (ผ่านตัวจำกัดคีย์ `settings:<user>`) -> None เมื่อผ่าน หรือ response ปฏิเสธ"""
    throttle = app.extensions["login_throttle"]
    key = _throttle_key(username)
    wait = throttle.retry_after(key)
    if wait:
        minutes = max(1, -(-wait // 60))
        return jsonify(error="throttled", retry_after=wait,
                       message=f"ใส่รหัสผ่านผิดหลายครั้ง กรุณาลองใหม่ในอีก {minutes} นาที"), 429
    role = None
    if password and _encodable(password):  # รหัสที่เข้ารหัส UTF-8 ไม่ได้ (surrogate เดี่ยว) = รหัสผิด ไม่ใช่ 500
        role = app.config["STORE"].check_user(username, password)
    if role is None:
        throttle.fail(key)
        return jsonify(error="password", message=MSG_PASSWORD), 403
    throttle.clear(key)
    return None


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _run_hook(app: Flask, hook) -> None:
    try:
        hook(app) if getattr(hook, "wants_app", False) else hook()
    except Exception as exc:  # noqa: BLE001 — เธรด Timer: ห้ามล้มเงียบพร้อมข้อความที่อาจมีค่า
        log.error("settings restart hook failed: %s", type(exc).__name__)
        _set_restart_pending(app, False)  # ไม่รีสตาร์ตจริง: ให้การบันทึกครั้งถัดไปตั้ง Timer ใหม่ได้


def _restart_state(app: Flask) -> dict:
    return app.extensions.setdefault("settings_restart", {"pending": False})


def _set_restart_pending(app: Flask, value: bool) -> None:
    with _WRITE_LOCK:
        _restart_state(app)["pending"] = value


def _schedule_restart(app: Flask) -> str:
    """"scheduled" (ใต้ gunicorn) หรือ "manual" (ต้องให้ผู้ดูแลเริ่มบริการใหม่เอง).

    Timer ถูกตั้งครั้งเดียวต่อแอป: บันทึกครั้งแรกตั้ง Timer และตั้งธง pending; บันทึกถัดมาระหว่างรอ ยังเขียนไฟล์
    แต่ตอบ "scheduled" โดยไม่ตั้ง Timer ซ้ำ. ธงถูกล้างเฉพาะเมื่อ hook ล้มเหลว (กัน UI ถูกบอก scheduled ตลอดไป)"""
    if not request.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
        return "manual"
    with _WRITE_LOCK:
        state = _restart_state(app)
        if state["pending"]:
            return "scheduled"
        try:
            timer = threading.Timer(float(app.config["RESTART_DELAY"]), _run_hook,
                                    args=(app, app.config["RESTART_HOOK"]))
            timer.daemon = True
            timer.start()
        except Exception as exc:  # noqa: BLE001
            log.error("settings restart not scheduled: %s", type(exc).__name__)
            return "manual"
        state["pending"] = True
    return "scheduled"


def _audit(app: Flask, action: str, user: str, detail: str) -> None:
    """บันทึก audit — ค่าถูกเขียนลงไฟล์ไปแล้ว จึงไม่ให้ความล้มเหลวของ audit ทำให้การรีสตาร์ตหาย (log ชนิด exception)"""
    try:
        app.config["STORE"].add_audit(action, user, detail=detail)
    except Exception as exc:  # noqa: BLE001
        log.error("settings audit write failed (action=%s): %s", action, type(exc).__name__)


def _guarded(view):
    """ข้อผิดพลาดที่ไม่คาดคิด -> 500 ข้อความไทยคงที่ (log เฉพาะชื่อชนิด ไม่มีข้อความ/ค่า)"""
    @wraps(view)
    def wrapped():
        try:
            return view()
        except Exception as exc:  # noqa: BLE001
            log.error("settings request failed: %s", type(exc).__name__)
            return jsonify(error="server", message=MSG_SERVER), 500
    return wrapped


def _guarded_page(view):
    """เหมือน _guarded แต่ตอบหน้า HTML ภาษาไทยคงที่ (หน้า GET)"""
    @wraps(view)
    def wrapped():
        try:
            return view()
        except Exception as exc:  # noqa: BLE001
            log.error("settings page failed: %s", type(exc).__name__)
            try:
                return render_template("message.html", page_title="เกิดข้อผิดพลาด", message=MSG_SERVER), 500
            except Exception:  # noqa: BLE001
                return MSG_SERVER, 500, {"Content-Type": "text/plain; charset=utf-8"}
    return wrapped


def _db_deps(app: Flask) -> dict:
    deps = app.config.get("CHECK_DEPS") or {}
    return {"connect": deps.get("connect"), "tables_source": deps.get("tables_source")}


def _is_blank_secret(key: str, value: str) -> bool:
    """ช่องลับที่เว้นว่าง = ใช้ค่าที่ตั้งอยู่ (DB_PASS เว้นวรรคล้วนเป็นรหัสที่ถูกต้อง)"""
    return key in settings_store.SECRET_KEYS and (value == "" or (key != "DB_PASS" and not value.strip()))


def _norm_port(value) -> str:
    """พอร์ตที่แอปจะใช้ต่อจริง — ใช้ตัวแปลงเดียวกับ production (wiring._port: int(strip) ไม่งั้น 3306)
    เพื่อให้ "3_307" / "+3307" ใน env ฐานเทียบกับค่าใหม่ได้ตรงกับที่ make_connect จะเห็น"""
    from wiring import _port   # import ตอนใช้: wiring -> app -> settings_views
    return str(_port(str(value) if value is not None else ""))


def _db_endpoint(env) -> tuple[str, str, str]:
    return (str(env.get("DB_HOST") or "").strip(), _norm_port(env.get("DB_PORT")),
            str(env.get("DB_USER") or "").strip())


def _password_required(current, new, supplied_password: bool) -> bool:
    """R8: เปลี่ยนโฮสต์/พอร์ต/ผู้ใช้เทียบกับค่าที่มีผลอยู่ โดยไม่ได้ส่ง DB_PASS ใหม่ (ไม่ว่าง) มาในคำขอเดียวกัน
    = ห้ามเอารหัสที่เก็บไว้ไปต่อปลายทางใหม่ (ผู้ฟังปลอมที่ผู้ใช้พิมพ์ที่อยู่ให้จะเก็บรหัสจริงได้)"""
    return not supplied_password and _db_endpoint(current) != _db_endpoint(new)


def _password_required_response():
    return jsonify(error="password_required", message=MSG_PASSWORD_REQUIRED), 400


def _db_gate_conflict(app: Flask, gate_env, changes: dict, clear_keys: list) -> bool:
    """M-1: เรียกใน _WRITE_LOCK ก่อนเขียน — ตรวจซ้ำกติการหัสผ่านกับค่าที่มีผลอยู่ "ตอนนี้".

    ด่าน (รหัสผ่านใหม่/ทดสอบ DB) ตัดสินจาก env ณ ตอนนั้นก่อนถือ lock; ถ้าผู้ดูแลอีกคนบันทึกแทรกระหว่างนั้น
    (เช่น A เปลี่ยนโฮสต์+รหัส ส่วน B ส่งแค่รหัสใหม่ซึ่งผ่านด่านเพราะ "ไม่ได้เปลี่ยนโฮสต์") รหัสของ B จะไปอยู่กับโฮสต์ที่ B
    ไม่เคยทดสอบ/ไม่ได้ตั้งใจ. จึงถือเป็นความขัดแย้งเมื่อปลายทาง (โฮสต์/พอร์ต/ผู้ใช้) ที่มีผลอยู่ต่างจากที่ด่านเห็น
    หรือกติการหัสผ่านใหม่ไม่ผ่านกับค่าปัจจุบัน — ไม่เขียนอะไร (ผู้ใช้โหลดหน้าใหม่แล้วลองอีกครั้ง)"""
    fresh = dict(app.config["ENV_PROVIDER"]())
    merged = {**fresh, **changes, **{k: "" for k in clear_keys}}
    return (_db_endpoint(fresh) != _db_endpoint(gate_env)
            or _password_required(fresh, merged, "DB_PASS" in changes))


def _parse_values(raw):
    """`values` ของปุ่มทดสอบ -> dict[str, str] หรือ None เมื่อรูปแบบผิด"""
    if not isinstance(raw, dict) or len(raw) > MAX_KEYS:
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        return None
    return raw


def _test_env(app: Flask, values: dict):
    """env สำหรับทดสอบ = ค่าที่มีผลอยู่ (ENV_PROVIDER ไม่ปิดความลับ) ทับด้วย `values` ที่ผ่าน validate
    -> (env, current, None) หรือ (None, None, response 400); `current` = ค่าที่มีผลอยู่ก่อนทับ.
    ช่องลับว่าง = ใช้ค่าเดิม; ไม่เขียนอะไรลงไฟล์."""
    for key in values:
        if key not in settings_store.ALLOWED:
            return None, None, _invalid(None, MSG_UNKNOWN_KEY)
    current = dict(app.config["ENV_PROVIDER"]())
    env = dict(current)
    for key, value in values.items():
        if _is_blank_secret(key, value):
            continue
        try:
            env[key] = settings_store.validate(key, value)
        except settings_store.SettingsError as err:
            return None, None, _invalid(err.key, str(err))
    if not str(env.get("MOPH_BASE_URL") or "").strip():
        env["MOPH_BASE_URL"] = app.config["SETTINGS"].moph_base_url
    return env, current, None


def _rate_limited(app: Flask, kind: str, username: str):
    limiter: SlidingWindowLimiter = app.extensions["settings_test_limiter"]
    if limiter.allow(kind, clean_username(username).lower()):
        return None
    return jsonify(error="rate_limited", message=MSG_RATE_LIMITED[kind]), 429


def _page_items(app: Flask) -> tuple[list[dict], settings_store.Overlay]:
    """รายการต่อตัวแปรสำหรับหน้าจอ (ไม่มีค่าลับ) + overlay ปัจจุบัน (ไว้เอา updated_*/has_prev/error)"""
    path: Path = app.config["SETTINGS_PATH"]
    settings = app.config["SETTINGS"]
    base = app.config.get("SETTINGS_BASE_ENV") or {}
    # เฉพาะตัวแปรที่ describe ใช้ (ไม่ส่งทั้ง env ต่อ) และชี้ไปไฟล์เดียวกับที่ view นี้เขียน
    env = {k: base[k] for k in settings_store.ALLOWED if k in base}
    env["DENGUE_DB_PATH"] = str(path.parent / "dengue.db")
    info = settings_store.describe(env)
    effective = {
        "DENGUE_START_FROM": settings.start_from.isoformat(),
        "DENGUE_BASE_URL": settings.base_url,
        "DENGUE_DATA_AUDITED": "1" if settings.data_audited else "0",
        "DENGUE_DISEASES": ",".join(r.key for r in settings.enabled_rules()),
    }
    items = []
    for key, d in info.items():
        item = {"key": key, "label": d["label"], "secret": d["secret"], "state": d["state"],
                "source": d["source"], "value": None}
        if not d["secret"]:
            item["value"] = d["value"]
            if d["value"] is None and key in SETTINGS_FALLBACK:
                # ค่าเริ่มต้นของ config (state/source ยังบอกว่า unset/default): ส่งเป็น `fallback` เสมอ ส่วน `value` —
                # DENGUE_START_FROM (วันเริ่มระบบ เลื่อนทุกครั้งที่ restart) ต้องแสดงช่องว่าง ไม่ใช่ทำเหมือนตั้งไว้แล้ว
                # (ไม่งั้นพิมพ์ซ้ำวันเดิมไม่ได้ = ตรึง go-live ไม่ได้); BASE_URL / DATA_AUDITED คงแสดงค่าที่แอปใช้
                item["fallback"] = effective[key]
                if key != "DENGUE_START_FROM":
                    item["value"] = effective[key]
        items.append(item)
    return items, settings_store.read_overlay(path)


def _is_loopback(raw) -> bool:
    try:
        ip = ipaddress.ip_address(str(raw or "").strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _insecure_transport() -> bool:
    """True = คำขอนี้ไม่ใช่ https และไคลเอนต์ไม่ใช่เครื่องเดียวกัน -> ความลับที่พิมพ์ในหน้านี้วิ่งเป็นข้อความธรรมดา.

    `request.is_secure` เชื่อ X-Forwarded-Proto หลัง PrefixMiddleware (Apache ที่ฟัง 443 ส่ง https). เข้าผ่าน SSH tunnel
    (ถึง 127.0.0.1:6800 โดยตรง) ถือว่าปลอดภัย. remote_addr เป็น loopback อย่างเดียวไม่พอ: proxy บนเครื่องเดียวกันที่ส่งต่อ
    ไคลเอนต์ LAN ผ่าน http จะมี X-Forwarded-For -> ใช้ที่อยู่ท้ายสุดของหัวนั้น (ตัวเดียวกับด่าน LAN) ถ้ามี"""
    if request.is_secure:
        return False
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded is not None:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        client = parts[-1] if parts else ""
        return not (_is_loopback(client) and _is_loopback(request.remote_addr))
    return not _is_loopback(request.remote_addr)


def _stamp_text(stamp: str | None) -> str | None:
    """ISO ของ updated_at -> "9 พ.ย. 2569 14:05" (พ.ศ.); อ่านไม่ได้ = None"""
    try:
        return thai_datetime(datetime.fromisoformat(stamp)) if stamp else None
    except ValueError:
        return None


def _disease_rule_rows(settings) -> list[dict]:
    """กฎที่ใช้จริงของทุกโรค (อ่านอย่างเดียว) + ชื่อ env ที่ปรับได้ — ไม่มีข้อมูลผู้ป่วย."""
    rows = []
    for disease in REGISTRY:   # ทุกโรค (ที่ปิดอยู่ติดป้าย) — ดูเกณฑ์/รหัสก่อนตัดสินใจเปิด
        rule = settings.rule(disease.key)
        if rule is None:
            continue
        d = rule.disease
        rows.append({"key": rule.key, "name": d.name_th, "enabled": rule.enabled, "icds": ", ".join(rule.icds),
                     "mode": MODE_LABELS[d.mode], "group": GROUP_LABELS[d.group_by],
                     "min_cases": rule.min_cases, "days": rule.days,
                     "radius_m": rule.radius_m if d.group_by == "pin_radius" else None,
                     "env": env_names(rule.key)})
    return rows


def register_settings_routes(app: Flask) -> None:
    app.config.setdefault("RESTART_HOOK", default_restart_hook)
    app.config.setdefault("RESTART_DELAY", 1.5)
    app.extensions.setdefault("settings_test_limiter", SlidingWindowLimiter())

    @app.get("/settings")
    @lan_only
    @login_required
    @role_required("admin")
    @_guarded_page
    def settings_page():
        items, overlay = _page_items(app)
        start_from_default = any(i["key"] == "DENGUE_START_FROM" and i["source"] == "default" for i in items)
        data = {
            "items": items,
            "updated_at": overlay.updated_at,
            "updated_by": overlay.updated_by,
            "updated_at_text": _stamp_text(overlay.updated_at),
            "has_prev": overlay.has_prev,
            "overlay_error": overlay.error,
            # ข้อมูลล้วน ๆ สำหรับ static/settings.js (ไม่มีค่าลับ): URL ผ่าน url_for (รองรับ sub-path /dengue),
            # วันนี้ของเซิร์ฟเวอร์ (CLOCK) ใช้เทียบเตือน DENGUE_START_FROM ย้อนหลัง
            "save_url": url_for("settings_save"),
            "revert_url": url_for("settings_revert"),
            "test_url": url_for("settings_test"),
            "send_test_url": url_for("settings_send_test"),
            "healthz_url": url_for("healthz"),
            "login_url": url_for("login", next=url_for("settings_page")),
            "server_today": app.config["CLOCK"]().date().isoformat(),
            "start_from_warning": MSG_START_FROM_PAST,
            "boot_id": app.config["BOOT_ID"],   # ค่าเดียวกับ /healthz ตอนนี้ — หน้าใช้ตรวจว่าเริ่มระบบใหม่แล้ว
            # I-1: เปิดผ่าน http จากเครื่องอื่น -> แถบเตือนสีส้ม role=alert (ข้อความอยู่ในเทมเพลต/ค่าคงที่ฝั่งเซิร์ฟเวอร์)
            "insecure_transport": _insecure_transport(),
            "insecure_transport_warning": MSG_INSECURE_TRANSPORT,
            # I-2: START_FROM ยังไม่ตั้ง (ใช้วันเริ่มระบบ ซึ่งเลื่อนทุกครั้งที่ restart) -> เตือน + ช่องกรอกว่าง
            "start_from_default": start_from_default,
            "start_from_unset_warning": config.MSG_START_FROM_UNSET,
        }
        return render_template("settings.html", page_title="ตั้งค่า", settings_data=data,
                               disease_rules=_disease_rule_rows(app.config["SETTINGS"]))

    @app.post("/api/settings/save")
    @lan_only
    @login_required
    @role_required("admin")
    @api_write_guard
    @_guarded
    def settings_save():
        body, too_large = _read_json_body()
        if too_large is not None:
            return too_large
        parsed = _parse_save_body(body)
        if parsed is None:
            return _bad_request()
        values, clear, password, force = parsed
        username = session["user"]
        denied = _verify_password(app, username, password)
        if denied is not None:
            return denied

        # ตรวจชื่อคีย์ทั้งหมดก่อน: ไม่รู้จัก = ปฏิเสธ (ไม่ทิ้งเงียบ ๆ)
        for key in values:
            if key not in settings_store.ALLOWED:
                return _invalid(None, MSG_UNKNOWN_KEY)
        for key in clear:
            if key not in settings_store.SECRET_KEYS:
                return _invalid(key, MSG_CLEAR_KEY if key in settings_store.ALLOWED else MSG_UNKNOWN_KEY)
        clear_keys = list(dict.fromkeys(clear))

        # ค่าที่มีผลอยู่ตอนนี้ (ตัวที่ไม่ลับ) — ส่งค่าเดิมซ้ำ = ไม่เปลี่ยน (ความลับเทียบไม่ได้: เปลี่ยนเมื่อไม่ว่างหรือสั่งล้างเท่านั้น)
        shown = [i for i in _page_items(app)[0] if not i["secret"]]
        current = {i["key"]: i["value"] for i in shown}
        # ค่าเริ่มต้นของ config (source=default) ไม่ใช่ "ค่าที่ตั้งไว้": ส่งมา = ตั้งค่าจริงเสมอ (ไม่ข้ามเมื่อเท่าค่าสำรอง)
        defaulted = {i["key"] for i in shown if i["source"] == "default"}
        changes: dict[str, str] = {}
        for key, value in values.items():
            if _is_blank_secret(key, value):
                continue  # ช่องลับว่าง = ไม่เปลี่ยน (DB_PASS เว้นวรรคล้วนเป็นรหัสที่ถูกต้อง)
            if key in clear_keys:
                return _invalid(key, MSG_CLEAR_CONFLICT)
            try:
                new_value = settings_store.validate(key, value)
            except settings_store.SettingsError as err:
                return _invalid(err.key, str(err))
            if (key not in settings_store.SECRET_KEYS and key not in defaulted
                    and current.get(key) == new_value):
                continue  # เท่าค่าที่ตั้งไว้จริง (overlay/env): ไม่เขียน ไม่ติดด่าน DB ไม่รีสตาร์ต
            changes[key] = new_value

        changed = [*changes, *clear_keys]
        if not changed:
            return jsonify(ok=True, changed=[], restart="none")
        gate_env = None   # env ที่ใช้ตัดสินด่าน DB (None = บันทึกนี้ไม่แตะ DB_*) — ตรวจซ้ำใน lock (M-1)
        if any(k.startswith("DB_") for k in changed):
            current = dict(app.config["ENV_PROVIDER"]())
            gate_env = current
            merged = dict(current)   # ค่ารวม: ปัจจุบัน + ที่เปลี่ยน, ที่สั่งล้างเป็นค่าว่าง
            merged.update(changes)
            merged.update({k: "" for k in clear_keys})
            # R8 (force ข้ามไม่ได้): เปลี่ยนโฮสต์/พอร์ต/ผู้ใช้ต้องส่งรหัสใหม่มาด้วย ไม่งั้นรหัสที่เก็บไว้จะถูกแอปส่งไปปลายทางใหม่
            # (`changes` ไม่มีช่องลับที่ว่าง — "DB_PASS" อยู่ในนั้นเมื่อส่งรหัสใหม่มาจริง)
            if _password_required(current, merged, "DB_PASS" in changes):
                return _password_required_response()
            if not force:   # force ข้ามเฉพาะการต่อทดสอบ (และโควตาของมัน) — ทดสอบก่อนถือ lock เขียน (ใช้เวลาได้หลายวินาที)
                limited = _rate_limited(app, "test", username)   # ต่อฐานออกนอกเหมือนปุ่มทดสอบ: ถังเดียวกัน
                if limited is not None:
                    return limited
                steps = settings_checks.run_db_test(merged, **_db_deps(app))
                if not _no_fail(steps):
                    return jsonify(error="untested", steps=steps), 409

        to_write = {**changes, **{k: "" for k in clear_keys}}
        user = clean_username(username)
        try:
            with _WRITE_LOCK:
                if gate_env is not None and _db_gate_conflict(app, gate_env, changes, clear_keys):
                    return jsonify(error="conflict", message=MSG_CONFLICT), 409
                settings_store.write_overlay(app.config["SETTINGS_PATH"], to_write, user)
        except settings_store.SettingsError as err:
            return _invalid(err.key, str(err))
        _audit(app, "settings_change", user, ",".join(changed))
        return jsonify(ok=True, changed=changed, restart=_schedule_restart(app))

    @app.post("/api/settings/revert")
    @lan_only
    @login_required
    @role_required("admin")
    @api_write_guard
    @_guarded
    def settings_revert():
        body, too_large = _read_json_body()
        if too_large is not None:
            return too_large
        password = _parse_revert_body(body)
        if password is None:
            return _bad_request()
        username = session["user"]
        denied = _verify_password(app, username, password)
        if denied is not None:
            return denied
        user = clean_username(username)
        with _WRITE_LOCK:
            reverted = settings_store.revert_overlay(app.config["SETTINGS_PATH"], user)
        if not reverted:
            return jsonify(error="no_prev"), 409
        _audit(app, "settings_revert", user, MSG_REVERT_DETAIL)
        return jsonify(ok=True, restart=_schedule_restart(app))

    @app.post("/api/settings/test")
    @lan_only
    @login_required
    @role_required("admin")
    @api_write_guard
    @_guarded
    def settings_test():
        limited = _rate_limited(app, "test", session["user"])
        if limited is not None:
            return limited
        body, too_large = _read_json_body()
        if too_large is not None:
            return too_large
        if not isinstance(body, dict) or body.get("target") not in TEST_TARGETS:
            return _bad_request()
        values = _parse_values(body.get("values", {}))
        if values is None:
            return _bad_request()
        env, current, bad = _test_env(app, values)
        if bad is not None:
            return bad
        if body["target"] == "db":
            supplied = "DB_PASS" in values and not _is_blank_secret("DB_PASS", values["DB_PASS"])
            if _password_required(current, env, supplied):
                return _password_required_response()
            steps = settings_checks.run_db_test(env, **_db_deps(app))
        else:
            steps = settings_checks.run_moph_test(
                env, tcp_check=(app.config.get("CHECK_DEPS") or {}).get("tcp_check"))
        return jsonify(ok=_no_fail(steps), steps=steps)

    @app.post("/api/settings/send-test")
    @lan_only
    @login_required
    @role_required("admin")
    @api_write_guard
    @_guarded
    def settings_send_test():
        username = session["user"]
        limited = _rate_limited(app, "send", username)
        if limited is not None:
            return limited
        body, too_large = _read_json_body()
        if too_large is not None:
            return too_large
        # ส่งข้อความจริงเข้ากลุ่ม LINE: ต้องมี confirm เป็น boolean true จริง ๆ (ไม่รับ "true" / 1)
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return _bad_request()
        values = _parse_values(body.get("values", {}))
        if values is None:
            return _bad_request()
        env, _current, bad = _test_env(app, values)
        if bad is not None:
            return bad
        steps = settings_checks.send_test_message(
            env, sender=(app.config.get("CHECK_DEPS") or {}).get("sender"))
        ok = _no_fail(steps)
        _audit(app, "settings_send_test", clean_username(username), MSG_SEND_OK if ok else MSG_SEND_FAILED)
        return jsonify(ok=ok, steps=steps)
