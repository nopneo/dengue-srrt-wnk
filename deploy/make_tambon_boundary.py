"""สร้างไฟล์ขอบเขตตำบล boundaries/<รหัสอำเภอ>.tambon.geojson จากไฟล์ GeoJSON ใดก็ได้ที่มีรหัสตำบลและชื่อ.

    python deploy/make_tambon_boundary.py 3025 tha_admin_boundaries.geojson --expect 302501,302502,302503,302504,302505

- ต้นทาง: ไฟล์ตำบลจาก สสอ./สสจ. หรือไฟล์ระดับประเทศของ UN OCHA บน HDX
  ("Thailand - Subnational Administrative Boundaries", CC BY-IGO) — **สคริปต์ไม่ดาวน์โหลดเอง**
- อ่านแบบสตรีมทีละ feature (ไฟล์ระดับประเทศหลายร้อย MB ไม่ถูกโหลดทั้งไฟล์) เก็บเฉพาะตำบลของอำเภอที่ระบุ
- ตรวจรูปแบบ/ขนาด/กรอบพิกัดก่อนเขียน; ไฟล์ใหญ่เกิน 500 KB จะลดความละเอียดเอง; เขียนแบบอะตอมมิก; ไม่ทับไฟล์เดิมถ้าไม่มี --force
- รหัสออก: 0 สำเร็จ · 1 ใช้ผิด/เดาฟิลด์ไม่ได้/ไม่พบตำบล/--expect ขาด/ไฟล์มีอยู่แล้ว/ใหญ่เกินแม้ลดความละเอียด
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boundary  # noqa: E402

# ชื่อฟิลด์ที่รู้จัก (เทียบแบบไม่สนตัวพิมพ์) เรียงตามความสำคัญ — ไฟล์ UN OCHA/HDX ใช้ adm3_pcode กับ adm3_name1 (ชื่อไทย)
CODE_PROPS = ("adm3_pcode", "tam_code", "tambon_code", "tam_id", "code")
NAME_PROPS = ("adm3_th", "adm3_name1", "tam_th", "tam_namt", "name_th", "adm3_name", "name")
EPSILON_START = 0.0002          # องศา
EPSILON_ROUNDS = 10
_SPECIAL = re.compile(r'["\\{}\[\]]')


# ---- ตัวแยก feature แบบสตรีม ----
def iter_features(stream: TextIO, chunk_size: int = 1 << 20) -> Iterator[dict]:
    """คืน object ชนิด Feature ที่ "เปิดที่ความลึก 2" (รากเป็น object ความลึก 1, อาร์เรย์ features ความลึก 2)
    ทีละตัวโดยอ่านจากสตรีมทีละ `chunk_size` ตัวอักษร (สถานะ ความลึก/อยู่ในสตริง/escape ข้ามขอบ chunk ได้ทุกตำแหน่ง)."""
    depth = 0
    in_str = False
    esc = False                  # chunk ก่อนหน้าจบด้วย backslash ในสตริง: ข้ามตัวแรกของ chunk นี้
    parts: list[str] | None = None
    seg_start = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        n = len(chunk)
        pos = 0
        if esc:
            pos, esc = 1, False
        while True:
            m = _SPECIAL.search(chunk, pos)
            if m is None:
                break
            i = m.start()
            ch = chunk[i]
            if in_str:
                if ch == "\\":
                    if i + 1 >= n:
                        esc, pos = True, n
                        break
                    pos = i + 2
                    continue
                if ch == '"':
                    in_str = False
                pos = i + 1
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                if ch == "{" and depth == 2 and parts is None:
                    parts, seg_start = [], i
                depth += 1
            else:
                depth -= 1
                if ch == "}" and parts is not None and depth == 2:
                    parts.append(chunk[seg_start:i + 1])
                    obj = json.loads("".join(parts))
                    parts = None
                    if isinstance(obj, dict) and obj.get("type") == "Feature":
                        yield obj
            pos = i + 1
        if parts is not None:
            parts.append(chunk[seg_start:])
            seg_start = 0


def normalize_code(raw: object) -> str | None:
    """รหัสตำบล 6 หลัก (ตัดคำนำหน้า TH ของ P-code); ผิดรูป -> None."""
    s = str(raw).strip() if raw is not None else ""
    if s[:2].upper() == "TH":
        s = s[2:]
    return s if len(s) == 6 and s.isascii() and s.isdigit() else None


def guess_props(props: dict) -> tuple[str, str] | None:
    """เดาชื่อฟิลด์รหัส/ชื่อตำบลแบบไม่สนตัวพิมพ์ (คืนชื่อฟิลด์ตามที่เขียนในไฟล์); ต้องพบทั้งสองอย่าง."""
    by_lower = {str(k).lower(): k for k in props}
    code = next((by_lower[k] for k in CODE_PROPS if k in by_lower), None)
    name = next((by_lower[k] for k in NAME_PROPS if k in by_lower), None)
    return (code, name) if code and name else None


# ---- ลดความละเอียด (Douglas–Peucker แบบไม่เรียกซ้ำ) ----
def _dp(points: list[list[float]], eps: float) -> list[list[float]]:
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        (x1, y1), (x2, y2) = points[lo][:2], points[hi][:2]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        far, far_d = -1, eps
        for i in range(lo + 1, hi):
            x, y = points[i][:2]
            d = (abs(dy * (x - x1) - dx * (y - y1)) / norm) if norm else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
            if d > far_d:
                far, far_d = i, d
        if far >= 0:
            keep[far] = True
            stack += [(lo, far), (far, hi)]
    return [p for p, k in zip(points, keep) if k]


def _simplify_ring(ring: list[list[float]], eps: float) -> list[list[float]]:
    new = _dp(ring[:-1], eps) + [ring[0]]            # ตัดจุดปิดออกก่อนลด แล้วปิดใหม่
    return new if len(new) >= 4 else ring            # ยุบจนต่ำกว่า 4 จุด -> คงเดิม


def simplify_geometry(geometry: dict, epsilon: float) -> dict:
    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [_simplify_ring(r, epsilon) for r in geometry["coordinates"]]}
    return {"type": "MultiPolygon",
            "coordinates": [[_simplify_ring(r, epsilon) for r in poly] for poly in geometry["coordinates"]]}


# ---- สร้างไฟล์ ----
def build_collection(amphoe: str, features: Iterable[dict], code_prop: str, name_prop: str,
                     source: str, fetched: date) -> dict:
    """เก็บเฉพาะตำบลที่รหัสขึ้นต้นด้วย `amphoe`; ผ่าน validate_tambon_collection (ซ้ำ/ชื่อว่าง/geometry ผิด -> BoundaryError)."""
    out = []
    for f in features:
        props = f.get("properties") or {}
        code = normalize_code(props.get(code_prop))
        if code is None or not code.startswith(amphoe):
            continue
        name = props.get(name_prop)
        out.append({"type": "Feature",
                    "properties": {"code": code, "name": name if isinstance(name, str) else "",
                                   "source": source, "fetched": fetched.isoformat()},
                    "geometry": f.get("geometry")})
    return boundary.validate_tambon_collection({"type": "FeatureCollection", "features": out}, amphoe)


def _encode(doc: dict) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("amphoe", help="รหัสจังหวัด+อำเภอ 4 หลัก เช่น 3025")
    ap.add_argument("source", help="ไฟล์ GeoJSON ต้นทาง (FeatureCollection)")
    ap.add_argument("--code-prop", help="ชื่อฟิลด์รหัสตำบล (ไม่ระบุ = เดา)")
    ap.add_argument("--name-prop", help="ชื่อฟิลด์ชื่อตำบล (ไม่ระบุ = เดา)")
    ap.add_argument("--expect", default="", help="รหัสตำบลที่ต้องพบ คอมมาคั่น (ขาด = ไม่เขียนไฟล์)")
    ap.add_argument("--source-label", help="ข้อความที่มา/ใบอนุญาตที่เก็บในไฟล์ (ค่าเริ่มต้น = ชื่อไฟล์ต้นทาง)")
    ap.add_argument("--force", action="store_true", help="เขียนทับไฟล์เดิม")
    ap.add_argument("--out-dir", default=str(boundary.BOUNDARY_DIR), help="โฟลเดอร์ปลายทาง")
    args = ap.parse_args(argv)

    if not (len(args.amphoe) == 4 and args.amphoe.isascii() and args.amphoe.isdigit()):
        print("รหัสอำเภอต้องเป็นตัวเลข 4 หลัก (รหัสจังหวัด 2 หลัก + อำเภอ 2 หลัก)")
        return 1
    src = Path(args.source)
    if not src.is_file():
        print(f"ไม่พบไฟล์ต้นทาง {src}")
        return 1
    out_dir = Path(args.out_dir)
    out = out_dir / f"{args.amphoe}{boundary.TAMBON_SUFFIX}"
    if out.exists() and not args.force:
        print(f"มีไฟล์ {out} อยู่แล้ว — ใช้ --force หากต้องการเขียนทับ")
        return 1

    code_prop, name_prop = args.code_prop, args.name_prop
    matched: list[dict] = []
    try:
        with open(src, encoding="utf-8") as fh:
            for feature in iter_features(fh):
                props = feature.get("properties") or {}
                if code_prop is None or name_prop is None:
                    guessed = guess_props(props)
                    if guessed is None:
                        print("เดาชื่อฟิลด์รหัส/ชื่อตำบลไม่ได้ — ระบุ --code-prop และ --name-prop")
                        print("ฟิลด์ของ feature แรก: " + ", ".join(sorted(map(str, props))))
                        return 1
                    code_prop, name_prop = code_prop or guessed[0], name_prop or guessed[1]
                code = normalize_code(props.get(code_prop))
                if code is not None and code.startswith(args.amphoe):
                    matched.append(feature)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"อ่านไฟล์ต้นทางไม่ได้ ({type(e).__name__})")
        return 1
    if not matched:
        print(f"ไม่พบตำบลของอำเภอ {args.amphoe} ในไฟล์ต้นทาง (ใช้ฟิลด์รหัส {code_prop})")
        return 1

    label = args.source_label or src.name
    try:
        doc = build_collection(args.amphoe, matched, code_prop, name_prop, label, date.today())
    except boundary.BoundaryError as e:
        print(f"ใช้ข้อมูลที่ได้ไม่ได้: {e}")
        return 1
    found = [f["properties"]["code"] for f in doc["features"]]
    expect = [c.strip() for c in args.expect.split(",") if c.strip()]
    lost = [c for c in expect if c not in found]
    if lost:
        print("ไม่พบตำบลที่คาดไว้: " + ", ".join(lost) + " — ไม่เขียนไฟล์")
        return 1

    payload = _encode(doc)
    eps = EPSILON_START
    for _ in range(EPSILON_ROUNDS):
        if len(payload) <= boundary.MAX_BYTES:
            break
        simplified = {**doc, "features": [{**f, "geometry": simplify_geometry(f["geometry"], eps)}
                                          for f in doc["features"]]}
        payload = _encode(boundary.validate_tambon_collection(simplified, args.amphoe))
        eps *= 2
    if len(payload) > boundary.MAX_BYTES:
        print(f"ไฟล์ใหญ่เกิน {boundary.MAX_BYTES} ไบต์แม้ลดความละเอียดแล้ว ({len(payload)}) — ไม่เขียน")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"ใช้ฟิลด์รหัส={code_prop} ชื่อ={name_prop} · พบ {len(found)} ตำบล: " +
          ", ".join(f"{f['properties']['code']} {f['properties']['name']}" for f in doc["features"]))
    print(f"เขียน {out} แล้ว ({len(payload)} ไบต์) · ที่มา: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
