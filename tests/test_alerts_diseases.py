"""ข้อความ LINE และการแจ้งเตือนตามชนิด/โหมดของโรค (สเปกข้อ 3, 4.4)"""
import json
import re
from datetime import datetime, timedelta

from alerts import evaluate_clusters, send_pending
from config import load_settings
from fakes import FakeSender
from flexmsg import cancel_message, cluster_message, new_case_message, reactivate_message

DX = datetime(2026, 11, 8, 9, 0)
BASE = "https://wnkhos.go.th/dengue"
HN = "0123456"


def settings(diseases="dengue,hfmd", **env):
    base = {"DENGUE_START_FROM": "2026-11-01", "DENGUE_DISEASES": diseases}
    base.update(env)
    return load_settings(base)


def add(store, key, disease="dengue", icd="A90", dx=DX, village="40010103", hn=None):
    return store.add_case(key, "OPD", hn or "HN" + key, icd, dx, village, "12", "1", 8, True, disease=disease)


def text_of(messages) -> str:
    return json.dumps(messages, ensure_ascii=False)


def _case(**over):
    case = {"id": 42, "visit_type": "OPD", "hn": HN, "icd10": "B084", "dx_at": DX, "village_code": "30200307",
            "sex": "1", "age_years": 4, "in_area": True, "pin_state": "none", "disease": "hfmd"}
    case.update(over)
    return case


# ---------------------------------------------------------------- flexmsg

def test_dengue_titles_unchanged_when_disease_missing_or_dengue():
    for case in (_case(icd10="A911", disease="dengue"), {k: v for k, v in _case(icd10="A911").items() if k != "disease"}):
        assert "สงสัยไข้เลือดออก" in text_of(new_case_message(case, BASE))
        assert "ยกเลิกเคสไข้เลือดออก" in text_of(cancel_message(case, BASE))
        assert "คืนสถานะเคสไข้เลือดออก" in text_of(reactivate_message(case, BASE))


def test_other_disease_case_messages_use_its_name_and_color():
    new = text_of(new_case_message(_case(), BASE))
    assert "สงสัยมือเท้าปาก" in new and "ไข้เลือดออก" not in new
    assert "#ec4899" in new and "#be185d" in new
    assert "ยกเลิกเคสมือเท้าปาก" in text_of(cancel_message(_case(), BASE))
    assert "มือเท้าปาก" in text_of(reactivate_message(_case(), BASE))
    assert "ไม่ใช่มือเท้าปากแล้ว" in text_of(cancel_message(_case(), BASE))


def test_cluster_message_dengue_defaults_unchanged():
    msg = text_of(cluster_message(3, "หมู่ 3", DX, DX, 2, 7, BASE))
    assert "พบกลุ่มผู้ป่วยไข้เลือดออก" in msg and "ในรัศมี 100 ม. ภายใน 14 วัน" in msg
    assert "#ff4d5e" in msg                                    # หัวแดงเดิม


def test_cluster_message_village_mode_has_no_radius_and_uses_disease():
    msg = text_of(cluster_message(3, "หมู่ 3", DX, DX, 2, 7, BASE, radius_m=100, days=7,
                                  disease_key="hfmd", group_by="village"))
    assert "พบกลุ่มผู้ป่วยมือเท้าปาก" in msg and "ในหมู่บ้านเดียวกัน ภายใน 7 วัน" in msg
    assert "รัศมี" not in msg and "#ec4899" in msg
    upd = text_of(cluster_message(4, "หมู่ 3", DX, DX, 2, 7, BASE, updated=True, days=7,
                                  disease_key="hfmd", group_by="village"))
    assert "กลุ่มผู้ป่วยมือเท้าปากเพิ่มขึ้น" in upd


def test_no_patient_identifiers_in_any_disease_message():
    for key in ("dengue", "hfmd", "lepto", "diarrhea"):
        msgs = [new_case_message(_case(disease=key), BASE), cancel_message(_case(disease=key), BASE),
                reactivate_message(_case(disease=key), BASE),
                cluster_message(3, "หมู่ 3", DX, DX, 2, 7, BASE, disease_key=key)]
        for m in msgs:
            assert HN not in text_of(m)


# ---------------------------------------------------------------- send_pending

def test_per_case_alert_only_for_per_case_pin_diseases(store):
    d = add(store, "1")
    h = add(store, "2", "hfmd", "B084")
    sender = FakeSender()
    assert send_pending(store, sender, settings()) == 1
    assert store.alert_sent("new", case_id=d) and not store.alert_sent("new", case_id=h)
    assert "สงสัยไข้เลือดออก" in text_of(sender.calls[0])


