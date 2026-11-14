"""หน้า /trend — พารามิเตอร์ สรุป กราฟ เกณฑ์ระบาด ตารางพื้นที่ และกรณีฐานล่ม (เวลาจำลอง 10 พ.ย. 2026 = ปี พ.ศ. 2569)"""
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource, HosxpError
from trend import Reference, VisitRow
from trend_views import chart_geometry, parse_params

NOW = datetime(2026, 11, 10, 9, 0)
TODAY = NOW.date()


@pytest.fixture
def settings(tmp_path):
    return load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                          "DENGUE_DB_PATH": str(tmp_path / "d.db"),
                          "DENGUE_AREA_AMPHOE": "3630"})


def month_start(year, m):
    """วันที่ 5 ของ ต.ค. (m=0), พ.ย. (m=1), ธ.ค. (m=2) … ของปี พ.ศ. `year` (ปีปฏิทิน: ต.ค.–ธ.ค. อยู่ปีเดียวกับ ม.ค.)."""
    return date(year - 543, (m + 9) % 12 + 1, 5)


def rows_for(year, m, n, amp="30", moo="3", tmb="01", tag=""):
    d = month_start(year, m)
    return [VisitRow(f"{tag}{year}-{m}-{i}", d, "36", amp, tmb, moo) for i in range(n)]


def base_visits():
    # พ.ย. (index 1): 2568=2, 2567=4, 2566=6 -> มัธยฐาน 4 เกณฑ์ ~7.27;
    # ธ.ค. ของ 2568 (50 ราย) ต้องไม่ถูกนับเมื่อเทียบถึง พ.ย.
    out = rows_for(2568, 1, 2) + rows_for(2567, 1, 4) + rows_for(2566, 1, 6)
    out += rows_for(2568, 0, 1) + rows_for(2567, 0, 1) + rows_for(2566, 0, 1)
    out += rows_for(2568, 2, 50)
    return out


def make_app(settings, store, visits, **kw):
    src = FakeSource(visit_rows=visits, **kw)
    app = create_app(settings=settings, store=store, source=src, sender=FakeSender())
    app.config["CLOCK"] = lambda: NOW
    return app, src


@pytest.fixture
def client_for(login_as):
    def make(app, role="viewer"):
        c = app.test_client()
        login_as(c, "u", role)
        return c
    return make


def page(client, query=""):
    r = client.get("/trend" + query)
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_requires_login(settings, store):
    app, _ = make_app(settings, store, [])
    assert app.test_client().get("/trend").status_code in (302, 401)


def test_viewer_can_open_and_nav_link_present(settings, store, client_for):
    app, _ = make_app(settings, store, base_visits())
    html = page(client_for(app), "")
    assert "เปรียบเทียบปี พ.ศ." in html and 'href="/trend"' in html


def test_default_selects_current_year_and_three_prior(settings, store, client_for):
    app, src = make_app(settings, store, base_visits())
    html = page(client_for(app))
    assert re.search(r'<option value="2569"[^>]*selected', html)
    assert len(re.findall(r'<option value="25\d\d"', html)) == 9
    assert len(src.episode_calls) == 4          # ปีที่เลือก + ย้อนหลัง 3 ปี


def test_bad_params_fall_back_silently():
    p = parse_params({"year": "9999", "years": "7", "all": "x"}, TODAY)
    assert (p.year, p.years, p.include_outside) == (2569, 3, False)
    p = parse_params({"year": "abc", "years": "2", "all": "1"}, TODAY)
    assert (p.year, p.years, p.include_outside) == (2569, 2, True)
    assert parse_params({"year": "2565"}, TODAY).year == 2565


def test_bad_params_page_ok(settings, store, client_for):
    app, _ = make_app(settings, store, base_visits())
    html = page(client_for(app), "?year=9999&years=7&all=zz&refresh=maybe")
    assert "Traceback" not in html and "ValueError" not in html


