"""trend.py — ปี พ.ศ. นับรายซ้ำ เกณฑ์ระบาด ตารางพื้นที่ (ตรรกะล้วน ไม่ต้องมีฐานข้อมูล)"""
import math
from datetime import date, timedelta

import pytest

import trend
from trend import CountRow, VisitRow

AREA = ("3630",)  # จังหวัด 36 อำเภอ 30


def visit(hn="1", day=date(2025, 10, 5), chw="36", amp="30", tmb="01", moo="3"):
    return VisitRow(hn=hn, day=day, chw=chw, amp=amp, tmb=tmb, moo=moo)


def test_calendar_year_boundaries():
    # ปี พ.ศ. ปฏิทิน: ต.ค.–ธ.ค. ยังเป็นปีเดิม (ไม่ย้ายไปปีถัดไปแบบปีงบ)
    assert trend.year_of(date(2025, 12, 31)) == 2568
    assert trend.year_of(date(2026, 1, 1)) == 2569
    assert trend.year_of(date(2025, 10, 1)) == 2568
    assert trend.year_of(date(2024, 2, 29)) == 2567
    assert trend.year_range(2569) == (date(2026, 1, 1), date(2026, 12, 31))
    assert trend.month_index(date(2026, 1, 1)) == 0
    assert trend.month_index(date(2026, 10, 15)) == 9
    assert trend.month_index(date(2026, 12, 31)) == 11
    assert len(trend.MONTH_LABELS) == 12 and trend.MONTH_LABELS[0] == "ม.ค." and trend.MONTH_LABELS[11] == "ธ.ค."


def test_recent_years():
    assert trend.recent_years(date(2026, 11, 10)) == [2569, 2568, 2567, 2566, 2565, 2564, 2563, 2562, 2561]
    assert trend.recent_years(date(2026, 1, 1), 2) == [2569, 2568]
    assert trend.recent_years(date(2025, 12, 31), 2) == [2568, 2567]


def starts(rows, realert=21, start=date(2025, 10, 1), end=date(2026, 9, 30)):
    return [e.start for e in trend.build_episodes(rows, realert, start, end, AREA)]


def test_episode_gap_21_vs_22_days():
    d0 = date(2025, 11, 1)
    rows = [visit(day=d0), visit(day=d0 + timedelta(days=21)), visit(day=d0 + timedelta(days=22))]
    # วันที่ 21 ยังเป็นรายเดิม วันที่ 22 เป็นรายใหม่ (นับจากวันเริ่มราย)
    assert starts(rows) == [d0, d0 + timedelta(days=22)]
    assert starts(rows[:2]) == [d0]


def test_episode_anchor_is_episode_start_not_last_visit():
    d0 = date(2025, 11, 1)
    rows = [visit(day=d0), visit(day=d0 + timedelta(days=15)), visit(day=d0 + timedelta(days=30))]
    assert starts(rows) == [d0, d0 + timedelta(days=30)]


def test_episode_started_before_range_suppresses_followups():
    rows = [visit(day=date(2025, 9, 25)), visit(day=date(2025, 10, 3))]
    assert starts(rows) == []  # รายเริ่มก่อนช่วง วันที่ 3 ต.ค. เป็นแค่ติดตามผล
    rows = [visit(day=date(2025, 9, 25)), visit(day=date(2025, 10, 20))]
    assert starts(rows) == [date(2025, 10, 20)]


def test_multiple_hn_and_same_day_visits():
    d = date(2025, 12, 1)
    rows = [visit(hn="1", day=d), visit(hn="1", day=d), visit(hn="2", day=d)]
    assert starts(rows) == [d, d]


def test_unsorted_rows_are_ordered_per_hn():
    d0 = date(2025, 11, 1)
    rows = [visit(day=d0 + timedelta(days=10)), visit(day=d0)]
    assert starts(rows) == [d0]


def test_unknown_area_buckets():
    cases = [(None, "00"), ("", "00"), ("0", "00"), ("00", "00"), ("x", "00"), (" 3 ", "03"), ("12", "12")]
    for moo, expected in cases:
        (ep,) = trend.build_episodes([visit(moo=moo)], 21, date(2025, 10, 1), date(2026, 9, 30), AREA)
        assert ep.moo == expected, moo
        assert ep.tmb == "363001"
    (ep,) = trend.build_episodes([visit(tmb=None)], 21, date(2025, 10, 1), date(2026, 9, 30), AREA)
    assert ep.tmb == ""
    (ep,) = trend.build_episodes([visit(tmb="1")], 21, date(2025, 10, 1), date(2026, 9, 30), AREA)
    assert ep.tmb == ""
    # ในเขต = chw+amp อยู่ใน area; ขาดส่วนใดส่วนหนึ่ง = นอกเขต
    flags = [trend.build_episodes([v], 21, date(2025, 10, 1), date(2026, 9, 30), AREA)[0].in_area
             for v in (visit(), visit(amp="31"), visit(chw=None), visit(amp=""))]
    assert flags == [True, False, False, False]


