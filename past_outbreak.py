"""จำนวนรายต่อหมู่บ้านของปี พ.ศ.หนึ่ง สำหรับวงกลมการระบาดปีก่อนบนแผนที่รวม (`/api/past.json`).

ตอบเฉพาะจำนวนนับ + พิกัดจุดกลางหมู่บ้าน (ไม่ใช่พิกัดบ้านรายคน) นับเฉพาะผู้ป่วยที่อยู่ในอำเภอ (`in_area`).
หมู่บ้านที่ไม่มีจุดกลาง และราย "ไม่ทราบหมู่บ้าน" นับใน `unmapped` เสมอ เพื่อให้ผลรวมตรงกับ total.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from trend import CountRow
from trend_service import YearResult

BUSY_TEXT = "กำลังคำนวณอยู่"
CENTROID_ERROR_TEXT = "ดึงพิกัดหมู่บ้านไม่สำเร็จ"


def _village_code(row: CountRow) -> str | None:
    """รหัสหมู่บ้าน 8 หลัก = ตำบล 6 หลัก + หมู่ 2 หลัก; ไม่ครบ/หมู่ 00 = ไม่ทราบหมู่บ้าน."""
    return row.tmb + row.moo if len(row.tmb) == 6 and row.moo != "00" else None


def village_codes(rows: Iterable[CountRow]) -> list[str]:
    """รหัสหมู่บ้านที่ใช้ถามจุดกลาง (เฉพาะในอำเภอ ไม่ซ้ำ เรียงตามรหัส)."""
    return sorted({c for r in rows if r.in_area and (c := _village_code(r)) is not None})


def build_past(year: int, result: YearResult, centroids: Mapping[str, tuple[float, float]],
               names: Mapping[str, str], centroid_error: bool = False) -> dict:
    """`centroid_error` = ถามจุดกลางจากฐานไม่สำเร็จ (ไม่ใช่ "ไม่มีพิกัดในตาราง") — ตั้ง error และธงให้หน้าแสดงต่างกัน."""
    per_village: dict[str, int] = {}
    unknown_cases = 0
    for r in result.rows:
        if not r.in_area:
            continue
        code = _village_code(r)
        if code is None:
            unknown_cases += r.cases
        else:
            per_village[code] = per_village.get(code, 0) + r.cases
    villages = []
    unmapped_villages = 0
    unmapped_cases = unknown_cases
    for code, cases in sorted(per_village.items(), key=lambda kv: (-kv[1], kv[0])):
        point = centroids.get(code)
        if point is None:
            unmapped_villages += 1
            unmapped_cases += cases
            continue
        villages.append({"code": code, "name": names.get(code) or f"ม.{int(code[6:8])}",
                         "lat": float(point[0]), "lng": float(point[1]), "cases": cases})
    error = result.error or (BUSY_TEXT if result.busy else None) or (CENTROID_ERROR_TEXT if centroid_error else None)
    return {
        "year": year,
        "total": sum(per_village.values()) + unknown_cases,
        "villages": villages,
        "unmapped": {"villages": unmapped_villages, "cases": unmapped_cases},
        "computed_at": result.computed_at.isoformat(timespec="seconds") if result.computed_at else None,
        "stale": bool(result.stale),
        "error": error,
        "centroid_error": bool(centroid_error),
    }
