from datetime import date, datetime, timedelta

import pytest

DX = datetime(2026, 11, 8, 9, 30)


def _case(store, visit_key="V1", hn="HN1", village="30140101", house="12/3",
          icd10="A90", dx_at=DX, in_area=True, visit_type="OPD"):
    return store.add_case(visit_key, visit_type, hn, icd10, dx_at, village,
                          house, "F", 25, in_area)


def test_add_case_dedupes_by_visit_key(store):
    first = _case(store, icd10="A90")
    assert isinstance(first, int)
    assert _case(store, icd10="A91") is None
    assert len(store.active_cases()) == 1
    c = store.get_case(first)
    assert c["icd10"] == "A90"
    assert c["dx_at"] == DX
    assert c["in_area"] is True
    assert c["status"] == "active"
    assert c["pin_state"] == "none"
    assert c["alerted_at"] is None
    assert store.get_case(999) is None


def test_list_cases_filters_and_cancel(store):
    a = _case(store, "V1", dx_at=datetime(2026, 11, 1, 8, 0))
    b = _case(store, "V2", dx_at=datetime(2026, 11, 5, 8, 0), in_area=False)
    store.mark_cancelled(a)
    assert [c["id"] for c in store.active_cases()] == [b]
    assert {c["id"] for c in store.list_cases(date(2026, 11, 1))} == {a, b}
    assert [c["id"] for c in store.list_cases(date(2026, 11, 2))] == [b]
    assert [c["id"] for c in store.list_cases(date(2026, 11, 1), status="cancelled")] == [a]
    assert [c["id"] for c in store.list_cases(date(2026, 11, 1), in_area=False)] == [b]


def test_recent_case_for_hn_returns_latest_active(store):
    _case(store, "V1", hn="H", dx_at=DX - timedelta(days=30))
    mid = _case(store, "V2", hn="H", dx_at=DX - timedelta(days=5))
    new = _case(store, "V3", hn="H", dx_at=DX - timedelta(days=1))
    store.mark_cancelled(new)
    assert store.recent_case_for_hn("H", DX - timedelta(days=21))["id"] == mid
    assert store.recent_case_for_hn("H", DX - timedelta(days=2)) is None
    assert store.recent_case_for_hn("other", DX - timedelta(days=60)) is None


def test_set_pin_confirmed_writes_memory_and_audit(store):
    cid = _case(store, hn="HN9", village="30140101", house=" 12/3 ")
    store.set_pin(cid, 14.1, 101.8, "confirmed", "manual", "somchai")
    pin = store.get_pin(cid)
    assert (pin["lat"], pin["lng"]) == (14.1, 101.8)
    assert pin["state"] == "confirmed" and pin["source"] == "manual"
    assert pin["set_by"] == "somchai"
    assert isinstance(pin["set_at"], datetime)
    assert store.get_case(cid)["pin_state"] == "confirmed"
    assert store.default_pin("HN9", None, None) == (14.1, 101.8, "hn_memory")
    assert store.default_pin("zzz", "30140101", "12/3") == (14.1, 101.8, "house_memory")

    store.set_pin(cid, 14.2, 101.9, "confirmed", "gps", "somchai")
    with store._connect() as con:
        log = con.execute(
            "SELECT action, user, case_id, detail FROM audit_log ORDER BY rowid"
        ).fetchall()
    assert [r["action"] for r in log] == ["set_pin", "set_pin"]
    assert log[0]["user"] == "somchai" and log[0]["case_id"] == cid
    assert "14.1" in log[1]["detail"] and "14.2" in log[1]["detail"]


def test_set_pin_estimated_does_not_write_memory(store):
    cid = _case(store, hn="HN8")
    store.set_pin(cid, 14.0, 101.0, "estimated", "village_centroid", "system")
    assert store.get_case(cid)["pin_state"] == "estimated"
    assert store.default_pin("HN8", "30140101", "12/3") is None


def test_set_pin_rejects_bad_state(store):
    cid = _case(store)
    with pytest.raises(ValueError):
        store.set_pin(cid, 1.0, 2.0, "none", "manual", "u")