def test_summary_totals_use_same_month_range_for_open_year(settings, store, client_for):
    app, _ = make_app(settings, store, base_visits() + rows_for(2569, 0, 3) + rows_for(2569, 1, 2))
    html = page(client_for(app))
    assert re.search(r'data-testid="total-2569"[^>]*>\s*5\s*<', html)
    assert re.search(r'data-testid="total-2568"[^>]*>\s*3\s*<', html)    # ต.ค.+พ.ย. ไม่รวม ธ.ค. 50 ราย
    assert re.search(r'data-testid="total-2567"[^>]*>\s*5\s*<', html)
    assert re.search(r'data-testid="total-2566"[^>]*>\s*7\s*<', html)
    assert "ม.ค.–พ.ย." in html
    # เดือนที่ยังไม่ถึงของปีที่เลือกเป็นช่องว่าง "—" ไม่ใช่ 0
    assert re.search(r'data-testid="sel-11"[^>]*>\s*—\s*<', html)


@pytest.mark.parametrize("n,badge", [(3, "ปกติ"), (5, "สูงกว่าปกติ"), (8, "เกินเกณฑ์")])
def test_threshold_badges(settings, store, client_for, n, badge):
    app, _ = make_app(settings, store, base_visits() + rows_for(2569, 1, n))
    html = page(client_for(app))
    m = re.search(r'data-testid="month-status"[^>]*>\s*([^<]+?)\s*<', html)
    assert m and m.group(1) == badge
    assert "เกณฑ์หยาบ" not in html


def test_two_years_marks_rough_threshold(settings, store, client_for):
    app, _ = make_app(settings, store, base_visits() + rows_for(2569, 1, 3))
    html = page(client_for(app), "?years=2")
    assert "เกณฑ์หยาบ" in html and "ข้อมูลย้อนหลังไม่พอ" not in html


def test_one_usable_prior_year_shows_insufficient_and_no_threshold(settings, store, client_for):
    visits = rows_for(2569, 1, 3) + rows_for(2568, 1, 2)   # 2567, 2566 ไม่มีข้อมูล
    app, _ = make_app(settings, store, visits)
    html = page(client_for(app))
    assert "ข้อมูลย้อนหลังไม่พอ" in html and 'data-testid="month-status"' not in html
    assert "ปี พ.ศ. 2567" in html and "ไม่มีข้อมูล" in html


def test_empty_prior_year_is_excluded_with_warning(settings, store, client_for):
    visits = [v for v in base_visits() if not v.hn.startswith("2566-")] + rows_for(2569, 1, 3)
    app, _ = make_app(settings, store, visits)
    html = page(client_for(app))
    assert "ปี พ.ศ. 2566: ไม่มีข้อมูลผู้ป่วยในฐาน HOSxP" in html
    assert 'data-testid="total-2566"' not in html            # ไม่แสดงเป็นการ์ดศูนย์
    assert "เกณฑ์หยาบ" in html                                # เหลือ 2 ปี


def test_outside_toggle_and_area_rows(settings, store, client_for):
    visits = base_visits() + rows_for(2569, 0, 4) + rows_for(2569, 0, 2, amp="31", tag="out")
    app, _ = make_app(settings, store, visits)
    c = client_for(app)
    off = page(c)
    on = page(c, "?all=1")
    assert 'data-testid="area-outside"' not in off and 'data-testid="area-outside"' in on
    assert re.search(r'data-testid="total-2569"[^>]*>\s*4\s*<', off)
    assert re.search(r'data-testid="total-2569"[^>]*>\s*6\s*<', on)


def test_unknown_area_row_present_and_totals_match_chart_table(settings, store, client_for):
    visits = (rows_for(2569, 0, 3) + rows_for(2569, 1, 2, moo=None, tag="m")
              + [VisitRow("nochw", month_start(2569, 0), None, None, None, None)]
              + rows_for(2569, 1, 1, tmb="02", moo="9", tag="t") + base_visits())
    app, _ = make_app(settings, store, visits)
    html = page(client_for(app))
    card = int(re.search(r'data-testid="total-2569"[^>]*>\s*(\d+)\s*<', html).group(1))
    tops = [int(x) for x in re.findall(r'class="area-top"[^>]*data-total="(\d+)"', html)]
    assert sum(tops) == card == 6    # ราย nochw ไม่มีรหัสอำเภอ = นอกอำเภอ ไม่นับเมื่อ all=0
    assert "ไม่ระบุหมู่" in html


