"""Store: คอลัมน์ disease + ย้ายฐานรูปแบบ 1.4.0 + trend cache แยกโรค (สเปกข้อ 4.1)"""
import sqlite3
from datetime import date, datetime, timedelta

from store import Store
from trend import CountRow

DX = datetime(2026, 11, 8, 9, 30)

# สคีมา 1.4.0 (ก่อนมีคอลัมน์ disease) — ฐานจริงบนเซิร์ฟเวอร์เป็นรูปนี้
LEGACY_SCHEMA = """
CREATE TABLE "case" (
    id INTEGER PRIMARY KEY AUTOINCREMENT, visit_key TEXT NOT NULL UNIQUE, visit_type TEXT NOT NULL,
    hn TEXT NOT NULL, icd10 TEXT NOT NULL, dx_at TEXT NOT NULL, village_code TEXT, house_no TEXT, sex TEXT,
    age_years INTEGER, in_area INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
    pin_state TEXT NOT NULL DEFAULT 'none', alerted_at TEXT, absent_cycles INTEGER NOT NULL DEFAULT 0);
CREATE TABLE cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_case_ids TEXT NOT NULL, first_dx_at TEXT NOT NULL,
    last_dx_at TEXT NOT NULL, alerted_size INTEGER NOT NULL DEFAULT 0, alerted_at TEXT);
CREATE TABLE trend_cache (
    year INTEGER NOT NULL, month INTEGER NOT NULL, in_area INTEGER NOT NULL, tmb TEXT NOT NULL,
    moo TEXT NOT NULL, cases INTEGER NOT NULL, PRIMARY KEY (year, month, in_area, tmb, moo));
CREATE TABLE trend_meta (year INTEGER PRIMARY KEY, computed_at TEXT NOT NULL, signature TEXT NOT NULL);
INSERT INTO "case" (visit_key, visit_type, hn, icd10, dx_at, village_code) VALUES ('VN:1','OPD','H1','A90','2026-11-01T08:00:00','30250101');
INSERT INTO cluster (member_case_ids, first_dx_at, last_dx_at) VALUES ('[1]','2026-11-01T08:00:00','2026-11-01T08:00:00');
INSERT INTO trend_cache VALUES (2569, 1, 1, '302501', '30250101', 3);
INSERT INTO trend_meta VALUES (2569, '2026-11-01T08:00:00', 'v1|A90,A91|21|3025');
"""


def _case(store, key="V1", hn="H", icd="A90", dx=DX, village="30250101", disease="dengue", in_area=True):
    return store.add_case(key, "OPD", hn, icd, dx, village, None, "1", 5, in_area, disease=disease)


def test_legacy_database_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.commit()
    con.close()
    store = Store(str(path))
    (case,) = store.list_cases(date(2026, 1, 1))
    assert case["disease"] == "dengue" and case["icd10"] == "A90"
    (cl,) = store.clusters()
    assert cl["disease"] == "dengue" and cl["member_case_ids"] == [1]
    assert store.trend_get(2569) is None                       # แคชเก่าถูกล้าง (คำนวณใหม่เองได้)
    store.trend_put(2569, "sig", [CountRow(1, True, "302501", "30250101", 2)], DX)
    assert store.trend_get(2569)[1][0].cases == 2
    Store(str(path))                                           # เปิดซ้ำต้องไม่ error และข้อมูลไม่หาย
    assert store.trend_get(2569) is not None and len(store.list_cases(date(2026, 1, 1))) == 1


def test_new_case_default_disease_is_dengue(store):
    cid = store.add_case("V1", "OPD", "H", "A90", DX, None, None, None, None, True)
    assert store.get_case(cid)["disease"] == "dengue"


def test_case_queries_filter_by_disease_and_default_is_unfiltered(store):
    a = _case(store, "V1", icd="A90")
    b = _case(store, "V2", icd="B084", disease="hfmd")
    assert {c["id"] for c in store.active_cases()} == {a, b}
    assert [c["id"] for c in store.active_cases("hfmd")] == [b]
    assert {c["id"] for c in store.list_cases(date(2026, 1, 1))} == {a, b}
    assert [c["id"] for c in store.list_cases(date(2026, 1, 1), disease="dengue")] == [a]
    store.set_pin(a, 14.4, 101.8, "confirmed", "manual", "u")
    store.set_pin(b, 14.4, 101.8, "estimated", "village_centroid", "system")
    assert {c["id"] for c in store.pinned_cases()} == {a, b}
    assert [c["id"] for c in store.pinned_cases(disease="hfmd")] == [b]


def test_recent_case_for_hn_is_per_disease(store):
    _case(store, "V1", hn="H", disease="dengue", dx=DX - timedelta(days=2))
    since = DX - timedelta(days=21)
    assert store.recent_case_for_hn("H", since)["disease"] == "dengue"
    assert store.recent_case_for_hn("H", since, disease="dengue") is not None
    assert store.recent_case_for_hn("H", since, disease="hfmd") is None


def test_clusters_are_per_disease(store):
    d = store.save_cluster([1, 2], DX, DX)
    h = store.save_cluster([3, 4], DX, DX, disease="hfmd")
    assert {c["id"] for c in store.clusters()} == {d, h}
    assert [c["id"] for c in store.clusters("hfmd")] == [h]
    assert [c["id"] for c in store.clusters("dengue")] == [d]
    assert store.clusters("hfmd")[0]["disease"] == "hfmd"


def test_active_counts_per_disease_since_date(store):
    _case(store, "V1")
    _case(store, "V2", disease="hfmd")
    _case(store, "V3", disease="hfmd", dx=DX - timedelta(days=60))
    old = _case(store, "V4", disease="hfmd")
    store.mark_cancelled(old)
    assert store.active_counts(date(2026, 11, 1)) == {"dengue": 1, "hfmd": 1}
    assert store.active_counts(date(2026, 1, 1)) == {"dengue": 1, "hfmd": 2}


def test_trend_cache_is_per_disease(store):
    row = lambda n: [CountRow(1, True, "302501", "30250101", n)]
    store.trend_put(2569, "s1", row(1), DX)
    store.trend_put(2569, "s2", row(7), DX, disease="hfmd")
    assert store.trend_get(2569)[1][0].cases == 1
    assert store.trend_get(2569, disease="hfmd")[1][0].cases == 7
    assert store.trend_get(2569, disease="hfmd")[0]["signature"] == "s2"
    assert store.trend_get(2569, disease="lepto") is None
    store.trend_put(2569, "s3", row(2), DX)                     # แทนที่เฉพาะโรคเดียว
    assert store.trend_get(2569, disease="hfmd")[1][0].cases == 7
