"""แผนที่ ปีก่อนหน้า และเปรียบเทียบปี พ.ศ.แยกตามโรค (สเปกข้อ 4.6)"""
import json
import re
from datetime import date, datetime, timedelta

import pytest

from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource
from map_views import past_year_choices
from trend import VisitRow
from trend_service import TrendService

NOW = datetime(2026, 11, 10, 9, 0)
HFMD = ("B084",)
DENGUE = ("A90", "A91")


def make_settings(tmp_path, diseases="dengue,hfmd"):
    return load_settings({"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
                          "DENGUE_AREA_AMPHOE": "3025", "DENGUE_DISEASES": diseases})


@pytest.fixture
def app(tmp_path, store):
    src = FakeSource(visit_rows=[VisitRow(f"V{i}", date(2025, 11, 5), "30", "25", "01", "3") for i in range(3)])
    a = create_app(settings=make_settings(tmp_path), store=store, source=src, sender=FakeSender())
    a.config["CLOCK"] = lambda: NOW
    return a


@pytest.fixture
def client(app, login_as):
    c = app.test_client()
    login_as(c, "u1", "viewer")
    return c


def pinned(store, key, disease, icd, state="confirmed", lat=14.35):
    cid = store.add_case(key, "OPD", "H" + key, icd, datetime.now().replace(microsecond=0), "40010103",
                         "1", "1", 30, True, disease=disease)
    store.set_pin(cid, lat, 101.9, state, "manual", "t")
    return cid


def map_data(html):
    m = re.search(r'<script type="application/json" id="map-data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


# ---------------------------------------------------------------- /map

def test_map_json_filters_by_disease_and_uses_rule_radius_days(client, store):
    d = pinned(store, "1", "dengue", "A90")
    h = pinned(store, "2", "hfmd", "B084", state="estimated")
    body = client.get("/api/map.json").get_json()
    assert [c["id"] for c in body["cases"]] == [d] and body["radius_m"] == 100 and body["days"] == 14
    body = client.get("/api/map.json?d=hfmd").get_json()
    assert [c["id"] for c in body["cases"]] == [h]
    assert body["radius_m"] == 0 and body["days"] == 7          # โหมดหมู่บ้าน: ไม่มีวงรัศมี · ช่วงวันตามโรค


def test_map_page_carries_disease_in_links_and_api_url(client, store):
    html = client.get("/map?d=hfmd").get_data(as_text=True)
    data = map_data(html)
    assert "d=hfmd" in data["api_url"] and data["radius_m"] == 0 and data["days"] == 7
    assert "d=hfmd" in re.findall(r'href="(/map\?[^"]*days=30[^"]*)"', html)[0]


def test_cluster_param_of_another_disease_is_ignored(client, store):
    a = pinned(store, "1", "dengue", "A90")
    b = pinned(store, "2", "dengue", "A90", lat=14.3501)
    cl = store.save_cluster([a, b], datetime.now(), datetime.now(), disease="dengue")
    assert map_data(client.get(f"/map?cluster={cl}").get_data(as_text=True))["selected_cluster"] == cl
    assert map_data(client.get(f"/map?d=hfmd&cluster={cl}").get_data(as_text=True))["selected_cluster"] is None


def test_single_disease_map_urls_unchanged(tmp_path, store, login_as):
    a = create_app(settings=make_settings(tmp_path, "dengue"), store=store, source=FakeSource(), sender=FakeSender())
    c = a.test_client()
    login_as(c, "u1", "viewer")
    data = map_data(c.get("/map").get_data(as_text=True))
    assert "d=" not in data["api_url"] and data["radius_m"] == 100


# ---------------------------------------------------------------- /api/past.json

def test_past_json_queries_the_selected_disease_and_caches_per_disease(app, client):
    src = app.config["SOURCE"]
    year = past_year_choices(NOW.date())[0]
    assert client.get(f"/api/past.json?year={year}").status_code == 200
    assert client.get(f"/api/past.json?year={year}&d=hfmd").status_code == 200
    assert [c[0] for c in src.episode_calls] == [DENGUE, HFMD]
    client.get(f"/api/past.json?year={year}")
    client.get(f"/api/past.json?year={year}&d=hfmd")
    assert len(src.episode_calls) == 2                            # ครั้งที่สองใช้แคชต่อโรค


# ---------------------------------------------------------------- /trend และ TrendService

def test_trend_service_disease_isolated_cache_locks_and_signature(tmp_path, store):
    src = FakeSource(visit_rows=[VisitRow("V1", date(2025, 11, 5), "30", "25", "01", "3")])
    svc = TrendService(store, src, make_settings(tmp_path), clock=lambda: NOW)
    svc.year(2569)
    svc.year(2569, disease="hfmd")
    assert [c[0] for c in src.episode_calls] == [DENGUE, HFMD]
    svc.year(2569)
    svc.year(2569, disease="hfmd")
    assert len(src.episode_calls) == 2
    assert store.trend_get(2569)[0]["signature"] != store.trend_get(2569, disease="hfmd")[0]["signature"]
    assert svc._lock_for((("dengue"), 2569)) is not svc._lock_for(("hfmd", 2569))


def test_trend_service_failure_backoff_is_per_disease(tmp_path, store):
    src = FakeSource(failure=RuntimeError("x"), fail_methods=["episode_rows"])
    svc = TrendService(store, src, make_settings(tmp_path), clock=lambda: NOW)
    assert svc.year(2569, disease="hfmd").error
    src.failure = None
    assert svc.year(2569).error is None                           # dengue ไม่ติดช่วงพักของ hfmd
    assert svc.year(2569, disease="hfmd").error                   # hfmd ยังอยู่ในช่วงพัก


def test_trend_page_uses_selected_disease_and_keeps_it_in_form(app, client):
    src = app.config["SOURCE"]
    html = client.get("/trend?d=hfmd").get_data(as_text=True)
    assert src.episode_calls and all(c[0] == HFMD for c in src.episode_calls)
    assert 'name="d" value="hfmd"' in html and "d=hfmd" in html.split("คำนวณใหม่")[0].rsplit("href", 1)[1]
    assert "มือเท้าปาก" in html


def test_trend_page_bad_disease_falls_back(app, client):
    assert client.get("/trend?d=bogus").status_code == 200
    assert all(c[0] == DENGUE for c in app.config["SOURCE"].episode_calls)

def test_map_legend_has_no_radius_for_village_mode_disease(client, store):
    assert "รัศมี 100 ม." in client.get("/map").get_data(as_text=True)
    html = client.get("/map?d=hfmd").get_data(as_text=True)
    assert "รัศมี 0" not in html and "หมู่บ้านเดียวกัน" in html


def test_index_hides_pin_reminder_for_cluster_mode_disease(client, store):
    for i in range(2):
        h = store.add_case(f"VN:{i}", "OPD", f"H{i}", "B084", datetime.now(), "40010103", None, "1", 3, True, disease="hfmd")
        d = store.add_case(f"VN:D{i}", "OPD", f"HD{i}", "A90", datetime.now(), "40010103", None, "1", 3, True)
        for cid in (h, d):
            store.set_pin(cid, 14.4, 101.8, "estimated", "village_centroid", "system")
    assert "ยังไม่ปักหมุดครบ" in client.get("/").get_data(as_text=True)          # ไข้เลือดออก: เตือนเหมือนเดิม
    assert "ยังไม่ปักหมุดครบ" not in client.get("/?d=hfmd").get_data(as_text=True)  # โรคกลุ่มก้อนไม่ต้องปักหมุด