def test_cluster_mode_cancel_and_reactivate_are_never_sent(store):
    h = add(store, "2", "hfmd", "B084")
    store.mark_cancelled(h)
    sender = FakeSender()
    assert send_pending(store, sender, settings()) == 0 and sender.calls == []


def test_disabled_disease_cases_are_not_alerted(store):
    add(store, "1", "lepto", "A27")                    # โรคปิดอยู่ (ค่าเริ่มต้น)
    sender = FakeSender()
    assert send_pending(store, sender, settings()) == 0 and sender.calls == []
    assert send_pending(store, sender, settings(diseases="dengue,lepto")) == 1
    assert "สงสัยเล็ปโตสไปโรสิส" in text_of(sender.calls[0])


# ---------------------------------------------------------------- evaluate_clusters

def test_village_cluster_alerts_once_then_on_growth(store):
    add(store, "1", "hfmd", "B084", DX)
    sender = FakeSender()
    assert list(evaluate_clusters(store, sender, settings(), now=DX)) == []          # 1 ราย ยังไม่ถึงเกณฑ์
    add(store, "2", "hfmd", "B084", DX + timedelta(days=2))
    assert len(evaluate_clusters(store, sender, settings(), now=DX)) == 1
    assert len(sender.calls) == 1
    msg = text_of(sender.calls[0])
    assert "พบกลุ่มผู้ป่วยมือเท้าปาก" in msg and "ในหมู่บ้านเดียวกัน" in msg
    assert evaluate_clusters(store, sender, settings(), now=DX) == [] and len(sender.calls) == 1
    add(store, "3", "hfmd", "B084", DX + timedelta(days=3))
    assert len(evaluate_clusters(store, sender, settings(), now=DX)) == 1
    assert "กลุ่มผู้ป่วยมือเท้าปากเพิ่มขึ้น" in text_of(sender.calls[1])
    (cl,) = store.clusters("hfmd")
    assert cl["disease"] == "hfmd" and store.clusters("dengue") == []


def test_village_cluster_needs_no_pins_and_ignores_other_villages_and_diseases(store):
    add(store, "1", "hfmd", "B084", DX, village="40010103")
    add(store, "2", "hfmd", "B084", DX, village="40010104")          # หมู่บ้านอื่น
    add(store, "3", "dengue", "A90", DX, village="40010103")          # โรคอื่น หมู่บ้านเดียวกัน
    sender = FakeSender()
    assert evaluate_clusters(store, sender, settings(), now=DX) == [] and sender.calls == []


def test_village_cluster_threshold_comes_from_rule(store):
    for i in range(4):
        add(store, str(i), "diarrhea", "A09", DX + timedelta(hours=i))
    s = settings(diseases="diarrhea")
    sender = FakeSender()
    assert evaluate_clusters(store, sender, s, now=DX) == []         # เกณฑ์ ≥5 / 3 วัน
    add(store, "9", "diarrhea", "A09", DX + timedelta(hours=5))
    assert len(evaluate_clusters(store, sender, s, now=DX)) == 1
    assert "อุจจาระร่วง" in text_of(sender.calls[0])


def test_pin_radius_cluster_is_per_disease(store):
    a = add(store, "1", "dengue", "A90")
    b = add(store, "2", "dengue", "A90", hn="HN2")
    c = add(store, "3", "lepto", "A27", hn="HN3")
    for cid in (a, b, c):
        store.set_pin(cid, 14.5, 101.8, "confirmed", "manual", "t")
    sender = FakeSender()
    out = evaluate_clusters(store, sender, settings(diseases="dengue,lepto"), now=DX)
    assert len(out) == 1                                              # dengue 2 ราย; lepto มีรายเดียว
    assert "ไข้เลือดออก" in text_of(sender.calls[0]) and "ในรัศมี 100 ม." in text_of(sender.calls[0])
    assert [x["disease"] for x in store.clusters()] == ["dengue"]


def test_cancelled_village_member_does_not_count_or_resend(store):
    a = add(store, "1", "hfmd", "B084", DX)
    add(store, "2", "hfmd", "B084", DX + timedelta(days=1))
    sender = FakeSender()
    evaluate_clusters(store, sender, settings(), now=DX)
    store.mark_cancelled(a)
    assert evaluate_clusters(store, sender, settings(), now=DX) == [] and len(sender.calls) == 1


def test_disabled_disease_clusters_are_not_evaluated(store):
    add(store, "1", "hfmd", "B084", DX)
    add(store, "2", "hfmd", "B084", DX)
    sender = FakeSender()
    assert evaluate_clusters(store, sender, settings(diseases="dengue"), now=DX) == [] and sender.calls == []
