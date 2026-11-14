"""สถานพยาบาล (รพ./รพ.สต.) บนแผนที่รวม — จัดประเภท จับคู่ชื่อกับ HOSxP และอ่าน CSV พิกัด (ฟังก์ชันล้วน).

ข้อมูลเป็นที่ตั้งสถานพยาบาล (สาธารณะ) ไม่มีข้อมูลผู้ป่วย. ไฟล์ CSV จริงมักถูก Excel แปลงรหัสหน่วยงานเป็นเลขวิทยาศาสตร์
(`8.89921E+12`) จึงจับคู่ด้วย "ชื่อ" เป็นหลัก และใช้คอลัมน์รหัสเฉพาะเมื่อเป็นเลข 5 หลักตรง ๆ.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from boundary import THAI_BBOX

RPST_PREFIX = "โรงพยาบาลส่งเสริมสุขภาพตำบล"
HOSPITAL_PREFIX = "โรงพยาบาล"
_SCIENTIFIC = re.compile(r"^\d+(\.\d+)?[eE][+-]?\d+$")
_PROVINCE_SUFFIX = re.compile(r"\s+จังหวัด.*$")

NAME_COLUMNS = ("หน่วยงาน", "ชื่อ", "name")
LAT_COLUMNS = ("ละติจูด", "lat", "latitude")
LNG_COLUMNS = ("ลองติจูด", "ลองจิจูด", "lng", "lon", "longitude")
CODE_COLUMNS = ("hospcode", "รหัสสถานพยาบาล", "รหัสหน่วยงาน")


def classify(name: str) -> str | None:
    """`rpst` (รพ.สต.) / `hospital` (โรงพยาบาลอื่น) / None (สสอ. คลินิก ฯลฯ — ไม่แสดงบนแผนที่)."""
    n = (name or "").strip()
    if n.startswith(RPST_PREFIX):
        return "rpst"
    if n.startswith(HOSPITAL_PREFIX):
        return "hospital"
    return None


def normalize_name(raw: str) -> str:
    """ยุบช่องว่างซ้ำ ตัดคำต่อท้าย " จังหวัด…" (ที่พบในไฟล์จาก สธ.) เพื่อเทียบกับชื่อใน HOSxP."""
    s = re.sub(r"\s+", " ", raw or "").strip()
    return _PROVINCE_SUFFIX.sub("", s).strip()


def looks_scientific(raw: str) -> bool:
    return bool(_SCIENTIFIC.match((raw or "").strip()))


def valid_coords(lat: object, lng: object) -> tuple[float, float] | None:
    """(lat, lng) เมื่อเป็นตัวเลขจำกัด (ไม่ใช่ bool/สตริง) อยู่ในกรอบประเทศไทย และไม่ใช่ 0,0; มิฉะนั้น None."""
    for v in (lat, lng):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
    lat_min, lat_max, lng_min, lng_max = THAI_BBOX
    if (lat == 0 and lng == 0) or not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
        return None
    return float(lat), float(lng)


@dataclass(frozen=True)
class CsvRow:
    line: int
    name: str
    code: str | None
    lat: object          # float เมื่อแปลงได้ ไม่เช่นนั้นสตริงดิบ (ให้ match_rows รายงานเป็นพิกัดเสีย)
    lng: object


def _to_number(raw: str) -> object:
    try:
        return float(raw.strip())
    except ValueError:
        return raw


def _find(header: list[str], options: tuple[str, ...]) -> str | None:
    lowered = {h.strip().lower(): h for h in header if h}
    for opt in options:
        if opt.lower() in lowered:
            return lowered[opt.lower()]
    return None


def read_csv(path: Path) -> tuple[list[CsvRow], list[str]]:
    """อ่าน CSV (UTF-8 รับ BOM) -> (แถว, ปัญหาที่พบ). ขาดคอลัมน์/ถอดรหัสไม่ได้/เปิดไฟล์ไม่ได้ ยก ValueError ข้อความไทย."""
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("อ่านไฟล์ไม่ได้ — ต้องเป็น CSV แบบ UTF-8 (บันทึกจาก Excel เป็น \"CSV UTF-8\")") from None
    except OSError:
        raise ValueError(f"เปิดไฟล์ไม่ได้: {path}") from None
    reader = csv.DictReader(io.StringIO(text))
    header = list(reader.fieldnames or [])
    name_col, lat_col, lng_col = _find(header, NAME_COLUMNS), _find(header, LAT_COLUMNS), _find(header, LNG_COLUMNS)
    missing = [label for label, col in (("ชื่อ (หน่วยงาน)", name_col), ("ละติจูด", lat_col),
                                        ("ลองจิจูด (ลองติจูด)", lng_col)) if col is None]
    if missing:
        raise ValueError("ไม่พบคอลัมน์: " + ", ".join(missing))
    code_col = _find(header, CODE_COLUMNS)
    rows: list[CsvRow] = []
    problems: list[str] = []
    for rec in reader:
        values = [(v or "").strip() for v in rec.values() if not isinstance(v, list)]
        if not any(values):
            continue
        name = (rec.get(name_col) or "").strip()
        if not name:
            problems.append(f"แถว {reader.line_num}: ไม่มีชื่อ")
            continue
        raw_code = (rec.get(code_col) or "").strip() if code_col else ""
        code = raw_code if (len(raw_code) == 5 and raw_code.isascii() and raw_code.isdigit()
                            and not looks_scientific(raw_code)) else None
        rows.append(CsvRow(reader.line_num, name, code,
                           _to_number(rec.get(lat_col) or ""), _to_number(rec.get(lng_col) or "")))
    return rows, problems


@dataclass
class MatchResult:
    matched: list[tuple[dict, float, float]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    bad_coords: list[str] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)      # สถานพยาบาลของ HOSxP ที่ไม่มีแถวใน CSV


def match_rows(rows: list[CsvRow], catalog: list[dict]) -> MatchResult:
    """จับคู่แถว CSV กับรายชื่อจาก HOSxP: รหัส 5 หลักที่ตรงก่อน ไม่งั้นชื่อ (หลัง normalize). ไม่เดา."""
    by_code = {c["hospcode"]: c for c in catalog}
    by_name: dict[str, list[dict]] = {}
    for c in catalog:
        by_name.setdefault(normalize_name(c["name"]), []).append(c)
    res = MatchResult()
    seen: set[str] = set()
    for row in rows:
        label = normalize_name(row.name) or row.name
        coords = valid_coords(row.lat, row.lng)
        if coords is None:
            res.bad_coords.append(label)
            continue
        item = by_code.get(row.code) if row.code else None
        if item is None:
            candidates = by_name.get(normalize_name(row.name), [])
            if not candidates:
                res.unmatched.append(label)
                continue
            if len(candidates) > 1:
                res.ambiguous.append(label)
                continue
            item = candidates[0]
        if item["hospcode"] in seen:
            res.ambiguous.append(f"ซ้ำในไฟล์: {label}")
            continue
        seen.add(item["hospcode"])
        res.matched.append((item, coords[0], coords[1]))
    res.missing = sorted((c for c in catalog if c["hospcode"] not in seen), key=lambda c: c["hospcode"])
    return res
