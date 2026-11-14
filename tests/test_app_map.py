"""แผนที่รวม (Task 8): GET /map และ GET /api/map.json."""
import json
import re
from datetime import datetime, timedelta

import pytest

from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource

# เวลาอิงปัจจุบัน — หน้าต่างวันของ /map คำนวณจาก date.today()
NOW = datetime.now().replace(microsecond=0)
LAT, LNG = 14.3500, 101.9000
NAME, PHONE, ADDR = "นายทดสอบ ใจดี", "0899990001", "บ้านเลขที่ 99 หมู่ 3"
HN = "ZX9071"


@pytest.fixture
def settings(tmp_path):
    return load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                          "DENGUE_DB_PATH": str(tmp_path / "d.db")})


@pytest.fixture
def app(settings, store):
    return create_app(settings=settings, store=store, source=FakeSource(),
                      sender=FakeSender())


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def viewer(client, login_as):
    login_as(client, "viewer1", "viewer")
    return client


def add_case(store, n=1, dx_at=NOW, hn=None, in_area=True, icd="A90"):
    return store.add_case(f"VN:{n}", "OPD", hn or f"{n:06d}", icd, dx_at,
                          "40010103", str(n), "1", 30, in_area)


def pin_at(store, cid, lat=LAT, lng=LNG, state="confirmed"):
    store.set_pin(cid, lat, lng, state, "manual", "seed")


def pinned(store, n=1, state="confirmed", **kw):
    cid = add_case(store, n, **kw)
    pin_at(store, cid, lat=LAT + n * 0.0001, state=state)
    return cid


def api(client, query=""):
    return client.get("/api/map.json" + query)


def map_data(html):
    m = re.search(r'<script type="application/json" id="map-data">(.*?)</script>',
                  html, re.S)
    assert m, "ไม่พบบล็อก map-data"
    return json.loads(m.group(1))


# ---------------------------------------------------------------- สิทธิ์

def test_map_json_requires_login(client, store):
    pinned(store)
    r = api(client)
    assert r.status_code == 401
    page = client.get("/map")
    assert page.status_code == 302 and "/login" in page.headers["Location"]


def test_map_json_401_without_session(client):
    r = api(client, "?days=7")
    assert r.status_code == 401 and r.is_json
    assert "cases" not in r.get_json()


def test_map_page_open_to_any_role(viewer, store):
    pinned(store)
    assert viewer.get("/map").status_code == 200
    assert api(viewer).status_code == 200


# ---------------------------------------------------------------- JSON

def test_map_json_excludes_cancelled_and_none_pin(viewer, store):
    ok = pinned(store, 1, state="estimated")
    cancelled = pinned(store, 2)
    store.mark_cancelled(cancelled)
    add_case(store, 3)  # ยังไม่ปักหมุด (pin_state none)
    body = api(viewer).get_json()
    assert [c["id"] for c in body["cases"]] == [ok]
    assert {c["pin_state"] for c in body["cases"]} <= {"estimated", "confirmed"}


def test_map_json_has_no_hn_or_name(viewer, store, app):
    cid = pinned(store, 1, hn=HN)
    app.config["SOURCE"].contacts[HN] = {"name": NAME, "phone": PHONE, "address": ADDR}
    r = api(viewer)
    raw = r.get_data(as_text=True)
    for secret in (HN, NAME, PHONE, ADDR, "VN:1", "house", "address", "phone", "sex",
                   "age", "hn", "name", "visit"):
        assert secret not in raw
    [c] = r.get_json()["cases"]
    assert set(c) == {"id", "lat", "lng", "pin_state", "dx_at", "in_area",
                      "cluster_id", "outside_window", "url"}
    assert c["id"] == cid and c["url"] == f"/case/{cid}"
    assert c["in_area"] is True and c["cluster_id"] is None
    assert c["outside_window"] is False
    assert c["pin_state"] == "confirmed"
    assert c["lat"] == pytest.approx(LAT + 0.0001) and c["lng"] == pytest.approx(LNG)
    assert datetime.fromisoformat(c["dx_at"]) == NOW


def test_map_json_top_level_shape(viewer, store, settings):
    pinned(store)
    body = api(viewer).get_json()
    assert body["radius_m"] == settings.cluster_radius_m
    assert body["days"] == settings.cluster_days


