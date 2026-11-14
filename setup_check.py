"""ตรวจสภาพแวดล้อมก่อนใช้แอป (กฎ "ตรวจก่อนใช้" ข้อ 1 — สเปกข้อ 10).

  python setup_check.py        พิมพ์ผล ✔/⚠/✘ ทีละรายการ; รหัสออก 1 เมื่อมี ✘, ไม่งั้น 0

- `run_checks` คืน list ของ (สถานะ "ok"|"warn"|"fail", ชื่อรายการ, รายละเอียด) แต่ละรายการแยกกัน
  รายการหนึ่งล้ม/ยก exception ไม่หยุดรายการอื่น (exception -> fail โดยรายละเอียดมีเฉพาะชื่อชนิด)
- อ่าน HOSxP ผ่านเมธอดอ่านอย่างเดียวของ HosxpSource ซึ่งคืนเฉพาะจำนวนนับ/โครงสร้าง ไม่มีข้อมูลรายคน
- ไม่พิมพ์กุญแจ/รหัสผ่าน/ชื่อบัญชีฐานข้อมูล
- ไม่ import Flask/pymysql ตอนโหลดโมดูล (เพื่อให้รายงานว่าไลบรารีหายได้ ไม่ใช่ล้มก่อนตรวจ)
"""
from __future__ import annotations

import importlib
import re
import socket
import sys
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit

import boundary
from diseases import GROUP_LABELS, MODE_LABELS, REGISTRY, env_names
import trend
from config import MSG_START_FROM_UNSET, base_url_problem, looks_like_placeholder, secret_key_problem
from envfile import load_dotenv_if_present

OK, WARN, FAIL = "ok", "warn", "fail"
ICONS = {OK: "✔", WARN: "⚠", FAIL: "✘"}

MIN_PYTHON = (3, 10)
LIBRARIES = ("flask", "pymysql", "requests")
TZ_WARN_MINUTES = 5
STALE_DAYS = 30
LOOKBACK_DAYS = 365
MOPH_TIMEOUT_S = 5
INT_TYPES = frozenset({"tinyint", "smallint", "mediumint", "int", "integer", "bigint"})
THAI_RE = re.compile("[฀-๿]")

# ตาราง -> คอลัมน์ที่แอปอ่านจริง (ดู hosxp.py); thaiaddress มีไว้แปลงรหัสเป็นชื่อพื้นที่เท่านั้น
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "ovstdiag": ("vn", "hn", "icd10", "vstdate", "vsttime"),
    "iptdiag": ("an", "icd10"),
    "ovst": ("vn", "vstdate", "vsttime"),
    "ipt": ("an", "hn", "regdate", "regtime"),
    "patient": ("hn", "pname", "fname", "lname", "mobile_phone_number", "hometel",
                "informtel", "addrpart", "moopart", "tmbpart", "amppart", "chwpart",
                "sex", "birthday"),
    "village": ("village_code", "village_name", "address_id"),
    "village_gis_location": ("village_code", "latitude", "longitude"),
    "thaiaddress": ("addressid", "name"),
}
OPTIONAL_TABLES = ("thaiaddress",)
KEY_COLUMNS = (("ovstdiag", "vn"), ("ovst", "vn"), ("iptdiag", "an"), ("ipt", "an"))


def _type_name(exc: BaseException) -> str:
    return type(exc).__name__


class _NoStore(Exception):
    """ไม่มีฐานข้อมูลของแอปให้ตรวจพิกัดสถานพยาบาล."""