def test_default_pin_prefers_house_over_hn(store):
    a = _case(store, "V1", hn="HN1", village="30140101", house="5")
    store.set_pin(a, 14.0, 101.0, "confirmed", "manual", "u")
    b = _case(store, "V2", hn="HN2", village="30140101", house="5")
    store.set_pin(b, 14.5, 101.5, "confirmed", "manual", "u")
    assert store.default_pin("HN1", "30140101", "5") == (14.5, 101.5, "house_memory")
    assert store.default_pin("HN1", "30140101", "99") == (14.0, 101.0, "hn_memory")
    assert store.default_pin("HN1", None, None) == (14.0, 101.0, "hn_memory")
    assert store.default_pin("nobody", "30140101", "99") is None


def test_pinned_cases_joins_pin_and_filters_since(store):
    a = _case(store, "V1", dx_at=datetime(2026, 11, 1, 8, 0))
    b = _case(store, "V2", dx_at=datetime(2026, 11, 6, 8, 0))
    _case(store, "V3", dx_at=datetime(2026, 11, 7, 8, 0))  # no pin
    store.set_pin(a, 14.0, 101.0, "estimated", "village_centroid", "system")
    store.set_pin(b, 14.1, 101.1, "confirmed", "manual", "u")
    rows = store.pinned_cases()
    assert {r["id"] for r in rows} == {a, b}
    ra = next(r for r in rows if r["id"] == a)
    assert (ra["lat"], ra["lng"], ra["pin_source"]) == (14.0, 101.0, "village_centroid")
    assert ra["pin_state"] == "estimated"
    assert isinstance(ra["dx_at"], datetime)
    assert [r["id"] for r in store.pinned_cases(since=date(2026, 11, 2))] == [b]


def test_check_user_uses_pbkdf2_and_rejects_wrong_password(store):
    store.add_user("nurse", "s3cret", "srrt")
    assert store.check_user("nurse", "s3cret") == "srrt"
    assert store.check_user("nurse", "wrong") is None
    assert store.check_user("nobody", "s3cret") is None
    with store._connect() as con:
        h = con.execute('SELECT password_hash FROM "user"').fetchone()[0]
    assert h.startswith("pbkdf2:sha256")
    with pytest.raises(ValueError):
        store.add_user("x", "p", "root")


def test_log_alert_counts_attempts(store):
    cid = _case(store)
    assert store.alert_attempts("new", case_id=cid) == 0
    assert store.alert_sent("new", case_id=cid) is False
    store.log_alert("new", cid, None, False, 500, "boom")
    store.log_alert("new", cid, None, False, None, "timeout")
    assert store.alert_attempts("new", case_id=cid) == 2
    assert store.alert_sent("new", case_id=cid) is False
    store.log_alert("new", cid, None, True, 200, None)
    assert store.alert_attempts("new", case_id=cid) == 3
    assert store.alert_sent("new", case_id=cid) is True
    assert store.alert_sent("cancel", case_id=cid) is False


def test_clusters_roundtrip(store):
    a = _case(store, "V1")
    b = _case(store, "V2")
    first, last = datetime(2026, 11, 1, 8), datetime(2026, 11, 6, 8)
    cl = store.save_cluster([a, b], first, last)
    [row] = store.clusters()
    assert row["id"] == cl and row["member_case_ids"] == [a, b]
    assert row["first_dx_at"] == first and row["last_dx_at"] == last
    assert row["alerted_size"] == 0 and row["alerted_at"] is None
    store.log_alert("cluster", None, cl, True, 200, None)
    assert store.alert_sent("cluster", cluster_id=cl) is True
    store.update_cluster_alerted(cl, 2)
    [row] = store.clusters()
    assert row["alerted_size"] == 2 and isinstance(row["alerted_at"], datetime)