def test_map_json_days_window_and_invalid_days_fallback(viewer, store, settings):
    recent = pinned(store, 1, dx_at=NOW - timedelta(days=2))
    mid = pinned(store, 2, dx_at=NOW - timedelta(days=10))
    old = pinned(store, 3, dx_at=NOW - timedelta(days=60))

    def ids(query):
        return sorted(c["id"] for c in api(viewer, query).get_json()["cases"])

    assert ids("?days=7") == [recent]
    assert ids("?days=14") == [recent, mid]
    assert ids("?days=90") == [recent, mid, old]
    assert api(viewer, "?days=7").get_json()["days"] == 7
    # ค่าไม่ถูกต้อง/นอกช่วง 1-365 -> ค่าเริ่มต้น (settings.cluster_days = 14)
    for bad in ("abc", "0", "-5", "366", "", "1.5", "99999999999999999999"):
        body = api(viewer, f"?days={bad}").get_json()
        assert body["days"] == settings.cluster_days, bad
        assert sorted(c["id"] for c in body["cases"]) == [recent, mid], bad
    assert api(viewer, "?days=365").get_json()["days"] == 365


def test_map_json_cluster_id_from_saved_cluster(viewer, store):
    a, b, c = pinned(store, 1), pinned(store, 2), pinned(store, 3)
    cid = store.save_cluster([a, b], NOW, NOW)
    by_id = {x["id"]: x for x in api(viewer).get_json()["cases"]}
    assert by_id[a]["cluster_id"] == cid and by_id[b]["cluster_id"] == cid
    assert by_id[c]["cluster_id"] is None


def test_map_json_cluster_param_picks_selected_when_overlapping(viewer, store):
    a, b, c = pinned(store, 1), pinned(store, 2), pinned(store, 3)
    older = store.save_cluster([a, b], NOW, NOW)
    newer = store.save_cluster([b, c], NOW, NOW)
    default = {x["id"]: x["cluster_id"] for x in api(viewer).get_json()["cases"]}
    assert default == {a: older, b: newer, c: newer}
    picked = {x["id"]: x["cluster_id"]
              for x in api(viewer, f"?cluster={older}").get_json()["cases"]}
    assert picked == {a: older, b: older, c: newer}


def old_cluster(store, days_ago=60):
    old = NOW - timedelta(days=days_ago)
    a = pinned(store, 1, dx_at=old)
    b = pinned(store, 2, dx_at=old)
    cid = store.save_cluster([a, b], old, old)
    return a, b, cid


def test_map_json_selected_cluster_includes_members_outside_window(viewer, store):
    a, b, cid = old_cluster(store)
    recent = pinned(store, 3)
    sel = api(viewer, f"?days=7&cluster={cid}").get_json()["cases"]
    by_id = {c["id"]: c for c in sel}
    assert set(by_id) == {a, b, recent}
    assert by_id[a]["outside_window"] is True and by_id[b]["outside_window"] is True
    assert by_id[a]["cluster_id"] == cid and by_id[recent]["outside_window"] is False
    plain = api(viewer, "?days=7").get_json()["cases"]
    assert [c["id"] for c in plain] == [recent]
    assert api(viewer, f"?days=7&cluster={cid}").get_json()["days"] == 7


def test_map_json_selected_cluster_excludes_cancelled_and_unpinned_members(viewer, store):
    a, b, cid = old_cluster(store)
    store.mark_cancelled(b)
    unpinned = add_case(store, 9, dx_at=NOW - timedelta(days=60))
    store.update_cluster(cid, [a, b, unpinned], NOW, NOW)
    ids = [c["id"] for c in api(viewer, f"?days=7&cluster={cid}").get_json()["cases"]]
    assert ids == [a]


