"""หน้าเปรียบเทียบปี พ.ศ. `GET /trend` — สเปก 2026-11-10-fiscal-year-trend-design ข้อ 5–7.

แสดงเฉพาะจำนวนนับ (ไม่มี HN ชื่อ ที่อยู่รายบุคคล) กราฟเป็น SVG ที่คำนวณพิกัดฝั่งเซิร์ฟเวอร์.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from flask import Flask, render_template, request

import trend
from disease_views import current_rule
from auth import login_required
from trend import MONTH_LABELS, Reference

YEARS_CHOICES = (2, 3)
DEFAULT_YEARS = 3
STATUS_LABELS = {"normal": ("ปกติ", "pill-green"), "high": ("สูงกว่าปกติ", "pill-orange"),
                 "over": ("เกินเกณฑ์", "pill-red")}
LINE_COLORS = ("#1d6ff2", "#f97316", "#16a34a", "#a855f7")   # ปีที่เลือกก่อน แล้วปีย้อนหลังตามลำดับ


@dataclass(frozen=True)
class TrendParams:
    year: int
    years: int
    include_outside: bool
    refresh: bool


def parse_params(args: Mapping[str, str], today: date) -> TrendParams:
    """ค่านอกรายการอนุญาต/ผิดรูป -> ค่าเริ่มต้นเงียบ ๆ (ไม่แสดง error ดิบ)."""
    choices = trend.recent_years(today)
    try:
        year = int((args.get("year") or "").strip())
    except ValueError:
        year = choices[0]
    if year not in choices:
        year = choices[0]
    try:
        years = int((args.get("years") or "").strip())
    except ValueError:
        years = DEFAULT_YEARS
    if years not in YEARS_CHOICES:
        years = DEFAULT_YEARS
    return TrendParams(year, years, args.get("all") == "1", args.get("refresh") == "1")


# ---- เรขาคณิตกราฟ SVG ----
@dataclass(frozen=True)
class ChartLine:
    year: int
    color: str
    width: float
    segments: list[str]                      # polyline points ("x,y x,y …") ต่อช่วงที่ไม่ขาด
    dots: list[tuple[float, float, int]]     # (x, y, ค่า)


@dataclass(frozen=True)
class ChartGeometry:
    width: int
    height: int
    plot: tuple[float, float, float, float]   # left, top, right, bottom
    y_ticks: list[tuple[float, int]]          # (พิกัด y, ค่า)
    x_labels: list[tuple[float, str]]
    lines: list[ChartLine]
    median: str | None
    threshold: str | None


def _nice_step(raw: float) -> int:
    if raw <= 1:
        return 1
    exp = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * exp:
            return int(m * exp)
    return int(10 * exp)


def chart_geometry(series_by_year: dict[int, list[int | None]], reference: Reference | None,
                   width: int = 720, height: int = 280) -> ChartGeometry:
    """ปีแรกของ dict คือปีที่เลือก (เส้นหนา). ค่า None ตัดเส้น; แกน Y ต่ำสุด 0..5."""
    left, top, right, bottom = 44.0, 14.0, width - 14.0, height - 34.0
    values = [v for s in series_by_year.values() for v in s if v is not None]
    if reference is not None:
        values += [t for t in reference.threshold]
    y_max = max([5.0] + [float(v) for v in values])
    step = _nice_step(y_max / 4)
    top_value = max(5, math.ceil(y_max / step) * step)

    def px(i: int) -> float:
        return round(left + (right - left) * i / 11, 1)

    def py(v: float) -> float:
        return round(bottom - (bottom - top) * v / top_value, 1)

    y_ticks = [(py(v), v) for v in range(0, top_value + 1, step)]
    x_labels = [(px(i), MONTH_LABELS[i]) for i in range(12)]
    lines: list[ChartLine] = []
    for n, (year, series) in enumerate(series_by_year.items()):
        segments: list[str] = []
        run: list[str] = []
        dots: list[tuple[float, float, int]] = []
        for i, v in enumerate(series):
            if v is None:
                if len(run) >= 2:
                    segments.append(" ".join(run))
                run = []
                continue
            run.append(f"{px(i)},{py(v)}")
            dots.append((px(i), py(v), v))
        if len(run) >= 2:
            segments.append(" ".join(run))
        lines.append(ChartLine(year, LINE_COLORS[n % len(LINE_COLORS)], 3.2 if n == 0 else 1.8,
                               segments, dots))
    median = threshold = None
    if reference is not None:
        median = " ".join(f"{px(i)},{py(v)}" for i, v in enumerate(reference.median))
        threshold = " ".join(f"{px(i)},{py(v)}" for i, v in enumerate(reference.threshold))
    return ChartGeometry(width, height, (left, top, right, bottom), y_ticks, x_labels, lines,
                         median, threshold)


# ---- ประกอบข้อมูลหน้า ----
def _label_area(row: trend.AreaRow, names: dict[str, str]) -> str:
    if row.label_kind == "outside":
        return "นอกอำเภอ"
    if row.label_kind == "unknown":
        return "ไม่ทราบพื้นที่" if not row.key else "ไม่ระบุหมู่"
    if row.label_kind == "tambon":
        return "ต." + names.get(row.key, row.key)
    moo = int(row.key[6:8]) if len(row.key) == 8 else 0
    name = names.get(row.key)
    return f"ม.{moo} {name}" if name else f"ม.{moo}"


def _area_view(table: list[trend.AreaRow], yrs: list[int], names: dict[str, str],
               selected: int, prior: list[int]) -> list[dict]:
    def convert(row: trend.AreaRow) -> dict:
        base = [row.totals[year] for year in prior]
        mean = sum(base) / len(base) if base else 0
        sel = row.totals[selected]
        return {
            "key": row.key, "kind": row.label_kind, "label": _label_area(row, names),
            "totals": [row.totals[year] for year in yrs], "selected": sel,
            "hot": bool(prior) and sel > mean and sel > 0,
            "children": [convert(c) for c in row.children],
        }
    return [convert(r) for r in table]


def build_context(svc, params: TrendParams, today: date, disease: str = "dengue") -> dict:
    current_yr = trend.year_of(today)
    through = trend.month_index(today) if params.year == current_yr else None
    yrs = [params.year - i for i in range(params.years + 1)]
    results = {year: svc.year(year, refresh=params.refresh and year == current_yr, disease=disease) for year in yrs}
    warnings: list[str] = []
    for year in yrs:
        r = results[year]
        if r.busy:
            warnings.append(f"ปี พ.ศ. {year}: กำลังคำนวณอยู่ — เปิดหน้านี้ใหม่ในอีกสักครู่"
                            + ("" if r.rows else " (ยังไม่มีข้อมูลแสดง)"))
        elif r.error and r.computed_at is None:
            warnings.append(f"ปี พ.ศ. {year}: {r.error}")
        elif r.stale:
            warnings.append(f"ปี พ.ศ. {year}: {r.error} — ใช้ข้อมูลที่เก็บไว้เมื่อ "
                            f"{r.computed_at:%d/%m/%Y %H:%M}")
        if r.throttled:
            warnings.append("กดคำนวณใหม่ได้ไม่เกินทุก 5 นาที — แสดงข้อมูลที่คำนวณล่าสุด")

    selected = results[params.year]
    prior_yrs: list[int] = []
    for year in yrs[1:]:
        r = results[year]
        failed = (r.error is not None or r.busy) and r.computed_at is None
        has_data = any(c.cases > 0 for c in r.rows)
        if failed:
            continue
        if not has_data:
            warnings.append(f"ปี พ.ศ. {year}: ไม่มีข้อมูลผู้ป่วยในฐาน HOSxP — ไม่นำมาคำนวณเกณฑ์")
            continue
        prior_yrs.append(year)

    inc = params.include_outside
    full = {year: trend.monthly_series(results[year].rows, inc, None) for year in [params.year] + prior_yrs}
    sel_series = trend.monthly_series(selected.rows, inc, through)
    full[params.year] = sel_series
    through_idx = 11 if through is None else through
    totals = {year: trend.total_through(full[year][:through_idx + 1]) for year in full}

    ref = trend.reference([full[year] for year in prior_yrs])
    notes: list[str] = []
    if len(prior_yrs) < 2:
        notes.append("ข้อมูลย้อนหลังไม่พอ (ต้องมีอย่างน้อย 2 ปีที่มีข้อมูล) — ไม่แสดงเกณฑ์ระบาด")
    elif ref is not None and ref.rough:
        notes.append("เกณฑ์หยาบ (คำนวณจาก 2 ปี)")

    rows = []
    for m in range(12):
        value = sel_series[m]
        status = None
        if ref is not None and value is not None:
            status = trend.month_status(value, ref.median[m], ref.threshold[m])
        rows.append({
            "label": MONTH_LABELS[m], "selected": value,
            "prior": [full[year][m] for year in prior_yrs],
            "median": None if ref is None else ref.median[m],
            "threshold": None if ref is None else ref.threshold[m],
            "status": status and {"key": status, "label": STATUS_LABELS[status][0],
                                  "css": STATUS_LABELS[status][1]},
            "current": through is not None and m == through,
        })
    current_row = next((r for r in rows if r["current"]), None)

    cards = [{"year": params.year, "total": totals[params.year], "selected": True, "pct": None}]
    for year in prior_yrs:
        cards.append({"year": year, "total": totals[year], "selected": False,
                      "pct": trend.pct_change(totals[params.year], totals[year])})

    area_years = {params.year: selected.rows}
    for year in prior_yrs:
        area_years[year] = results[year].rows
    table = trend.area_table(area_years, inc, through)
    codes: list[str] = []
    for r in table:
        if r.label_kind == "tambon":
            codes.append(r.key)
            codes += [c.key for c in r.children if c.label_kind == "village"]
    names = svc.names(codes) if codes else {}
    area_yrs = list(area_years)
    area = _area_view(table, area_yrs, names, params.year, prior_yrs)

    chart_series = {year: full[year] for year in [params.year] + prior_yrs}
    return {
        "params": params, "year_options": trend.recent_years(today), "current_yr": current_yr,
        "through": through, "months_label": (f"ม.ค.–{MONTH_LABELS[through]}" if through is not None
                                              else "ม.ค.–ธ.ค."),
        "warnings": warnings, "notes": notes, "cards": cards, "rows": rows,
        "current_row": current_row, "prior_yrs": prior_yrs, "ref": ref,
        "chart": chart_geometry(chart_series, ref), "chart_yrs": list(chart_series),
        "area": area, "area_yrs": area_yrs,
        "selected_failed": (selected.error is not None or selected.busy) and selected.computed_at is None,
        "computed_at": selected.computed_at,
        "partial_note": (f"นับถึงวันที่ {today.day} ยังไม่ครบเดือน — ป้ายสถานะอาจต่ำกว่าความจริงช่วงต้นเดือน"
                         if through is not None else None),
    }


def register_trend_routes(app: Flask) -> None:
    @app.get("/trend")
    @login_required
    def trend_page():
        svc = app.config.get("TREND")
        today: date = app.config["CLOCK"]().date()
        params = parse_params(request.args, today)
        rule = current_rule(app.config["SETTINGS"], request.args)
        if svc is None:
            return render_template("trend.html", page_title="เปรียบเทียบปี พ.ศ.", no_source=True,
                                   params=params, year_options=trend.recent_years(today))
        ctx = build_context(svc, params, today, rule.key)
        return render_template("trend.html", page_title="เปรียบเทียบปี พ.ศ.", no_source=False, **ctx)

