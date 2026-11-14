"""ปุ่มทดสอบของหน้า /settings: ทดสอบ HOSxP, ทดสอบ MOPH (เครือข่ายเท่านั้น), ส่งข้อความทดสอบ (สเปก §5, §7, §8).

โมดูลนี้ถือรหัสผ่านฐานข้อมูลและ key MOPH ตอนทดสอบ จึงเข้มงวดเรื่องความลับ:
- ไม่มี Flask; ของที่ต้องต่อออกนอก (connect / tables_source / tcp_check / sender) ฉีดได้ — ค่าเริ่มต้นคือของจริง
  (`wiring.make_connect`, `HosxpSource.table_columns`, `setup_check.tcp_reachable`, `moph_notify.send_messages`)
- ทุกขั้นเป็น `{"name": str, "status": "ok"|"warn"|"fail", "detail": str}` ภาษาไทย; `detail` ปลอดภัยโดยโครงสร้าง:
  มาจาก `describe_error` (รหัสข้อผิดพลาด/ชื่อชนิดเท่านั้น) หรือข้อความคงที่ — **ไม่เคยใช้ `str(exc)`** ที่อาจมี
  ชื่อผู้ใช้ โฮสต์ รหัสผ่าน หรือกุญแจ
- ทดสอบ HOSxP ไม่อ่านแถวผู้ป่วย (ถามแค่ information_schema) และใช้การเชื่อมที่ `make_connect` บังคับ READ ONLY
  (ตั้งไม่ได้ = ล้ม); ปิดการเชื่อมเสมอ
- ทดสอบ MOPH ต่อ TCP อย่างเดียว ไม่ส่งกุญแจ/ข้อความ; การ "ส่งข้อความทดสอบ" แยกเป็นอีกฟังก์ชันที่ผู้ดูแลยืนยันเอง
"""
from __future__ import annotations

import re
import socket
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import pymysql

import moph_notify
import settings_store
import setup_check
from config import looks_like_placeholder
from hosxp import MSG_DB_NOT_CONFIGURED, MSG_READ_ONLY_FAILED, HosxpError, HosxpSource
from moph_notify import ERROR_HINTS, NotifyError

OK, WARN, FAIL = "ok", "warn", "fail"

TEST_MESSAGE = "ทดสอบการส่งจากระบบแจ้งเตือนไข้เลือดออก SRRT (ไม่มีข้อมูลผู้ป่วย)"
DB_REQUIRED = ("DB_HOST", "DB_USER", "DB_PASS")
MOPH_KEYS = settings_store.MOPH_KEYS
DEFAULT_MOPH_PORT = 443
TEST_CONNECT_TIMEOUT_S = 5   # เส้นทางทดสอบรอสั้นกว่าการใช้งานจริง (10/60) — กันเธรดค้างเมื่อปลายทางตอบช้า
TEST_READ_TIMEOUT_S = 10

default_tcp_check = setup_check.tcp_reachable

# HosxpError ที่มาจากโค้ดเราเองและมีข้อความคงที่ (ไม่มีค่าที่กรอก/ข้อความไดรเวอร์)
_READ_ONLY_FAILED_RE = re.compile(re.escape(MSG_READ_ONLY_FAILED) + r" \(\w{1,80}\)")

# NotifyError ที่ moph_notify สร้างเองด้วยรูปแบบคงที่ (มีแค่ชื่อชนิด exception / เลข HTTP / เลข code) — ผ่านได้ทั้งข้อความ
_NOTIFY_FIXED = tuple(re.compile(p) for p in (
    r"ยังไม่ได้ตั้งค่า MOPH Notify \(base_url / client-key / secret-key\)",
    r"เชื่อมต่อ MOPH Notify ไม่ได้ \(\w{1,80}\)",
    r"MOPH Notify ตอบกลับเป็นการเปลี่ยนเส้นทาง \(HTTP \d{3}\) ไม่ส่งคีย์ต่อ — ตรวจ base_url ให้ถูกต้อง",
    r"MOPH Notify ตอบกลับไม่ใช่ JSON \(HTTP \d{3}\)",
    r"MOPH Notify ตอบกลับรูปแบบไม่ถูกต้อง \(HTTP \d{3}\)",
    r"MOPH Notify ตอบกลับไม่มีรหัสสถานะ \(message_code/status\)",
    r"ไม่ทราบสาเหตุ \(code \d{1,6}\)",
))
_CODE_RE = re.compile(r"\(code (\d{1,6})\)")


