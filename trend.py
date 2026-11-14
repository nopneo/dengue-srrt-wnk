"""ตรรกะล้วนของหน้าเปรียบเทียบปี พ.ศ. (`/trend`) — ปี พ.ศ. ปฏิทิน (ม.ค.–ธ.ค.) นับราย เกณฑ์ระบาด ตารางพื้นที่.

ไม่ติดต่อฐานข้อมูลและไม่รู้จัก Flask; เก็บ/คืนเฉพาะจำนวนนับ (HN ใช้ในหน่วยความจำระหว่าง
`build_episodes` เท่านั้น แล้วทิ้ง).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

MONTH_LABELS = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")
UNKNOWN_MOO = "00"


# ---- ปี พ.ศ. ปฏิทิน (1 ม.ค.–31 ธ.ค.) ----

def year_of(d: date) -> int:
    """ปี พ.ศ. ของวันที่ (ปีปฏิทิน: ม.ค.–ธ.ค.)."""
    return d.year + 543


def year_range(year: int) -> tuple[date, date]:
    """(1 ม.ค., 31 ธ.ค.) ของปี พ.ศ. `year` เป็น ค.ศ."""
    return date(year - 543, 1, 1), date(year - 543, 12, 31)


def month_index(d: date) -> int:
    """ม.ค. = 0 … ธ.ค. = 11."""
    return d.month - 1


def recent_years(today: date, count: int = 9) -> list[int]:
    """ปี พ.ศ. ของ `today` ย้อนหลังรวม `count` ปี (ใหม่ → เก่า)."""
    current = year_of(today)
    return [current - i for i in range(count)]


# ---- นับ "ราย" ----

@dataclass(frozen=True)
class VisitRow:
    hn: str
    day: date
    chw: str | None
    amp: str | None
    tmb: str | None
    moo: str | None


@dataclass(frozen=True)
class Episode:
    start: date
    in_area: bool
    tmb: str   # chw+amp+tmb 6 หลัก หรือ "" เมื่อไม่ครบ
    moo: str   # 2 หลัก หรือ "00" เมื่อไม่ทราบ


@dataclass(frozen=True)
class CountRow:
    month: int
    in_area: bool
    tmb: str
    moo: str
    cases: int


def _code2(value: str | None) -> str | None:
    v = (value or "").strip()
    return v if len(v) == 2 and v.isascii() and v.isdigit() else None


def _tmb_key(row: VisitRow) -> str:
    parts = (_code2(row.chw), _code2(row.amp), _code2(row.tmb))
    return "".join(parts) if all(parts) else ""


def _moo_key(value: str | None) -> str:
    v = (value or "").strip()
    if v.isascii() and v.isdigit() and 0 < int(v) <= 99:
        return f"{int(v):02d}"
    return UNKNOWN_MOO


def build_episodes(rows: Iterable[VisitRow], realert_days: int, start: date, end: date,
                   area: tuple[str, ...]) -> list[Episode]:
    """ราย = visit แรกของผู้ป่วยหนึ่งคน; visit ถัดไปภายใน `realert_days` วันนับจาก **วันเริ่มราย**
    เป็นรายเดิม (เหมือนกฎแจ้งเตือน). คืนเฉพาะรายที่เริ่มใน [start, end] (ผู้เรียกควรส่งแถวที่เริ่มก่อน
    `start` อย่างน้อย `realert_days` วัน เพื่อให้ติดตามผลข้ามขอบช่วงไม่ถูกนับเป็นรายใหม่)."""
    by_hn: dict[str, list[VisitRow]] = {}
    for r in rows:
        by_hn.setdefault(r.hn, []).append(r)
    window = timedelta(days=realert_days)
    out: list[Episode] = []
    for visits in by_hn.values():
        visits.sort(key=lambda r: r.day)
        anchor: date | None = None
        for r in visits:
            if anchor is not None and r.day - anchor <= window:
                continue
            anchor = r.day
            if start <= r.day <= end:
                chw, amp = _code2(r.chw), _code2(r.amp)
                out.append(Episode(r.day, bool(chw and amp and (chw + amp) in area),
                                   _tmb_key(r), _moo_key(r.moo)))
    return out


def aggregate(episodes: Iterable[Episode]) -> list[CountRow]:
    counts: dict[tuple, int] = {}
    for e in episodes:
        key = (month_index(e.start), e.in_area, e.tmb, e.moo)
        counts[key] = counts.get(key, 0) + 1
    return [CountRow(*k, n) for k, n in sorted(counts.items(), key=lambda kv: (kv[0][0], not kv[0][1], kv[0][2], kv[0][3]))]


# ---- อนุกรมรายเดือน เกณฑ์ระบาด ----

def monthly_series(rows: Iterable[CountRow], include_outside: bool,
                   through: int | None) -> list[int | None]:
    """จำนวนรายต่อเดือน (index 0–11); เดือนหลัง `through` เป็น None (ยังมาไม่ถึง ≠ 0)."""
    out: list[int | None] = [0] * 12
    for r in rows:
        if r.in_area or include_outside:
            out[r.month] += r.cases  # type: ignore[operator]
    if through is not None:
        for i in range(through + 1, 12):
            out[i] = None
    return out


def total_through(series: list[int | None]) -> int:
    return sum(v for v in series if v is not None)


@dataclass(frozen=True)
class Reference:
    median: list[float]
    threshold: list[float]
    years_used: int
    rough: bool


def reference(prior: list[list[int | None]]) -> Reference | None:
    """มัธยฐานและเกณฑ์ระบาด (ค่าเฉลี่ย + 2 SD ประชากร) ต่อเดือนจากปีย้อนหลังที่มีข้อมูล.
    น้อยกว่า 2 ปี -> None; เท่ากับ 2 ปี -> rough."""
    if len(prior) < 2:
        return None
    median: list[float] = []
    threshold: list[float] = []
    for m in range(12):
        vals = [s[m] for s in prior if s[m] is not None]
        if not vals:
            median.append(0.0)
            threshold.append(0.0)
            continue
        median.append(float(statistics.median(vals)))
        threshold.append(statistics.fmean(vals) + 2 * statistics.pstdev(vals))
    return Reference(median, threshold, len(prior), len(prior) == 2)


def month_status(value: int, median: float, threshold: float) -> str:
    if value > threshold:
        return "over"
    if value > median:
        return "high"
    return "normal"


def pct_change(current: int, base: int) -> float | None:
    if base == 0:
        return None
    return (current - base) / base * 100.0


# ---- ตารางพื้นที่ ----

@dataclass(frozen=True)
class AreaRow:
    key: str
    label_kind: str            # tambon | village | unknown | outside
    totals: dict[int, int]
    children: list["AreaRow"] = field(default_factory=list)


def area_table(years: dict[int, list[CountRow]], include_outside: bool,
               through: int | None) -> list[AreaRow]:
    """แถวตำบล (ลูก = หมู่บ้าน) ต่อปี พ.ศ.ใน `years`; เรียงตามยอดของปีแรก (ปีที่เลือก) มาก → น้อย.
    ท้ายตาราง: "ไม่ทราบพื้นที่" (ในอำเภอแต่ตำบลไม่ครบ) แล้ว "นอกอำเภอ" (เมื่อ include_outside)."""
    yrs = list(years)
    tmb_totals: dict[str, dict[int, int]] = {}
    moo_totals: dict[tuple[str, str], dict[int, int]] = {}
    outside: dict[int, int] = {year: 0 for year in yrs}
    for year, rows in years.items():
        for r in rows:
            if through is not None and r.month > through:
                continue
            if not r.in_area:
                if include_outside:
                    outside[year] += r.cases
                continue
            t = tmb_totals.setdefault(r.tmb, {f: 0 for f in yrs})
            t[year] += r.cases
            if r.tmb:
                m = moo_totals.setdefault((r.tmb, r.moo), {f: 0 for f in yrs})
                m[year] += r.cases

    first = yrs[0] if yrs else None
    tambons: list[AreaRow] = []
    for tmb, totals in tmb_totals.items():
        if not tmb:
            continue
        villages = sorted(((moo, t) for (tt, moo), t in moo_totals.items() if tt == tmb),
                          key=lambda kv: (kv[0] == UNKNOWN_MOO, kv[0]))
        children = [AreaRow(tmb + moo, "unknown" if moo == UNKNOWN_MOO else "village", t)
                    for moo, t in villages]
        tambons.append(AreaRow(tmb, "tambon", totals, children))
    tambons.sort(key=lambda r: (-(r.totals[first] if first is not None else 0), r.key))

    table = tambons
    if "" in tmb_totals:
        table.append(AreaRow("", "unknown", tmb_totals[""]))
    if include_outside:
        table.append(AreaRow("outside", "outside", outside))
    return table
