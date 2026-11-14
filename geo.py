"""ระยะ รหัสหมู่บ้าน และ cluster ของผู้ป่วย (Python ล้วน ไม่พึ่ง DB/Flask/ไลบรารีภายนอก)"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """ระยะทางบนผิวโลก (เมตร) ระหว่างสองพิกัด"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _digits(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or not (s.isascii() and s.isdigit()):
        return None
    return s


def village_code(
    chw: str | None, amp: str | None, tmb: str | None, moo: str | None
) -> str | None:
    """รหัสหมู่บ้าน 8 หลัก = จังหวัด+อำเภอ+ตำบล+หมู่(เติมศูนย์ 2 หลัก)

    คืน None ถ้าส่วนใดว่าง ไม่ใช่ตัวเลข หรือหมู่เป็น 0/00
    """
    parts = [_digits(chw), _digits(amp), _digits(tmb), _digits(moo)]
    if any(p is None for p in parts):
        return None
    chw_s, amp_s, tmb_s, moo_s = parts
    if int(moo_s) == 0:
        return None
    code = chw_s + amp_s + tmb_s + moo_s.zfill(2)
    return code if len(code) == 8 else None


def find_clusters(
    cases: list[dict], radius_m: int, days: int, min_cases: int
) -> list[frozenset[int]]:
    """กลุ่มผู้ป่วย = ส่วนเชื่อมต่อของกราฟ ขอบ = ห่าง <= radius_m และ <= days วัน

    นับเฉพาะ pin_state == "confirmed" และ status == "active"
    คืนเฉพาะกลุ่มขนาด >= min_cases เรียงตาม id ที่น้อยสุดของแต่ละกลุ่ม
    """
    items = [
        c for c in cases if c["pin_state"] == "confirmed" and c["status"] == "active"
    ]
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    window = days * 86400
    for i in range(n):
        a = items[i]
        for j in range(i + 1, n):
            b = items[j]
            if abs((a["dx_at"] - b["dx_at"]).total_seconds()) > window:
                continue
            if haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= radius_m:
                union(i, j)

    groups: dict[int, set[int]] = {}
    for i, c in enumerate(items):
        groups.setdefault(find(i), set()).add(c["id"])

    result = [frozenset(g) for g in groups.values() if len(g) >= min_cases]
    result.sort(key=min)
    return result

def find_village_clusters(
    cases: list[dict], days: int, min_cases: int
) -> list[frozenset[int]]:
    """กลุ่มก้อนระดับหมู่บ้าน = เคส active ที่ `village_code` เดียวกัน เชื่อมกันเมื่อ dx_at ห่าง <= days วัน
    (ส่วนเชื่อมต่อ — ลูกโซ่ A–B–C นับเป็นกลุ่มเดียว) ไม่ใช้พิกัด; เคสที่ไม่มีรหัสหมู่บ้านไม่นับ.

    คืนเฉพาะกลุ่มขนาด >= min_cases เรียงตาม id ที่น้อยสุดของแต่ละกลุ่ม
    """
    by_village: dict[str, list[dict]] = {}
    for c in cases:
        if c["status"] == "active" and c.get("village_code"):
            by_village.setdefault(c["village_code"], []).append(c)

    window = days * 86400
    result: list[frozenset[int]] = []
    for items in by_village.values():
        items.sort(key=lambda c: (c["dx_at"], c["id"]))
        group = [items[0]]
        groups = [group]
        for prev, cur in zip(items, items[1:]):
            if (cur["dx_at"] - prev["dx_at"]).total_seconds() <= window:
                group.append(cur)
            else:
                group = [cur]
                groups.append(group)
        result.extend(frozenset(c["id"] for c in g) for g in groups if len(g) >= min_cases)
    result.sort(key=min)
    return result