def _step(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _first_arg(exc: BaseException) -> Any:
    return exc.args[0] if exc.args else None


def describe_error(exc: BaseException) -> str:
    """ข้อความสั้น ๆ ที่ปลอดภัยแทน exception — ไม่เคยแสดงข้อความดิบ/ค่าของ exception ใด ๆ."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "หมดเวลา"
    if isinstance(exc, pymysql.MySQLError):
        code = _first_arg(exc)
        if isinstance(code, int) and not isinstance(code, bool):
            if code == 1045:
                return "รหัสผ่านหรือสิทธิ์ไม่ถูกต้อง (Access denied 1045)"
            if code == 1049:
                return "ไม่พบฐานข้อมูล (1049)"
            if code == 2003:
                return "ต่อไม่ได้ (2003)"
            if code in (2006, 2013):
                return "หมดเวลา"
            return f"ข้อผิดพลาดฐานข้อมูล ({code})"
        return type(exc).__name__
    if isinstance(exc, HosxpError):
        message = _first_arg(exc)
        if message == MSG_DB_NOT_CONFIGURED:
            return MSG_DB_NOT_CONFIGURED
        if isinstance(message, str) and _READ_ONLY_FAILED_RE.fullmatch(message):
            return message
        cause = exc.__cause__
        if cause is not None and not isinstance(cause, HosxpError):
            return describe_error(cause)  # HosxpSource ห่อ error ของไดรเวอร์ไว้ — ใช้รหัสของสาเหตุ
    return type(exc).__name__


def _env_text(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------- ทดสอบ HOSxP

class _KeepOpen:
    """การเชื่อมที่เปิดแล้วที่ HosxpSource เรียก close() ได้โดยไม่ปิดจริง (ผู้เรียกปิดเอง)"""

    def __init__(self, con):
        self._con = con

    def cursor(self):
        return self._con.cursor()

    def close(self) -> None:
        pass


def default_tables_source(con) -> Mapping[str, Mapping[str, str]]:
    """{ตาราง: {คอลัมน์: ชนิด}} ของตารางที่แอปใช้ ผ่าน information_schema บนการเชื่อมที่เปิดแล้ว (โครงสร้างเท่านั้น)"""
    return HosxpSource(lambda: _KeepOpen(con)).table_columns(tuple(setup_check.REQUIRED_COLUMNS))


def _close_quietly(con) -> None:
    try:
        con.close()
    except Exception:  # noqa: BLE001 — ปิดไม่ได้ก็ไม่ใช่เรื่องของผู้ใช้
        pass


def run_db_test(env: Mapping[str, str], connect: Callable | None = None,
                tables_source: Callable | None = None) -> list[dict]:
    """ทดสอบต่อ HOSxP ด้วยค่าใน `env` (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS).

    `connect` มีรูปแบบเดียวกับ `wiring.make_connect(host, port, database, user, password) -> () -> connection`
    (ค่าเริ่มต้น = ตัวจริงที่บังคับ SESSION READ ONLY และล้มถ้าตั้งไม่ได้); `tables_source(con)` คืน {ตาราง: คอลัมน์}"""
    steps: list[dict] = []
    missing = [k for k in ("DB_HOST", "DB_USER") if not _env_text(env, k).strip()]
    if not _env_text(env, "DB_PASS"):   # รหัสเป็นช่องว่างล้วนยังถือว่าตั้งแล้ว (เก็บตามที่พิมพ์)
        missing.append("DB_PASS")
    if missing:
        steps.append(_step("ตั้งค่าครบ", FAIL, "ยังไม่ได้ตั้ง: " + ", ".join(missing)))
        return steps
    steps.append(_step("ตั้งค่าครบ", OK, "มี " + " / ".join(DB_REQUIRED) + " ครบ"))

    if connect is None:
        from wiring import make_connect as connect  # import ตอนใช้: wiring -> app -> settings_views -> โมดูลนี้
    from wiring import _port
    factory = connect(_env_text(env, "DB_HOST"), _port(_env_text(env, "DB_PORT")),
                      _env_text(env, "DB_NAME") or "hos", _env_text(env, "DB_USER"),
                      _env_text(env, "DB_PASS"),
                      connect_timeout=TEST_CONNECT_TIMEOUT_S, read_timeout=TEST_READ_TIMEOUT_S)
    try:
        con = factory()
    except Exception as exc:  # noqa: BLE001 — ข้อความ exception อาจมีโฮสต์/ผู้ใช้/รหัสผ่าน: ใช้ describe_error เท่านั้น
        message = _first_arg(exc)
        if (isinstance(exc, HosxpError) and isinstance(message, str)
                and _READ_ONLY_FAILED_RE.fullmatch(message)):
            steps.append(_step("เชื่อมต่อฐานข้อมูล", OK, "ต่อฐานข้อมูลได้"))
            steps.append(_step("เซสชันอ่านอย่างเดียว", FAIL,
                               "ตั้งการเชื่อมต่อเป็นอ่านอย่างเดียวไม่สำเร็จ — ไม่ใช้การเชื่อมต่อนี้"))
        else:
            steps.append(_step("เชื่อมต่อฐานข้อมูล", FAIL, describe_error(exc)))
        return steps
    try:
        steps.append(_step("เชื่อมต่อฐานข้อมูล", OK, "ต่อฐานข้อมูลได้"))
        steps.append(_step("เซสชันอ่านอย่างเดียว", OK, "ตั้งการเชื่อมต่อเป็นอ่านอย่างเดียว (READ ONLY) แล้ว"))
        try:
            cols = (tables_source or default_tables_source)(con)
            absent = [t for t in setup_check.REQUIRED_COLUMNS if t not in cols]
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("ตารางที่ใช้", WARN, f"ตรวจรายการตารางไม่สำเร็จ ({describe_error(exc)})"))
        else:
            # แยกตารางเสริม (setup_check.OPTIONAL_TABLES เช่น thaiaddress) ออกจากตารางที่ต้องมี
            required_absent = [t for t in absent if t not in setup_check.OPTIONAL_TABLES]
            optional_absent = [t for t in absent if t in setup_check.OPTIONAL_TABLES]
            if required_absent:
                detail = f"ตารางไม่ครบ {len(required_absent)} ตาราง"
                if optional_absent:
                    detail += f" · ไม่พบตารางเสริม {len(optional_absent)} ตาราง"
                steps.append(_step("ตารางที่ใช้", WARN, detail))
            elif optional_absent:
                steps.append(_step("ตารางที่ใช้", WARN,
                                   f"ไม่พบตารางเสริม {len(optional_absent)} ตาราง (ชื่อพื้นที่จะแสดงเป็นรหัสแทน)"))
            else:
                steps.append(_step("ตารางที่ใช้", OK, f"พบครบ {len(setup_check.REQUIRED_COLUMNS)} ตาราง"))
    finally:
        _close_quietly(con)
    return steps


# ---------------------------------------------------------------- ทดสอบ MOPH (เครือข่ายเท่านั้น)

def _keys(env: Mapping[str, str]) -> tuple[str, str]:
    return _env_text(env, MOPH_KEYS[0]), _env_text(env, MOPH_KEYS[1])


def run_moph_test(env: Mapping[str, str], tcp_check: Callable[[str, int], Any] | None = None) -> list[dict]:
    """ตรวจกุญแจครบ/รูปแบบ และเครือข่ายถึงโฮสต์ MOPH (TCP) — ไม่ส่งกุญแจหรือข้อความใด ๆ."""
    steps: list[dict] = []
    keys = _keys(env)
    if all(k.strip() for k in keys):
        steps.append(_step("กุญแจครบ", OK, "ตั้ง Client Key และ Secret Key ครบทั้งสองตัว"))
    else:
        steps.append(_step("กุญแจครบ", WARN, "ยังไม่ได้ตั้งกุญแจ"))

    problems: list[str] = []
    checked = 0
    for name, value in zip(MOPH_KEYS, keys):
        if not value.strip():
            continue
        checked += 1
        try:
            settings_store.validate(name, value)
        except settings_store.SettingsError as err:
            problems.append(f"{settings_store.ALLOWED[name].label}: {err.reason}")  # ข้อความกฎคงที่ ไม่มีค่า
    if problems:
        steps.append(_step("รูปแบบกุญแจ", FAIL, " · ".join(problems)))
    elif checked:
        steps.append(_step("รูปแบบกุญแจ", OK, "รูปแบบกุญแจถูกต้อง"))
    else:
        steps.append(_step("รูปแบบกุญแจ", WARN, "ข้าม — ยังไม่มีกุญแจให้ตรวจ"))

    host, port = _moph_endpoint(_env_text(env, "MOPH_BASE_URL"))
    if not host:
        steps.append(_step("เครือข่ายถึง MOPH", FAIL, "อ่านที่อยู่ MOPH_BASE_URL ไม่ได้"))
        return steps
    try:
        (tcp_check or default_tcp_check)(host, port)
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("เครือข่ายถึง MOPH", FAIL,
                           f"ต่อเซิร์ฟเวอร์ MOPH ไม่ได้ ({describe_error(exc)}) — ตรวจไฟร์วอลล์ขาออก"))
    else:
        steps.append(_step("เครือข่ายถึง MOPH", OK, "ต่อเซิร์ฟเวอร์ MOPH ได้ (ไม่ได้ส่งกุญแจ/ข้อความ)"))
    return steps


def _moph_endpoint(base_url: str) -> tuple[str, int]:
    """(host, port) ของ MOPH_BASE_URL — host ว่างเมื่ออ่านไม่ได้ (พอร์ตเริ่มต้น 443)."""
    try:
        parts = urlsplit(base_url.strip())
        return parts.hostname or "", parts.port or DEFAULT_MOPH_PORT
    except ValueError:
        return "", DEFAULT_MOPH_PORT


# ---------------------------------------------------------------- ส่งข้อความทดสอบ

def _notify_detail(exc: NotifyError) -> str:
    """ข้อความ NotifyError -> ข้อความคงที่ที่ปลอดภัย (ข้อความ message ของเซิร์ฟเวอร์ไม่ผ่าน เหลือเฉพาะคำอธิบายของเรา)."""
    message = _first_arg(exc)
    if not isinstance(message, str):
        return type(exc).__name__
    for hint in ERROR_HINTS.values():
        if hint in message:
            code = _CODE_RE.search(message)
            return f"{hint} ({code.group(0)[1:-1]})" if code else hint
    if any(p.fullmatch(message) for p in _NOTIFY_FIXED):
        return message
    return type(exc).__name__


def send_test_message(env: Mapping[str, str], sender: Callable | None = None) -> list[dict]:
    """ส่งข้อความตัวอักษรสั้นคงที่ (ไม่มีข้อมูลผู้ป่วย) 1 ข้อความเข้ากลุ่ม LINE ที่ผูกกับกุญแจ.

    `sender(messages, base_url, client_key, secret_key)` (ค่าเริ่มต้น `moph_notify.send_messages`)."""
    client_key, secret_key = _keys(env)
    base_url = _env_text(env, "MOPH_BASE_URL").strip()
    ready = all(k.strip() for k in (client_key, secret_key)) and not any(
        looks_like_placeholder(k) for k in (client_key, secret_key))
    if not ready:
        return [_step("กุญแจครบ", FAIL, "ยังไม่ได้ตั้งกุญแจ MOPH (หรือยังเป็นค่าตัวอย่าง) — ไม่ได้ส่งข้อความ")]
    steps = [_step("กุญแจครบ", OK, "ตั้ง Client Key และ Secret Key ครบทั้งสองตัว")]
    if not base_url:
        steps.append(_step("ส่งข้อความทดสอบ", FAIL, "ยังไม่ได้ตั้ง MOPH_BASE_URL — ไม่ได้ส่งข้อความ"))
        return steps
    try:
        (sender or moph_notify.send_messages)([{"type": "text", "text": TEST_MESSAGE}], base_url, client_key, secret_key)
    except NotifyError as exc:
        steps.append(_step("ส่งข้อความทดสอบ", FAIL, _notify_detail(exc)))
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("ส่งข้อความทดสอบ", FAIL, describe_error(exc)))
    else:
        steps.append(_step("ส่งข้อความทดสอบ", OK, "ส่งแล้ว — ตรวจในกลุ่ม LINE"))
    return steps
