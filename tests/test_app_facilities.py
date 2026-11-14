"""/api/facilities.json และ PUT /api/facilities/<hospcode> — สิทธิ์ CSRF พิกัด และกรณีฐานล่ม"""
import json
from datetime import datetime

import pytest

from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource, HosxpError
from trend import VisitRow

NOW = datetime(2026, 11, 10, 9, 0)
RPST = "โรงพยาบาลส่งเสริมสุขภาพตำบล"
CATALOG = [
    {"hospcode": "02846", "name": RPST + "ระเริง", "kind": "rpst", "tmb": "302503"},
    {"hospcode": "02844", "name": RPST + "ยุบอีปูน", "kind": "rpst", "tmb": "302502"},
    {"hospcode": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital", "tmb": "302505"},
]
HDR = {"X-Requested-With": "dengue"}


def make_app(tmp_path, store, **kw):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
                              "DENGUE_AREA_AMPHOE": "3025"})
    src = kw.pop("source", None) or FakeSource(facilities=CATALOG, names={"302503": "ระเริง"}, **kw)
    app = create_app(settings=settings, store=store, source=src, sender=FakeSender())
    app.config["CLOCK"] = lambda: NOW
    return app, src


@pytest.fixture
def client_for(login_as):
    def make(app, role="admin"):
        c = app.test_client()
        login_as(c, f"{role}1", role)
        return c
    return make


def seed(store, code="02846", lat=14.43, lng=101.82, src="csv"):
    item = next(c for c in CATALOG if c["hospcode"] == code)
    store.facility_upsert(code, item["name"], item["kind"], item["tmb"], lat, lng, src, "import", NOW)


def put(client, code, body, headers=HDR, **kw):
    return client.put(f"/api/facilities/{code}", json=body, headers=headers, **kw)


# ---------- GET ----------

def test_get_requires_login(tmp_path, store):
    app, _ = make_app(tmp_path, store)
    assert app.test_client().get("/api/facilities.json").status_code in (302, 401)


@pytest.mark.parametrize("role,editable", [("admin", True), ("srrt", False), ("viewer", False)])
def test_get_returns_rows_unplaced_and_editable_by_role(tmp_path, store, client_for, role, editable):
    seed(store)
    app, _ = make_app(tmp_path, store)
    body = client_for(app, role).get("/api/facilities.json").get_json()
    assert body["editable"] is editable
    assert [f["code"] for f in body["facilities"]] == ["02846"]
    assert body["facilities"][0]["tmb_name"] == "ระเริง"
    assert [u["code"] for u in body["unplaced"]] == ["02844", "10894"]
    assert set(body["facilities"][0]) == {"code", "name", "kind", "tmb", "tmb_name", "lat", "lng", "source"}
    assert set(body["unplaced"][0]) == {"code", "name", "kind"}


def test_get_survives_hosxp_down_using_sqlite_rows(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store, failure=HosxpError("down 10.1.2.3"), fail_methods=["facilities"])
    r = client_for(app).get("/api/facilities.json")
    assert r.status_code == 200 and [f["code"] for f in r.get_json()["facilities"]] == ["02846"]
    assert r.get_json()["unplaced"] == [] and "10.1.2.3" not in r.get_data(as_text=True)


def test_get_has_no_patient_identifiers(tmp_path, store, client_for):
    secret = "HN-SECRET-123"
    seed(store)
    app, _ = make_app(tmp_path, store, visit_rows=[VisitRow(secret, NOW.date(), "30", "25", "03", "1")])
    text = client_for(app).get("/api/facilities.json").get_data(as_text=True)
    assert secret not in text and set(json.loads(text)) == {"facilities", "unplaced", "editable"}


# ---------- PUT ----------