def test_map_page_outside_window_notice(viewer, store):
    a, b, cid = old_cluster(store)
    html = viewer.get(f"/map?days=7&cluster={cid}").get_data(as_text=True)
    assert 'data-testid="cluster-outside-notice"' in html
    assert "รวมสมาชิกกลุ่มที่อยู่นอกช่วง 7 วัน" in html
    # สมาชิกอยู่ในช่วง / ไม่ได้เลือก cluster / เลือกแล้วสมาชิกถูกยกเลิกหมด -> ไม่มีป้าย
    assert 'data-testid="cluster-outside-notice"' not in viewer.get(
        f"/map?days=90&cluster={cid}").get_data(as_text=True)
    assert 'data-testid="cluster-outside-notice"' not in viewer.get(
        "/map?days=7").get_data(as_text=True)
    store.mark_cancelled(a)
    store.mark_cancelled(b)
    assert 'data-testid="cluster-outside-notice"' not in viewer.get(
        f"/map?days=7&cluster={cid}").get_data(as_text=True)


def test_map_newest_cluster_choice_is_order_independent(viewer, store, monkeypatch):
    a, b, c = pinned(store, 1), pinned(store, 2), pinned(store, 3)
    older = store.save_cluster([a, b], NOW, NOW)
    newer = store.save_cluster([b, c], NOW, NOW)
    real = store.clusters
    monkeypatch.setattr(store, "clusters", lambda *a: list(reversed(real(*a))))
    got = {x["id"]: x["cluster_id"] for x in api(viewer).get_json()["cases"]}
    assert got == {a: older, b: newer, c: newer}


def test_map_clusters_read_once_per_request(viewer, store, monkeypatch):
    pinned(store)
    calls = []
    real = store.clusters
    monkeypatch.setattr(store, "clusters", lambda *a: calls.append(1) or real(*a))
    api(viewer, "?cluster=1")
    assert len(calls) == 1
    viewer.get("/map?cluster=1")
    assert len(calls) == 2


# ---------------------------------------------------------------- หน้า /map

def test_map_cluster_param_marks_members(viewer, store):
    a, b, c = pinned(store, 1), pinned(store, 2), pinned(store, 3)
    cid = store.save_cluster([a, b], NOW, NOW)
    html = viewer.get(f"/map?cluster={cid}").get_data(as_text=True)
    data = map_data(html)
    assert data["selected_cluster"] == cid
    # JS ได้ cluster_id ของสมาชิกจาก api_url เดียวกับที่หน้าเรียก
    body = viewer.get(data["api_url"]).get_json()
    members = sorted(x["id"] for x in body["cases"] if x["cluster_id"] == data["selected_cluster"])
    assert members == [a, b]


def test_map_cluster_param_unknown_is_ignored(viewer, store):
    pinned(store)
    for bad in ("999", "abc", "-1", "", "1.5"):
        r = viewer.get(f"/map?cluster={bad}")
        assert r.status_code == 200, bad
        data = map_data(r.get_data(as_text=True))
        assert data["selected_cluster"] is None, bad
        assert "cluster" not in data["api_url"], bad


def test_map_page_data_block(viewer, store, settings):
    html = viewer.get("/map?days=30").get_data(as_text=True)
    data = map_data(html)
    assert set(data) == {"api_url", "radius_m", "tile_url", "attribution",
                         "selected_cluster", "days", "login_url",
                         "boundary_url", "past_url", "past",
                         "tambon_url", "facility_url", "facility_put_url"}
    assert data["days"] == 30 and data["radius_m"] == settings.cluster_radius_m
    assert data["api_url"].startswith("/api/map.json") and "days=30" in data["api_url"]
    assert data["tile_url"] == settings.tile_url
    assert "OpenStreetMap" in data["attribution"]
    assert data["selected_cluster"] is None
    default = map_data(viewer.get("/map").get_data(as_text=True))
    assert default["days"] == settings.cluster_days
    assert map_data(viewer.get("/map?days=zzz").get_data(as_text=True))["days"] == 14


def test_map_page_has_no_patient_data(viewer, store, app):
    pinned(store, 1, hn=HN)
    app.config["SOURCE"].contacts[HN] = {"name": NAME, "phone": PHONE, "address": ADDR}
    html = viewer.get("/map").get_data(as_text=True)
    for secret in (HN, NAME, PHONE, ADDR):
        assert secret not in html