def test_update_cluster_replaces_members_and_dates(store):
    a = _case(store, "V1")
    b = _case(store, "V2")
    c = _case(store, "V3")
    cl = store.save_cluster([a, b], datetime(2026, 11, 1, 8), datetime(2026, 11, 6, 8))
    store.update_cluster_alerted(cl, 2)
    store.update_cluster(cl, [a, b, c], datetime(2026, 10, 30, 8), datetime(2026, 11, 8, 8))
    [row] = store.clusters()
    assert row["member_case_ids"] == [a, b, c]
    assert row["first_dx_at"] == datetime(2026, 10, 30, 8)
    assert row["last_dx_at"] == datetime(2026, 11, 8, 8)
    assert row["alerted_size"] == 2  # ไม่แตะสถานะการแจ้ง


def test_alert_failures_counts_since_last_success_per_key(store):
    cid = _case(store)
    assert store.alert_failures("new", case_id=cid) == (0, None)
    store.log_alert("new", cid, None, False, None, "x")
    store.log_alert("new", cid, None, False, None, "y")
    n, last = store.alert_failures("new", case_id=cid)
    assert n == 2 and isinstance(last, datetime)
    store.log_alert("new", cid, None, True, None, None)
    assert store.alert_failures("new", case_id=cid) == (0, None)
    store.log_alert("new", cid, None, False, None, "z")
    assert store.alert_failures("new", case_id=cid)[0] == 1
    assert store.alert_failures("cancel", case_id=cid) == (0, None)


def test_alert_failures_is_per_cluster_size(store):
    a, b = _case(store, "V1"), _case(store, "V2")
    cl = store.save_cluster([a, b], DX, DX)
    store.record_cluster_alert(cl, 2, False, None, "e1")
    store.record_cluster_alert(cl, 2, False, None, "e2")
    assert store.alert_failures("cluster", cluster_id=cl, size=2)[0] == 2
    assert store.alert_failures("cluster", cluster_id=cl, size=3) == (0, None)
    store.record_cluster_alert(cl, 2, True, None, None)
    assert store.alert_failures("cluster", cluster_id=cl, size=2) == (0, None)
    store.record_cluster_alert(cl, 3, False, None, "e3")
    assert store.alert_failures("cluster", cluster_id=cl, size=3)[0] == 1


def test_record_cluster_alert_is_atomic_with_size_and_alerted(store):
    a, b = _case(store, "V1"), _case(store, "V2")
    cl = store.save_cluster([a, b], DX, DX)
    store.record_cluster_alert(cl, 2, False, 500, "boom")
    [row] = store.clusters()
    assert row["alerted_size"] == 0 and row["alerted_at"] is None
    store.record_cluster_alert(cl, 2, True, None, None)
    [row] = store.clusters()
    assert row["alerted_size"] == 2 and isinstance(row["alerted_at"], datetime)
    logs = store.alerts("cluster", cluster_id=cl)
    assert [(r["ok"], r["size"], r["response_code"], r["error"]) for r in logs] == [
        (False, 2, 500, "boom"), (True, 2, None, None)]


def test_alerts_reader_filters_and_orders(store):
    a, b = _case(store, "V1"), _case(store, "V2")
    store.log_alert("new", a, None, True, None, None)
    store.log_alert("new", b, None, False, 500, "e")
    store.log_alert("cancel", a, None, True, None, None)
    assert [r["id"] for r in store.alerts()] == [1, 2, 3]
    assert [r["kind"] for r in store.alerts(case_id=a)] == ["new", "cancel"]
    [row] = store.alerts("new", case_id=b)
    assert row["ok"] is False and row["error"] == "e" and isinstance(row["sent_at"], datetime)


def test_old_database_gets_size_column(tmp_path):
    import sqlite3
    from store import Store
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE alert_log (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER,"
        " cluster_id INTEGER, kind TEXT NOT NULL, sent_at TEXT NOT NULL, ok INTEGER NOT NULL,"
        " response_code INTEGER, error TEXT);")
    con.close()
    store = Store(str(path))
    store.log_alert("cluster", None, None, False, None, "e", size=4)
    assert store.alerts()[0]["size"] == 4


