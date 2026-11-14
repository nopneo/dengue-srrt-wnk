import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta

import alerts
from alerts import evaluate_clusters, region_label, send_pending
from config import load_settings
from fakes import FakeSender
from moph_notify import NotifyError

DX = datetime(2026, 11, 8, 9, 0)
LAT, LNG = 14.5, 101.8
NEAR = 0.0005  # ~55 ม. (อยู่ในรัศมี 100 ม.)
GAP = 0.002    # ~222 ม. (ห่างเกินรัศมี)


def settings(**env):
    base = {"DENGUE_START_FROM": "2026-11-01"}
    base.update(env)
    return load_settings(base)


def add(store, key, dx_at=DX, village="40010103", icd10="A90", hn=None, house="12"):
    return store.add_case(key, "OPD", hn or "HN" + key, icd10, dx_at, village, house, "1",
                          30, True)


def pin(store, case_id, state="confirmed", dlat=0.0):
    store.set_pin(case_id, LAT + dlat, LNG, state, "manual", "tester")


def text_of(messages) -> str:
    return json.dumps(messages, ensure_ascii=False)


def log_rows(store, **where):
    return store.alerts(**where)


# ---------------------------------------------------------------- เคสใหม่ / ยกเลิก

def test_new_case_sent_once(store):
    a = add(store, "1", DX)
    b = add(store, "2", DX - timedelta(hours=3))  # เก่ากว่า ต้องส่งก่อน
    sender = FakeSender()
    assert send_pending(store, sender, settings()) == 2
    assert len(sender.calls) == 2
    assert f"/case/{b}" in text_of(sender.calls[0])
    assert f"/case/{a}" in text_of(sender.calls[1])
    assert store.alert_sent("new", case_id=a) and store.alert_sent("new", case_id=b)
    assert send_pending(store, sender, settings()) == 0
    assert len(sender.calls) == 2


def test_failed_send_retries_up_to_five_attempts(store):
    a = add(store, "1")
    sender = FakeSender(error=NotifyError("ส่งไม่ได้"))
    for _ in range(8):
        assert send_pending(store, sender, settings()) == 0
    assert len(sender.calls) == 5
    assert store.alert_attempts("new", case_id=a) == 5
    assert store.alert_sent("new", case_id=a) is False


def test_failure_does_not_stop_other_cases_or_raise(store):
    a = add(store, "1", DX - timedelta(hours=1))
    b = add(store, "2", DX)
    calls = []

    def flaky(messages):
        calls.append(messages)
        if len(calls) == 1:
            raise RuntimeError("ล่ม")

    assert send_pending(store, flaky, settings()) == 1
    assert store.alert_sent("new", case_id=a) is False
    assert store.alert_sent("new", case_id=b) is True


def test_cancel_sent_only_if_case_was_alerted(store):
    alerted = add(store, "1", DX - timedelta(hours=2))
    silent = add(store, "2", DX)
    store.mark_cancelled(silent)  # ถูกยกเลิกก่อนเคยถูกแจ้ง
    sender = FakeSender()
    assert send_pending(store, sender, settings()) == 1  # แจ้งเฉพาะเคสที่ยัง active
    assert store.alert_sent("new", case_id=silent) is False
    store.mark_cancelled(alerted)
    assert send_pending(store, sender, settings()) == 1
    assert "ยกเลิกเคสไข้เลือดออก" in text_of(sender.calls[-1])
    assert store.alert_sent("cancel", case_id=alerted) is True
    assert store.alert_attempts("cancel", case_id=silent) == 0
    assert send_pending(store, sender, settings()) == 0  # ไม่แจ้งยกเลิกซ้ำ
    assert len(sender.calls) == 2


def test_cancel_retry_is_capped(store):
    a = add(store, "1")
    send_pending(store, FakeSender(), settings())
    store.mark_cancelled(a)
    sender = FakeSender(error=NotifyError("x"))
    for _ in range(7):
        send_pending(store, sender, settings())
    assert len(sender.calls) == 5
    assert store.alert_attempts("cancel", case_id=a) == 5