class _Context:
    """ผลการอ่านฐานที่ใช้ร่วมกันหลายรายการ (อ่านครั้งเดียว; ถ้าล้มจะยกซ้ำให้ทุกรายการที่ต้องใช้)."""

    def __init__(self, settings, source, moph_configured: bool, overlay=None, store=None):
        self.settings = settings
        self.source = source
        self.store = store  # ฐานของแอป (ตาราง facility) — None = ตรวจพิกัดสถานพยาบาลไม่ได้
        self.moph_configured = moph_configured
        self.overlay = overlay  # settings_store.Overlay ที่แอปอ่านตอนเริ่ม (None = ไม่มีไฟล์)
        self.since = date.today() - timedelta(days=LOOKBACK_DAYS)
        self._cache: dict[str, tuple[bool, Any]] = {}

    def once(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            try:
                self._cache[key] = (True, fn())
            except Exception as e:  # noqa: BLE001
                self._cache[key] = (False, e)
        ok, value = self._cache[key]
        if not ok:
            raise value
        return value

    # ---- การอ่านที่ใช้ร่วม ----
    def now(self) -> datetime:
        return self.once("now", self.source.server_now)

    def columns(self) -> dict[str, dict[str, str]]:
        return self.once("columns",
                         lambda: self.source.table_columns(tuple(REQUIRED_COLUMNS)))

    def points(self) -> list[dict]:
        return self.once("points", self.source.village_points)

    def villages(self) -> dict:
        return self.once("villages", self.source.village_counts)

    def default_area(self) -> tuple[str, ...]:
        return self.once("default_area", self.source.default_area_amphoe)

    def icd_found(self, key: str = "dengue") -> dict[str, int]:
        """จำนวน visit ตามรหัสที่พบจริงของโรค `key` (ค่าเริ่มต้น = ไข้เลือดออก)."""
        rule = self.settings.rule(key)
        icds = tuple(rule.icds) if rule is not None else tuple(self.settings.icds)
        prefixes = tuple(i.replace(".", "").strip().upper() for i in icds)
        # อ่านฐานครั้งเดียวสำหรับทุกโรค (ชุดรหัสรวม) แล้วแยกตามรหัสนำหน้าของแต่ละโรค
        found = self.once("icds", lambda: self.source.icd_counts(self._all_icds(), self.since))
        return {c: n for c, n in found.items() if c.startswith(prefixes)}

    def _all_icds(self) -> tuple[str, ...]:
        seen: dict[str, None] = dict.fromkeys(self.settings.icds)
        for d in REGISTRY:
            rule = self.settings.rule(d.key)
            seen.update(dict.fromkeys(rule.icds if rule is not None else d.icd_prefixes))
        return tuple(seen)

    def area_found(self) -> dict[str, dict]:
        return self.once("areas", lambda: self.source.area_counts(self.settings.icds, self.since))

    def year_visits(self) -> list[tuple[int, int]]:
        """[(ปี พ.ศ., จำนวน visit ตามรหัสโรค)] ปี พ.ศ.ปัจจุบันและย้อนหลัง 3 ปี (ใหม่ → เก่า) ไว้ดูว่าหน้า /trend มีข้อมูลเทียบไหม."""
        def read() -> list[tuple[int, int]]:
            today = date.today()
            yrs = trend.recent_years(today, 4)
            ranges = [(s, min(e, today)) for s, e in (trend.year_range(year) for year in yrs)]
            return list(zip(yrs, self.source.visits_in_ranges(self.settings.icds, ranges)))
        return self.once("years", read)

    def boundary_rows(self) -> list[dict]:
        """[{"code", "ok"}] ของอำเภอที่ถือว่า "ในเขต": มีไฟล์เส้นขอบที่ใช้ได้ไหม (อ่านไฟล์ในเครื่องเท่านั้น ไม่ออกเน็ต)."""
        def read() -> list[dict]:
            area, _ = self.area_used()
            return [{"code": c, "ok": boundary.load_boundary(c, boundary.BOUNDARY_DIR) is not None}
                    for c in area]
        return self.once("boundary", read)

    def tambon_rows(self) -> list[dict]:
        """[{"code", "ok", "names"}] ของอำเภอที่ถือว่า "ในเขต": มีไฟล์ตำบลที่ใช้ได้ไหม (ไฟล์ในเครื่องเท่านั้น ไม่ออกเน็ต)."""
        def read() -> list[dict]:
            area, _ = self.area_used()
            out = []
            for c in area:
                doc = boundary.load_tambons(c, boundary.BOUNDARY_DIR)
                out.append({"code": c, "ok": doc is not None,
                            "names": [f["properties"]["name"] for f in doc["features"]] if doc else []})
            return out
        return self.once("tambon", read)

    def facility_state(self) -> dict:
        """รายชื่อ รพ./รพ.สต. จาก HOSxP เทียบกับพิกัดใน SQLite ของแอป (ไม่มี store -> _NoStore)."""
        def read() -> dict:
            if self.store is None:
                raise _NoStore()
            area, _ = self.area_used()
            catalog = list(self.source.facilities(tuple(area))) if area else []
            placed = {r["hospcode"] for r in self.store.facility_all()}
            return {"catalog": len(catalog),
                    "placed": sum(1 for c in catalog if c["hospcode"] in placed),
                    "unplaced": [{"code": c["hospcode"], "name": c["name"], "kind": c["kind"]}
                                 for c in catalog if c["hospcode"] not in placed]}
        return self.once("facilities", read)

    def area_used(self) -> tuple[tuple[str, ...], str]:
        """(รหัสอำเภอ 4 หลักที่ถือว่า "ในเขต", แหล่งที่มา "env"|"village")."""
        if self.settings.area_amphoe:
            return tuple(self.settings.area_amphoe), "env"
        return tuple(self.default_area()), "village"


# ---------- รายการตรวจ (แต่ละอัน คืน (status, detail)) ----------

def _check_python(ctx):
    v = tuple(sys.version_info[:3])
    text = ".".join(str(p) for p in v)
    if v < MIN_PYTHON:
        return FAIL, f"Python {text} ต่ำกว่า {MIN_PYTHON[0]}.{MIN_PYTHON[1]} (โค้ดใช้ไวยากรณ์ PEP 604)"
    return OK, f"Python {text}"


def _check_libs(ctx):
    missing = []
    for name in LIBRARIES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if missing:
        return FAIL, "ไม่พบไลบรารี: " + ", ".join(missing) + " (pip install -r requirements.txt)"
    return OK, "พบ " + ", ".join(LIBRARIES)


def _check_connection(ctx):
    try:
        ctx.now()
    except Exception as e:  # noqa: BLE001
        return FAIL, f"ต่อ HOSxP ไม่ได้ ({_type_name(e)}) — ตรวจ DB_HOST / DB_USER / DB_PASS"
    return OK, "ต่อ HOSxP ได้"


def _check_statement_timeout(ctx):
    if ctx.once("timeout", ctx.source.statement_timeout_supported):
        return OK, "ตั้ง timeout คำสั่งตรวจ (max_statement_time) ได้"
    return WARN, ("ตั้ง timeout คำสั่งไม่ได้ (max_statement_time) "
                  "— คำสั่งตรวจที่ช้าจะไม่ถูกตัดอัตโนมัติ")


def _check_timezone(ctx):
    diff = abs((ctx.now() - datetime.now()).total_seconds()) / 60
    if diff > TZ_WARN_MINUTES:
        return WARN, (f"เวลาฐาน HOSxP ต่างจากเซิร์ฟเวอร์แอป {diff:.0f} นาที "
                      "— ตรวจเขตเวลา (เวลาวินิจฉัยและช่วงวันจะเพี้ยน)")
    return OK, f"เวลาตรงกัน (ต่างกัน {diff:.1f} นาที)"


def _check_read_only(ctx):
    if ctx.once("ro", ctx.source.session_read_only):
        return OK, "การเชื่อมต่อตั้งเป็นอ่านอย่างเดียว (READ ONLY)"
    return FAIL, "การเชื่อมต่อนี้ไม่ได้เป็นอ่านอย่างเดียว — ห้ามใช้งาน"


def _check_grants(ctx):
    privs = ctx.once("grants", ctx.source.write_privileges)
    if privs:
        return WARN, ("บัญชีฐานข้อมูลมีสิทธิ์เขียน: " + ", ".join(privs)
                      + " — ควรใช้บัญชีที่มีเฉพาะ SELECT")
    return OK, "บัญชีฐานข้อมูลไม่มีสิทธิ์เขียนบนฐาน HOSxP"


def _check_tables(ctx):
    cols = ctx.columns()
    missing = [t for t in REQUIRED_COLUMNS if t not in cols]
    must = [t for t in missing if t not in OPTIONAL_TABLES]
    if must:
        return FAIL, "ไม่พบตาราง: " + ", ".join(must)
    if missing:
        return WARN, ("ไม่พบตาราง: " + ", ".join(missing)
                      + " — ชื่อพื้นที่จะแสดงเป็นรหัสแทน")
    return OK, f"พบครบ {len(REQUIRED_COLUMNS)} ตาราง"


def _check_columns(ctx):
    cols = ctx.columns()
    missing, missing_optional = [], []
    for table, needed in REQUIRED_COLUMNS.items():
        if table not in cols:
            continue  # ตารางหายรายงานในรายการ "ตารางที่ใช้" แล้ว
        lost = [f"{table}.{c}" for c in needed if c not in cols[table]]
        (missing_optional if table in OPTIONAL_TABLES else missing).extend(lost)
    if missing:
        return FAIL, "ไม่พบคอลัมน์: " + ", ".join(missing + missing_optional)
    if missing_optional:
        return WARN, "ไม่พบคอลัมน์: " + ", ".join(missing_optional)
    return OK, "คอลัมน์ที่แอปอ่านมีครบ"


def _check_key_types(ctx):
    cols = ctx.columns()
    ints = [f"{t}.{c}={cols[t][c]}" for t, c in KEY_COLUMNS
            if t in cols and cols[t].get(c) in INT_TYPES]
    if ints:
        return WARN, ("vn/an เป็นชนิดตัวเลข: " + ", ".join(ints)
                      + " — ตัวตรวจจับเทียบคีย์ VN:<n> เป็นข้อความ ต้องทดสอบก่อนใช้")
    return OK, "vn/an เป็นชนิดข้อความ"


def _check_thai(ctx):
    samples = ctx.once("thai", ctx.source.thai_samples)
    if not samples:
        return WARN, "ไม่มีข้อความตัวอย่างในตาราง village ให้ตรวจภาษาไทย"
    if any(THAI_RE.search(s) for s in samples):
        return OK, "อ่านภาษาไทยได้"
    return FAIL, ("อ่านภาษาไทยไม่ได้ (ได้เครื่องหมาย ? หรืออักขระเพี้ยน) "
                  "— ตรวจ charset utf8mb4 ของการเชื่อมต่อและของตาราง")


def _check_latest_visit(ctx):
    latest = ctx.once("latest", ctx.source.latest_visit_date)
    if latest is None:
        return WARN, "ไม่พบวันที่ในตารางวินิจฉัย OPD (ovstdiag)"
    age = (date.today() - latest).days
    if age > STALE_DAYS:
        return WARN, (f"วินิจฉัยล่าสุด {latest.isoformat()} ({age} วันก่อน) "
                      "— ตรวจว่าต่อฐานที่ใช้งานจริงและข้อมูลเป็นปัจจุบัน")
    return OK, f"วินิจฉัยล่าสุด {latest.isoformat()}"


def _check_village(ctx):
    total = ctx.villages()["total"]
    if total == 0:
        return WARN, "ตาราง village ไม่มีแถว — หมู่บ้านของ รพ. และ \"ในเขต\" ใช้ค่าเริ่มต้นไม่ได้"
    return OK, f"ตาราง village มี {total} หมู่บ้าน"


def _check_area(ctx):
    area, origin = ctx.area_used()
    if origin == "env":
        return OK, "ตั้งเองด้วย DENGUE_AREA_AMPHOE: " + ", ".join(area)
    if not area:
        return WARN, ("ยังไม่ได้ตั้ง DENGUE_AREA_AMPHOE และหาอำเภอจากตาราง village ไม่ได้ "
                      "— ทุกเคสจะถูกนับว่านอกเขต")
    return OK, "ใช้ค่าเริ่มต้นจากตาราง village: " + ", ".join(area)


def _check_centroid(ctx):
    c = ctx.villages()
    if c["total"] == 0:
        return WARN, "ไม่มีหมู่บ้านให้ตรวจจุดกลาง"
    lacking = c["total"] - c["with_centroid"]
    if lacking > 0:
        return WARN, (f"หมู่บ้านของ รพ. {c['total']} แห่ง มีจุดกลาง {c['with_centroid']} แห่ง "
                      f"(ขาด {lacking}) — เคสในหมู่บ้านที่ขาดจะต้องปักหมุดมือ")
    return OK, f"หมู่บ้านของ รพ. {c['total']} แห่ง มีจุดกลางครบ (ตรวจตำแหน่งด้วยตาที่หน้า /check)"


def _check_icds(ctx):
    found = ctx.icd_found()
    used = ", ".join(ctx.settings.icds)
    if not found:
        return WARN, (f"แอปใช้ {used} · ไม่พบรหัสที่ตรงใน 12 เดือนล่าสุด "
                      "— ตรวจ DENGUE_ICDS ว่าตรงกับรหัสที่ HOSxP ของ รพ. นี้ใช้")
    return OK, (f"แอปใช้ {used} · พบในข้อมูล 12 เดือน {len(found)} รหัส "
                f"({sum(found.values())} visit)")


def _check_disease_icds(ctx, rule):
    """รหัสโรคของโรคอื่นที่เปิด (ไข้เลือดออกมีแถว DENGUE_ICDS ของตัวเอง)."""
    found = ctx.icd_found(rule.key)
    used = ", ".join(rule.icds)
    if not found:
        return WARN, (f"แอปใช้ {used} · ไม่พบรหัสที่ตรงใน 12 เดือนล่าสุด "
                      f"— ตรวจ {env_names(rule.key)['icds']} ว่าตรงกับรหัสที่ HOSxP ของ รพ. นี้ใช้")
    return OK, (f"แอปใช้ {used} · พบในข้อมูล 12 เดือน {len(found)} รหัส "
                f"({sum(found.values())} visit)")


def _check_year_visits(ctx):
    try:
        rows = ctx.year_visits()
    except Exception as e:  # noqa: BLE001 — เสริมหน้า /trend เท่านั้น ไม่ควรทำให้การตรวจทั้งชุดล้ม
        return WARN, (f"นับ visit ตามปี พ.ศ.ไม่สำเร็จ ({_type_name(e)}) "
                      "— หน้า /trend อาจดึงข้อมูลย้อนหลังไม่ได้หรือช้า")
    summary = " · ".join(f"{year}: {n}" for year, n in rows)
    empty = [str(year) for year, n in rows[1:] if n == 0]
    if empty:
        return WARN, (f"ปี พ.ศ. {', '.join(empty)} ไม่มี visit ตามรหัสโรค — หน้า /trend จะตัดปีนั้นออกจากเกณฑ์ระบาด "
                      f"({summary})")
    return OK, f"จำนวน visit ตามปี พ.ศ. (ปีปัจจุบันนับถึงวันนี้) {summary}"


def _check_boundary(ctx):
    try:
        rows = ctx.boundary_rows()
    except Exception as e:  # noqa: BLE001 — เสริมหน้าแผนที่เท่านั้น ไม่ควรทำให้การตรวจทั้งชุดล้ม
        return WARN, (f"ตรวจไฟล์เส้นขอบอำเภอไม่สำเร็จ ({_type_name(e)}) "
                      "— แผนที่จะใช้พิกัดตั้งต้น")
    if not rows:
        return WARN, "ยังไม่ทราบอำเภอในเขต — แผนที่ไม่มีเส้นขอบและใช้พิกัดตั้งต้น"
    missing = [r["code"] for r in rows if not r["ok"]]
    if missing:
        return WARN, (f"ไม่มีไฟล์เส้นขอบ (หรือใช้ไม่ได้) ของอำเภอ {', '.join(missing)} — แผนที่จะใช้พิกัดตั้งต้น · "
                      "สร้างด้วย python deploy/fetch_boundary.py <รหัส> --query \"<ชื่ออำเภอ>\" "
                      "(ดู INSTALL.md ข้อ 13)")
    return OK, "มีไฟล์เส้นขอบอำเภอ " + ", ".join(r["code"] for r in rows)


def _check_tambon(ctx):
    try:
        rows = ctx.tambon_rows()
    except Exception as e:  # noqa: BLE001 — เสริมหน้าแผนที่เท่านั้น ไม่ควรทำให้การตรวจทั้งชุดล้ม
        return WARN, f"ตรวจไฟล์ขอบเขตตำบลไม่สำเร็จ ({_type_name(e)}) — แผนที่จะไม่มีชั้นตำบล"
    if not rows:
        return WARN, "ยังไม่ทราบอำเภอในเขต — แผนที่ไม่มีชั้นตำบล"
    missing = [r["code"] for r in rows if not r["ok"]]
    if missing:
        return WARN, (f"ไม่มีไฟล์ขอบเขตตำบล (หรือใช้ไม่ได้) ของอำเภอ {', '.join(missing)} — แผนที่จะไม่มีชั้นตำบล · "
                      "สร้างด้วย python deploy/make_tambon_boundary.py <รหัสอำเภอ> <ไฟล์ต้นทาง.geojson> "
                      "(ดู INSTALL.md ข้อ 14)")
    return OK, "มีไฟล์ขอบเขตตำบล " + " · ".join(f"{r['code']} ({len(r['names'])} ตำบล)" for r in rows)


def _check_facilities(ctx):
    try:
        st = ctx.facility_state()
    except _NoStore:
        return WARN, "ตรวจพิกัดสถานพยาบาลไม่ได้ (ไม่มีฐานข้อมูลแอป)"
    except Exception as e:  # noqa: BLE001
        return WARN, (f"ตรวจรายชื่อสถานพยาบาลไม่สำเร็จ ({_type_name(e)}) "
                      "— หมุด รพ./รพ.สต. บนแผนที่อาจไม่ครบ")
    if st["catalog"] == 0:
        return WARN, "ไม่พบ รพ./รพ.สต. ของอำเภอที่ใช้ในตาราง hospcode — ไม่มีหมุดสถานพยาบาลบนแผนที่"
    if st["unplaced"]:
        names = ", ".join(u["name"] for u in st["unplaced"])
        return WARN, (f"{len(st['unplaced'])} แห่งยังไม่มีพิกัด: {names} · นำเข้าด้วย "
                      "python run.py import-facilities <ไฟล์.csv> หรือให้ผู้ดูแลวางหมุดเองที่หน้าแผนที่ "
                      "(ปุ่ม \"จัดตำแหน่งสถานพยาบาล\")")
    return OK, f"สถานพยาบาล {st['catalog']} แห่งมีพิกัดครบ"


def _check_moph_keys(ctx):
    s = ctx.settings
    if any(looks_like_placeholder(k or "") for k in (s.moph_client_key, s.moph_secret_key)):
        return WARN, ("MOPH_CLIENT_KEY / MOPH_SECRET_KEY ยังเป็นค่าตัวอย่าง <...> "
                      "— ถือว่ายังไม่ได้ตั้ง จะตรวจจับเคสได้แต่ไม่ส่ง LINE")
    if ctx.moph_configured:
        return OK, "ตั้งกุญแจ MOPH Notify แล้ว"
    return WARN, "ยังไม่ได้ตั้ง MOPH_CLIENT_KEY / MOPH_SECRET_KEY — จะตรวจจับเคสได้แต่ไม่ส่ง LINE"


def tcp_reachable(host: str, port: int, timeout: float = MOPH_TIMEOUT_S) -> None:
    """เปิด-ปิดการเชื่อม TCP ไป host:port (ไม่ส่งข้อมูลใด ๆ) — ยก exception เมื่อต่อไม่ได้.
    ใช้ร่วมกับปุ่ม "ทดสอบ MOPH" ในหน้าตั้งค่า (settings_checks)"""
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _check_moph_host(ctx):
    parsed = urlparse(ctx.settings.moph_base_url)
    host = parsed.hostname
    if not host:
        return WARN, "MOPH_BASE_URL อ่านชื่อโฮสต์ไม่ได้"
    port = parsed.port or 443
    try:
        tcp_reachable(host, port)
    except Exception as e:  # noqa: BLE001 — เครือข่ายล้มทุกแบบ = เตือนเท่านั้น
        return WARN, f"ต่อ {host}:{port} ไม่ได้ ({_type_name(e)}) — ตรวจไฟร์วอลล์ขาออก"
    return OK, f"ต่อ {host}:{port} ได้ (ไม่ได้ส่งกุญแจ/ข้อความ)"


def _check_secret(ctx):
    problem = secret_key_problem(ctx.settings.secret_key)
    if problem:
        return FAIL, f"{problem} (แอปเปิดสู่อินเทอร์เน็ตผ่าน Cloudflare จึงไม่ยอมเริ่ม)"
    return OK, "ตั้ง SECRET_KEY แล้ว"


def _check_base_url(ctx):
    problem = base_url_problem(ctx.settings.base_url)
    if problem:
        return FAIL, f"{problem} — ลิงก์ในข้อความ LINE จะใช้ไม่ได้"
    parts = urlsplit(ctx.settings.base_url.strip())
    return OK, f"ลิงก์ในข้อความ LINE ใช้ {parts.scheme}://{parts.hostname}"


def _check_start_from(ctx):
    s = ctx.settings
    note = getattr(s, "start_from_note", "")
    if note == "unset":   # ค่าจะเลื่อนไปเป็นวันที่เริ่มระบบทุกครั้งที่ restart: เคสที่ลงรหัสย้อนหลังหลัง restart ไม่ถูกตรวจ
        return WARN, MSG_START_FROM_UNSET
    if note == "invalid":
        return WARN, ("DENGUE_START_FROM อ่านเป็นวันที่ไม่ได้ (ต้องเป็น YYYY-MM-DD ปี ค.ศ.) "
                      f"— ตอนนี้ใช้วันนี้ ({s.start_from.isoformat()}) แทน ตรวจค่าใน .env")
    if note == "future":
        return WARN, (f"DENGUE_START_FROM = {s.start_from.isoformat()} เป็นวันที่ในอนาคตไกล "
                      "— ถ้าใส่ปี พ.ศ. ให้แก้เป็น ค.ศ. ไม่เช่นนั้นแอปจะไม่แจ้งเตือนเคสใดเลยจนถึงวันนั้น")
    return OK, f"แจ้งเฉพาะวินิจฉัยตั้งแต่ {s.start_from.isoformat()}"


def _check_audited(ctx):
    if ctx.settings.data_audited:
        return OK, "ตั้ง DENGUE_DATA_AUDITED=1 แล้ว (แถบเตือนถูกซ่อน)"
    return WARN, ("ยังไม่ได้ตั้ง DENGUE_DATA_AUDITED=1 — แถบเตือนจะแสดงจนกว่าจะตรวจตัวเลข"
                  "กับข้อมูล รพ. นี้แล้วและตั้งค่า หรือกดยืนยันที่หน้า /check")


def _check_overlay(ctx):
    """ไฟล์ค่าตั้งจากหน้าเว็บ (data/settings.json): ไม่มี/อ่านได้ครบ = ok · เสียหรือบางค่าถูกทิ้ง = warn.
    แสดงเฉพาะจำนวนค่าและข้อความคงที่ (มีแต่ชื่อคีย์) — ไม่แสดงค่า"""
    ov = ctx.overlay
    if ov is None:
        return OK, "ไม่มี"
    if ov.error is not None:
        used = f" (ยังใช้ค่าที่ถูกต้อง {len(ov.values)} ค่า)" if ov.values else ""
        return WARN, f"{ov.error}{used} — ตรวจที่หน้าตั้งค่า หรือย้อนกลับ/ลบไฟล์ settings.json"
    if not ov.values:
        return OK, "ไม่มี"
    return OK, f"ใช้ค่าจากหน้าตั้งค่า {len(ov.values)} ค่า"


CHECKS: tuple[tuple[str, Callable], ...] = (
    ("Python", _check_python),
    ("ไลบรารี", _check_libs),
    ("ไฟล์ตั้งค่าจากหน้าเว็บ", _check_overlay),
    ("เชื่อมต่อ HOSxP", _check_connection),
    ("timeout คำสั่งฐานข้อมูล", _check_statement_timeout),
    ("เขตเวลา", _check_timezone),
    ("อ่านอย่างเดียว (session)", _check_read_only),
    ("สิทธิ์บัญชีฐานข้อมูล", _check_grants),
    ("ตารางที่ใช้", _check_tables),
    ("คอลัมน์ที่ใช้", _check_columns),
    ("ชนิด vn/an", _check_key_types),
    ("ภาษาไทย", _check_thai),
    ("วินิจฉัยล่าสุด", _check_latest_visit),
    ("ตาราง village", _check_village),
    ("อำเภอในเขต", _check_area),
    ("จุดกลางหมู่บ้าน", _check_centroid),
    ("รหัสโรค DENGUE_ICDS", _check_icds),
    ("ข้อมูลย้อนหลังตามปี พ.ศ. (หน้า /trend)", _check_year_visits),
    ("ขอบเขตอำเภอบนแผนที่", _check_boundary),
    ("ขอบเขตตำบลบนแผนที่", _check_tambon),
    ("พิกัดสถานพยาบาล", _check_facilities),
    ("กุญแจ MOPH", _check_moph_keys),
    ("เข้าถึง MOPH", _check_moph_host),
    ("SECRET_KEY", _check_secret),
    ("ลิงก์ฐาน (DENGUE_BASE_URL)", _check_base_url),
    ("วันเริ่มแจ้งเตือน (DENGUE_START_FROM)", _check_start_from),
    ("DENGUE_DATA_AUDITED", _check_audited),
)


def _run_all(ctx: _Context) -> list[tuple[str, str, str]]:
    results = []

    def run(name, fn):
        try:
            status, detail = fn(ctx)
        except Exception as e:  # noqa: BLE001 — รายการเดียวล้มต้องไม่หยุดรายการอื่น
            status, detail = FAIL, f"ตรวจไม่สำเร็จ ({_type_name(e)})"
        results.append((status, name, detail))

    for name, fn in CHECKS:
        run(name, fn)
        if fn is _check_icds:   # โรคอื่นที่เปิด ต่อท้ายแถวรหัสโรคของไข้เลือดออก
            for rule in _other_rules(ctx.settings):
                run(f"รหัสโรค {rule.disease.name_th}", lambda c, r=rule: _check_disease_icds(c, r))
    return results


def _other_rules(settings):
    return [r for r in settings.enabled_rules() if r.key != "dengue"]


def _all_other_rules(settings):
    """กฎของทุกโรคที่ไม่ใช่ไข้เลือดออก (รวมที่ยังปิดอยู่ — ให้ตรวจรหัสในข้อมูลก่อนเปิด)."""
    return [r for d in REGISTRY if d.key != "dengue" and (r := settings.rule(d.key)) is not None]


def run_checks(settings, source, moph_configured: bool, overlay=None, store=None) -> list[tuple[str, str, str]]:
    return _run_all(_Context(settings, source, moph_configured, overlay, store))


# ---------- ข้อมูลเทียบ "แอปใช้ vs พบในข้อมูล" สำหรับหน้า /check ----------

def _safely(fn: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001
        return None, _type_name(e)


def _audit(ctx: _Context) -> dict:
    """จำนวนนับของรหัสโรค/อำเภอ/หมู่บ้านเพื่อเทียบกับที่แอปใช้; ไม่ยก exception (ส่วนที่ล้ม = error ชื่อชนิด)."""
    settings = ctx.settings
    found, icd_error = _safely(ctx.icd_found)
    icd_rows = []
    for prefix in settings.icds:
        norm = prefix.replace(".", "").strip().upper()
        codes = sorted(((c, n) for c, n in (found or {}).items() if c.startswith(norm)),
                       key=lambda cn: (-cn[1], cn[0]))
        icd_rows.append({"prefix": prefix, "codes": codes,
                         "visits": sum(n for _, n in codes)})

    used_result, area_error = _safely(ctx.area_used)
    used, origin = used_result if used_result else ((), "")
    areas, areas_error = _safely(ctx.area_found)
    area_rows = []
    for code, v in sorted((areas or {}).items(), key=lambda kv: (-kv[1]["visits"], kv[0])):
        area_rows.append({"code": code, "visits": v["visits"], "mapped": v["mapped"],
                          "in_area": code in used})
    counts, village_error = _safely(ctx.villages)
    points, points_error = _safely(ctx.points)
    years_rows, year_error = _safely(ctx.year_visits)
    boundary_rows, boundary_error = _safely(ctx.boundary_rows)
    tambon_rows, tambon_error = _safely(ctx.tambon_rows)
    fac, fac_error = _safely(ctx.facility_state)
    current_yr = trend.year_of(date.today())
    diseases = []
    for rule in _all_other_rules(settings):
        d_found, d_error = _safely(lambda r=rule: ctx.icd_found(r.key))
        d_rows = []
        for prefix in rule.icds:
            norm = prefix.replace(".", "").strip().upper()
            codes = sorted(((c, n) for c, n in (d_found or {}).items() if c.startswith(norm)),
                           key=lambda cn: (-cn[1], cn[0]))
            d_rows.append({"prefix": prefix, "codes": codes, "visits": sum(n for _, n in codes)})
        diseases.append({"key": rule.key, "name": rule.disease.name_th, "enabled": rule.enabled,
                         "used": tuple(rule.icds),
                         "rows": d_rows, "error": d_error, "env": env_names(rule.key),
                         "mode": MODE_LABELS[rule.mode], "group": GROUP_LABELS[rule.group_by],
                         "min_cases": rule.min_cases, "days": rule.days,
                         "radius_m": rule.radius_m if rule.group_by == "pin_radius" else None})
    return {
        "diseases": diseases,
        "boundary": {"rows": boundary_rows or [], "error": boundary_error},
        "tambon": {"rows": tambon_rows or [], "error": tambon_error},
        "facilities": {"catalog": (fac or {}).get("catalog", 0), "placed": (fac or {}).get("placed", 0),
                       "unplaced": (fac or {}).get("unplaced", []), "error": fac_error},
        "years": {"rows": [{"year": year, "visits": n, "current": year == current_yr}
                            for year, n in (years_rows or [])], "error": year_error},
        "icd": {"used": tuple(settings.icds), "rows": icd_rows, "error": icd_error},
        "area": {"used": used, "origin": origin, "rows": area_rows,
                 "visits": sum(r["visits"] for r in area_rows),
                 "mapped": sum(r["mapped"] for r in area_rows),
                 "error": area_error or areas_error},
        "villages": {"counts": counts, "points": points or [],
                     "error": village_error or points_error},
    }


def run_report(settings, source, moph_configured: bool, overlay=None, store=None) -> tuple[list, dict]:
    """ผลตรวจ + ข้อมูลเทียบสำหรับหน้า /check (อ่านฐานซ้ำกันให้น้อยที่สุดด้วย context เดียว)."""
    ctx = _Context(settings, source, moph_configured, overlay, store)
    return _run_all(ctx), _audit(ctx)


# ---------- บรรทัดคำสั่ง ----------

def main(argv: list[str] | None = None, build: Callable[[], Any] | None = None) -> int:
    """พิมพ์ผลตรวจทีละบรรทัด; คืน 1 เมื่อมี fail (หรือสร้างระบบไม่สำเร็จ) ไม่งั้น 0."""
    import argparse
    argparse.ArgumentParser(
        prog="setup_check.py",
        description="ตรวจสภาพแวดล้อมก่อนใช้แอปแจ้งเตือนไข้เลือดออก SRRT (อ่านอย่างเดียว)",
    ).parse_args(argv)
    if build is None:  # รันจากบรรทัดคำสั่งจริง: โหลด ./.env (หรือ DENGUE_ENV_FILE)
        load_dotenv_if_present()
    try:
        if build is None:
            from wiring import build_runtime as build
        rt = build()
    except Exception as e:  # noqa: BLE001 — ข้อความ exception อาจมีรหัสผ่าน/โฮสต์
        print(f"{ICONS[FAIL]} สร้างระบบไม่สำเร็จ ({_type_name(e)}) "
              "— ตรวจ .env และไลบรารีที่ติดตั้ง")
        return 1
    results = run_checks(rt.settings, rt.source, rt.sender is not None,
                         overlay=getattr(rt, "overlay", None), store=getattr(rt, "store", None))
    for status, name, detail in results:
        print(f"{ICONS[status]} {name} — {detail}")
    counts = {s: sum(1 for r in results if r[0] == s) for s in (OK, WARN, FAIL)}
    print(f"\nสรุป: ผ่าน {counts[OK]} · เตือน {counts[WARN]} · ล้มเหลว {counts[FAIL]}")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # ✔/⚠/✘ และภาษาไทยต้องไม่ล้มบนคอนโซล Windows
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