def test_map_page_has_no_external_script_or_style(viewer, store):
    pinned(store)
    html = viewer.get("/map").get_data(as_text=True)
    assert not re.findall(r'<(?:script|link)[^>]+(?:src|href)="(?:https?:)?//', html)
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert srcs and all(s.startswith("/static/") for s in srcs)
    assert "/static/leaflet/leaflet.js" in srcs and "/static/map.js" in srcs
    links = re.findall(r'<link[^>]+href="([^"]+)"', html)
    assert "/static/leaflet/leaflet.css" in links
    assert all(h.startswith("/static/") for h in links)


def test_map_page_filter_pills_and_nav(viewer, store):
    html = viewer.get("/map?days=30").get_data(as_text=True)
    for n in (7, 14, 30, 90):
        assert f"/map?days={n}" in html
    on = re.findall(r'class="fpill on"[^>]*>([^<]*)<', html)
    assert on == ["30 วัน", "ไม่แสดง"]     # กลุ่มช่วงวัน + กลุ่ม "ปี พ.ศ.ก่อนหน้า" (ค่าเริ่มต้น = ไม่แสดง)
    # ลิงก์ในเมนูข้างใช้ url_for และไฮไลต์เมื่ออยู่หน้าแผนที่
    assert re.search(r'<a href="/map" class="on">', html)
    other = viewer.get("/").get_data(as_text=True)
    assert re.search(r'<a href="/map" class="">', other)


def test_map_url_respects_script_prefix(viewer, store):
    cid = pinned(store)
    hdr = {"X-Forwarded-Prefix": "/dengue"}
    body = viewer.get("/api/map.json", headers=hdr).get_json()
    assert body["cases"][0]["url"] == f"/dengue/case/{cid}"
    html = viewer.get("/map?days=7", headers=hdr).get_data(as_text=True)
    data = map_data(html)
    assert data["api_url"].startswith("/dengue/api/map.json")
    assert 'href="/dengue/map?days=14"' in html
    assert 'href="/dengue/map"' in html  # เมนูข้าง
    assert 'src="/dengue/static/map.js"' in html


def test_map_page_custom_days_pill_and_links_keep_cluster(viewer, store):
    a, b = pinned(store, 1), pinned(store, 2)
    cid = store.save_cluster([a, b], NOW, NOW)
    html = viewer.get(f"/map?days=45&cluster={cid}").get_data(as_text=True)
    assert re.findall(r'class="fpill on"[^>]*>([^<]*)<', html) == ["45 วัน", "ไม่แสดง"]
    assert map_data(html)["days"] == 45
    for n in (7, 14, 30, 90, 45):
        assert f"/map?days={n}&amp;cluster={cid}" in html
    # ปุ่ม "ดูทั้งหมด" ล้าง cluster แต่คงช่วงวัน
    assert 'href="/map?days=45"' in html
    assert api(viewer, "?days=45").get_json()["days"] == 45


def test_map_page_login_url_in_data_is_prefix_aware(viewer, store):
    data = map_data(viewer.get("/map?days=7").get_data(as_text=True))
    assert data["login_url"].startswith("/login?next=")
    hdr = {"X-Forwarded-Prefix": "/dengue"}
    data = map_data(viewer.get("/map?days=7", headers=hdr).get_data(as_text=True))
    assert data["login_url"].startswith("/dengue/login?next=")


def test_map_json_error_paths(client, viewer, store):
    # สถานะที่ JS ต้องรับมือ: 401 เป็น JSON (ไม่ redirect), เส้นทางไม่ล้มเมื่อไม่มีข้อมูล
    assert api(viewer).get_json() == {"cases": [], "radius_m": 100, "days": 14}
    client.post("/logout")
    r = api(client)
    assert r.status_code == 401 and r.is_json


# ---------------------------------------------------------------- เส้นขอบ + การระบาดปีก่อน
def test_parse_past_whitelist():
    from datetime import date
    from map_views import parse_past
    today = date(2026, 11, 10)      # ปี พ.ศ. 2569 (ปีปัจจุบันไม่นับเป็น "ปีก่อนหน้า")
    assert [parse_past(str(y), today) for y in (2568, 2567, 2566)] == [2568, 2567, 2566]
    for bad in ("2569", "2565", "abc", "", None, "-2568", "2568.0", " "):
        assert parse_past(bad, today) is None


