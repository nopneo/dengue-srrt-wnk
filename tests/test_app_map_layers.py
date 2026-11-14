"""/api/boundary.json และ /api/past.json — เลเยอร์เสริมของแผนที่รวม (เวลาจำลอง 10 พ.ย. 2026 = ปี พ.ศ. 2569 ยังไม่จบ ปีก่อนหน้า = 2568..2566)"""
import json
from datetime import date, datetime, timedelta

import pytest

import map_views
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource, HosxpError
from trend import VisitRow

NOW = datetime(2026, 11, 10, 9, 0)
CENTROIDS = {"30250103": (14.31, 101.81)}
NAMES = {"30250103": "บ้านทดสอบ"}


def v(hn, day, amp="25", tmb="01", moo="3"):
    return VisitRow(hn, day, "30", amp, tmb, moo)


def visits_2568():
    d = date(2025, 1, 5)       # ปี พ.ศ. 2568 (ม.ค.–ธ.ค. 2025)
    return ([v(f"a{i}", d) for i in range(3)]                         # 30250103 มีจุดกลาง
            + [v(f"b{i}", d, moo="5") for i in range(2)]              # 30250105 ไม่มีจุดกลาง
            + [v("c0", d, moo=None)]                                  # ไม่ทราบหมู่
            + [v("d0", d, amp="26")])                                 # นอกอำเภอ


def make_app(tmp_path, store, visits=(), area="3025", source="default", **kw):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                              "DENGUE_DB_PATH": str(tmp_path / "d.db"), "DENGUE_AREA_AMPHOE": area})
    if source == "default":
        source = FakeSource(visit_rows=visits, centroids=CENTROIDS, names=NAMES, **kw)
    app = create_app(settings=settings, store=store, source=source, sender=FakeSender())
    app.config["CLOCK"] = lambda: NOW
    return app, source


@pytest.fixture
def client_for(login_as):
    def make(app, role="viewer"):
        c = app.test_client()
        login_as(c, "u", role)
        return c
    return make


# ---------- /api/boundary.json ----------

def test_boundary_requires_login(tmp_path, store):
    app, _ = make_app(tmp_path, store)
    assert app.test_client().get("/api/boundary.json").status_code in (302, 401)


def test_boundary_returns_committed_3025_feature(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store)
    body = client_for(app).get("/api/boundary.json").get_json()
    assert body["missing"] == [] and len(body["features"]) == 1
    assert body["features"][0]["properties"]["code"] == "3025"


def test_boundary_missing_code_listed_and_not_error(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, area="3025,9999")
    r = client_for(app).get("/api/boundary.json")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["features"]) == 1 and body["missing"] == ["9999"]


