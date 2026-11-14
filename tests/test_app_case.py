"""หน้าเคส + API ปักหมุด (Task 7)."""
import dataclasses
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from app import create_app
from geo import haversine_m
from config import load_settings
from fakes import FakeSender, FakeSource, HosxpError

HDR = {"X-Requested-With": "dengue"}
NOW = datetime(2026, 11, 8, 9, 0)
# พิกัดทดสอบในเขตอำเภอวังน้ำเขียว (ห่างกันราว 11 ม. ต่อ 0.0001 องศา lat)
LAT, LNG = 14.3500, 101.9000
NAME, PHONE, ADDR = "นายทดสอบ ใจดี", "0899990001", "บ้านเลขที่ 99 หมู่ 3"


def make_settings(tmp_path, **env):
    base = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db")}
    base.update(env)
    return load_settings(base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def source():
    return FakeSource(contacts={"000001": {"name": NAME, "phone": PHONE, "address": ADDR}})


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def app(settings, store, source, sender):
    return create_app(settings=settings, store=store, source=source, sender=sender)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def srrt(client, login_as):
    login_as(client, "nurse", "srrt")
    return client


def add_case(store, n=1, hn=None, icd="A90", visit_type="OPD", dx_at=NOW):
    key = f"{'AN' if visit_type == 'IPD' else 'VN'}:{n}"
    return store.add_case(key, visit_type, hn or f"{n:06d}", icd, dx_at,
                          "40010103", str(n), "1", 30, True)


def pin_at(store, cid, lat=LAT, lng=LNG, state="confirmed"):
    store.set_pin(cid, lat, lng, state, "manual", "seed")


def post_pin(client, cid, body=None, headers=HDR, **kw):
    body = {"lat": LAT, "lng": LNG, "state": "confirmed"} if body is None else body
    return client.post(f"/api/case/{cid}/pin", json=body, headers=headers, **kw)


def audit_rows(store):
    return store.audit()


def case_data(html):
    m = re.search(r'<script type="application/json" id="case-data">(.*?)</script>',
                  html, re.S)
    assert m, "ไม่พบบล็อก case-data"
    return json.loads(m.group(1))


# ---------------------------------------------------------------- หน้าเคส

def test_case_page_requires_login(client, store):
    cid = add_case(store)
    r = client.get(f"/case/{cid}")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_case_404_for_unknown_id(srrt):
    assert srrt.get("/case/999").status_code == 404


def test_case_page_shows_contact_from_source_not_store(srrt, store, app):
    cid = add_case(store)
    pin_at(store, cid)
    html = srrt.get(f"/case/{cid}").get_data(as_text=True)
    assert NAME in html and PHONE in html and ADDR in html
    assert "000001" in html  # HN แสดงให้ผู้ที่ login แล้ว
    # ข้อมูลติดต่อต้องไม่หลุดเข้า <title>, บล็อก JSON, หรือฐานข้อมูลของแอป
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    assert NAME not in title and PHONE not in title and "000001" not in title
    data = json.dumps(case_data(html), ensure_ascii=False)
    for secret in (NAME, PHONE, ADDR, "000001"):
        assert secret not in data
    raw = Path(store.path).read_bytes()
    for secret in (NAME, PHONE, ADDR):
        assert secret.encode() not in raw


def test_case_page_escapes_contact_html(srrt, store, source):
    cid = add_case(store)
    source.contacts["000001"] = {"name": "<script>alert(1)</script>", "phone": "",
                                 "address": ""}
    html = srrt.get(f"/case/{cid}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_case_page_survives_hosxp_error(srrt, store, source):
    cid = add_case(store)
    pin_at(store, cid)
    source.failure = HosxpError("down")
    r = srrt.get(f"/case/{cid}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "ดึงข้อมูลจาก HOSxP ไม่ได้ในขณะนี้" in html
    assert NAME not in html
    assert case_data(html)["pin"]["state"] == "confirmed"  # ยังได้แผนที่


def test_case_page_survives_unexpected_source_error_and_no_source(login_as, store,
                                                                 settings):
    cid = add_case(store)
    app = create_app(settings=settings, store=store, source=None, sender=None)
    c2 = app.test_client()
    login_as(c2)
    html = c2.get(f"/case/{cid}").get_data(as_text=True)
    assert "ดึงข้อมูลจาก HOSxP ไม่ได้ในขณะนี้" in html
    app.config["SOURCE"] = FakeSource(failure=RuntimeError("boom"))
    r = c2.get(f"/case/{cid}")
    assert r.status_code == 200
    assert "ดึงข้อมูลจาก HOSxP ไม่ได้ในขณะนี้" in r.get_data(as_text=True)


def test_case_page_header_and_data_block(srrt, store, settings):
    cid = add_case(store, icd="A911", visit_type="IPD")
    pin_at(store, cid, state="estimated")
    html = srrt.get(f"/case/{cid}").get_data(as_text=True)
    assert f"เคส #{cid} · A91.1 · IPD" in html
    assert "ประมาณ" in html and "ในเขต" in html
    d = case_data(html)
    assert d["case_id"] == cid and d["can_edit"] is True
    assert d["radius_m"] == settings.cluster_radius_m
    assert d["pin"] == {"lat": LAT, "lng": LNG, "state": "estimated"}
    assert d["tile_url"] == settings.tile_url
    assert "© OpenStreetMap contributors" in html
    assert d["attribution"] == "© OpenStreetMap contributors"


def test_case_page_hides_osm_attribution_for_custom_tiles(tmp_path, store, source,
                                                          login_as):
    s = make_settings(tmp_path, DENGUE_TILE_URL="https://tiles.example.org/{z}/{x}/{y}.png")
    c = create_app(settings=s, store=store, source=source).test_client()
    login_as(c)
    cid = add_case(store)
    html = c.get(f"/case/{cid}").get_data(as_text=True)
    assert "© OpenStreetMap contributors" not in html
    assert case_data(html)["attribution"] == ""
    assert case_data(html)["tile_url"] == "https://tiles.example.org/{z}/{x}/{y}.png"


def test_case_page_default_center_without_pin(srrt, store):
    cid = add_case(store)
    other = add_case(store, 2)
    pin_at(store, other, 14.40, 101.95)
    d = case_data(srrt.get(f"/case/{cid}").get_data(as_text=True))
    assert d["pin"] == {"lat": None, "lng": None, "state": "none"}
    assert d["center"] == {"lat": 14.40, "lng": 101.95, "zoom": 12}


def test_case_page_default_center_hospital_area_when_nothing_pinned(srrt, store):
    cid = add_case(store)
    d = case_data(srrt.get(f"/case/{cid}").get_data(as_text=True))
    assert d["center"] == {"lat": 14.35, "lng": 101.90, "zoom": 12}


def test_case_page_has_no_external_script_or_style(srrt, store):
    cid = add_case(store)
    pin_at(store, cid)
    html = srrt.get(f"/case/{cid}").get_data(as_text=True)
    assert not re.search(r'<script[^>]+src="(?:https?:)?//', html)
    assert not re.search(r'<link[^>]+href="(?:https?:)?//', html)
    assert "/static/leaflet/leaflet.js" in html and "/static/leaflet/leaflet.css" in html
    assert "/static/case.js" in html
    # ไม่มี JS inline นอกจากบล็อกข้อมูล JSON (และสคริปต์นาฬิกา/แบนเนอร์ใน base.html)
    for m in re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", html, re.S):
        attrs, body = m.groups()
        if 'type="application/json"' in attrs:
            continue
        assert "L." not in body and "case-data" not in body


def test_case_page_hides_edit_controls_for_viewer(client, login_as, store):
    login_as(client, "v", "viewer")
    cid = add_case(store)
    pin_at(store, cid)
    html = client.get(f"/case/{cid}").get_data(as_text=True)
    assert "ยืนยันหมุด" not in html and "ใช้ตำแหน่งปัจจุบัน" not in html
    assert case_data(html)["can_edit"] is False


def test_case_page_shows_edit_controls_for_srrt(srrt, store):
    cid = add_case(store)
    html = srrt.get(f"/case/{cid}").get_data(as_text=True)
    assert "ยืนยันหมุด" in html and "ใช้ตำแหน่งปัจจุบัน" in html


def test_cancelled_case_banner_and_no_controls(srrt, store):
    cid = add_case(store)
    pin_at(store, cid)
    store.mark_cancelled(cid)
    html = srrt.get(f"/case/{cid}").get_data(as_text=True)
    assert "เคสนี้ถูกยกเลิก (แพทย์แก้รหัสวินิจฉัย)" in html
    assert "ยืนยันหมุด" not in html and "ใช้ตำแหน่งปัจจุบัน" not in html
    assert case_data(html)["can_edit"] is False


def test_outside_circle_cases_not_listed(srrt, store):
    a, near, far = add_case(store, 1), add_case(store, 2), add_case(store, 3)
    pin_at(store, a)
    pin_at(store, near, LAT + 0.0005, LNG, "estimated")  # ~56 ม.
    pin_at(store, far, LAT + 0.01, LNG)                  # ~1.1 กม.
    html = srrt.get(f"/case/{a}").get_data(as_text=True)
    ids = [n["case_id"] for n in case_data(html)["neighbors"]]
    assert ids == [near]
    assert f'href="/case/{near}"' in html and f'href="/case/{far}"' not in html
    assert "000002" in html and "000003" not in html
    assert "ยังไม่มีผู้ป่วยรายอื่นในรัศมีนี้" not in html


def test_case_page_neighbors_exclude_cancelled(srrt, store):
    a, live, dead = add_case(store, 1), add_case(store, 2), add_case(store, 3)
    for cid in (a, live, dead):
        pin_at(store, cid)
    store.mark_cancelled(dead)
    html = srrt.get(f"/case/{a}").get_data(as_text=True)
    assert [n["case_id"] for n in case_data(html)["neighbors"]] == [live]
    assert f'href="/case/{dead}"' not in html


def test_case_page_neighbors_empty_state_and_no_pin(srrt, store):
    a, b = add_case(store, 1), add_case(store, 2)
    pin_at(store, a)
    html = srrt.get(f"/case/{a}").get_data(as_text=True)
    assert "ยังไม่มีผู้ป่วยรายอื่นในรัศมีนี้" in html
    # เคสไม่มีหมุด ไม่มี neighbors และไม่ดึงข้อมูลคนอื่น
    pin_at(store, b)
    c = add_case(store, 3)
    d = case_data(srrt.get(f"/case/{c}").get_data(as_text=True))
    assert d["neighbors"] == []


def test_neighbors_do_not_fetch_contact(srrt, store, source):
    a, b = add_case(store, 1), add_case(store, 2)
    pin_at(store, a)
    pin_at(store, b)
    calls = []
    original = source.contact
    source.contact = lambda hn: (calls.append(hn), original(hn))[1]
    srrt.get(f"/case/{a}")
    assert calls == ["000001"]


def test_list_view_link_uses_case_detail(srrt, store, app):
    cid = add_case(store)
    html = srrt.get("/").get_data(as_text=True)
    assert f'href="/case/{cid}"' in html
    with app.test_request_context():
        from flask import url_for
        assert url_for("case_detail", case_id=cid) == f"/case/{cid}"


# ---------------------------------------------------------------- API: สิทธิ์

def test_pin_requires_srrt_role(client, login_as, store):
    cid = add_case(store)
    login_as(client, "v", "viewer")
    r = post_pin(client, cid)
    assert r.status_code == 403 and r.is_json
    assert store.get_pin(cid) is None and audit_rows(store) == []


def test_pin_requires_login_json_401(client, store):
    cid = add_case(store)
    r = post_pin(client, cid)
    assert r.status_code == 401 and r.is_json


def test_pin_requires_json_and_csrf_header(srrt, store):
    cid = add_case(store)
    r = srrt.post(f"/api/case/{cid}/pin", data="lat=14&lng=101&state=confirmed",
                  headers=HDR)
    assert r.status_code == 415 and r.is_json
    r = srrt.post(f"/api/case/{cid}/pin",
                  json={"lat": LAT, "lng": LNG, "state": "confirmed"})
    assert r.status_code == 403 and r.is_json
    r = srrt.post(f"/api/case/{cid}/pin", headers={"X-Requested-With": "x"},
                  json={"lat": LAT, "lng": LNG, "state": "confirmed"})
    assert r.status_code == 403
    assert store.get_pin(cid) is None and audit_rows(store) == []


def test_viewer_gets_403_even_without_csrf_header(client, login_as, store):
    cid = add_case(store)
    login_as(client, "v", "viewer")
    r = client.post(f"/api/case/{cid}/pin", json={"lat": LAT, "lng": LNG,
                                                  "state": "confirmed"})
    assert r.status_code == 403 and r.get_json()["error"] != "csrf"


def test_pin_unknown_case_404_json(srrt):
    r = post_pin(srrt, 999)
    assert r.status_code == 404 and r.is_json


# ---------------------------------------------------------------- API: บันทึก

def test_pin_confirm_saves_and_writes_audit(srrt, store):
    cid = add_case(store)
    r = post_pin(srrt, cid, {"lat": LAT, "lng": LNG, "state": "confirmed"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["pin_state"] == "confirmed"
    assert body["lat"] == LAT and body["lng"] == LNG
    assert body["clusters_alerted"] == []
    pin = store.get_pin(cid)
    assert (pin["lat"], pin["lng"], pin["state"], pin["source"], pin["set_by"]) == \
        (LAT, LNG, "confirmed", "manual", "nurse")
    assert store.get_case(cid)["pin_state"] == "confirmed"
    [row] = audit_rows(store)
    assert row["action"] == "set_pin" and row["user"] == "nurse" and row["case_id"] == cid
    assert store.default_pin("000001", "40010103", "1")[:2] == (LAT, LNG)


def test_pin_accepts_estimated_and_gps_source_and_ints(srrt, store):
    cid = add_case(store)
    r = post_pin(srrt, cid, {"lat": 14, "lng": 102, "state": "estimated", "source": "gps"})
    assert r.status_code == 200
    pin = store.get_pin(cid)
    assert (pin["state"], pin["source"], pin["lat"], pin["lng"]) == ("estimated", "gps", 14.0, 102.0)


def test_pin_estimated_does_not_run_cluster_evaluation(srrt, store, sender):
    a, b = add_case(store, 1), add_case(store, 2)
    pin_at(store, a)
    assert post_pin(srrt, b, {"lat": LAT, "lng": LNG, "state": "estimated"}).status_code == 200
    assert sender.calls == [] and store.clusters() == []


@pytest.mark.parametrize("body", [
    {"lat": 102, "lng": 14, "state": "confirmed"},          # สลับ lat/lng
    {"lat": 0, "lng": 0, "state": "confirmed"},
    {"lat": 0.0, "lng": 0.0, "state": "confirmed"},
    {"lat": 4.99, "lng": 101.9, "state": "confirmed"},
    {"lat": 21.01, "lng": 101.9, "state": "confirmed"},
    {"lat": 14.3, "lng": 96.99, "state": "confirmed"},
    {"lat": 14.3, "lng": 106.01, "state": "confirmed"},
    {"lat": 51.5, "lng": -0.12, "state": "confirmed"},      # ลอนดอน
])
def test_pin_rejects_out_of_range(srrt, store, body):
    cid = add_case(store)
    r = post_pin(srrt, cid, body)
    j = r.get_json()
    assert r.status_code == 400 and j["error"] and j["message"]
    assert store.get_pin(cid) is None and audit_rows(store) == []
    assert store.get_case(cid)["pin_state"] == "none"


def test_pin_rejects_swapped_lat_lng(srrt, store):
    cid = add_case(store)
    r = post_pin(srrt, cid, {"lat": 102, "lng": 14, "state": "confirmed"})
    assert r.status_code == 400
    assert store.get_pin(cid) is None and audit_rows(store) == []


def test_pin_rejects_zero_zero_and_outside_thailand(srrt, store):
    cid = add_case(store)
    for lat, lng in ((0, 0), (35.68, 139.69), (-33.9, 151.2)):
        r = post_pin(srrt, cid, {"lat": lat, "lng": lng, "state": "confirmed"})
        assert r.status_code == 400
    assert store.get_pin(cid) is None and audit_rows(store) == []


def test_pin_rejects_non_numeric(srrt, store):
    cid = add_case(store)
    r = post_pin(srrt, cid, {"lat": "abc", "lng": LNG, "state": "confirmed"})
    assert r.status_code == 400 and r.get_json()["error"]
    assert store.get_pin(cid) is None


@pytest.mark.parametrize("lat,lng", [
    ("14.35", 101.9), (14.35, "101.9"), (None, 101.9), (14.35, None),
    (True, 101.9), (14.35, True), (False, False), ([14.35], 101.9), ({"a": 1}, 101.9),
])
def test_pin_rejects_non_numeric_and_bool_and_nan(srrt, store, lat, lng):
    cid = add_case(store)
    r = post_pin(srrt, cid, {"lat": lat, "lng": lng, "state": "confirmed"})
    assert r.status_code == 400 and r.is_json
    assert store.get_pin(cid) is None and audit_rows(store) == []


@pytest.mark.parametrize("raw", [
    '{"lat": NaN, "lng": 101.9, "state": "confirmed"}',
    '{"lat": 14.3, "lng": Infinity, "state": "confirmed"}',
    '{"lat": -Infinity, "lng": 101.9, "state": "confirmed"}',
    '{"lat": 1e999, "lng": 101.9, "state": "confirmed"}',
])
def test_pin_rejects_nan_and_infinity(srrt, store, raw):
    cid = add_case(store)
    r = srrt.post(f"/api/case/{cid}/pin", data=raw, headers=HDR,
                  content_type="application/json")
    assert r.status_code == 400 and r.is_json
    assert store.get_pin(cid) is None and audit_rows(store) == []


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '"x"', "null", ""])
def test_pin_rejects_malformed_body(srrt, store, raw):
    cid = add_case(store)
    r = srrt.post(f"/api/case/{cid}/pin", data=raw, headers=HDR,
                  content_type="application/json")
    assert r.status_code == 400 and r.is_json
    assert store.get_pin(cid) is None and audit_rows(store) == []


@pytest.mark.parametrize("extra", [
    {"state": "none"}, {"state": "Confirmed"}, {"state": None}, {"state": 1},
    {"state": "confirmed", "source": "magic"}, {"state": "confirmed", "source": 5},
])
def test_pin_rejects_bad_state_or_source(srrt, store, extra):
    cid = add_case(store)
    body = {"lat": LAT, "lng": LNG, **extra}
    r = post_pin(srrt, cid, body)
    assert r.status_code == 400 and r.is_json
    assert store.get_pin(cid) is None and audit_rows(store) == []


def test_pin_missing_state_rejected(srrt, store):
    cid = add_case(store)
    r = post_pin(srrt, cid, {"lat": LAT, "lng": LNG})
    assert r.status_code == 400


def test_estimated_cannot_overwrite_confirmed_pin_409(srrt, store):
    cid = add_case(store)
    pin_at(store, cid, LAT, LNG, "confirmed")
    before = audit_rows(store)
    r = post_pin(srrt, cid, {"lat": LAT + 0.001, "lng": LNG, "state": "estimated"})
    assert r.status_code == 409 and r.get_json()["error"] == "already_confirmed"
    pin = store.get_pin(cid)
    assert (pin["lat"], pin["state"]) == (LAT, "confirmed")
    assert audit_rows(store) == before
    # ยืนยันซ้ำ (ย้ายหมุดที่ยืนยันแล้ว) ทำได้
    r = post_pin(srrt, cid, {"lat": LAT + 0.001, "lng": LNG, "state": "confirmed"})
    assert r.status_code == 200 and store.get_pin(cid)["lat"] == LAT + 0.001


def test_pin_on_cancelled_case_rejected(srrt, store):
    cid = add_case(store)
    store.mark_cancelled(cid)
    r = post_pin(srrt, cid)
    assert r.status_code == 409 and r.get_json()["error"] == "case_cancelled"
    assert store.get_pin(cid) is None and audit_rows(store) == []


# ---------------------------------------------------------------- API: cluster

def test_confirm_two_nearby_cases_sends_cluster_alert_once(srrt, store, sender):
    a, b = add_case(store, 1), add_case(store, 2)
    r = post_pin(srrt, a)
    assert r.get_json()["clusters_alerted"] == [] and sender.calls == []
    r = post_pin(srrt, b, {"lat": LAT + 0.0003, "lng": LNG, "state": "confirmed"})
    [cid] = r.get_json()["clusters_alerted"]
    assert len(sender.calls) == 1
    assert f"cluster={cid}" in json.dumps(sender.sent, ensure_ascii=False)
    # ยืนยันซ้ำที่เดิม ไม่แจ้งซ้ำ
    r = post_pin(srrt, b, {"lat": LAT + 0.0003, "lng": LNG, "state": "confirmed"})
    assert r.get_json()["clusters_alerted"] == [] and len(sender.calls) == 1


def test_rejected_pin_not_counted_in_cluster(srrt, store, sender):
    a, b = add_case(store, 1), add_case(store, 2)
    post_pin(srrt, a)
    r = post_pin(srrt, b, {"lat": 102, "lng": 14, "state": "confirmed"})
    assert r.status_code == 400
    r = post_pin(srrt, b, {"lat": LAT, "lng": LNG, "state": "estimated"})
    assert r.status_code == 200
    assert sender.calls == [] and store.clusters() == []
    assert store.get_case(b)["pin_state"] == "estimated"


def test_pin_ok_even_if_cluster_alert_send_fails(app, srrt, store):
    failing = FakeSender(error=RuntimeError("line down"))
    app.config["SENDER"] = failing
    a, b = add_case(store, 1), add_case(store, 2)
    post_pin(srrt, a)
    r = post_pin(srrt, b)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert store.get_pin(b)["state"] == "confirmed"


def test_pin_ok_if_evaluate_raises(app, srrt, store, monkeypatch, caplog):
    import case_views

    def boom(*a, **k):
        raise RuntimeError("secret-detail-HN000001")
    monkeypatch.setattr(case_views, "evaluate_clusters", boom)
    cid = add_case(store)
    with caplog.at_level("WARNING"):
        r = post_pin(srrt, cid)
    assert r.status_code == 200 and r.get_json()["clusters_alerted"] == []
    assert "RuntimeError" in caplog.text and "secret-detail" not in caplog.text


def test_pin_without_sender_skips_cluster_evaluation(settings, store, source, login_as):
    c = create_app(settings=settings, store=store, source=source, sender=None).test_client()
    login_as(c)
    a, b = add_case(store, 1), add_case(store, 2)
    post_pin(c, a)
    r = post_pin(c, b)
    assert r.status_code == 200 and r.get_json()["clusters_alerted"] == []
    assert store.clusters() == []


# ---------------------------------------------------------------- รอบแก้ 1

def test_case_page_keeps_same_origin_referrer_policy(srrt, store):
    cid = add_case(store)
    r = srrt.get(f"/case/{cid}")
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_tile_layer_requests_send_origin_only_referrer():
    """OSM ต้องการ Referer: ตั้งที่ L.tileLayer เท่านั้น (หัวทั้งแอปคงเป็น same-origin)."""
    js = (Path(__file__).resolve().parent.parent / "static" / "case.js").read_text(
        encoding="utf-8")
    m = re.search(r"L\.tileLayer\(data\.tile_url,\s*\{(.*?)\}\)", js, re.S)
    assert m and re.search(r'referrerPolicy:\s*"origin"', m.group(1))
    assert "data.attribution" in m.group(1)


def test_case_page_urls_carry_ids_not_hn(srrt, store):
    a, b = add_case(store, 1), add_case(store, 2)
    pin_at(store, a)
    pin_at(store, b)
    html = srrt.get(f"/case/{a}").get_data(as_text=True)
    urls = re.findall(r'(?:href|src|action)="([^"]*)"', html)
    assert any(u == f"/case/{b}" for u in urls)
    assert not [u for u in urls if "000001" in u or "000002" in u]
    d = case_data(html)
    assert not [u for u in [d["pin_url"]] + [n["url"] for n in d["neighbors"]]
                if "0000" in u]
    assert all(n["url"] == f"/case/{n['case_id']}" for n in d["neighbors"])


def _offset_lat(lat, lng, metres):
    """lat ที่ห่างจาก (lat, lng) ไปทางเหนือ `metres` ม. พอดี (หาโดย bisection ด้วย haversine_m)."""
    lo, hi = lat, lat + 0.01
    for _ in range(80):
        mid = (lo + hi) / 2
        if haversine_m(lat, lng, mid, lng) < metres:
            lo = mid
        else:
            hi = mid
    return hi


def test_neighbors_distance_and_order_nearest_first(srrt, store):
    a, far, near = add_case(store, 1), add_case(store, 2), add_case(store, 3)
    pin_at(store, a)
    pin_at(store, far, _offset_lat(LAT, LNG, 80), LNG, "estimated")
    pin_at(store, near, _offset_lat(LAT, LNG, 30), LNG)
    html = srrt.get(f"/case/{a}").get_data(as_text=True)
    ns = case_data(html)["neighbors"]
    assert [(n["case_id"], n["distance_m"]) for n in ns] == [(near, 30), (far, 80)]
    assert html.index("30 ม.") < html.index("80 ม.")
    assert html.index(f'href="/case/{near}"') < html.index(f'href="/case/{far}"')


def test_neighbor_radius_boundary_is_inclusive(app, srrt, store):
    a, edge, beyond = add_case(store, 1), add_case(store, 2), add_case(store, 3)
    pin_at(store, a)
    lat_edge = _offset_lat(LAT, LNG, 100)
    d_edge = haversine_m(LAT, LNG, lat_edge, LNG)
    pin_at(store, edge, lat_edge, LNG)
    pin_at(store, beyond, lat_edge + 1e-5, LNG)  # ไกลกว่าขอบ ~1 ม.
    # ตั้งรัศมี = ระยะจริงของ edge พอดี: edge อยู่บนขอบ (<=) ต้องแสดง, beyond เกินขอบ ไม่แสดง
    app.config["SETTINGS"] = dataclasses.replace(app.config["SETTINGS"],
                                                 cluster_radius_m=d_edge)
    ids = [n["case_id"] for n in case_data(
        srrt.get(f"/case/{a}").get_data(as_text=True))["neighbors"]]
    assert ids == [edge]
    app.config["SETTINGS"] = dataclasses.replace(app.config["SETTINGS"],
                                                 cluster_radius_m=d_edge - 1e-6)
    ids = [n["case_id"] for n in case_data(
        srrt.get(f"/case/{a}").get_data(as_text=True))["neighbors"]]
    assert ids == []


# ---------------------------------------------------------------- I1: pin API ไม่บล็อกบนล็อกแจ้งเตือน

def test_pin_reports_cluster_check_done(srrt, store, sender):
    a, b = add_case(store, 1), add_case(store, 2)
    r = post_pin(srrt, a)
    assert r.get_json()["cluster_check"] == "done"
    r = post_pin(srrt, b, {"lat": LAT + 0.0003, "lng": LNG, "state": "confirmed"})
    body = r.get_json()
    assert body["cluster_check"] == "done" and len(body["clusters_alerted"]) == 1


def test_pin_does_not_block_when_alert_lock_is_held(srrt, store, sender):
    import time
    import alerts
    a, b = add_case(store, 1), add_case(store, 2)
    post_pin(srrt, a)
    assert alerts._LOCK.acquire(timeout=1)       # เช่น scheduler กำลังส่ง LINE ช้า
    try:
        t0 = time.monotonic()
        r = post_pin(srrt, b, {"lat": LAT + 0.0003, "lng": LNG, "state": "confirmed"})
        assert time.monotonic() - t0 < 1.0
    finally:
        alerts._LOCK.release()
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["cluster_check"] == "deferred" and body["clusters_alerted"] == []
    assert store.get_pin(b)["state"] == "confirmed" and sender.calls == []
    # รอบถัดไปของ scheduler (หรือการปักหมุดครั้งหน้า) ประเมิน cluster ได้ตามปกติ
    import alerts as _a
    from alerts import evaluate_clusters
    assert len(evaluate_clusters(store, sender, srrt.application.config["SETTINGS"])) == 1


def test_pin_cluster_check_deferred_when_evaluate_raises(app, srrt, store, monkeypatch):
    import case_views

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(case_views, "evaluate_clusters", boom)
    r = post_pin(srrt, add_case(store))
    assert r.status_code == 200 and r.get_json()["cluster_check"] == "deferred"