def test_map_page_has_past_buttons_and_keeps_days_cluster(viewer, store):
    from datetime import date
    from trend import year_of
    cid = pinned(store, 1)
    cl = store.save_cluster([cid], NOW, NOW)
    html = viewer.get(f"/map?days=30&cluster={cl}&past=" + str(year_of(date.today()) - 1)).get_data(as_text=True)
    cur = year_of(date.today())
    block = re.search(r'class="fgroup past-opts".*?</div>', html, re.S).group(0)
    assert "ไม่แสดง" in block
    for y in (cur - 1, cur - 2, cur - 3):
        link = re.search(rf'<a class="fpill[^"]*" href="([^"]*past={y}[^"]*)"', block)
        assert link, y
        assert "days=30" in link.group(1) and f"cluster={cl}" in link.group(1)
    assert str(cur) not in re.sub(r"<[^>]+>", " ", block)          # ไม่มีปีปัจจุบัน
    off = re.search(r'<a class="fpill[^"]*" href="([^"]*)">ไม่แสดง', block).group(1)
    assert "past=" not in off and "days=30" in off
    # ปุ่มช่วงวันคง past ไว้
    assert re.search(r'href="[^"]*days=7[^"]*past=%d' % (cur - 1), html) or         re.search(r'href="[^"]*past=%d[^"]*days=7' % (cur - 1), html)


def test_map_data_has_boundary_url_and_past_url_only_when_selected(viewer, store):
    from datetime import date
    from trend import year_of
    cur = year_of(date.today())
    none = map_data(viewer.get("/map").get_data(as_text=True))
    assert none["boundary_url"] == "/api/boundary.json" and none["past_url"] is None and none["past"] is None
    sel = map_data(viewer.get(f"/map?past={cur - 2}").get_data(as_text=True))
    assert sel["past"] == cur - 2 and sel["past_url"] == f"/api/past.json?year={cur - 2}"


def test_map_page_bad_past_ignored(viewer, store):
    from datetime import date
    from trend import year_of
    cur = year_of(date.today())
    for bad in (str(cur), str(cur - 4), "abc", ""):
        data = map_data(viewer.get(f"/map?past={bad}").get_data(as_text=True))
        assert data["past"] is None and data["past_url"] is None


def test_legend_past_only_when_selected(viewer, store):
    from datetime import date
    from trend import year_of
    cur = year_of(date.today())
    assert 'data-testid="legend-past"' not in viewer.get("/map").get_data(as_text=True)
    html = viewer.get(f"/map?past={cur - 1}").get_data(as_text=True)
    assert 'data-testid="legend-past"' in html and str(cur - 1) in html
    assert 'id="past-note"' in html and 'id="layer-note"' in html


# ---------------------------------------------------------------- เลเยอร์ตำบลและสถานพยาบาล
def test_map_page_has_layer_toggles_and_legends(viewer, store):
    html = viewer.get("/map").get_data(as_text=True)
    for needle in ('id="toggle-tambon"', 'id="toggle-facility"', 'id="legend-tambon"', 'id="legend-facility"',
                   'id="facility-admin"', 'id="facility-edit"', 'id="facility-unplaced"', 'id="facility-status"',
                   'id="facility-note"'):
        assert needle in html, needle
    assert re.search(r'id="toggle-tambon"[^>]*checked', html) and re.search(r'id="toggle-facility"[^>]*checked', html)
    assert re.search(r'id="facility-admin"[^>]*hidden', html) and re.search(r'id="legend-tambon"[^>]*hidden', html)


def test_map_data_urls_are_prefix_aware(viewer, store):
    data = map_data(viewer.get("/map", headers={"X-Forwarded-Prefix": "/dengue"}).get_data(as_text=True))
    assert data["tambon_url"] == "/dengue/api/tambons.json"
    assert data["facility_url"] == "/dengue/api/facilities.json"
    assert data["facility_put_url"].startswith("/dengue/api/facilities/")


def test_facility_put_url_template_contains_placeholder(viewer, store):
    data = map_data(viewer.get("/map").get_data(as_text=True))
    assert data["facility_put_url"] == "/api/facilities/__CODE__"
    assert data["tambon_url"] == "/api/tambons.json" and data["facility_url"] == "/api/facilities.json"