def test_alert_log_records_error_text(store):
    a = add(store, "1")
    send_pending(store, FakeSender(error=NotifyError("Unauthorized — คีย์ไม่ถูกต้อง")), settings())
    send_pending(store, FakeSender(error=RuntimeError("secret-key=abc123")), settings())
    rows = log_rows(store, case_id=a)
    assert [r["kind"] for r in rows] == ["new", "new"]
    assert [r["ok"] for r in rows] == [False, False]
    assert rows[0]["error"] == "Unauthorized — คีย์ไม่ถูกต้อง"
    assert "abc123" not in rows[1]["error"] and "RuntimeError" in rows[1]["error"]
    send_pending(store, FakeSender(), settings())
    last = log_rows(store, case_id=a)[-1]
    assert last["ok"] is True and last["error"] is None and last["response_code"] is None


def test_area_lookup_label_is_used_and_failures_ignored(store):
    add(store, "1", village="40010103")
    sender = FakeSender()
    assert send_pending(store, sender, settings(),
                        area_lookup=lambda code: f"ต.ทดสอบ ม.9 ({code})") == 1
    assert "ต.ทดสอบ ม.9 (40010103)" in text_of(sender.calls[0])

    add(store, "2", village="40010105")

    def boom(code):
        raise RuntimeError("lookup ล่ม")

    sender2 = FakeSender()
    assert send_pending(store, sender2, settings(), area_lookup=boom) == 1
    assert "หมู่ 5" in text_of(sender2.calls[0])


# ---------------------------------------------------------------- region_label

def test_region_label_uses_most_frequent_village():
    cases = [{"village_code": "40010103"}, {"village_code": "40010107"},
             {"village_code": "40010107"}, {"village_code": None}]
    assert region_label(cases) == "หมู่ 7"
    assert region_label(cases, lambda c: "ต.ก ม.7") == "ต.ก ม.7"
    assert region_label(cases, lambda c: "") == "หมู่ 7"
    assert region_label(cases, lambda c: None) == "หมู่ 7"

    def boom(code):
        raise RuntimeError("x")

    assert region_label(cases, boom) == "หมู่ 7"
    assert region_label([{"village_code": None}, {}]) == "-"
    assert region_label([]) == "-"
    assert region_label([{"village_code": "abc"}]) == "-"


# ---------------------------------------------------------------- cluster

def two_confirmed(store):
    a = add(store, "1", DX - timedelta(days=2))
    b = add(store, "2", DX)
    pin(store, a)
    pin(store, b, dlat=NEAR)
    return a, b


def test_cluster_alert_once_per_group(store):
    a, b = two_confirmed(store)
    sender = FakeSender()
    ids = evaluate_clusters(store, sender, settings())
    [row] = store.clusters()
    assert ids == [row["id"]]
    assert sorted(row["member_case_ids"]) == [a, b]
    assert row["alerted_size"] == 2
    assert len(sender.calls) == 1
    body = text_of(sender.calls[0])
    assert "พบกลุ่มผู้ป่วยไข้เลือดออก" in body
    assert f"cluster={row['id']}" in body
    assert "หมู่ 3" in body
    assert evaluate_clusters(store, sender, settings()) == []
    assert len(sender.calls) == 1
    assert len(store.clusters()) == 1
    assert store.alert_sent("cluster", cluster_id=row["id"])


def test_cluster_growth_sends_updated_message_once(store):
    a, b = two_confirmed(store)
    sender = FakeSender()
    [cid] = evaluate_clusters(store, sender, settings())
    c = add(store, "3", DX + timedelta(days=1))
    pin(store, c, dlat=2 * NEAR)
    assert evaluate_clusters(store, sender, settings()) == [cid]
    assert len(sender.calls) == 2
    assert "กลุ่มผู้ป่วยไข้เลือดออกเพิ่มขึ้น" in text_of(sender.calls[1])
    [row] = store.clusters()
    assert sorted(row["member_case_ids"]) == [a, b, c]
    assert row["alerted_size"] == 3 and row["id"] == cid
    assert row["last_dx_at"] == DX + timedelta(days=1)
    assert evaluate_clusters(store, sender, settings()) == []
    assert len(sender.calls) == 2


