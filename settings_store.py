"""ที่เก็บค่าตั้งที่แก้จากหน้า /settings — `data/settings.json` ซ้อนทับ `.env` (ดูสเปก 2026-11-09-settings-page-design)

โมดูลนี้ถือรหัสผ่านฐานข้อมูลและ key MOPH จึงเข้มงวดเรื่องความลับ:
- รับ/ส่งเฉพาะ 11 ตัวแปรใน `ALLOWED` — `SECRET_KEY` และตัวแปรอื่นทั้งหมด **ไม่มีทาง** เขียน/อ่านผ่านโมดูลนี้
- ตัวตรวจค่า (`validate`) คืนค่าที่ตัดช่องว่างหัวท้ายแล้ว (ยกเว้น `DB_PASS` ที่เก็บ "ตรงตามที่พิมพ์ทุกตัวอักษร")
  หรือยก `SettingsError` ข้อความไทยที่ **ไม่มีค่าที่ผู้ใช้กรอก** · ปฏิเสธสตริงที่เข้ารหัส UTF-8 ไม่ได้ (surrogate เดี่ยว)
  และอักขระควบคุมในค่าข้อความอิสระ
- `read_overlay` ตรวจค่าที่เก็บไว้ด้วยกฎเดียวกันตอนอ่านด้วย (วันที่ตรวจเฉพาะรูปแบบ ไม่ตรวจช่วง ±366 วัน) คีย์ที่ไม่ผ่านถูกทิ้ง
  และ `error` ระบุเฉพาะชื่อคีย์ที่ถูกทิ้ง
- ไฟล์เขียนแบบ atomic (ไฟล์ชั่วคราวในโฟลเดอร์เดียวกัน → fsync → os.replace) สิทธิ์ 600 บน POSIX
  และหมุนไฟล์เดิมเป็น `settings.json.prev` ก่อนแทนที่ (ย้อนได้ด้วย `revert_overlay`)
- `read_overlay`/`effective_env`/`describe` ไม่ยก exception เพราะไฟล์เสีย (ไฟล์เสีย = ไม่มี overlay + บอก error สั้น ๆ)
- บันทึก log เฉพาะ "ชื่อตัวแปร" และ "ชื่อชนิด exception" (และชื่อไฟล์) — ไม่เคยใส่ค่า เนื้อไฟล์ หรือ path เต็ม

ข้อควรรู้สำหรับผู้เรียกใช้ (Task 3-4):
- `describe()` รายงาน `state=unset/source=default` สำหรับตัวแปรที่ `config.py` มีค่าเริ่มต้นให้ (เช่น DENGUE_BASE_URL,
  DENGUE_DATA_AUDITED) — หน้าจอต้องนำไปรวมกับ `Settings` (load_settings) เองเพื่อแสดงค่าที่แอปใช้จริง
- `overlay_info()`/`read_overlay()` คืน `Overlay` ที่ `.values` มี "ความลับจริง" — ห้ามส่งให้เทมเพลต/JSON ตอบกลับ
  ใช้ `describe()` สำหรับแสดงผลเท่านั้น (`value` ของตัวแปรลับเป็น None เสมอ)
- โมดูลนี้ **ไม่ปลอดภัยต่อหลาย process/เธรดพร้อมกัน** (อ่าน-รวม-เขียน ไม่มีล็อก) — ผู้เขียนต้องทำให้เรียงลำดับเอง
  (Task 4 จะเพิ่มล็อก) · เขียนพร้อมกันแล้วค่าของอีกฝ่ายอาจหายตอน merge
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Mapping

import config
from diseases import DISEASES, REGISTRY

log = logging.getLogger(__name__)

FILE_NAME = "settings.json"
PREV_SUFFIX = ".prev"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 1024 * 1024  # ไฟล์ตั้งค่าจริงไม่กี่ร้อยไบต์ — ใหญ่กว่านี้ถือว่าเสีย
START_FROM_MAX_DAYS = 366
BASE_URL_MAX_LEN = 2048
_CONTROL = frozenset(chr(i) for i in [*range(0x20), 0x7F])

_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_IDENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_PORT_RE = re.compile(r"[0-9]{1,5}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Field:
    secret: bool
    label: str


ALLOWED: dict[str, Field] = {
    "DB_HOST": Field(False, "ที่อยู่ฐานข้อมูล"),
    "DB_PORT": Field(False, "พอร์ตฐานข้อมูล"),
    "DB_NAME": Field(False, "ชื่อฐานข้อมูล"),
    "DB_USER": Field(False, "ผู้ใช้ฐานข้อมูล"),
    "DB_PASS": Field(True, "รหัสผ่านฐานข้อมูล"),
    "MOPH_CLIENT_KEY": Field(True, "MOPH Client Key"),
    "MOPH_SECRET_KEY": Field(True, "MOPH Secret Key"),
    "DENGUE_START_FROM": Field(False, "วันเริ่มนับเคส"),
    "DENGUE_BASE_URL": Field(False, "URL ของแอป"),
    "DENGUE_DATA_AUDITED": Field(False, "ยืนยันตรวจข้อมูลแล้ว"),
    "DENGUE_DISEASES": Field(False, "โรคที่ติดตาม"),
}
SECRET_KEYS = frozenset(k for k, f in ALLOWED.items() if f.secret)
MOPH_KEYS = ("MOPH_CLIENT_KEY", "MOPH_SECRET_KEY")


class SettingsError(ValueError):
    """ค่าตั้งไม่ถูกต้อง — `.key` คือชื่อตัวแปร ข้อความ (str) เป็นภาษาไทยและไม่มีค่าที่ผู้ใช้กรอก"""

    def __init__(self, key: str, message: str, reason: str | None = None):
        super().__init__(message)
        self.key = key
        self.reason = reason or message  # เฉพาะกฎ (ไม่มีชื่อช่อง) สำหรับที่ที่มีชื่อช่องอยู่แล้ว เช่น CLI


def _err(key: str, rule: str) -> SettingsError:
    return SettingsError(key, f"{ALLOWED[key].label} ({key}) ไม่ถูกต้อง: {rule}", rule)


# ---------- ตัวตรวจ (คืนค่าที่ปรับรูปแล้ว หรือยก SettingsError) ----------
# `today=None` = ตอนอ่านไฟล์ (ไม่ตรวจช่วงวันที่)

def _v_host(key: str, v: str, today: date | None) -> str:
    if not _HOST_RE.fullmatch(v) or config.looks_like_placeholder(v):
        raise _err(key, "ต้องเป็นชื่อโฮสต์หรือ IP ใช้ได้เฉพาะ A-Z a-z 0-9 . - ยาวไม่เกิน 253 ตัวอักษร")
    return v


def _v_port(key: str, v: str, today: date | None) -> str:
    if not _PORT_RE.fullmatch(v) or not 1 <= int(v) <= 65535:
        raise _err(key, "ต้องเป็นเลขจำนวนเต็ม 1-65535")
    return str(int(v))


def _v_ident(key: str, v: str, today: date | None) -> str:
    if not _IDENT_RE.fullmatch(v):
        raise _err(key, "ใช้ได้เฉพาะ A-Z a-z 0-9 _ . - ยาว 1-64 ตัวอักษร")
    return v


def _v_db_pass(key: str, v: str, today: date | None) -> str:
    # เก็บตรงตามที่พิมพ์ (ไม่ตัดช่องว่าง): ช่องว่าง/แท็บหัวท้ายหรือรหัสที่เป็นช่องว่างล้วนใช้ได้
    if not 1 <= len(v) <= 128 or any(c in v for c in "\r\n\x00"):
        raise _err(key, "ต้องยาว 1-128 ตัวอักษร และห้ามขึ้นบรรทัดใหม่")
    return v


def _v_moph(key: str, v: str, today: date | None) -> str:
    if v == "":
        return v  # ว่าง = ล้างค่า
    if (not 8 <= len(v) <= 256 or any(c.isspace() or c in _CONTROL for c in v)
            or config.looks_like_placeholder(v)):
        raise _err(key, "เว้นว่าง หรือยาว 8-256 ตัวอักษร ไม่มีช่องว่าง และไม่ใช่ค่าตัวอย่าง <...>")
    return v


def _v_start_from(key: str, v: str, today: date | None) -> str:
    d: date | None = None
    if _DATE_RE.fullmatch(v):
        try:
            d = date.fromisoformat(v)
        except ValueError:
            d = None
    if d is None:
        raise _err(key, "รูปแบบต้องเป็น YYYY-MM-DD")
    # ตอนอ่านไฟล์ (today=None) ตรวจแค่รูปแบบ — วัน go-live เก่า ๆ ที่ถูกต้องต้องไม่ถูกทิ้ง
    if today is not None and abs((d - today).days) > START_FROM_MAX_DAYS:
        raise _err(key, f"วันที่ห่างจากวันนี้เกิน 1 ปี — ใช้ปี ค.ศ. (ปีนี้ {today.year}) ไม่ใช่ พ.ศ.")
    return d.isoformat()


def _v_base_url(key: str, v: str, today: date | None) -> str:
    # ใช้กฎเดียวกับ config.base_url_problem แต่ไม่ส่งข้อความของมันต่อ (บางกรณีมีส่วนของค่าที่กรอก)
    if (len(v) > BASE_URL_MAX_LEN or any(c in _CONTROL for c in v)
            or config.base_url_problem(v) is not None):
        raise _err(key, "ต้องเป็น URL ขึ้นต้นด้วย http:// หรือ https:// มีชื่อโฮสต์ ไม่มีช่องว่างหรืออักขระควบคุม "
                        f"ยาวไม่เกิน {BASE_URL_MAX_LEN} ตัวอักษร และไม่ใช่ค่าตัวอย่าง <...>")
    return v


def _v_audited(key: str, v: str, today: date | None) -> str:
    if v not in ("0", "1"):
        raise _err(key, "ต้องเป็น 0 หรือ 1")
    return v


def _v_diseases(key: str, v: str, today: date | None) -> str:
    """รายการ key ของโรคคั่นด้วยจุลภาค (ต้องอยู่ในทะเบียน ≥ 1 โรค ซ้ำได้) -> เรียงตามทะเบียน ไม่ซ้ำ."""
    parts = [p.strip().lower() for p in v.split(",")]
    if not parts or any(p not in DISEASES for p in parts):
        raise _err(key, "ต้องเป็นชื่อโรคในรายการ คั่นด้วยจุลภาค อย่างน้อย 1 โรค "
                        "(" + ", ".join(d.key for d in REGISTRY) + ")")
    return ",".join(d.key for d in REGISTRY if d.key in parts)


_VALIDATORS: dict[str, Callable[[str, str, date | None], str]] = {
    "DB_HOST": _v_host,
    "DB_PORT": _v_port,
    "DB_NAME": _v_ident,
    "DB_USER": _v_ident,
    "DB_PASS": _v_db_pass,
    "MOPH_CLIENT_KEY": _v_moph,
    "MOPH_SECRET_KEY": _v_moph,
    "DENGUE_START_FROM": _v_start_from,
    "DENGUE_BASE_URL": _v_base_url,
    "DENGUE_DATA_AUDITED": _v_audited,
    "DENGUE_DISEASES": _v_diseases,
}
assert set(_VALIDATORS) == set(ALLOWED)


def _validate(key: str, value: str, today: date | None) -> str:
    if key not in ALLOWED:
        raise SettingsError(str(key), "ไม่รู้จักตัวแปรที่ขอตั้ง")
    if not isinstance(value, str):
        raise _err(key, "ต้องเป็นข้อความ")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _err(key, "มีอักขระที่บันทึกไม่ได้") from None
    # DB_PASS เก็บตรงตามที่พิมพ์ ตัวอื่นตัดช่องว่างหัวท้าย
    return _VALIDATORS[key](key, value if key == "DB_PASS" else value.strip(), today)


def validate(key: str, value: str, today: date | None = None) -> str:
    """ตรวจ+ปรับรูปค่าตามสเปก §4.2 — ผิดยก `SettingsError` (ข้อความไม่มีค่าที่กรอก).

    ตัดช่องว่างหัวท้ายทุกตัวแปร **ยกเว้น DB_PASS** (ตรวจและเก็บตามที่พิมพ์ทุกตัวอักษร ห้ามเฉพาะ CR/LF/NUL)
    สตริงที่เข้ารหัส UTF-8 ไม่ได้ (เช่น surrogate เดี่ยว) ถูกปฏิเสธทุกคีย์.
    ค่าว่างผ่านเฉพาะ MOPH_CLIENT_KEY/MOPH_SECRET_KEY (หมายถึง "ล้างค่า")."""
    return _validate(key, value, today or date.today())


def _validate_stored(key: str, value: str) -> str:
    """ตรวจค่าที่อ่านจากไฟล์: กฎเดียวกับ validate แต่ไม่ตรวจช่วงวันที่ และ "" ใช้ได้กับตัวแปรลับ (= ล้างค่า)"""
    if value == "" and key in SECRET_KEYS:
        return ""
    return _validate(key, value, None)


def validate_start_from(value: str, today: date | None = None) -> str:
    return validate("DENGUE_START_FROM", value, today)


# ---------- อ่านไฟล์ ----------

@dataclass(repr=False)
class Overlay:
    # values เก็บค่าจริง (รวมความลับ) จึงไม่อยู่ใน repr/str — __repr__ ด้านล่างแสดงเฉพาะชื่อคีย์
    values: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    has_prev: bool = False

    def __repr__(self) -> str:
        return (f"Overlay(keys={sorted(self.values)}, error={self.error!r}, updated_at={self.updated_at!r}, "
                f"updated_by={self.updated_by!r}, has_prev={self.has_prev})")


def _prev_path(path: Path) -> Path:
    return path.with_name(path.name + PREV_SUFFIX)


def _bad(path: Path, error: str) -> Overlay:
    return Overlay(values={}, error=error, has_prev=_prev_path(path).exists())


_WARNED_PERMS: set[str] = set()   # ไฟล์ที่เตือนสิทธิ์ไปแล้วในโปรเซสนี้ (ENV_PROVIDER อ่านไฟล์ทุกคำขอ — เตือนครั้งเดียวพอ)
_WARNED_PERMS_LOCK = threading.Lock()


def _warn_permissions_once(path: Path) -> None:
    key = os.path.abspath(path)
    with _WARNED_PERMS_LOCK:
        if key in _WARNED_PERMS:
            return
        _WARNED_PERMS.add(key)
    log.warning("settings file permissions too open (group/other bits set): %s", path.name)


def read_overlay(path: Path) -> Overlay:
    """อ่าน settings.json — ไม่ยก exception เด็ดขาด.

    ไม่มีไฟล์ → ว่าง ไม่มี error · อ่านไม่ได้/JSON เสีย/ไม่ใช่ object/version ≠ 1/values ไม่ใช่ object
    → values ว่าง + `error` สั้น ๆ (ไม่มีเนื้อไฟล์ ไม่มี path) · คีย์นอก ALLOWED และค่าที่ไม่ใช่สตริงถูกทิ้งเงียบ ๆ
    · ค่าของคีย์ใน ALLOWED ที่ไม่ผ่านตัวตรวจ (กฎเดียวกับตอนเขียน แต่วันที่ตรวจแค่รูปแบบ) ถูกทิ้งเฉพาะคีย์นั้น
    และ `error` ระบุชื่อคีย์ที่ถูกทิ้ง (ไม่มีค่า) · ไฟล์ที่ group/other เข้าถึงได้ (POSIX) → log เตือนหนึ่งครั้งต่อไฟล์ต่อโปรเซส"""
    path = Path(path)
    try:
        has_prev = _prev_path(path).exists()
        try:
            with open(path, "rb") as fh:
                if _posix():
                    try:
                        if os.fstat(fh.fileno()).st_mode & 0o077:
                            _warn_permissions_once(path)
                    except OSError:
                        pass
                raw = fh.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return Overlay(has_prev=has_prev)
        except OSError as exc:
            log.warning("settings overlay unreadable: %s", type(exc).__name__)
            return _bad(path, "อ่านไฟล์ค่าตั้งไม่ได้ (สิทธิ์หรือชนิดไฟล์ไม่ถูกต้อง)")
        if len(raw) > MAX_FILE_BYTES:
            return _bad(path, "ไฟล์ค่าตั้งใหญ่ผิดปกติ จึงไม่ถูกใช้")
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
        except ValueError as exc:  # JSONDecodeError และ UnicodeDecodeError เป็น ValueError ทั้งคู่
            log.warning("settings overlay invalid: %s", type(exc).__name__)
            return _bad(path, "ไฟล์ค่าตั้งเสียหรืออ่านเป็น JSON ไม่ได้ จึงไม่ถูกใช้")
        if not isinstance(doc, dict) or doc.get("version") != FORMAT_VERSION or type(doc.get("version")) is not int:
            return _bad(path, "ไฟล์ค่าตั้งมีรูปแบบหรือเวอร์ชันที่ไม่รู้จัก จึงไม่ถูกใช้")
        raw_values = doc.get("values")
        if not isinstance(raw_values, dict):
            return _bad(path, "ไฟล์ค่าตั้งมีรูปแบบไม่ถูกต้อง จึงไม่ถูกใช้")
        values: dict[str, str] = {}
        dropped: list[str] = []
        for key, value in raw_values.items():
            if key not in ALLOWED or not isinstance(value, str):
                continue
            try:
                values[key] = _validate_stored(key, value)
            except SettingsError:
                dropped.append(key)
        if dropped:
            log.warning("settings overlay has invalid values, ignored keys: %s", sorted(dropped))
        at, by = doc.get("updated_at"), doc.get("updated_by")
        return Overlay(values=values, updated_at=at if isinstance(at, str) else None,
                       updated_by=by if isinstance(by, str) else None, has_prev=has_prev,
                       error=("ค่าบางตัวในไฟล์ค่าตั้งไม่ถูกต้องจึงไม่ถูกใช้: " + ", ".join(sorted(dropped))) if dropped else None)
    except Exception as exc:  # noqa: BLE001 — สัญญา: ไม่ยกเด็ดขาด
        log.warning("settings overlay failed: %s", type(exc).__name__)
        return Overlay(values={}, error="อ่านไฟล์ค่าตั้งไม่สำเร็จ จึงไม่ถูกใช้")


# ---------- เขียนไฟล์ ----------

def _posix() -> bool:
    return os.name == "posix"


def _temp_path(directory: Path, base: str) -> Path:
    return directory / f".{base}.{os.getpid()}.{secrets.token_hex(6)}.tmp"


def _write_temp(directory: Path, base: str, data: bytes) -> Path:
    """สร้างไฟล์ชั่วคราว (O_EXCL, 0o600) ในโฟลเดอร์เดียวกัน เขียน+fsync — ล้มเหลวแล้วลบทิ้งก่อนยก exception"""
    tmp = _temp_path(directory, base)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            if _posix():
                os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    return tmp


def _unlink_quiet(p: Path) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    """POSIX: fsync โฟลเดอร์เพื่อให้การเปลี่ยนชื่อไฟล์คงอยู่หลังไฟดับ (พยายามเต็มที่ ข้อผิดพลาดถูกข้าม)"""
    if not _posix():
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _replace_from_temp(tmp: Path, dest: Path) -> None:
    try:
        os.replace(tmp, dest)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    _fsync_dir(dest.parent)


def _is_regular_file(p: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def write_overlay(path: Path, values: dict[str, str], user: str, now: datetime | None = None) -> None:
    """รวม `values` เข้ากับค่าเดิมในไฟล์ (ไฟล์เสียนับเป็นว่าง) แล้วเขียนแบบ atomic.

    ตรวจทุกคีย์ก่อน — คีย์ใดผิด/ไม่รู้จักยก `SettingsError` และไม่เขียนอะไรเลย (เนื้อไฟล์ที่จะเขียนใหญ่เกิน
    MAX_FILE_BYTES → `SettingsError` คีย์ "file" และไม่เขียนเช่นกัน).
    ค่าว่าง "" ใช้ได้เฉพาะตัวแปรลับ 3 ตัว (DB_PASS, MOPH_*) หมายถึง "ล้างค่า" (ทับ .env ด้วยค่าว่าง)
    ลำดับ: สำเนาไฟล์เดิมเป็น `.prev` (atomic) → เขียนไฟล์ใหม่ชั่วคราว+fsync → os.replace ทับ — ล้มเหลวที่ขั้นใด
    ไฟล์เดิมยังอยู่ครบและไม่เหลือไฟล์ชั่วคราว. ไฟล์เดิมที่อ่านไม่ผ่าน (error) จะไม่ถูกหมุนเป็น `.prev`
    เพื่อไม่ให้ทับรุ่นดีล่าสุดที่เก็บไว้."""
    path = Path(path)
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in ALLOWED:
            raise SettingsError(str(key), "ไม่รู้จักตัวแปรที่ขอตั้ง")
        if value == "" and key in SECRET_KEYS:
            clean[key] = ""
        else:
            clean[key] = validate(key, value)
    current = read_overlay(path)
    merged = dict(current.values)
    merged.update(clean)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    doc = {"version": FORMAT_VERSION, "updated_at": stamp, "updated_by": str(user)[:100], "values": merged}
    data = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise SettingsError("file", "ค่าตั้งรวมกันใหญ่เกินกำหนด จึงไม่ได้บันทึก")

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if _is_regular_file(path) and current.error is None:
        tmp_prev = _write_temp(directory, _prev_path(path).name, path.read_bytes())
        _replace_from_temp(tmp_prev, _prev_path(path))
    tmp = _write_temp(directory, path.name, data)
    _replace_from_temp(tmp, path)
    log.info("settings written: keys=%s", sorted(clean))


def revert_overlay(path: Path, user: str, now: datetime | None = None) -> bool:
    """สลับ settings.json กับ settings.json.prev — ไม่มี .prev หรือ .prev อ่านไม่ผ่าน → False.

    เป็นการ "สลับ" (ไม่ใช่ลบ) จึงเรียกซ้ำสองครั้งแล้วกลับไปค่าล่าสุดได้ ไม่ทำค่าหาย
    ลำดับ (settings.json ไม่เคยหายหรือขาดครึ่ง): 1) สำเนาปัจจุบันเป็นไฟล์ชั่วคราว (600) 2) os.replace(prev → ปัจจุบัน)
    3) os.replace(ชั่วคราว → prev). ล้มเหลวขั้น 1-2: ลบไฟล์ชั่วคราว ยก exception ไม่มีอะไรเปลี่ยน ·
    ล้มเหลวขั้น 3: การย้อน "มีผลแล้ว" จึงคืน True (log เฉพาะชนิด exception) พยายามเปลี่ยนชื่อชั่วคราว → .prev ซ้ำ
    ถ้ายังไม่ได้จะเก็บไฟล์ชั่วคราวไว้ (มีรุ่นก่อนย้อนอยู่ในนั้น กู้ได้).
    ไม่มี settings.json แต่มี .prev → ย้าย .prev มาเป็นไฟล์จริง
    `user`/`now` รับไว้ให้อินเทอร์เฟซตรงกับ write_overlay (ยังไม่ใช้)."""
    path = Path(path)
    prev = _prev_path(path)
    if not _is_regular_file(prev) or read_overlay(prev).error is not None:
        return False
    directory = path.parent
    if not _is_regular_file(path):
        os.replace(prev, path)
        _fsync_dir(directory)
        log.info("settings reverted: current was missing")
        return True
    aside = _write_temp(directory, path.name, path.read_bytes())  # 1) สำเนาปัจจุบัน
    try:
        os.replace(prev, path)  # 2) prev → ปัจจุบัน (atomic: ปัจจุบันไม่เคยหาย)
    except BaseException:
        _unlink_quiet(aside)
        raise
    try:
        os.replace(aside, prev)  # 3) สำเนา → prev
    except Exception as exc:  # noqa: BLE001
        log.error("settings revert could not refresh .prev: %s", type(exc).__name__)
        try:
            os.replace(aside, prev)  # ลองอีกครั้งแบบพยายามเต็มที่ ไม่สำเร็จก็เก็บไฟล์ชั่วคราวไว้ให้กู้
        except OSError:
            pass
    _fsync_dir(directory)
    log.info("settings reverted")
    return True


# ---------- ซ้อนทับ env ----------

def settings_path(env: Mapping[str, str]) -> Path:
    """โฟลเดอร์ของ DENGUE_DB_PATH (ค่าเริ่มต้น data/dengue.db) + settings.json"""
    return Path(env.get("DENGUE_DB_PATH") or "data/dengue.db").parent / FILE_NAME


@dataclass(repr=False)
class Resolved:
    """ผลการซ้อนทับครั้งเดียว: `env` = env ที่มีผลจริง · `base` = สำเนา env ก่อนซ้อนทับ · `overlay` = ไฟล์ที่อ่านได้.

    `base`/`env`/`overlay.values` มีความลับจริง — ห้ามใส่ใน repr/log/เทมเพลต (repr ซ่อนไว้ทั้งหมด)"""
    env: dict[str, str]
    base: dict[str, str]
    overlay: Overlay

    def __repr__(self) -> str:
        return f"Resolved(overlay={self.overlay!r})"


def resolve(base: Mapping[str, str] | None = None) -> Resolved:
    """อ่านไฟล์ค่าตั้งหนึ่งครั้งแล้วคืน env ที่ซ้อนทับ + สำเนา base + overlay (ผู้เรียกที่ต้องการ error ใช้ตัวนี้
    แทนการเรียก effective_env แล้ว read_overlay ซ้ำ)."""
    base_env = dict(os.environ if base is None else base)
    overlay = read_overlay(settings_path(base_env))
    merged = dict(base_env)
    merged.update(overlay.values)  # values กรองเหลือเฉพาะ ALLOWED มาแล้วใน read_overlay
    return Resolved(env=merged, base=base_env, overlay=overlay)


def effective_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """สำเนา env ที่ซ้อน overlay แล้ว (คีย์ใน overlay ชนะ · "" ใน overlay = ล้างค่า env).

    ไม่ยก exception เพราะไฟล์เสีย (ไม่ใช้ overlay) และไม่แตะ os.environ."""
    return resolve(base).env


def overlay_info(base: Mapping[str, str] | None = None) -> Overlay:
    """overlay ปัจจุบัน (ไว้แสดง error/updated_by/has_prev ในหน้าตั้งค่า) — ตำแหน่งไฟล์ตาม settings_path(base)"""
    return read_overlay(settings_path(dict(os.environ if base is None else base)))


def describe(base: Mapping[str, str] | None = None) -> dict[str, dict]:
    """สถานะต่อตัวแปรสำหรับแสดงผล: label, secret, state (set/unset), source (overlay/env/default), value.

    `value` เป็น None เสมอสำหรับตัวแปรลับ; ตัวแปรอื่นคือค่าที่มีผลจริง (None เมื่อ unset)."""
    env = dict(os.environ if base is None else base)
    overlay = read_overlay(settings_path(env))
    out: dict[str, dict] = {}
    for key, spec in ALLOWED.items():
        if key in overlay.values:
            source, effective = "overlay", overlay.values[key]
        elif env.get(key):
            source, effective = "env", env[key]
        else:
            source, effective = "default", ""
        out[key] = {
            "label": spec.label,
            "secret": spec.secret,
            "state": "set" if effective else "unset",
            "source": source,
            "value": None if spec.secret or not effective else effective,
        }
    return out