def test_check_user_unknown_user_still_runs_password_hash(store, monkeypatch):
    """กัน timing เดา username: ผู้ใช้ที่ไม่มีอยู่ต้องผ่าน check_password_hash กับแฮชจำลองด้วย."""
    import store as store_module
    calls = []
    real = store_module.check_password_hash

    def spy(pwhash, password):
        calls.append(pwhash)
        return real(pwhash, password)

    monkeypatch.setattr(store_module, "check_password_hash", spy)
    assert store.check_user("nobody", "whatever") is None
    assert len(calls) == 1 and calls[0].startswith("pbkdf2:sha256:")
    store.add_user("nurse", "s3cret", "srrt")
    assert store.check_user("nurse", "s3cret") == "srrt"
    assert len(calls) == 2


def test_audit_reader_returns_rows_in_order_with_decoded_detail(store):
    a = _case(store, "V1", hn="HN1")
    b = _case(store, "V2", hn="HN2")
    assert store.audit() == []
    store.set_pin(a, 14.35, 101.9, "estimated", "manual", "u1")
    store.set_pin(b, 14.36, 101.91, "confirmed", "gps", "u2")
    store.set_pin(a, 14.351, 101.9, "confirmed", "manual", "u1")
    rows = store.audit()
    assert [(r["user"], r["case_id"], r["action"]) for r in rows] == [
        ("u1", a, "set_pin"), ("u2", b, "set_pin"), ("u1", a, "set_pin")]
    assert rows[0]["detail"]["before"] is None
    assert rows[2]["detail"]["before"]["state"] == "estimated"
    assert rows[2]["detail"]["after"]["state"] == "confirmed"
    assert [r["case_id"] for r in store.audit(case_id=a)] == [a, a]
    assert store.audit(case_id=999) == []


def test_add_audit_writes_row_readable_by_audit(store):
    store.add_audit("settings_change", "boss", detail="DB_HOST,DB_PASS")
    store.add_audit("settings_revert", "boss")
    a = _case(store, "V1", hn="HN1")
    store.add_audit("note", "u2", detail="x", case_id=a)
    rows = store.audit()
    assert [(r["action"], r["user"], r["case_id"], r["detail"]) for r in rows] == [
        ("settings_change", "boss", None, "DB_HOST,DB_PASS"),
        ("settings_revert", "boss", None, None),
        ("note", "u2", a, "x")]
    assert all(r["at"] for r in rows)
    assert [r["action"] for r in store.audit(case_id=a)] == ["note"]


# ---------------------------------------------------------------- alert_health

MAX_TOTAL = 20


def _fail(store, kind, case_id, n=1):
    for _ in range(n):
        store.log_alert(kind, case_id, None, False, 500, "NotifyError")


def _ok(store, kind, case_id):
    store.log_alert(kind, case_id, None, True, None, None)


def _cluster(store, n=2):
    a = _case(store, "C%d-a" % n, hn="HC%d-a" % n)
    b = _case(store, "C%d-b" % n, hn="HC%d-b" % n)
    return store.save_cluster([a, b], DX, DX)


def test_alert_health_no_failures_is_zero(store):
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}
    cid = _case(store)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}
    _ok(store, "new", cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_one_failing_new_alert(store):
    cid = _case(store)
    _fail(store, "new", cid, 3)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 0}


def test_alert_health_counts_each_case_once(store):
    a, b = _case(store, "V1", hn="H1"), _case(store, "V2", hn="H2")
    _fail(store, "new", a, 4)
    _fail(store, "new", b, 1)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 2, "given_up": 0}


def test_alert_health_failure_then_success_not_counted(store):
    cid = _case(store)
    _fail(store, "new", cid, 2)
    _ok(store, "new", cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_given_up_threshold(store):
    a, b = _case(store, "V1", hn="H1"), _case(store, "V2", hn="H2")
    _fail(store, "new", a, MAX_TOTAL - 1)
    _fail(store, "new", b, MAX_TOTAL)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 2, "given_up": 1}
    _fail(store, "new", a, 1)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 2, "given_up": 2}
    assert store.alert_health(max_total=MAX_TOTAL + 1) == {"failing": 2, "given_up": 0}