def test_boundary_ignores_query_params(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    base = c.get("/api/boundary.json").get_json()
    assert c.get("/api/boundary.json?code=../x&area=9999&file=../../etc/passwd").get_json() == base


def test_boundary_corrupt_file_skipped(tmp_path, store, client_for, monkeypatch):
    bad = tmp_path / "boundaries"
    bad.mkdir()
    (bad / "3025.geojson").write_text('{"type": "FeatureCollection", "features": [', encoding="utf-8")
    monkeypatch.setattr(map_views, "BOUNDARY_DIR", bad)
    app, _ = make_app(tmp_path, store)
    r = client_for(app).get("/api/boundary.json")
    assert r.status_code == 200 and r.get_json() == {"features": [], "missing": ["3025"]}


def test_boundary_without_trend_and_without_area_returns_empty(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, area="", source=None)
    assert client_for(app).get("/api/boundary.json").get_json() == {"features": [], "missing": []}


def test_boundary_uses_default_area_from_source_and_survives_its_failure(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, area="", source=FakeSource(area=("3025",)))
    assert len(client_for(app).get("/api/boundary.json").get_json()["features"]) == 1
    down = FakeSource(area=("3025",), failure=HosxpError("down"), fail_methods=["default_area_amphoe"])
    app2, _ = make_app(tmp_path, store, area="", source=down)
    r = client_for(app2).get("/api/boundary.json")
    assert r.status_code == 200 and r.get_json() == {"features": [], "missing": []}


# ---------- /api/past.json ----------

def past(client, year="2568"):
    return client.get(f"/api/past.json?year={year}")


def test_past_requires_login(tmp_path, store):
    app, _ = make_app(tmp_path, store)
    assert app.test_client().get("/api/past.json?year=2568").status_code in (302, 401)


@pytest.mark.parametrize("year", ["2569", "2565", "abc", "", "2568.5", "-2568", "9999"])
def test_past_rejects_fy_outside_whitelist(tmp_path, store, client_for, year):
    app, _ = make_app(tmp_path, store, visits_2568())
    r = past(client_for(app), year)
    assert r.status_code == 400 and r.get_json() == {"error": "ปี พ.ศ.ไม่ถูกต้อง"}


def test_past_missing_fy_param_is_400(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store)
    assert client_for(app).get("/api/past.json").status_code == 400


def test_past_counts_per_village_with_centroids_and_names(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, visits_2568())
    r = past(client_for(app))
    assert r.status_code == 200
    body = r.get_json()
    assert body["villages"] == [{"code": "30250103", "name": "บ้านทดสอบ", "lat": 14.31, "lng": 101.81,
                                 "cases": 3}]
    assert body["unmapped"] == {"villages": 1, "cases": 3}                  # 30250105 (2) + ไม่ทราบหมู่ (1)
    assert body["total"] == 6 and body["error"] is None                    # นอกอำเภอไม่นับ


def test_past_unmapped_when_no_centroid(tmp_path, store, client_for):
    app, src = make_app(tmp_path, store, visits_2568())
    src.centroids = {}
    body = past(client_for(app)).get_json()
    assert body["villages"] == [] and body["unmapped"]["cases"] == body["total"] == 6
    assert body["unmapped"]["villages"] == 2


def test_past_source_down_returns_200_with_error(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, [], failure=HosxpError("down 10.1.2.3"), fail_methods=["episode_rows"])
    r = past(client_for(app))
    body = r.get_json()
    assert r.status_code == 200 and body["error"] == "ดึงข้อมูลไม่สำเร็จ" and body["total"] == 0
    assert "10.1.2.3" not in r.get_data(as_text=True)


def test_past_centroid_failure_is_reported_not_shown_as_missing_gis_data(tmp_path, store, client_for):
    app, src = make_app(tmp_path, store, visits_2568())
    src.failure, src.fail_methods = HosxpError("down"), {"village_centroids"}
    r = past(client_for(app))
    body = r.get_json()
    assert r.status_code == 200 and body["villages"] == [] and body["total"] == 6
    assert body["centroid_error"] is True and body["error"] == "ดึงพิกัดหมู่บ้านไม่สำเร็จ"
    src.failure = None                                           # พักสั้น ๆ แล้วหายเอง
    app.config["TREND"].monotonic = lambda: 10**9
    ok = past(client_for(app)).get_json()
    assert ok["centroid_error"] is False and ok["error"] is None and len(ok["villages"]) == 1


def test_past_stale_cache_flag(tmp_path, store, client_for):
    app, src = make_app(tmp_path, store, visits_2568())
    c = client_for(app)
    assert past(c).get_json()["stale"] is False
    src.failure, src.fail_methods = HosxpError("down"), {"episode_rows"}
    # ปีที่จบแล้วนิ่งแล้วไม่คำนวณใหม่เอง — จำลองค่าตั้งเปลี่ยน (ลายเซ็นแคชไม่ตรง) แล้วฐานล่ม ต้องใช้แคชเดิมพร้อมธง stale
    meta, rows = store.trend_get(2568)
    store.trend_put(2568, "old-signature", rows, meta["computed_at"])
    body = past(c).get_json()
    assert body["stale"] is True and body["error"] == "ดึงข้อมูลไม่สำเร็จ" and body["total"] == 6


def test_past_without_source_gives_message_not_error_status(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, source=None)
    r = past(client_for(app))
    body = r.get_json()
    assert r.status_code == 200 and body["error"] == "ยังไม่ได้เชื่อมต่อฐานข้อมูล HOSxP"
    assert body["total"] == 0 and body["villages"] == []


def test_past_response_has_no_identifiers(tmp_path, store, client_for):
    secret = "HN-SECRET-123"
    rows = visits_2568() + [VisitRow(secret, date(2025, 2, 2), "30", "25", "01", "3")]
    app, _ = make_app(tmp_path, store, rows)
    text = past(client_for(app)).get_data(as_text=True)
    assert secret not in text
    assert set(json.loads(text)) == {"year", "total", "villages", "unmapped", "computed_at", "stale", "error",
                                     "centroid_error"}


# ---------- /api/tambons.json ----------

def tambon_file(base, code="3025", props=None, geometry=None):
    props = props or [{"code": f"{code}01", "name": "วังน้ำเขียว"}, {"code": f"{code}02", "name": "วังหมี"}]
    ring = [[101.5, 14.2], [102.0, 14.2], [102.0, 14.6], [101.5, 14.6], [101.5, 14.2]]
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": p, "geometry": geometry or {"type": "Polygon", "coordinates": [ring]}}
        for p in props]}
    base.mkdir(exist_ok=True)
    (base / f"{code}.tambon.geojson").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def tambon_dir(tmp_path, monkeypatch):
    d = tmp_path / "bounds"
    d.mkdir()
    monkeypatch.setattr(map_views, "BOUNDARY_DIR", d)
    return d