def test_aggregate_groups_counts():
    eps = [trend.Episode(date(2026, 1, 5), True, "363001", "03"),
           trend.Episode(date(2026, 1, 9), True, "363001", "03"),
           trend.Episode(date(2026, 2, 1), False, "", "00")]
    assert sorted(trend.aggregate(eps), key=lambda r: r.month) == [
        CountRow(0, True, "363001", "03", 2), CountRow(1, False, "", "00", 1)]


ROWS = [CountRow(0, True, "363001", "03", 2), CountRow(1, True, "363001", "03", 3),
        CountRow(1, False, "", "00", 4), CountRow(3, True, "363001", "00", 5)]


def test_monthly_series_future_months_are_none():
    s = trend.monthly_series(ROWS, include_outside=False, through=2)
    assert s == [2, 3, 0] + [None] * 9
    assert trend.total_through(s) == 5
    assert trend.monthly_series(ROWS, include_outside=True, through=2)[1] == 7
    full = trend.monthly_series(ROWS, include_outside=False, through=None)
    assert full[3] == 5 and None not in full


def test_reference_median_threshold_and_rough():
    prior = [[2] + [0] * 11, [4] + [0] * 11, [6] + [0] * 11]
    ref = trend.reference(prior)
    assert ref.median[0] == 4
    expected = 4 + 2 * math.sqrt(8 / 3)
    assert ref.threshold[0] == pytest.approx(expected)
    assert ref.years_used == 3 and ref.rough is False
    two = trend.reference(prior[:2])
    assert two.rough is True and two.median[0] == 3 and two.years_used == 2
    assert trend.reference(prior[:1]) is None
    assert trend.reference([]) is None


def test_reference_zero_sd_and_missing_months():
    ref = trend.reference([[5, None] + [0] * 10, [5, None] + [0] * 10])
    assert ref.threshold[0] == 5 and ref.median[0] == 5
    assert ref.median[1] == 0 and ref.threshold[1] == 0  # ไม่มีค่าเลย ไม่ล้ม


def test_month_status():
    assert trend.month_status(4, 4, 9) == "normal"
    assert trend.month_status(5, 4, 9) == "high"
    assert trend.month_status(9, 4, 9) == "high"
    assert trend.month_status(10, 4, 9) == "over"
    assert trend.month_status(1, 0, 0) == "over"
    assert trend.month_status(0, 0, 0) == "normal"


def test_pct_change_zero_base_is_none():
    assert trend.pct_change(5, 0) is None
    assert trend.pct_change(15, 10) == 50.0
    assert trend.pct_change(5, 10) == -50.0


def test_area_table_totals_match_series():
    y1 = [CountRow(0, True, "363001", "03", 2), CountRow(0, True, "363001", "00", 1),
          CountRow(1, True, "363001", "05", 4), CountRow(1, True, "363002", "01", 1),
          CountRow(2, True, "", "00", 3), CountRow(2, False, "", "00", 6),
          CountRow(5, True, "363001", "03", 9)]
    y0 = [CountRow(0, True, "363002", "01", 7)]
    years = {2569: y1, 2568: y0}
    for outside in (False, True):
        for through in (2, None):
            table = trend.area_table(years, include_outside=outside, through=through)
            for year, rows in years.items():
                want = trend.total_through(trend.monthly_series(rows, outside, through))
                assert sum(r.totals[year] for r in table) == want, (outside, through, year)
    table = trend.area_table(years, include_outside=True, through=2)
    kinds = [r.label_kind for r in table]
    assert kinds == ["tambon", "tambon", "unknown", "outside"]
    assert table[0].key == "363001" and table[0].totals == {2569: 7, 2568: 0}
    assert [c.label_kind for c in table[0].children] == ["village", "village", "unknown"]
    assert [c.key for c in table[0].children][:2] == ["36300103", "36300105"]
    assert "outside" not in [r.label_kind for r in trend.area_table(years, False, 2)]