def test_alert_health_excludes_cancelled_and_unknown_cases_for_new(store):
    cid = _case(store)
    _fail(store, "new", cid, 2)
    store.mark_cancelled(cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_cancel_rules(store):
    cid = _case(store)
    _ok(store, "new", cid)
    store.mark_cancelled(cid)
    _fail(store, "cancel", cid, 2)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 0}
    _fail(store, "cancel", cid, MAX_TOTAL)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 1}
    _ok(store, "cancel", cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_cancel_needs_successful_new_and_cancelled_case(store):
    never_sent = _case(store, "V1", hn="H1")
    store.mark_cancelled(never_sent)
    _fail(store, "cancel", never_sent, 2)        # ไม่เคยแจ้ง new สำเร็จ -> ไม่ต้องแจ้งยกเลิก
    still_active = _case(store, "V2", hn="H2")
    _ok(store, "new", still_active)
    _fail(store, "cancel", still_active, 2)      # ยังไม่ถูกยกเลิก -> ไม่นับ
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_cluster_counts_per_size(store):
    cl = _cluster(store)
    store.record_cluster_alert(cl, 2, False, 500, "NotifyError")
    store.record_cluster_alert(cl, 2, False, 500, "NotifyError")
    store.record_cluster_alert(cl, 3, False, 500, "NotifyError")
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 2, "given_up": 0}
    for _ in range(MAX_TOTAL):
        store.record_cluster_alert(cl, 4, False, 500, "NotifyError")
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 3, "given_up": 1}


def test_alert_health_cluster_success_clears_only_that_size(store):
    cl = _cluster(store)
    store.record_cluster_alert(cl, 2, False, 500, "NotifyError")
    store.record_cluster_alert(cl, 2, True, None, None)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}
    store.record_cluster_alert(cl, 3, False, 500, "NotifyError")
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 0}


def test_alert_health_cluster_superseded_size_not_counted(store):
    cl = _cluster(store)
    store.record_cluster_alert(cl, 2, False, 500, "NotifyError")
    store.record_cluster_alert(cl, 3, True, None, None)   # กลุ่มโตแล้วแจ้งขนาด 3 สำเร็จ
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_returns_counts_only(store):
    cid = _case(store, hn="HN-SECRET")
    store.log_alert("new", cid, None, False, 401, "ข้อความลับ HN-SECRET")
    h = store.alert_health(max_total=MAX_TOTAL)
    assert set(h) == {"failing", "given_up"}
    assert all(type(v) is int for v in h.values())


# ---------------------------------------------------------------- rearm_alerts

def test_old_database_gets_rearmed_column(tmp_path):
    import sqlite3
    from store import Store
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE alert_log (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER,"
        " cluster_id INTEGER, kind TEXT NOT NULL, sent_at TEXT NOT NULL, ok INTEGER NOT NULL,"
        " response_code INTEGER, error TEXT);"
        "INSERT INTO alert_log (kind, sent_at, ok) VALUES ('new', '2026-11-08T09:00:00', 0);")
    con.close()
    store = Store(str(path))
    store.log_alert("new", None, None, False, None, "e")
    store = Store(str(path))            # เปิดซ้ำต้องไม่ล้ม (migration แบบมีเงื่อนไข)
    with store._connect() as c:
        rows = [r["rearmed"] for r in c.execute("SELECT rearmed FROM alert_log ORDER BY id")]
    assert rows == [0, 0]
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_rearm_clears_health_and_failure_count_but_keeps_history(store):
    a, b = _case(store, "V1", hn="H1"), _case(store, "V2", hn="H2")
    _fail(store, "new", a, MAX_TOTAL)
    _fail(store, "new", b, 2)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 2, "given_up": 1}
    assert store.alert_failures("new", case_id=a)[0] == MAX_TOTAL
    assert store.rearm_alerts() == 2
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}
    assert store.alert_failures("new", case_id=a) == (0, None)
    assert len(store.alerts("new", case_id=a)) == MAX_TOTAL          # ประวัติยังอยู่
    _fail(store, "new", a, 1)                                         # ล้มเหลวใหม่นับจากศูนย์
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 0}
    assert store.alert_failures("new", case_id=a)[0] == 1