def test_estimated_pins_never_trigger_cluster_alert(store):
    a = add(store, "1", DX - timedelta(days=1))
    b = add(store, "2", DX)
    c = add(store, "3", DX)
    for cid in (a, b, c):
        pin(store, cid, state="estimated")
    sender = FakeSender()
    assert evaluate_clusters(store, sender, settings()) == []
    assert sender.calls == [] and store.clusters() == []
    # ยืนยันหมุดแค่ 1 ใน 3 ก็ยังไม่ถึงเกณฑ์ 2 ราย
    pin(store, a, state="confirmed")
    assert evaluate_clusters(store, sender, settings()) == []
    assert sender.calls == [] and store.clusters() == []


def test_cancelled_case_does_not_count_for_cluster(store):
    a, b = two_confirmed(store)
    store.mark_cancelled(b)
    sender = FakeSender()
    assert evaluate_clusters(store, sender, settings()) == []
    assert sender.calls == []


def test_failed_cluster_send_is_retried_and_not_marked_alerted(store):
    two_confirmed(store)
    failing = FakeSender(error=NotifyError("ล่ม"))
    assert evaluate_clusters(store, failing, settings()) == []
    [row] = store.clusters()
    assert row["alerted_size"] == 0
    assert store.alert_attempts("cluster", cluster_id=row["id"]) == 1
    ok = FakeSender()
    assert evaluate_clusters(store, ok, settings()) == [row["id"]]
    assert "พบกลุ่มผู้ป่วยไข้เลือดออก" in text_of(ok.calls[0])  # ยังไม่เคยแจ้ง = ไม่ใช่ updated
    assert "เพิ่มขึ้น" not in text_of(ok.calls[0])
    assert store.clusters()[0]["alerted_size"] == 2
    assert len(store.clusters()) == 1


def test_cluster_retry_is_capped_at_five_failures(store):
    two_confirmed(store)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(8):
        evaluate_clusters(store, failing, settings())
    assert len(failing.calls) == 5
    [row] = store.clusters()
    assert store.alert_attempts("cluster", cluster_id=row["id"]) == 5


def test_cluster_cap_resets_after_success_for_next_growth(store):
    two_confirmed(store)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(3):
        evaluate_clusters(store, failing, settings())
    assert evaluate_clusters(store, FakeSender(), settings()) != []
    c = add(store, "3", DX + timedelta(days=1))
    pin(store, c, dlat=2 * NEAR)
    for _ in range(7):
        evaluate_clusters(store, failing, settings())
    assert len(failing.calls) == 3 + 5  # หลังสำเร็จ งบ 5 ครั้งเริ่มนับใหม่


def test_overlapping_group_keeps_lowest_cluster_id(store):
    a1 = add(store, "1")
    a2 = add(store, "2")
    b1 = add(store, "3")
    b2 = add(store, "4")
    pin(store, a1)
    pin(store, a2, dlat=NEAR)
    pin(store, b1, dlat=GAP)
    pin(store, b2, dlat=GAP + NEAR)
    sender = FakeSender()
    low, high = evaluate_clusters(store, sender, settings())
    assert low < high and len(sender.calls) == 2
    # เคสสะพานเชื่อมสองกลุ่มเป็นกลุ่มเดียว: ใช้ cluster id ต่ำสุด
    bridge = add(store, "5")
    pin(store, bridge, dlat=0.0012)
    assert evaluate_clusters(store, sender, settings()) == [low]
    rows = {r["id"]: r for r in store.clusters()}
    assert sorted(rows[low]["member_case_ids"]) == sorted([a1, a2, b1, b2, bridge])
    assert rows[low]["alerted_size"] == 5
    assert len(sender.calls) == 3
    assert "กลุ่มผู้ป่วยไข้เลือดออกเพิ่มขึ้น" in text_of(sender.calls[-1])
    assert high in rows  # แถวเดิมไม่ถูกลบ
    assert evaluate_clusters(store, sender, settings()) == []


def test_cluster_message_uses_area_lookup_and_settings(store):
    two_confirmed(store)
    sender = FakeSender()
    s = replace(settings(), cluster_radius_m=150, cluster_days=10)
    evaluate_clusters(store, sender, s, area_lookup=lambda code: "ต.วังน้ำเขียว ม.3")
    body = text_of(sender.calls[0])
    assert "ต.วังน้ำเขียว ม.3" in body
    assert "ในรัศมี 150 ม. ภายใน 10 วัน" in body


