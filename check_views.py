"""หน้า Data Audit `GET /check` — เฉพาะเครือข่าย LAN และต้อง login (สเปกข้อ 10 ส่วนที่ 2).

แสดงเฉพาะจำนวนนับ/โครงสร้าง/ชื่อหมู่บ้านของ รพ. (ไม่มี HN ชื่อ เบอร์ ที่อยู่ หรือ visit key)
และไม่พิมพ์ความลับ (SECRET_KEY, กุญแจ MOPH, รหัสผ่านฐาน) — แสดงเพียง "ตั้งแล้ว/ยังไม่ได้ตั้ง".
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Mapping
from urllib.parse import urlsplit

from flask import Flask, render_template, request

from auth import lan_only, login_required
from case_views import OSM_ATTRIBUTION
from diseases import REGISTRY, env_names
from config import (DEFAULT_TILE_URL, Settings, base_url_problem, looks_like_placeholder,
                    secret_key_problem)
from setup_check import FAIL, OK, WARN, run_report
from settings_store import ALLOWED, SECRET_KEYS, describe, overlay_info

DEFAULT_VIEW = {"lat": 14.35, "lng": 101.90, "zoom": 12}
DB_DEFAULTS = {"DB_PORT": "3306", "DB_NAME": "hos"}   # ค่าที่ wiring ใช้เมื่อไม่ได้ตั้ง
SOURCE_LABELS = {"overlay": "หน้าตั้งค่า", "env": ".env", "default": "ค่าเริ่มต้น"}
CACHE_TTL_S = 300          # ผลตรวจใช้ซ้ำได้นานเท่านี้ (คำสั่งตรวจสแกนตารางใหญ่ของ HOSxP)
REFRESH_MIN_S = 30         # ?refresh=1 รันใหม่ได้อย่างเร็วทุกกี่วินาที (ต่อโปรเซส)


class ReportCache:
    """แคชผลตรวจ (results, audit) ในโปรเซส; lock กันสองคำขอรันพร้อมกัน.

    `clock` (วินาทีเดินหน้า) และ `wall` (เวลาตามนาฬิกา ไว้แสดง "ข้อมูลเมื่อ HH:MM") ฉีดจากเทสต์ได้."""

    def __init__(self, compute: Callable[[], tuple],
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = datetime.now,
                 ttl: float = CACHE_TTL_S, refresh_min: float = REFRESH_MIN_S):
        self.compute, self.clock, self.wall = compute, clock, wall
        self.ttl, self.refresh_min = ttl, refresh_min
        self._lock = threading.Lock()
        self._entry: tuple[tuple, float, datetime] | None = None

    def get(self, refresh: bool = False) -> tuple[tuple, datetime, bool]:
        """(ผล, เวลาที่คำนวณ, refresh_throttled). ถูกจำกัด = ขอ refresh แต่ยังไม่ถึง refresh_min."""
        with self._lock:
            now = self.clock()
            throttled = False
            if self._entry is not None:
                age = now - self._entry[1]
                if refresh and age < self.refresh_min:
                    throttled = True
                elif not refresh and age < self.ttl:
                    pass
                else:
                    self._entry = None
            if self._entry is None:
                self._entry = (self.compute(), now, self.wall())
            return self._entry[0], self._entry[2], throttled


def safe_url(url: str, keep_path: bool = True) -> str:
    """scheme://host[:port][/path] ไม่มี userinfo / query / fragment (ที่อาจมีคีย์)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "(อ่านไม่ได้)"
    if not host:
        return "(อ่านไม่ได้)"
    path = parts.path if keep_path else ""
    return f"{parts.scheme}://{host}{port}{path}"


# ชื่อตัวแปรที่ /check และหน้าตั้งค่าอ่านจาก "env ก่อนซ้อนทับ" (เพื่อบอกแหล่งที่มา/สถานะ) — ตัวอื่นไม่ถูกเก็บใน app.config
BASE_ENV_NAMES = (
    *ALLOWED, "SECRET_KEY", "DENGUE_DB_PATH", "DENGUE_ICDS", "DENGUE_POLL_SECONDS",
    "DENGUE_CLUSTER_MIN_CASES", "DENGUE_CLUSTER_RADIUS_M", "DENGUE_CLUSTER_DAYS",
    "DENGUE_REALERT_DAYS", "DENGUE_CANCEL_CHECK_DAYS", "DENGUE_AREA_AMPHOE", "DENGUE_TILE_URL",
    "DENGUE_LAN_PREFIXES", "MOPH_BASE_URL")
_MASKED = frozenset(SECRET_KEYS) | {"SECRET_KEY"}


