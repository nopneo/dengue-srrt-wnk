"""แก้ตามรีวิวอิสระรอบสุดท้ายของ 1.5.0 (ledger: F1, F2, F4, F5, F7, F8, F9, F10)"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import alerts
from app import create_app
from config import load_settings
from detector import run_cycle
from fakes import FakeSender, FakeSource, HosxpError, make_row
from store import Store

LAN = {"X-Forwarded-For": "192.168.70.135"}
ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 8, 12, 0)
CENTROID = {"40010103": (14.5, 101.8)}


def make_settings(tmp_path, diseases="dengue,hfmd", **env):
    base = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
            "DENGUE_START_FROM": "2026-01-01", "DENGUE_DISEASES": diseases, "DENGUE_AREA_AMPHOE": "4001"}
    base.update(env)
    return load_settings(base)


@pytest.fixture
def client(tmp_path, store, login_as):
    app = create_app(settings=make_settings(tmp_path), store=store, source=FakeSource(), sender=FakeSender())
    c = app.test_client()
    login_as(c, "u1", "srrt")
    return c


def case(store, key, disease, icd, hn=None, pin="estimated", dx=None):
    cid = store.add_case(key, "OPD", hn or "H" + key, icd, dx or datetime.now().replace(microsecond=0), "40010103",
                         "1", "1", 30, True, disease=disease)
    if pin:
        store.set_pin(cid, 14.35, 101.9, pin, "manual", "t")
    return cid


def map_data(html):
    return json.loads(re.search(r'<script type="application/json" id="map-data">(.*?)</script>', html, re.S).group(1))


# ---------------------------------------------------------------- F1: ลิงก์ cluster จาก LINE ไม่มี d

def test_cluster_link_without_d_selects_the_clusters_own_disease(client, store):
    h1, h2 = case(store, "1", "hfmd", "B084"), case(store, "2", "hfmd", "B084", pin="confirmed")
    cl = store.save_cluster([h1, h2], datetime.now(), datetime.now(), disease="hfmd")
    data = map_data(client.get(f"/map?cluster={cl}").get_data(as_text=True))
    assert data["selected_cluster"] == cl and "d=hfmd" in data["api_url"]
    html = client.get(f"/map?cluster={cl}").get_data(as_text=True)
    assert "มือเท้าปาก" in html and 'aria-current="page"' in html
    assert [c["id"] for c in client.get(f"/api/map.json?cluster={cl}").get_json()["cases"]] == [h1, h2]


def test_explicit_d_wins_over_cluster_disease(client, store):
    h = case(store, "1", "hfmd", "B084")
    cl = store.save_cluster([h], datetime.now(), datetime.now(), disease="hfmd")
    assert map_data(client.get(f"/map?d=dengue&cluster={cl}").get_data(as_text=True))["selected_cluster"] is None


def test_tab_script_keeps_cluster_links_and_query():
    js = (ROOT / "static" / "disease_tabs.js").read_text(encoding="utf-8")
    assert "cluster=" in js and "location.search" in js


# ---------------------------------------------------------------- F2: เปิดโรคทีหลังไม่ย้อนแจ้ง

def row(key, icd, hn, dx):
    return make_row(visit_key=key, hn=hn, icd10=icd, dx_at=dx)


def test_newly_enabled_disease_starts_from_the_enable_day(store, tmp_path):
    src = FakeSource([row("VN:1", "A27", "H1", datetime(2026, 11, 1, 9, 0)),
                      row("VN:2", "A27", "H2", datetime(2026, 11, 8, 9, 0)),
                      row("VN:3", "A90", "H3", datetime(2026, 11, 1, 9, 0))], centroids=CENTROID)
    run_cycle(store, src, make_settings(tmp_path, "dengue,lepto"), NOW)
    keys = {c["visit_key"] for c in store.list_cases(datetime(2026, 1, 1).date())}
    assert keys == {"VN:2", "VN:3"}                       # โรคที่เพิ่งเปิดไม่ย้อน · ไข้เลือดออกยังนับตั้งแต่ START_FROM


def test_reenabling_a_disease_resets_its_baseline(store, tmp_path):
    src = FakeSource([row("VN:1", "A27", "H1", datetime(2026, 11, 10, 9, 0))], centroids=CENTROID)
    run_cycle(store, src, make_settings(tmp_path, "dengue,lepto"), NOW)
    run_cycle(store, src, make_settings(tmp_path, "dengue"), NOW + timedelta(days=5))     # ปิดโรค
    src.rows.append(row("VN:2", "A27", "H2", datetime(2026, 11, 14, 9, 0)))               # เกิดระหว่างปิด
    src.rows.append(row("VN:3", "A27", "H3", datetime(2026, 11, 20, 9, 0)))
    run_cycle(store, src, make_settings(tmp_path, "dengue,lepto"), NOW + timedelta(days=12))   # เปิดใหม่ 20 พ.ย.
    keys = {c["visit_key"] for c in store.list_cases(datetime(2026, 1, 1).date())}
    assert "VN:2" not in keys and "VN:3" in keys


def test_enabled_marker_is_stable_across_cycles_and_dengue_has_none(store, tmp_path):
    s = make_settings(tmp_path, "dengue,lepto")
    src = FakeSource([], centroids=CENTROID)
    run_cycle(store, src, s, NOW)
    first = store.disease_enabled_at("lepto", NOW + timedelta(days=3))
    assert first == NOW                                    # ค่าเดิม ไม่เลื่อนตามรอบถัดไป


# ---------------------------------------------------------------- F4: ตาราง trend ของ 1.4.0 ไม่ถูกแตะ

def test_legacy_trend_tables_are_left_untouched(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE trend_cache (year INTEGER NOT NULL, month INTEGER NOT NULL, in_area INTEGER NOT NULL, tmb TEXT NOT NULL,
  moo TEXT NOT NULL, cases INTEGER NOT NULL, PRIMARY KEY (year, month, in_area, tmb, moo));
CREATE TABLE trend_meta (year INTEGER PRIMARY KEY, computed_at TEXT NOT NULL, signature TEXT NOT NULL);
INSERT INTO trend_cache VALUES (2569, 1, 1, '302501', '30250101', 3);
INSERT INTO trend_meta VALUES (2569, '2026-11-01T08:00:00', 'v1|A90,A91|21|3025');""")
    con.commit()
    con.close()
    Store(str(path))
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM trend_cache").fetchone()[0] == 1
    assert [r[1] for r in con.execute("PRAGMA table_info(trend_meta)")] == ["year", "computed_at", "signature"]
    con.close()