# ---------------------------------------------------------------- นโยบายลองซ้ำ (cooldown)

def later(minutes=31):
    """เวลา "ตอนนี้" ที่เลื่อนไปข้างหน้าจากเวลาจริงที่บันทึกใน alert_log"""
    return datetime.now() + timedelta(minutes=minutes)


def test_new_alert_quick_attempts_then_cooldown_then_retry(store):
    a = add(store, "1")
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(9):
        send_pending(store, failing, settings())
    assert len(failing.calls) == 5  # 5 ครั้งแรกติดกัน แล้วหยุดภายใน cooldown
    send_pending(store, failing, settings(), now=later(0))  # ยังไม่พ้น 30 นาที
    assert len(failing.calls) == 5
    send_pending(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 6
    send_pending(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 7  # พ้น cooldown อีกครั้ง (นับจากความล้มเหลวล่าสุด)
    ok = FakeSender()
    assert send_pending(store, ok, settings(), now=later(31)) == 1
    assert store.alert_failures("new", case_id=a) == (0, None)  # สำเร็จ = เริ่มนับใหม่


def test_new_alert_given_up_after_twenty_failures(store):
    a = add(store, "1")
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(40):
        send_pending(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 20
    assert store.alert_attempts("new", case_id=a) == 20


def test_cancel_follows_same_cooldown_policy(store):
    a = add(store, "1")
    send_pending(store, FakeSender(), settings())
    store.mark_cancelled(a)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(6):
        send_pending(store, failing, settings())
    assert len(failing.calls) == 5
    send_pending(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 6


def test_cluster_five_failures_stop_within_cooldown_and_retry_after(store):
    two_confirmed(store)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(8):
        evaluate_clusters(store, failing, settings())
    assert len(failing.calls) == 5
    evaluate_clusters(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 6
    ok = FakeSender()
    assert evaluate_clusters(store, ok, settings(), now=later(31)) != []
    assert store.clusters()[0]["alerted_size"] == 2


def test_cluster_given_up_after_twenty_failures(store):
    two_confirmed(store)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(40):
        evaluate_clusters(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 20


def test_cluster_growth_while_capped_gets_fresh_budget(store):
    two_confirmed(store)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(8):
        evaluate_clusters(store, failing, settings())
    assert len(failing.calls) == 5  # ขนาด 2 ติดเพดานชั่วคราว
    c = add(store, "3", DX + timedelta(days=1))
    pin(store, c, dlat=2 * NEAR)
    ok = FakeSender()
    [cid] = evaluate_clusters(store, ok, settings())  # โตเป็น 3 → งบใหม่ ส่งได้ทันที
    assert len(ok.calls) == 1 and store.clusters()[0]["alerted_size"] == 3
    assert store.alert_failures("cluster", cluster_id=cid, size=3) == (0, None)


# ---------------------------------------------------------------- ความทนทาน / ความเป็นส่วนตัว

def test_log_failure_does_not_abort_batch(store, monkeypatch):
    add(store, "1", DX - timedelta(hours=1))
    add(store, "2", DX)

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(store, "log_alert", boom)
    sender = FakeSender()
    assert send_pending(store, sender, settings()) == 2  # ส่งครบแม้บันทึกล้ม
    assert len(sender.calls) == 2


def test_cluster_record_failure_does_not_raise_and_is_resent(store, monkeypatch):
    two_confirmed(store)
    real = store.record_cluster_alert

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(store, "record_cluster_alert", boom)
    sender = FakeSender()
    assert evaluate_clusters(store, sender, settings()) != []
    assert store.clusters()[0]["alerted_size"] == 0  # ไม่ได้บันทึก → รอบหน้าส่งซ้ำ (at-least-once)
    monkeypatch.setattr(store, "record_cluster_alert", real)
    assert evaluate_clusters(store, sender, settings()) != []
    assert len(sender.calls) == 2


def test_concurrent_calls_do_not_send_duplicates(store):
    for k in range(3):
        add(store, str(k), DX - timedelta(hours=k))
    calls = []

    def slow(messages):
        time.sleep(0.05)
        calls.append(messages)

    threads = [threading.Thread(target=send_pending, args=(store, slow, settings()))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 3
    assert alerts._LOCK.locked() is False


def test_messages_contain_no_hn_or_house_number(store):
    a = add(store, "1", DX - timedelta(days=1), hn="HNSECRET9001", house="77/123")
    b = add(store, "2", DX, hn="HNSECRET9002", house="88/456")
    pin(store, a)
    pin(store, b, dlat=NEAR)
    sender = FakeSender()
    send_pending(store, sender, settings())
    evaluate_clusters(store, sender, settings())
    store.mark_cancelled(b)
    send_pending(store, sender, settings())
    assert len(sender.calls) == 4  # เคสใหม่ 2 + cluster + ยกเลิก
    body = text_of(sender.sent)
    for secret in ("HNSECRET", "9001", "9002", "77/123", "88/456"):
        assert secret not in body


def test_shrunk_group_does_not_realert_but_updates_members(store):
    a, b = two_confirmed(store)
    c = add(store, "3", DX + timedelta(days=1))
    pin(store, c, dlat=2 * NEAR)
    sender = FakeSender()
    [cid] = evaluate_clusters(store, sender, settings())
    assert store.clusters()[0]["alerted_size"] == 3 and len(sender.calls) == 1
    store.mark_cancelled(c)
    assert evaluate_clusters(store, sender, settings()) == []
    assert len(sender.calls) == 1
    [row] = store.clusters()
    assert row["id"] == cid and sorted(row["member_case_ids"]) == [a, b]
    assert row["alerted_size"] == 3


def test_given_up_alert_is_sent_again_after_rearm(store):
    cid = add(store, "R1")
    for _ in range(alerts.MAX_TOTAL_ATTEMPTS):
        store.log_alert("new", cid, None, False, 401, "NotifyError")
    sender = FakeSender()
    now = datetime.now() + timedelta(days=1)
    assert send_pending(store, sender, settings(), now=now) == 0 and not sender.calls
    assert store.rearm_alerts() == 1
    assert send_pending(store, sender, settings(), now=now) == 1
    assert len(sender.calls) == 1 and store.alert_sent("new", case_id=cid)
    assert store.alert_health(max_total=alerts.MAX_TOTAL_ATTEMPTS) == {"failing": 0, "given_up": 0}


def test_after_rearm_normal_retry_policy_restarts_from_zero(store):
    cid = add(store, "R2")
    for _ in range(alerts.MAX_TOTAL_ATTEMPTS):
        store.log_alert("new", cid, None, False, 401, "NotifyError")
    store.rearm_alerts()
    sender = FakeSender(error=NotifyError("down", 500))
    for _ in range(alerts.MAX_QUICK_ATTEMPTS):        # 5 ครั้งแรกลองทุกรอบ
        send_pending(store, sender, settings(), now=datetime.now())
    assert len(sender.calls) == alerts.MAX_QUICK_ATTEMPTS
    send_pending(store, sender, settings(), now=datetime.now())   # แล้วรอ cooldown 30 นาที
    assert len(sender.calls) == alerts.MAX_QUICK_ATTEMPTS
    later = datetime.now() + alerts.RETRY_COOLDOWN + timedelta(minutes=1)
    send_pending(store, sender, settings(), now=later)
    assert len(sender.calls) == alerts.MAX_QUICK_ATTEMPTS + 1


# ---------------------------------------------------------------- R24: คืนสถานะ / ข้อความยกเลิกระบุเคส

def _alerted_and_cancelled(store, sender):
    a = add(store, "1")
    send_pending(store, sender, settings())
    store.mark_cancelled(a)
    send_pending(store, sender, settings())
    assert store.alert_sent("cancel", case_id=a)
    return a


def test_cancel_message_carries_case_fields_and_link(store):
    a = add(store, "1")
    sender = FakeSender()
    send_pending(store, sender, settings())
    store.mark_cancelled(a)
    send_pending(store, sender, settings(DENGUE_BASE_URL="https://x.test/dengue"),
                 area_lookup=lambda code: "ต.ทดสอบ ม.3")
    text = text_of(sender.calls[-1])
    assert "ยกเลิกเคสไข้เลือดออก" in text and f"https://x.test/dengue/case/{a}" in text
    assert "ต.ทดสอบ ม.3" in text and "A90" in text and "HN1" not in text


def test_reactivated_case_gets_one_reactivate_message_and_no_second_new(store):
    sender = FakeSender()
    a = _alerted_and_cancelled(store, sender)
    assert store.reactivate_case(a)
    n = len(sender.calls)
    assert send_pending(store, sender, settings()) == 1
    assert len(sender.calls) == n + 1
    assert "คืนสถานะเคสไข้เลือดออก" in text_of(sender.calls[-1])
    assert f"/case/{a}" in text_of(sender.calls[-1]) and "HN1" not in text_of(sender.calls[-1])
    assert store.alert_sent("reactivate", case_id=a)
    assert store.alert_attempts("new", case_id=a) == 1       # ไม่มี "เคสใหม่" ซ้ำ
    assert send_pending(store, sender, settings()) == 0
    assert len(sender.calls) == n + 1


def test_second_cancel_reactivate_cycle_sends_no_further_message(store):
    sender = FakeSender()
    a = _alerted_and_cancelled(store, sender)
    store.reactivate_case(a)
    send_pending(store, sender, settings())
    n = len(sender.calls)
    store.mark_cancelled(a)
    assert send_pending(store, sender, settings()) == 0       # ยกเลิกรอบสอง ไม่ส่งซ้ำ
    store.reactivate_case(a)
    assert send_pending(store, sender, settings()) == 0       # คืนสถานะรอบสอง ไม่ส่งซ้ำ
    assert len(sender.calls) == n


def test_reactivated_before_cancel_was_sent_sends_nothing(store):
    a = add(store, "1")
    sender = FakeSender()
    send_pending(store, sender, settings())
    store.mark_cancelled(a)
    store.reactivate_case(a)                                  # ผู้รับไม่เคยเห็นยกเลิก
    n = len(sender.calls)
    assert send_pending(store, sender, settings()) == 0
    assert len(sender.calls) == n
    assert store.alert_attempts("reactivate", case_id=a) == 0 and not store.alert_sent("cancel", case_id=a)


def test_reactivate_message_follows_retry_policy(store):
    sender = FakeSender()
    a = _alerted_and_cancelled(store, sender)
    store.reactivate_case(a)
    failing = FakeSender(error=NotifyError("ล่ม"))
    for _ in range(8):
        send_pending(store, failing, settings())
    assert len(failing.calls) == 5 and store.alert_attempts("reactivate", case_id=a) == 5
    send_pending(store, failing, settings(), now=later(31))
    assert len(failing.calls) == 6


# ---------------------------------------------------------------- I1: wait=False ไม่บล็อก

def test_evaluate_clusters_nowait_returns_immediately_when_lock_busy(store):
    two_confirmed(store)
    sender = FakeSender()
    assert alerts._LOCK.acquire(timeout=1)
    try:
        t0 = time.monotonic()
        res = evaluate_clusters(store, sender, settings(), wait=False)
        assert time.monotonic() - t0 < 0.5
    finally:
        alerts._LOCK.release()
    assert res == [] and res.deferred is True
    assert sender.calls == [] and store.clusters() == []        # ไม่ได้ประเมินอะไรเลย
    # ล็อกว่างแล้ว: wait=False ทำงานปกติและปล่อยล็อกคืน
    res = evaluate_clusters(store, sender, settings(), wait=False)
    assert len(res) == 1 and res.deferred is False and len(sender.calls) == 1
    assert alerts._LOCK.locked() is False


def test_evaluate_clusters_default_waits_for_lock(store):
    two_confirmed(store)
    sender = FakeSender()
    assert alerts._LOCK.acquire(timeout=1)
    out = []
    t = threading.Thread(target=lambda: out.append(evaluate_clusters(store, sender, settings())))
    t.start()
    time.sleep(0.2)
    assert out == [] and t.is_alive()                          # wait=True (ค่าเริ่มต้น) รอล็อก
    alerts._LOCK.release()
    t.join(timeout=5)
    assert len(out) == 1 and len(out[0]) == 1 and out[0].deferred is False