def test_rearm_returns_item_count_not_row_count_and_is_idempotent(store):
    a = _case(store, "V1", hn="H1")
    _fail(store, "new", a, 7)
    cl = _cluster(store)
    store.record_cluster_alert(cl, 2, False, 500, "e")
    store.record_cluster_alert(cl, 2, False, 500, "e")
    store.record_cluster_alert(cl, 3, False, 500, "e")
    assert store.rearm_alerts() == 3          # new 1 + cluster ขนาด 2 และ 3
    assert store.rearm_alerts() == 0


def test_rearm_leaves_items_with_an_ok_row_untouched(store):
    done, stuck = _case(store, "V1", hn="H1"), _case(store, "V2", hn="H2")
    _fail(store, "new", done, 2)
    _ok(store, "new", done)
    _fail(store, "new", stuck, 3)
    cl = _cluster(store)
    store.record_cluster_alert(cl, 2, False, 500, "e")
    store.record_cluster_alert(cl, 2, True, None, None)
    assert store.rearm_alerts() == 1
    with store._connect() as c:
        rearmed = {r["case_id"]: r["n"] for r in c.execute(
            "SELECT case_id, SUM(rearmed) n FROM alert_log WHERE kind='new' GROUP BY case_id")}
        cluster_rearmed = c.execute(
            "SELECT SUM(rearmed) FROM alert_log WHERE kind='cluster'").fetchone()[0]
    assert rearmed == {done: 0, stuck: 3} and cluster_rearmed == 0


def test_rearm_new_and_cancel_are_separate_items(store):
    cid = _case(store)
    _ok(store, "new", cid)
    store.mark_cancelled(cid)
    _fail(store, "cancel", cid, 2)
    assert store.rearm_alerts() == 1
    assert store.alert_failures("new", case_id=cid) == (0, None)
    assert store.alert_failures("cancel", case_id=cid) == (0, None)


# ---------------------------------------------------------------- absent_cycles / reactivate (R24)

def _plain_case(store, key="VN:1", hn="H1"):
    return store.add_case(key, "OPD", hn, "A90", datetime(2026, 11, 8, 9, 0),
                          "40010103", "12", "1", 30, True)


def test_new_case_has_absent_cycles_zero(store):
    cid = _plain_case(store)
    assert store.get_case(cid)["absent_cycles"] == 0


def test_old_database_gets_absent_cycles_column(tmp_path):
    import sqlite3
    from store import Store
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        'CREATE TABLE "case" (id INTEGER PRIMARY KEY AUTOINCREMENT, visit_key TEXT NOT NULL'
        " UNIQUE, visit_type TEXT NOT NULL, hn TEXT NOT NULL, icd10 TEXT NOT NULL,"
        " dx_at TEXT NOT NULL, village_code TEXT, house_no TEXT, sex TEXT, age_years INTEGER,"
        " in_area INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',"
        " pin_state TEXT NOT NULL DEFAULT 'none', alerted_at TEXT);"
        "INSERT INTO \"case\" (visit_key, visit_type, hn, icd10, dx_at)"
        " VALUES ('VN:9','OPD','H9','A90','2026-11-08T09:00:00');")
    con.close()
    store = Store(str(path))
    assert store.get_case(1)["absent_cycles"] == 0
    store = Store(str(path))            # เปิดซ้ำต้องไม่ล้ม
    store.update_absent_cycles({1: 2})
    assert store.get_case(1)["absent_cycles"] == 2


