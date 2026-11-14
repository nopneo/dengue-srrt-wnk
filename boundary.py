"""เส้นขอบอำเภอบนแผนที่รวม — ตรวจ/โหลดไฟล์ GeoJSON (`boundaries/<รหัส 4 หลัก>.geojson`) และตัวช่วยของสคริปต์ดึง.

ข้อมูลเป็นพื้นที่ปกครองสาธารณะ (OpenStreetMap, ODbL) ไม่มีข้อมูลผู้ป่วย. ไฟล์ถูกตรวจรูปแบบ/ขนาดทุกครั้งที่อ่าน
และชื่อไฟล์มาจากรหัสอำเภอในค่าตั้งที่เป็นเลข 4 หลักเท่านั้น (ไม่รับจาก URL).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("dengue.boundary")

MAX_BYTES = 500_000
THAI_BBOX = (5.0, 21.0, 97.0, 106.0)   # lat_min, lat_max, lng_min, lng_max
BOUNDARY_DIR = Path(__file__).resolve().parent / "boundaries"
TAMBON_SUFFIX = ".tambon.geojson"
_POLYGONS = ("Polygon", "MultiPolygon")


class BoundaryError(ValueError):
    """ข้อความเป็นเหตุผลสั้น ๆ ภาษาไทย ไม่มีเนื้อหาจากไฟล์."""


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_position(pos: Any) -> None:
    if not isinstance(pos, (list, tuple)) or len(pos) < 2 or not (_is_number(pos[0]) and _is_number(pos[1])):
        raise BoundaryError("พิกัดไม่ใช่ตัวเลข [lng, lat]")
    lng, lat = pos[0], pos[1]
    lat_min, lat_max, lng_min, lng_max = THAI_BBOX
    if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
        raise BoundaryError("พิกัดอยู่นอกประเทศไทย")


def _check_ring(ring: Any) -> None:
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        raise BoundaryError("วงแหวนต้องมีอย่างน้อย 4 จุด")
    for pos in ring:
        _check_position(pos)
    if list(ring[0][:2]) != list(ring[-1][:2]):
        raise BoundaryError("วงแหวนไม่ปิด")


def _check_polygon(rings: Any) -> None:
    if not isinstance(rings, (list, tuple)) or not rings:
        raise BoundaryError("polygon ว่าง")
    for ring in rings:
        _check_ring(ring)


def _check_geometry(geometry: Any) -> None:
    if not isinstance(geometry, dict) or geometry.get("type") not in _POLYGONS:
        raise BoundaryError("geometry ต้องเป็น Polygon หรือ MultiPolygon")
    coords = geometry.get("coordinates")
    if geometry["type"] == "Polygon":
        _check_polygon(coords)
    else:
        if not isinstance(coords, (list, tuple)) or not coords:
            raise BoundaryError("MultiPolygon ว่าง")
        for polygon in coords:
            _check_polygon(polygon)


def validate_geojson(doc: object) -> dict:
    """คืน FeatureCollection ที่ผ่านการตรวจ (รับ FeatureCollection หรือ Feature เดี่ยว) — ไม่ผ่านยก BoundaryError."""
    if not isinstance(doc, dict):
        raise BoundaryError("ไม่ใช่ GeoJSON object")
    if doc.get("type") == "Feature":
        doc = {"type": "FeatureCollection", "features": [doc]}
    if doc.get("type") != "FeatureCollection":
        raise BoundaryError("ต้องเป็น FeatureCollection หรือ Feature")
    features = doc.get("features")
    if not isinstance(features, list) or not features:
        raise BoundaryError("ไม่มี feature")
    for f in features:
        if not isinstance(f, dict) or f.get("type") != "Feature":
            raise BoundaryError("feature ไม่ถูกต้อง")
        _check_geometry(f.get("geometry"))
    return doc


def _valid_code(code: object) -> bool:
    return isinstance(code, str) and len(code) == 4 and code.isascii() and code.isdigit()


def _ascii_digits(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and value.isascii() and value.isdigit()


def validate_tambon_collection(doc: object, amphoe: str) -> dict:
    """ไฟล์ตำบล: ผ่าน validate_geojson แล้วทุก feature ต้องมี properties.code (6 หลัก ขึ้นต้นด้วยรหัสอำเภอ ไม่ซ้ำ)
    และ properties.name (สตริงไม่ว่าง) — ไม่ผ่านยก BoundaryError."""
    doc = validate_geojson(doc)
    seen: set[str] = set()
    for f in doc["features"]:
        props = f.get("properties")
        if not isinstance(props, dict):
            raise BoundaryError("ตำบลไม่มี properties")
        code, name = props.get("code"), props.get("name")
        if not _ascii_digits(code, 6) or not code.startswith(amphoe):
            raise BoundaryError("รหัสตำบลไม่ถูกต้อง")
        if not isinstance(name, str) or not name.strip():
            raise BoundaryError("ตำบลไม่มีชื่อ")
        if code in seen:
            raise BoundaryError("รหัสตำบลซ้ำ")
        seen.add(code)
    return doc


def _load_file(code: object, suffix: str, base_dir: Path, validate) -> dict | None:
    if not _valid_code(code):
        return None
    path = Path(base_dir) / f"{code}{suffix}"
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_BYTES:
            log.warning("boundary %s%s: ไฟล์ใหญ่เกิน", code, suffix)
            return None
        doc = json.loads(path.read_bytes().decode("utf-8"))
        return validate(doc)
    except (OSError, ValueError) as e:   # BoundaryError/JSONDecodeError/UnicodeDecodeError เป็น ValueError
        log.warning("boundary %s%s: ใช้ไม่ได้ (%s)", code, suffix, type(e).__name__)
        return None


def load_boundary(code: str, base_dir: Path) -> dict | None:
    """อ่านและตรวจ `<base_dir>/<code>.geojson`; ไม่มี/เสีย/ใหญ่เกิน/รหัสผิดรูป -> None (ไม่ยก)."""
    return _load_file(code, ".geojson", base_dir, validate_geojson)


def load_tambons(amphoe: str, base_dir: Path) -> dict | None:
    """อ่านและตรวจ `<base_dir>/<amphoe>.tambon.geojson` (พฤติกรรมเดียวกับ load_boundary)."""
    return _load_file(amphoe, TAMBON_SUFFIX, base_dir, lambda doc: validate_tambon_collection(doc, amphoe))


def load_tambons_many(codes: Iterable[str], base_dir: Path) -> tuple[list[dict], list[str]]:
    """(features ตำบลของทุกอำเภอที่โหลดได้, รหัสอำเภอที่ขาด/ใช้ไม่ได้ ตามลำดับที่ให้)."""
    features: list[dict] = []
    missing: list[str] = []
    for code in codes:
        doc = load_tambons(code, base_dir)
        if doc is None:
            missing.append(code)
        else:
            features.extend(doc["features"])
    return features, missing


def load_boundaries(codes: Iterable[str], base_dir: Path) -> tuple[list[dict], list[str]]:
    """(features ของทุกรหัสที่โหลดได้, รหัสที่ขาด/ใช้ไม่ได้ ตามลำดับที่ให้)."""
    features: list[dict] = []
    missing: list[str] = []
    for code in codes:
        doc = load_boundary(code, base_dir)
        if doc is None:
            missing.append(code)
        else:
            features.extend(doc["features"])
    return features, missing


def _in_ring(lat: float, lng: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(lat: float, lng: float, rings: list) -> bool:
    return _in_ring(lat, lng, rings[0]) and not any(_in_ring(lat, lng, hole) for hole in rings[1:])


def point_in_boundary(lat: float, lng: float, doc: dict) -> bool:
    """จุด (lat, lng) อยู่ในขอบเขตของ FeatureCollection ที่ผ่าน validate_geojson (รองรับรู/MultiPolygon)."""
    for f in doc["features"]:
        g = f["geometry"]
        polygons = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        if any(_in_polygon(lat, lng, rings) for rings in polygons):
            return True
    return False


# ---- ตัวช่วยของ deploy/fetch_boundary.py ----

def _label(r: dict) -> str:
    return f"{r.get('osm_id')} ({str(r.get('display_name', ''))[:60]})"


def pick_result(results: list[dict], relation: int | None) -> dict:
    """เลือกผล Nominatim: ระบุ relation -> ตรง id; ไม่ระบุ -> ต้องมีพื้นที่ปกครองแบบ polygon เพียงหนึ่งเดียว."""
    if relation is not None:
        for r in results:
            if r.get("osm_type") == "relation" and r.get("osm_id") == relation:
                return r
        raise BoundaryError(f"ไม่พบ relation {relation} ในผลลัพธ์")
    admin = [r for r in results
             if r.get("osm_type") == "relation" and r.get("type") == "administrative"
             and isinstance(r.get("geojson"), dict) and r["geojson"].get("type") in _POLYGONS]
    if not admin:
        raise BoundaryError("ไม่พบพื้นที่ปกครองแบบ polygon — ลองคำค้นอื่นหรือระบุ --relation")
    if len(admin) > 1:
        raise BoundaryError("พบหลายผล ระบุ --relation ให้ตรง: " + "; ".join(_label(r) for r in admin))
    return admin[0]


def build_document(code: str, result: dict, fetched: date) -> dict:
    """FeatureCollection หนึ่ง Feature พร้อม properties ที่มา/วันที่ดึง (ผ่าน validate_geojson)."""
    name = str(result.get("display_name", "")).split(",")[0].strip()
    doc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"code": code, "name": name,
                       "source": f"OpenStreetMap relation {result.get('osm_id')}",
                       "fetched": fetched.isoformat()},
        "geometry": result.get("geojson"),
    }]}
    return validate_geojson(doc)