# ---------------------------------------------------------------- F5: เพื่อนบ้านในรัศมีเฉพาะโรคเดียวกัน

def test_case_page_neighbors_exclude_other_diseases(client, store):
    d = case(store, "1", "dengue", "A90", pin="confirmed")
    near_same = case(store, "2", "dengue", "A90", hn="HX", pin="confirmed")
    other = case(store, "3", "hfmd", "B084", hn="HY")
    html = client.get(f"/case/{d}").get_data(as_text=True)
    assert f"/case/{near_same}" in html and f"/case/{other}" not in html


# ---------------------------------------------------------------- F7: error ระบุโรค

def test_read_error_names_the_disease_and_mass_cancel_does_not_overwrite_it(store, tmp_path):
    src = FakeSource([row("VN:1", "A90", "H1", datetime(2026, 11, 8, 9, 0)),
                      row("VN:2", "B084", "H2", datetime(2026, 11, 8, 9, 0)),
                      row("VN:3", "B084", "H3", datetime(2026, 11, 8, 9, 0))], centroids=CENTROID,
                     failure=HosxpError("down"), fail_methods=[])
    s = make_settings(tmp_path)
    run_cycle(store, src, s, NOW)
    src.fail_icds = {("A90", "A91")}                       # ไข้เลือดออกอ่านไม่ได้ (มาก่อนในทะเบียน)
    src.cancelled = {"VN:2", "VN:3"}                       # มือเท้าปากหายยกชุด -> guard ของมือเท้าปาก
    for _ in range(3):
        result = run_cycle(store, src, s, NOW)
    assert result.error.startswith("ไข้เลือดออก:") and "down" in result.error     # error แรกของรอบคงอยู่ ไม่ถูก guard ทับ
    src.fail_icds = set()
    guard = run_cycle(store, src, s, NOW)
    assert guard.error.startswith("มือเท้าปาก:") and "ข้าม" in guard.error          # ระบุโรคที่ติด guard


def test_single_disease_error_text_is_unchanged(store, tmp_path):
    src = FakeSource([], failure=HosxpError("down"))
    assert run_cycle(store, src, make_settings(tmp_path, "dengue"), NOW).error == "down"


# ---------------------------------------------------------------- F8: ลิงก์พก d

def test_links_carry_the_selected_disease(client, store):
    h = case(store, "1", "hfmd", "B084")
    page = client.get("/?d=hfmd").get_data(as_text=True)
    assert 'href="/map?d=hfmd"' in page and 'href="/trend?d=hfmd"' in page
    assert 'href="/?d=hfmd"' in client.get(f"/case/{h}").get_data(as_text=True)          # กลับรายการเคสของโรคนั้น
    assert 'href="/map?d=' not in client.get(f"/case/{h}").get_data(as_text=True)         # เมนูซ้ายหน้าเคสไม่ล็อกโรค


# ---------------------------------------------------------------- F9: โรคโหมดหมู่บ้านบนหน้าเคส/KPI

def test_village_mode_case_page_has_no_radius_and_kpi_pending_is_zero(client, store):
    h = case(store, "1", "hfmd", "B084", pin=None)
    page = client.get(f"/case/{h}").get_data(as_text=True)
    assert "ในรัศมี" not in page and "รัศมี 100" not in page
    data = json.loads(re.search(r'id="case-data">(.*?)</script>', page, re.S).group(1))
    assert data["radius_m"] == 0
    idx = client.get("/?d=hfmd").get_data(as_text=True)
    assert re.search(r'data-testid="kpi-pending">0<', idx)
    d = case(store, "2", "dengue", "A90", pin=None)
    assert re.search(r'data-testid="kpi-pending">1<', client.get("/").get_data(as_text=True))


# ---------------------------------------------------------------- F10: แถบ LINE ล้มเหลวไม่ค้างเมื่อปิดโรค

def test_alert_health_ignores_disabled_and_cluster_mode_diseases(store, tmp_path):
    cid = case(store, "1", "lepto", "A27")
    store.log_alert("new", cid, None, False, 500, "x")
    on, off = make_settings(tmp_path, "dengue,lepto"), make_settings(tmp_path, "dengue")
    assert alerts.health(store, on)["failing"] == 1
    assert alerts.health(store, off)["failing"] == 0
    assert alerts.health(store)["failing"] == 1                       # ไม่ส่ง settings = นับทั้งหมดเหมือนเดิม
