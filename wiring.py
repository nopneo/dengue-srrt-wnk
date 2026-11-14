"""ประกอบส่วนประกอบจริงของแอป: ตั้งค่า -> ฐานแอป -> ตัวอ่าน HOSxP (อ่านอย่างเดียว) -> ตัวส่ง MOPH.

- การต่อ HOSxP เป็นแบบ lazy (ต่อเมื่อมีการ query) การ import/สร้าง runtime จึงไม่ต่อฐานข้อมูล
- รหัสผ่านฐาน/กุญแจ MOPH ถือในค่า config และ closure เท่านั้น ไม่อยู่ใน repr ของ Runtime
- ต้องไม่ import wsgi.py ที่นี่ (wsgi.py สร้างแอปตอน import)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import pymysql
from flask import Flask

import moph_notify
import scheduler
import settings_store
from app import create_app
from check_views import public_base_env
from config import Settings, load_settings, looks_like_placeholder
from hosxp import MSG_DB_NOT_CONFIGURED, MSG_READ_ONLY_FAILED, HosxpError, HosxpSource
from settings_store import Overlay
from store import Store


@dataclass
class Runtime:
    settings: Settings
    store: Store
    source: HosxpSource
    sender: Callable[[list[dict]], None] | None
    area_lookup: Callable[[str], str | None] | None
    # ผลอ่าน settings.json ตอนสร้าง (อ่านครั้งเดียวต่อโปรเซส) และ env ก่อนซ้อนทับ — มีความลับจริง จึงไม่อยู่ใน repr
    overlay: Overlay = field(default_factory=Overlay, repr=False)
    base_env: dict = field(default_factory=dict, repr=False)


PROD_CONNECT_TIMEOUT_S = 10
PROD_READ_TIMEOUT_S = 60


def make_connect(host: str, port: int, database: str, user: str, password: str,
                 connect_timeout: int | None = None,
                 read_timeout: int | None = None) -> Callable[[], Any]:
    """โรงงานการเชื่อมต่อ HOSxP: ตั้ง SESSION READ ONLY ทันที; ตั้งไม่ได้ = ล้ม (ห้ามใช้การเชื่อมที่เขียนได้).

    timeout เริ่มต้น 10/60 วินาทีสำหรับการใช้งานจริง; ปุ่มทดสอบของหน้า /settings ส่งค่าที่สั้นกว่า
    (pymysql ใช้ read_timeout กับทุกการอ่านตอน handshake — ปลายทางที่ตอบช้าจะไม่ค้างเธรดเป็นนาที)."""
    connect_timeout = PROD_CONNECT_TIMEOUT_S if connect_timeout is None else connect_timeout
    read_timeout = PROD_READ_TIMEOUT_S if read_timeout is None else read_timeout
    def connect():
        if not host or not user:
            raise HosxpError(MSG_DB_NOT_CONFIGURED)
        con = pymysql.connect(host=host, port=port, user=user, password=password,
                              database=database, charset="utf8mb4",
                              connect_timeout=connect_timeout, read_timeout=read_timeout)
        try:
            cur = con.cursor()
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.close()
        except Exception as e:  # noqa: BLE001
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
            raise HosxpError(f"{MSG_READ_ONLY_FAILED} ({type(e).__name__})") from None
        return con
    return connect


def _port(value: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 3306


def build_runtime(env: Mapping[str, str] | None = None) -> Runtime:
    """`env` ที่ส่งมาเป็น "base" (ตำแหน่ง settings.json ตาม DENGUE_DB_PATH ของ base) — ค่าในไฟล์ซ้อนทับเฉพาะ
    10 ตัวแปรที่ settings_store อนุญาต; ไฟล์เสียก็ยังสร้างได้ด้วยค่า env (ดู Runtime.overlay.error)."""
    r = settings_store.resolve(env)
    e = r.env
    settings = load_settings(e)
    store = Store(settings.db_path)
    source = HosxpSource(make_connect(
        e.get("DB_HOST", ""), _port(e.get("DB_PORT", "")), e.get("DB_NAME") or "hos",
        e.get("DB_USER", ""), e.get("DB_PASS", "")))
    sender = None
    keys = (settings.moph_client_key, settings.moph_secret_key)
    # กุญแจว่างหรือยังเป็น <...> จาก .env.example = ยังไม่ได้ตั้ง (ไม่ลองส่งด้วยกุญแจขยะ)
    if all(k.strip() and not looks_like_placeholder(k) for k in keys):
        def sender(messages: list[dict]) -> None:
            moph_notify.send_messages(messages, settings.moph_base_url,
                                      settings.moph_client_key, settings.moph_secret_key)
    return Runtime(settings=settings, store=store, source=source, sender=sender,
                   area_lookup=source.area_label, overlay=r.overlay, base_env=r.base)


# ตัวแปรที่ปุ่มทดสอบใช้จริง: 10 ตัวที่แก้จากหน้าเว็บได้ + ตำแหน่งไฟล์ค่าตั้ง + URL ของ MOPH
_PROVIDER_NAMES = (*settings_store.ALLOWED, "DENGUE_DB_PATH", "MOPH_BASE_URL")


def _env_provider(base_env: Mapping[str, str]) -> Callable[[], dict]:
    base = {k: base_env[k] for k in _PROVIDER_NAMES if k in base_env}
    return lambda: settings_store.effective_env(base)


def build_app(env: Mapping[str, str] | None = None) -> Flask:
    rt = build_runtime(env)
    e = rt.base_env  # DENGUE_SCHEDULER ไม่อยู่ใน 10 ตัวแปรที่ซ้อนทับได้ ใช้ค่าจาก base
    app = create_app(rt.settings, rt.store, rt.source, rt.sender, start_scheduler=False)
    app.config["AREA_LOOKUP"] = rt.area_lookup
    # ข้อความ error ของไฟล์ค่าตั้ง (ชื่อคีย์เท่านั้น) — /healthz ใช้เฉพาะสถานะ ไม่ส่งข้อความออก
    app.config["SETTINGS_OVERLAY_ERROR"] = rt.overlay.error
    # เฉพาะชื่อที่หน้า /check, /settings ใช้ และความลับถูกปิดเป็น "*" (ไม่ให้สำเนา env ทั้งหมดค้างใน app.config)
    app.config["SETTINGS_BASE_ENV"] = public_base_env(rt.base_env)
    # env ที่มีผลตอนนี้ (ไม่ปิดความลับ) ให้ปุ่มทดสอบ/ด่านบันทึกของ /settings — closure เก็บสำเนาเฉพาะชื่อที่ใช้
    # (repr ของ closure ไม่แสดงค่า); อ่าน settings.json ใหม่ทุกครั้งที่เรียก
    app.config["ENV_PROVIDER"] = _env_provider(rt.base_env)
    if e.get("DENGUE_SCHEDULER", "1").strip() != "0":
        app.config["SCHEDULER_EXPECTED"] = True  # /healthz: ไม่มีรอบนานเกิน 3 x poll = ไม่ ok
        # เก็บเธรดไว้ให้ hook รีสตาร์ตของ /settings รอรอบปัจจุบันจบก่อน SIGTERM (None = ไม่ได้ล็อก ไม่มีเธรดให้รอ)
        app.config["SCHEDULER_THREAD"] = scheduler.start_scheduler(
            app, rt.store, rt.source, rt.sender, rt.settings, rt.area_lookup)
    return app