def test_update_absent_cycles_sets_many_in_one_call(store):
    a, b, c = _plain_case(store, "VN:1"), _plain_case(store, "VN:2", "H2"), _plain_case(store, "VN:3", "H3")
    store.update_absent_cycles({a: 1, b: 2})
    assert [store.get_case(i)["absent_cycles"] for i in (a, b, c)] == [1, 2, 0]
    store.update_absent_cycles({})                      # ว่าง = ไม่ทำอะไร
    store.update_absent_cycles({a: 0})
    assert store.get_case(a)["absent_cycles"] == 0


def test_reactivate_case_restores_active_and_resets_counter_keeping_pin(store):
    cid = _plain_case(store)
    store.set_pin(cid, 14.5, 101.8, "confirmed", "manual", "u")
    store.update_absent_cycles({cid: 3})
    store.mark_cancelled(cid)
    assert store.reactivate_case(cid) is True
    case = store.get_case(cid)
    assert case["status"] == "active" and case["absent_cycles"] == 0
    assert case["pin_state"] == "confirmed" and store.get_pin(cid)["lat"] == 14.5


def test_reactivate_case_only_touches_cancelled(store):
    cid = _plain_case(store)
    assert store.reactivate_case(cid) is False          # active อยู่แล้ว
    assert store.reactivate_case(999) is False          # ไม่มีเคส


def test_alert_health_reactivate_rules(store):
    cid = _case(store)
    _ok(store, "new", cid)
    _ok(store, "cancel", cid)
    _fail(store, "reactivate", cid, 2)           # ยังเป็น cancelled อยู่ -> ไม่ต้องแจ้งคืนสถานะ
    store.mark_cancelled(cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}
    assert store.reactivate_case(cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 0}
    _fail(store, "reactivate", cid, MAX_TOTAL)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 1, "given_up": 1}
    _ok(store, "reactivate", cid)
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


def test_alert_health_reactivate_needs_prior_successful_cancel(store):
    cid = _case(store)
    _ok(store, "new", cid)
    _fail(store, "reactivate", cid, 2)           # ผู้รับไม่เคยเห็นยกเลิก -> ไม่มีอะไรต้องคืน
    assert store.alert_health(max_total=MAX_TOTAL) == {"failing": 0, "given_up": 0}


# ---------------------------------------------------------------- บัญชีผู้ใช้ (I5)

def test_user_role_returns_role_or_none(store):
    store.add_user("nurse", "pw-correct-1", "srrt")
    assert store.user_role("nurse") == "srrt"
    assert store.user_role("ghost") is None


def test_set_password_replaces_hash_with_pbkdf2(store):
    store.add_user("nurse", "old-password-1", "srrt")
    assert store.set_password("nurse", "new-password-2") is True
    assert store.check_user("nurse", "old-password-1") is None
    assert store.check_user("nurse", "new-password-2") == "srrt"
    with store._connect() as con:
        h = con.execute('SELECT password_hash FROM "user" WHERE username=?', ("nurse",)).fetchone()[0]
    assert h.startswith("pbkdf2:sha256")


def test_set_password_unknown_user_returns_false_and_creates_nothing(store):
    assert store.set_password("ghost", "new-password-2") is False
    assert store.user_role("ghost") is None


def test_remove_user_deletes_only_that_user(store):
    store.add_user("a", "password-aaa-1", "srrt")
    store.add_user("b", "password-bbb-1", "viewer")
    assert store.remove_user("a") is True
    assert store.user_role("a") is None and store.user_role("b") == "viewer"
    assert store.remove_user("a") is False


# ---- แคชหน้าเปรียบเทียบปี พ.ศ. ----
def test_trend_put_get_roundtrip_and_replace(store):
    from trend import CountRow
    at = datetime(2026, 11, 10, 9, 0)
    assert store.trend_get(2570) is None
    store.trend_put(2570, "sig1", [CountRow(0, True, "363001", "03", 2),
                                   CountRow(1, False, "", "00", 1)], at)
    meta, rows = store.trend_get(2570)
    assert meta == {"computed_at": at, "signature": "sig1"}
    assert sorted(rows, key=lambda r: r.month) == [CountRow(0, True, "363001", "03", 2),
                                                   CountRow(1, False, "", "00", 1)]
    store.trend_put(2570, "sig2", [CountRow(4, True, "363002", "01", 9)], at)  # แทนที่ทั้งปี
    meta, rows = store.trend_get(2570)
    assert meta["signature"] == "sig2" and rows == [CountRow(4, True, "363002", "01", 9)]
    assert store.trend_get(2569) is None