def public_base_env(base: Mapping[str, str]) -> dict[str, str]:
    """สำเนา env ที่เก็บใน `app.config["SETTINGS_BASE_ENV"]`: เฉพาะชื่อใน BASE_ENV_NAMES และความลับ
    (รหัส DB, กุญแจ MOPH, SECRET_KEY) ถูกแทนด้วย "*" — ที่เหลือที่ผู้ใช้ต้องการคือ "ตั้งหรือไม่" (ว่าง/ไม่ว่าง) เท่านั้น"""
    out: dict[str, str] = {}
    for name in BASE_ENV_NAMES:
        value = base.get(name)
        if value is None:
            continue
        out[name] = ("*" if value.strip() else "") if name in _MASKED else value
    return out


def _placeholder_problem(value: str) -> str | None:
    return "placeholder" if looks_like_placeholder(value) else None


def _secret_state(value: str, problem: Callable[[str], str | None]) -> str:
    """"unset" (ว่าง) · "invalid" (ค่าตัวอย่าง/อ่อนแอ) · "set" (ใช้ได้) — ไม่คืนตัวค่า."""
    if not (value or "").strip():
        return "unset"
    return "invalid" if problem(value) else "set"


def env_rows(settings: Settings, base_env: Mapping[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """(ค่าที่แสดงค่าได้, ค่าที่เป็นความลับ -> แสดงเฉพาะสถานะตั้ง/ไม่ตั้ง) พร้อมคอลัมน์ "แหล่งที่มา".

    `base_env` = env ก่อนซ้อนทับ (ตำแหน่ง settings.json ตาม DENGUE_DB_PATH ของมัน). 10 ตัวแปรที่แก้จากหน้าตั้งค่าได้
    ใช้ settings_store.describe() บอกแหล่งที่มา (หน้าตั้งค่า/.env/ค่าเริ่มต้น); ตัวอื่นเป็น .env หรือค่าเริ่มต้น.
    ความลับ (รหัสผ่านฐาน, กุญแจ MOPH, SECRET_KEY) ไม่เคยแสดงค่า."""
    base = {} if base_env is None else base_env
    info = describe(base)

    def src(name: str) -> str:
        if name in info:
            return SOURCE_LABELS[info[name]["source"]]
        return SOURCE_LABELS["env" if (base.get(name) or "").strip() else "default"]

    def db_value(name: str) -> str:
        d = info[name]
        if d["state"] == "set":
            return d["value"]
        return DB_DEFAULTS.get(name) or "(ยังไม่ได้ตั้ง)"

    tile = ("ค่าเริ่มต้น (OpenStreetMap)" if settings.tile_url == DEFAULT_TILE_URL
            else safe_url(settings.tile_url, keep_path=False))
    shown = [
        ("DENGUE_ICDS", ", ".join(settings.icds), "ขึ้นต้นรหัสโรคที่แจ้ง — เทียบกับที่พบในข้อมูลด้านบน"),
        ("DENGUE_POLL_SECONDS", str(settings.poll_seconds), "ความถี่ตรวจ HOSxP"),
        ("DENGUE_START_FROM", settings.start_from.isoformat(), "แจ้งเฉพาะวินิจฉัยตั้งแต่วันนี้"),
        ("DENGUE_CLUSTER_MIN_CASES", str(settings.cluster_min_cases), "จำนวนรายขั้นต่ำของกลุ่ม"),
        ("DENGUE_CLUSTER_RADIUS_M", str(settings.cluster_radius_m), "รัศมี (เมตร)"),
        ("DENGUE_CLUSTER_DAYS", str(settings.cluster_days), "ช่วงวัน"),
        ("DENGUE_REALERT_DAYS", str(settings.realert_days), "ผู้ป่วย HN เดิมมาตรวจติดตามภายใน N วัน นับเป็นรายเดิม ไม่สร้างเคสใหม่/ไม่แจ้งซ้ำ"),
        ("DENGUE_CANCEL_CHECK_DAYS", str(settings.cancel_check_days),
         "ตรวจว่ารหัสโรคหายไปหรือไม่ (ยกเลิกเมื่อหายติดกัน 3 รอบ) เฉพาะเคสที่วินิจฉัยภายใน N วันล่าสุด"),
        ("DENGUE_AREA_AMPHOE", ", ".join(settings.area_amphoe) or "(ไม่ได้ตั้ง)",
         "รหัสจังหวัด+อำเภอ 4 หลักที่ถือว่า \"ในเขต\" — ถ้าไม่ตั้งใช้อำเภอของหมู่บ้านในตาราง village"),
        ("DENGUE_BASE_URL",
         "ค่าไม่ถูกต้อง" if base_url_problem(settings.base_url) else safe_url(settings.base_url),
         "ฐานลิงก์ในข้อความ LINE (ต้องเป็น http:// หรือ https:// ไม่ใช่ค่าตัวอย่าง <...>)"),
        ("DENGUE_TILE_URL", tile, "แหล่ง tile แผนที่ (ไม่แสดง query ที่อาจมีคีย์)"),
        ("DENGUE_DATA_AUDITED", "1" if settings.data_audited else "0",
         "ตั้งเป็น 1 หลังตรวจตัวเลขกับข้อมูล รพ. นี้และทดลองคู่กับรายงานเดิมแล้ว"),
        ("DENGUE_LAN_PREFIXES", ", ".join(settings.lan_prefixes), "ที่อยู่ที่เปิดหน้านี้ได้"),
        ("MOPH_BASE_URL", safe_url(settings.moph_base_url), "ปลายทาง MOPH Notify"),
        ("DB_HOST", db_value("DB_HOST"), "ที่อยู่ฐานข้อมูล HOSxP"),
        ("DB_PORT", db_value("DB_PORT"), "พอร์ตฐานข้อมูล"),
        ("DB_NAME", db_value("DB_NAME"), "ชื่อฐานข้อมูล"),
        ("DB_USER", db_value("DB_USER"), "ผู้ใช้ฐานข้อมูล (ควรมีสิทธิ์อ่านอย่างเดียว)"),
    ]
    for disease in REGISTRY:   # โรคอื่นทุกโรค (รวมที่ปิดอยู่): เกณฑ์ที่ใช้จริงและชื่อ env ที่ปรับได้ (ไข้เลือดออกอยู่ด้านบนแล้ว)
        rule = settings.rule(disease.key)
        if rule is None or rule.key == "dengue":
            continue
        env, name = env_names(rule.key), rule.disease.name_th
        shown += [(env["icds"], ", ".join(rule.icds), f"ขึ้นต้นรหัสโรค{name} — เทียบกับที่พบในข้อมูลด้านบน"),
                  (env["min_cases"], str(rule.min_cases), f"จำนวนรายขั้นต่ำของกลุ่ม ({name})"),
                  (env["days"], str(rule.days), f"ช่วงวัน ({name})")]
        if rule.group_by == "pin_radius":
            shown.append((env["radius_m"], str(rule.radius_m), f"รัศมี เมตร ({name})"))
    secrets = [
        ("SECRET_KEY", _secret_state(settings.secret_key, secret_key_problem)),
        ("MOPH_CLIENT_KEY", _secret_state(settings.moph_client_key, _placeholder_problem)),
        ("MOPH_SECRET_KEY", _secret_state(settings.moph_secret_key, _placeholder_problem)),
        ("DB_PASS", info["DB_PASS"]["state"]),
    ]
    return ([{"name": n, "value": v, "note": note, "source": src(n)} for n, v, note in shown],
            [{"name": n, "state": st, "source": src(n)} for n, st in secrets])


def register_check_routes(app: Flask) -> None:
    def base_env() -> Mapping[str, str]:
        return app.config["SETTINGS_BASE_ENV"]

    def compute():
        # อ่านไฟล์ค่าตั้งเฉพาะตอนคำนวณใหม่ (ผลแคช 300 วินาที) — ไม่อ่านทุกคำขอ
        return run_report(app.config["SETTINGS"], app.config["SOURCE"],
                          app.config["SENDER"] is not None, overlay=overlay_info(base_env()),
                          store=app.config["STORE"])

    cache = ReportCache(compute)
    app.extensions["check_cache"] = cache

    @app.get("/check")
    @lan_only
    @login_required
    def check_page():
        settings: Settings = app.config["SETTINGS"]
        source = app.config["SOURCE"]
        shown, secrets = env_rows(settings, base_env())
        base = {"page_title": "ตรวจก่อนใช้ (Data Audit)", "env_shown": shown,
                "env_secrets": secrets, "results": [], "audit": None, "summary": None,
                "check_data": None, "computed_at": None, "throttled": False,
                "ttl_minutes": CACHE_TTL_S // 60, "refresh_seconds": REFRESH_MIN_S}
        if source is None:
            return render_template("check.html", **base)
        # app.extensions ไม่ใช่ค่าคงที่: เทสต์/แอปอื่นอาจสลับ cache (เช่นฉีด clock)
        (results, audit), computed_at, throttled = app.extensions["check_cache"].get(
            refresh=request.args.get("refresh") == "1")
        is_osm = settings.tile_url == DEFAULT_TILE_URL
        base.update(
            results=results, audit=audit, computed_at=computed_at, throttled=throttled,
            summary={s: sum(1 for r in results if r[0] == s) for s in (OK, WARN, FAIL)},
            show_osm_attribution=is_osm,
            check_data={
                "tile_url": settings.tile_url,
                "attribution": OSM_ATTRIBUTION if is_osm else "",
                "center": DEFAULT_VIEW,
                "villages": audit["villages"]["points"],
            })
        return render_template("check.html", **base)
