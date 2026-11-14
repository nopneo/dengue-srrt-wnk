"""ดึงเส้นขอบอำเภอจาก OpenStreetMap (Nominatim) เก็บเป็น boundaries/<รหัส 4 หลัก>.geojson — รันตอนติดตั้ง ไม่ใช่ตอนใช้งาน.

    python deploy/fetch_boundary.py 3025 --relation 18988716
    python deploy/fetch_boundary.py 3025 --query "Wang Nam Khiao District, Nakhon Ratchasima"

- ยิง Nominatim หนึ่งคำขอ (ส่งเฉพาะข้อความค้นหา/รหัส relation ไม่มีข้อมูลของ รพ.)
- ตรวจรูปแบบ ขนาด และกรอบพิกัดก่อนเขียน; เขียนแบบอะตอมมิก; ไม่ทับไฟล์เดิมถ้าไม่มี --force
- รหัสออก: 0 สำเร็จ · 1 ใช้ผิด/ไม่พบ/กำกวม/มีไฟล์อยู่แล้ว · 2 เครือข่ายล้ม
ข้อมูลเป็นของ OpenStreetMap contributors (ODbL) — ดู README ข้อ "ส่วนประกอบของผู้อื่น"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import boundary  # noqa: E402

NOMINATIM = "https://nominatim.openstreetmap.org"
USER_AGENT = "dengue-srrt (hospital epidemiology tool)"
TIMEOUT_S = 30
GEO_PARAMS = {"format": "json", "polygon_geojson": "1", "polygon_threshold": "0.0005"}


def fetch_nominatim(*, query: str | None = None, relation: int | None = None) -> list[dict]:
    """หนึ่งคำขอ HTTPS ไป Nominatim; ยก requests.RequestException เมื่อเครือข่าย/สถานะล้มเหลว."""
    if relation is not None:
        url, params = f"{NOMINATIM}/lookup", {**GEO_PARAMS, "osm_ids": f"R{relation}"}
    else:
        url, params = f"{NOMINATIM}/search", {**GEO_PARAMS, "q": query, "limit": "5"}
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def main(argv: list[str] | None = None, fetch=fetch_nominatim) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("code", help="รหัสจังหวัด+อำเภอ 4 หลัก เช่น 3025")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--relation", type=int, help="OSM relation id ของอำเภอ")
    group.add_argument("--query", help="คำค้นหา เช่น Wang Nam Khiao District, Nakhon Ratchasima")
    ap.add_argument("--force", action="store_true", help="เขียนทับไฟล์เดิม")
    ap.add_argument("--out-dir", default=str(boundary.BOUNDARY_DIR), help="โฟลเดอร์ปลายทาง")
    args = ap.parse_args(argv)

    if not (len(args.code) == 4 and args.code.isascii() and args.code.isdigit()):
        print("รหัสอำเภอต้องเป็นตัวเลข 4 หลัก (รหัสจังหวัด 2 หลัก + อำเภอ 2 หลัก)")
        return 1
    out_dir = Path(args.out_dir)
    out = out_dir / f"{args.code}.geojson"
    if out.exists() and not args.force:
        print(f"มีไฟล์ {out} อยู่แล้ว — ใช้ --force หากต้องการเขียนทับ")
        return 1
    try:
        results = fetch(query=args.query, relation=args.relation)
    except requests.RequestException as e:
        print(f"ติดต่อ Nominatim ไม่สำเร็จ ({type(e).__name__}) — ลองใหม่ภายหลัง")
        return 2
    try:
        doc = boundary.build_document(args.code, boundary.pick_result(results, args.relation), date.today())
    except boundary.BoundaryError as e:
        print(f"ใช้ผลที่ได้ไม่ได้: {e}")
        return 1
    payload = (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > boundary.MAX_BYTES:
        print(f"ไฟล์ใหญ่เกิน {boundary.MAX_BYTES} ไบต์ ({len(payload)}) — ไม่เขียน")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, out)
    props = doc["features"][0]["properties"]
    print(f"เขียน {out} แล้ว ({len(payload)} ไบต์) · {props['name']} · {props['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