def test_source_down_without_cache_page_ok(settings, store, client_for):
    app, _ = make_app(settings, store, [], failure=HosxpError("down 10.1.2.3"),
                      fail_methods=["episode_rows"])
    html = page(client_for(app))
    assert "ดึงข้อมูลไม่สำเร็จ" in html and "10.1.2.3" not in html


def test_source_down_with_stale_cache_shows_cache_and_warning(settings, store, client_for):
    app, src = make_app(settings, store, base_visits() + rows_for(2569, 1, 3))
    c = client_for(app)
    page(c)
    src.failure, src.fail_methods = HosxpError("down"), {"episode_rows"}
    app.config["CLOCK"] = lambda: NOW + timedelta(hours=30)
    html = page(c)
    assert "ใช้ข้อมูลที่เก็บไว้เมื่อ" in html
    assert re.search(r'data-testid="total-2568"[^>]*>\s*3\s*<', html)   # ปีที่จบแล้วยังใช้แคช


def test_no_source_message(settings, store, client_for):
    app = create_app(settings=settings, store=store, source=None, sender=FakeSender())
    html = page(client_for(app))
    assert "ยังไม่ได้เชื่อมต่อฐานข้อมูล" in html


def test_html_and_cache_have_no_patient_identifiers(settings, store, client_for):
    secret = "HN-SECRET-123"
    visits = base_visits() + [VisitRow(secret, month_start(2569, 1), "36", "30", "01", "3")]
    app, _ = make_app(settings, store, visits)
    html = page(client_for(app), "?all=1")
    assert secret not in html
    con = sqlite3.connect(store.path)
    dump = "\n".join(str(r) for t in ("trend_cache_d", "trend_meta_d")
                     for r in con.execute(f"SELECT * FROM {t}"))
    assert secret not in dump and dump


def test_refresh_param_is_throttled_not_error(settings, store, client_for):
    app, src = make_app(settings, store, base_visits())
    c = client_for(app)
    page(c)
    n = len(src.episode_calls)
    page(c, "?refresh=1")
    html = page(c, "?refresh=1")
    assert len(src.episode_calls) == n + 1       # ครั้งที่สองถูกจำกัด
    assert "ไม่เกินทุก 5 นาที" in html


def test_chart_geometry_none_breaks_lines_and_min_scale():
    g = chart_geometry({2569: [1, 2, None, 3, None, None] + [None] * 6, 2568: [0] * 12}, None)
    sel = g.lines[0]
    assert sel.year == 2569 and len(sel.segments) == 1 and len(sel.dots) == 3
    assert g.y_ticks[-1][1] >= 5                         # สเกลต่ำสุด 5
    assert g.median is None and g.threshold is None
    assert g.lines[0].width > g.lines[1].width


def test_chart_geometry_reference_and_bounds():
    ref = Reference([1.0] * 12, [9.0] * 12, 3, False)
    g = chart_geometry({2569: [2] * 12}, ref, width=720, height=280)
    assert g.median and g.threshold
    left, top, right, bottom = g.plot
    for line in g.lines:
        for x, y, _ in line.dots:
            assert left <= x <= right and top <= y <= bottom
    for pts in (g.median, g.threshold):
        for pair in pts.split():
            x, y = map(float, pair.split(","))
            assert left <= x <= right and top <= y <= bottom
    assert g.y_ticks[-1][1] >= 9                          # เส้นเกณฑ์อยู่ในสเกล


def test_open_month_status_is_labelled_partial(settings, store, client_for):
    app, _ = make_app(settings, store, base_visits() + rows_for(2569, 1, 3))
    html = page(client_for(app))
    assert "ยังไม่ครบเดือน" in html and "นับถึงวันที่ 10" in html
    closed = page(client_for(app), "?year=2568")
    assert "ยังไม่ครบเดือน" not in closed


def test_busy_year_shows_message(settings, store, client_for):
    app, src = make_app(settings, store, base_visits())
    svc = app.config["TREND"]
    svc.lock_wait = 0.01
    lock = svc._lock_for(("dengue", 2567))
    lock.acquire()
    try:
        html = page(client_for(app))
    finally:
        lock.release()
    assert "กำลังคำนวณอยู่" in html