def test_put_admin_ok_updates_and_audits(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    r = put(c, "02846", {"lat": 14.5, "lng": 101.9})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    got = store.facility_get("02846")
    assert (got["lat"], got["lng"], got["source"], got["updated_by"]) == (14.5, 101.9, "manual", "admin1")
    r2 = put(c, "02844", {"lat": 14.4, "lng": 101.7})                 # ยังไม่มีแถว แต่อยู่ใน catalog -> สร้างได้
    assert r2.status_code == 200 and store.facility_get("02844")["source"] == "manual"
    assert len([a for a in store.audit() if a["action"] == "facility_place"]) == 2


@pytest.mark.parametrize("role", ["srrt", "viewer"])
def test_put_forbidden_for_lower_roles_and_unauthenticated(tmp_path, store, client_for, role):
    seed(store)
    app, _ = make_app(tmp_path, store)
    assert put(client_for(app, role), "02846", {"lat": 14.5, "lng": 101.9}).status_code == 403
    assert put(app.test_client(), "02846", {"lat": 14.5, "lng": 101.9}).status_code in (401, 302)
    assert store.facility_get("02846")["lat"] == 14.43


def test_put_requires_json_and_csrf_header(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    assert put(c, "02846", {"lat": 14.5, "lng": 101.9}, headers={}).status_code == 403
    assert put(c, "02846", {"lat": 14.5, "lng": 101.9}, headers={"X-Requested-With": "x"}).status_code == 403
    r = c.put("/api/facilities/02846", data="lat=14.5&lng=101.9", headers=HDR,
              content_type="application/x-www-form-urlencoded")
    assert r.status_code == 415
    assert store.facility_get("02846")["lat"] == 14.43


@pytest.mark.parametrize("code", ["00000", "abcde", "1234", "123456", "٠٢٨٤٠", "0284", "02846x"])
def test_put_unknown_and_malformed_code_404(tmp_path, store, client_for, code):
    app, _ = make_app(tmp_path, store)
    r = put(client_for(app), code, {"lat": 14.5, "lng": 101.9})
    assert r.status_code == 404 and r.get_json()["error"] == "not_found"
    assert store.facility_all() == []


@pytest.mark.parametrize("body", [
    {"lat": "14.4", "lng": 101.8}, {"lat": None, "lng": 101.8}, {"lat": True, "lng": 101.8}, {"lat": 14.4, "lng": False},
    {"lat": 0, "lng": 0}, {"lat": 4, "lng": 101}, {"lat": 14.4, "lng": 120}, {"lat": 101.8, "lng": 14.4},
    {"lat": 14.4}, {"lng": 101.8}, {}, [], "text", 5,
])
def test_put_bad_coords_400_each_case(tmp_path, store, client_for, body):
    seed(store)
    app, _ = make_app(tmp_path, store)
    r = put(client_for(app), "02846", body)
    assert r.status_code == 400 and r.get_json()["error"] in ("bad_coords", "bad_request")
    assert store.facility_get("02846")["lat"] == 14.43


def test_put_nan_and_infinity_rejected(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    for raw in ('{"lat": NaN, "lng": 101.8}', '{"lat": 14.4, "lng": Infinity}', '{"lat": -Infinity, "lng": 101.8}'):
        r = c.put("/api/facilities/02846", data=raw, headers=HDR, content_type="application/json")
        assert r.status_code == 400
    assert store.facility_get("02846")["lat"] == 14.43


@pytest.mark.parametrize("raw", ["{not json", "null", "", "\ud800"])
def test_put_invalid_json_is_400(tmp_path, store, client_for, raw):
    app, _ = make_app(tmp_path, store)
    r = client_for(app).put("/api/facilities/02846", data=raw.encode("utf-8", "surrogatepass"), headers=HDR,
                            content_type="application/json")
    assert r.status_code == 400


def test_put_hosxp_down_without_row_503_with_fixed_message(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, failure=HosxpError("down 10.1.2.3"), fail_methods=["facilities"])
    r = put(client_for(app), "02844", {"lat": 14.4, "lng": 101.7})
    assert r.status_code == 503 and r.get_json() == {"error": "unavailable",
                                                      "message": "ตรวจรายชื่อสถานพยาบาลไม่ได้ ลองใหม่ภายหลัง"}
    assert "10.1.2.3" not in r.get_data(as_text=True) and store.facility_all() == []


def test_put_hosxp_down_with_existing_row_still_ok(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store, failure=HosxpError("down"), fail_methods=["facilities"])
    assert put(client_for(app), "02846", {"lat": 14.5, "lng": 101.9}).status_code == 200


def test_put_oversize_body_rejected(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store)
    big = '{"lat": 14.5, "lng": 101.9, "pad": "' + "x" * 70000 + '"}'
    r = client_for(app).put("/api/facilities/02846", data=big, headers=HDR, content_type="application/json")
    assert r.status_code == 413 and store.facility_get("02846")["lat"] == 14.43


def test_put_never_creates_rows_outside_catalog(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    for code in ("99999", "00246", "32218"):
        assert put(c, code, {"lat": 14.5, "lng": 101.9}).status_code == 404
    assert store.facility_all() == []


def test_put_extra_keys_are_ignored_and_not_stored(tmp_path, store, client_for):
    seed(store)
    app, _ = make_app(tmp_path, store)
    r = put(client_for(app), "02846", {"lat": 14.5, "lng": 101.9, "name": "แฮ็ก", "kind": "hospital", "source": "csv"})
    assert r.status_code == 200
    got = store.facility_get("02846")
    assert got["name"] == RPST + "ระเริง" and got["kind"] == "rpst" and got["source"] == "manual"