def test_tambons_requires_login(tmp_path, store):
    app, _ = make_app(tmp_path, store)
    assert app.test_client().get("/api/tambons.json").status_code in (302, 401)


def test_tambons_returns_valid_file_features(tmp_path, store, client_for, tambon_dir):
    tambon_file(tambon_dir)
    app, _ = make_app(tmp_path, store)
    body = client_for(app).get("/api/tambons.json").get_json()
    assert body["missing"] == [] and [f["properties"]["code"] for f in body["features"]] == ["302501", "302502"]


def test_tambons_missing_code_listed_not_error(tmp_path, store, client_for, tambon_dir):
    tambon_file(tambon_dir)
    app, _ = make_app(tmp_path, store, area="3025,9999")
    r = client_for(app).get("/api/tambons.json")
    assert r.status_code == 200 and r.get_json()["missing"] == ["9999"] and len(r.get_json()["features"]) == 2


def test_tambons_ignores_query_params(tmp_path, store, client_for, tambon_dir):
    tambon_file(tambon_dir)
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    base = c.get("/api/tambons.json").get_json()
    assert c.get("/api/tambons.json?code=../x&area=9999&file=../../etc/passwd").get_json() == base


@pytest.mark.parametrize("props", [
    [{"code": "302501", "name": "ก"}, {"code": "302501", "name": "ซ้ำ"}],     # รหัสซ้ำ
    [{"code": "302501", "name": ""}],                                          # ชื่อว่าง
    [{"code": "262501", "name": "อำเภออื่น"}],                                 # ไม่ขึ้นต้นรหัสอำเภอ
], ids=["duplicate", "blank-name", "foreign-code"])
def test_tambons_invalid_file_skipped(tmp_path, store, client_for, tambon_dir, props):
    tambon_file(tambon_dir, props=props)
    app, _ = make_app(tmp_path, store)
    r = client_for(app).get("/api/tambons.json")
    assert r.status_code == 200 and r.get_json() == {"features": [], "missing": ["3025"]}


def test_tambons_corrupt_json_skipped(tmp_path, store, client_for, tambon_dir):
    (tambon_dir / "3025.tambon.geojson").write_text("{not json", encoding="utf-8")
    app, _ = make_app(tmp_path, store)
    assert client_for(app).get("/api/tambons.json").get_json() == {"features": [], "missing": ["3025"]}


def test_tambons_without_trend_and_without_area_returns_empty(tmp_path, store, client_for):
    app, _ = make_app(tmp_path, store, area="", source=None)
    assert client_for(app).get("/api/tambons.json").get_json() == {"features": [], "missing": []}


def test_tambons_survives_default_area_failure_and_uses_default_area(tmp_path, store, client_for, tambon_dir):
    tambon_file(tambon_dir)
    app, _ = make_app(tmp_path, store, area="", source=FakeSource(area=("3025",)))
    assert len(client_for(app).get("/api/tambons.json").get_json()["features"]) == 2
    down = FakeSource(area=("3025",), failure=HosxpError("down"), fail_methods=["default_area_amphoe"])
    app2, _ = make_app(tmp_path, store, area="", source=down)
    r = client_for(app2).get("/api/tambons.json")
    assert r.status_code == 200 and r.get_json() == {"features": [], "missing": []}


def test_tambons_cached_and_cache_expires(tmp_path, store, client_for, tambon_dir):
    tambon_file(tambon_dir)
    app, _ = make_app(tmp_path, store)
    now = [1000.0]
    app.config["MONOTONIC"] = lambda: now[0]
    c = client_for(app)
    assert len(c.get("/api/tambons.json").get_json()["features"]) == 2
    (tambon_dir / "3025.tambon.geojson").unlink()
    assert len(c.get("/api/tambons.json").get_json()["features"]) == 2         # ยังอยู่ในแคช
    now[0] += 601
    assert c.get("/api/tambons.json").get_json() == {"features": [], "missing": ["3025"]}


def test_tambon_and_boundary_caches_do_not_collide(tmp_path, store, client_for, tambon_dir):
    tambon_file(tambon_dir)
    app, _ = make_app(tmp_path, store)
    c = client_for(app)
    t = c.get("/api/tambons.json").get_json()
    b = c.get("/api/boundary.json").get_json()
    assert len(t["features"]) == 2 and b["features"] == [] and b["missing"] == ["3025"]   # ไม่มี 3025.geojson ใน tmp