def test_trend_tables_hold_counts_only(store):
    with store._connect() as con:
        cols = {r["name"] for t in ("trend_cache", "trend_meta")
                for r in con.execute(f"PRAGMA table_info({t})")}
    assert not {"hn", "name", "visit_key", "address"} & cols


# ---- สถานพยาบาล (หมุด รพ./รพ.สต. บนแผนที่) ----
FAC_AT = datetime(2026, 11, 10, 9, 0)


def fac_row(code="02846", name="รพ.สต.ระเริง", kind="rpst", tmb="302503", lat=14.43, lng=101.82):
    return {"hospcode": code, "name": name, "kind": kind, "tmb": tmb, "lat": lat, "lng": lng}


def test_facility_upsert_get_all_order(store):
    assert store.facility_all() == [] and store.facility_get("02846") is None
    store.facility_upsert("02846", "รพ.สต.ระเริง", "rpst", "302503", 14.43, 101.82, "manual", "admin", FAC_AT)
    store.facility_upsert("10894", "โรงพยาบาลวังน้ำเขียว", "hospital", "302505", 14.41, 101.86, "csv", "import", FAC_AT)
    store.facility_upsert("02840", "รพ.สต.ศาลเจ้าพ่อ", "rpst", "302501", 14.37, 101.81, "csv", "import", FAC_AT)
    assert [f["hospcode"] for f in store.facility_all()] == ["10894", "02846", "02840"]     # โรงพยาบาลก่อน แล้วตามชื่อ (ระเริง < ศาลเจ้าพ่อ)
    got = store.facility_get("02846")
    assert got["source"] == "manual" and got["updated_by"] == "admin" and got["lat"] == 14.43
    assert got["updated_at"] == FAC_AT.isoformat(timespec="seconds")
    store.facility_upsert("02846", "รพ.สต.ระเริง", "rpst", "302503", 14.5, 101.9, "manual", "admin2", FAC_AT)
    assert store.facility_get("02846")["lat"] == 14.5 and store.facility_get("02846")["updated_by"] == "admin2"


def test_facility_bulk_upsert_skips_manual_unless_overwrite(store):
    store.facility_upsert("02846", "รพ.สต.ระเริง", "rpst", "302503", 14.5, 101.9, "manual", "admin", FAC_AT)
    rows = [fac_row("02846", lat=14.43), fac_row("10894", "โรงพยาบาลวังน้ำเขียว", "hospital", "302505", 14.41, 101.86)]
    written, skipped = store.facility_bulk_upsert(rows, "import", FAC_AT, overwrite_manual=False)
    assert (written, skipped) == (1, ["02846"])
    assert store.facility_get("02846")["lat"] == 14.5 and store.facility_get("02846")["source"] == "manual"
    assert store.facility_get("10894")["source"] == "csv"
    written, skipped = store.facility_bulk_upsert(rows, "import", FAC_AT, overwrite_manual=True)
    assert (written, skipped) == (2, [])
    assert store.facility_get("02846")["lat"] == 14.43 and store.facility_get("02846")["source"] == "csv"


def test_facility_bulk_upsert_is_atomic(store):
    import sqlite3
    rows = [fac_row("02846"), fac_row("10894", lat=None)]       # แถวที่สองผิด (NOT NULL)
    with pytest.raises(sqlite3.Error):
        store.facility_bulk_upsert(rows, "import", FAC_AT, overwrite_manual=False)
    assert store.facility_all() == []


def test_facility_table_has_no_patient_columns(store):
    with store._connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(facility)")}
    assert cols == {"hospcode", "name", "kind", "tmb", "lat", "lng", "source", "updated_at", "updated_by"}
