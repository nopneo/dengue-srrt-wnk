"""ส่งแจ้งเตือน LINE (เคสใหม่ · ยกเลิก · คืนสถานะ · cluster) และประเมิน cluster — ดูสเปกข้อ 5-6, 13

ตัวส่ง (sender) คือ callable `(messages: list[dict]) -> None` ที่ยก exception เมื่อส่งไม่สำเร็จ
ทุกครั้งที่ส่งจะบันทึกผลลง alert_log; ฟังก์ชันในโมดูลนี้ไม่ยก exception จากตัวส่งออกไปข้างนอก
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Callable

from config import Settings
from flexmsg import cancel_message, cluster_message, new_case_message, reactivate_message
from geo import find_clusters, find_village_clusters
from moph_notify import NotifyError
from store import Store

# ล้มเหลวติดกัน < MAX_QUICK_ATTEMPTS ลองซ้ำทุกรอบ; ถึงแล้วรอ RETRY_COOLDOWN ต่อครั้ง
# จนครบ MAX_TOTAL_ATTEMPTS จึงเลิก (LINE ล่มไม่กี่นาทีต้องไม่ทำให้แจ้งเตือนหาย)
MAX_QUICK_ATTEMPTS = 5
MAX_TOTAL_ATTEMPTS = 20
RETRY_COOLDOWN = timedelta(minutes=30)

log = logging.getLogger(__name__)

# ทั้ง scheduler และเว็บเรียกฟังก์ชันสาธารณะ → serialize กันส่งซ้ำ (ไม่ re-entrant)
_LOCK = threading.Lock()

Sender = Callable[[list[dict]], None]
AreaLookup = Callable[[str], "str | None"]


# ---------------------------------------------------------------- ตัวช่วย

def _error_text(exc: Exception) -> str:
    """ข้อความ error สำหรับ alert_log: NotifyError ปลอดภัยอยู่แล้ว (ไม่มีคีย์)
    exception ชนิดอื่นเก็บเฉพาะชื่อชนิด กันคีย์/ข้อมูลหลุดลง log"""
    if isinstance(exc, NotifyError):
        return str(exc)
    return type(exc).__name__


def _response_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _eligible(failures: int, last_failure: datetime | None, now: datetime) -> bool:
    """ลองส่งได้หรือไม่: ล้มเหลวติดกัน < 5 ครั้ง หรือ (< 20 ครั้ง และพ้น cooldown แล้ว)"""
    if failures < MAX_QUICK_ATTEMPTS:
        return True
    if failures >= MAX_TOTAL_ATTEMPTS:
        return False
    return last_failure is None or now - last_failure >= RETRY_COOLDOWN


def health(store: Store, settings: Settings | None = None) -> dict:
    """{"failing", "given_up"} — จำนวนรายการที่ส่ง LINE ไม่สำเร็จ / เลิกส่งแล้ว (ใช้กับแถบเตือนเว็บและ /healthz)
    ส่ง settings = นับเฉพาะโรคที่เปิดอยู่ (รายเคส: เฉพาะโหมด per_case_pin) ไม่งั้นนับทุกโรค"""
    if settings is None:
        return store.alert_health(max_total=MAX_TOTAL_ATTEMPTS)
    rules = settings.enabled_rules()
    return store.alert_health(max_total=MAX_TOTAL_ATTEMPTS,
                              case_diseases=tuple(r.key for r in rules if r.mode == "per_case_pin"),
                              cluster_diseases=tuple(r.key for r in rules))


def _gate(store: Store, now: datetime, kind: str, label: str, **key) -> bool:
    failures, last = store.alert_failures(kind, **key)
    if failures >= MAX_TOTAL_ATTEMPTS:
        log.warning("alert %s %s: ล้มเหลว %d ครั้ง เลิกส่ง", kind, label, failures)
        return False
    return _eligible(failures, last, now)


def _lookup(area_lookup: AreaLookup | None, code: str | None) -> str | None:
    """เรียก area_lookup แบบไม่ยอมให้ล้ม; คืน None เมื่อไม่มี/ว่าง/ผิดพลาด"""
    if area_lookup is None or not code:
        return None
    try:
        label = area_lookup(code)
    except Exception:
        return None
    return label if isinstance(label, str) and label.strip() else None


def _moo_label(code: str | None) -> str | None:
    if isinstance(code, str) and len(code) == 8 and code.isascii() and code.isdigit():
        moo = int(code[6:8])
        if moo > 0:
            return f"หมู่ {moo}"
    return None


def region_label(cases: list[dict], area_lookup: AreaLookup | None = None) -> str:
    """ชื่อบริเวณจากรหัสหมู่บ้านที่พบบ่อยที่สุดในกลุ่ม (เท่ากัน = รหัสน้อยกว่า)
    area_lookup(code) ถ้าได้ข้อความ → ใช้; ไม่ได้ → `หมู่ N`; ไม่มีรหัส → `-`"""
    counts = Counter(c.get("village_code") for c in cases if c.get("village_code"))
    if not counts:
        return "-"
    code = min(counts, key=lambda k: (-counts[k], k))
    return _lookup(area_lookup, code) or _moo_label(code) or "-"


def _safe(fn, *args, **kw) -> bool:
    """เรียกฟังก์ชันบันทึกลง DB โดยไม่ให้ล้มทั้งชุด (ส่งแบบ at-least-once: ถ้า process ดับ
    ระหว่างส่งกับบันทึก รอบหน้าอาจส่งซ้ำ ดีกว่าทำให้แจ้งเตือนหาย)"""
    try:
        fn(*args, **kw)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("บันทึก alert_log ไม่สำเร็จ (%s)", type(exc).__name__)
        return False


def _try_send(store: Store, sender: Sender, messages: list[dict], kind: str,
              case_id: int) -> bool:
    """ส่งข้อความรายเคสหนึ่งครั้ง + บันทึก alert_log; True เมื่อส่งสำเร็จ ไม่ยก exception"""
    try:
        sender(messages)
    except Exception as exc:  # noqa: BLE001 — ตัวส่งจริงยกได้หลายชนิด
        _safe(store.log_alert, kind, case_id, None, False, _response_code(exc),
              _error_text(exc))
        return False
    _safe(store.log_alert, kind, case_id, None, True, None, None)
    return True


# ---------------------------------------------------------------- เคสใหม่ / ยกเลิก

def _with_area(case: dict, area_lookup: AreaLookup | None) -> dict:
    label = _lookup(area_lookup, case.get("village_code"))
    if label:
        case["area_label"] = label
    return case


def send_pending(store: Store, sender: Sender, settings: Settings,
                 area_lookup: AreaLookup | None = None,
                 now: datetime | None = None) -> int:
    """ส่ง "เคสใหม่" ให้เคส active ที่ยังไม่เคยแจ้งสำเร็จ, "ยกเลิก" ให้เคส cancelled ที่เคยแจ้งแล้ว
    แต่ยังไม่แจ้งยกเลิก และ "คืนสถานะ" ให้เคส active ที่เคยแจ้งยกเลิกสำเร็จแล้วแต่ยังไม่เคยแจ้งคืนสถานะ
    (เก่าสุดก่อน; นโยบายลองซ้ำดู _eligible) คืนจำนวนที่ส่งสำเร็จ

    ยกเลิก/คืนสถานะส่งอย่างละครั้งต่อเคส "ตลอดไป": เคสที่ถูกยกเลิก-คืนสถานะวนซ้ำเป็นรอบที่สอง
    จะไม่มีข้อความเพิ่ม (กัน LINE ท่วมจากรหัสที่แพทย์แก้ไปมา) และเคสที่คืนสถานะไม่ได้รับ "เคสใหม่" ซ้ำ"""
    now = now or datetime.now()
    # แจ้งรายเคสเฉพาะโรคที่เปิดอยู่และเป็นโหมด per_case_pin (โรคโหมด cluster เก็บเคสเงียบ ๆ แจ้งเฉพาะกลุ่มก้อน)
    per_case = {r.key for r in settings.enabled_rules() if r.mode == "per_case_pin"}
    with _LOCK:
        sent = 0
        for case in sorted(store.active_cases(), key=lambda c: (c["dx_at"], c["id"])):
            if case["disease"] not in per_case:
                continue
            cid = case["id"]
            if not store.alert_sent("new", case_id=cid):
                if not _gate(store, now, "new", f"case {cid}", case_id=cid):
                    continue
                messages = new_case_message(_with_area(case, area_lookup), settings.base_url)
                if _try_send(store, sender, messages, "new", cid):
                    sent += 1
                continue
            # เคยแจ้งใหม่แล้ว: ถ้าเคยแจ้งยกเลิกสำเร็จแต่ตอนนี้กลับมา active ให้แจ้งคืนสถานะครั้งเดียว
            if (not store.alert_sent("cancel", case_id=cid)
                    or store.alert_sent("reactivate", case_id=cid)):
                continue
            if not _gate(store, now, "reactivate", f"case {cid}", case_id=cid):
                continue
            messages = reactivate_message(_with_area(case, area_lookup), settings.base_url)
            if _try_send(store, sender, messages, "reactivate", cid):
                sent += 1

        cancelled = sorted(store.list_cases(date.min, status="cancelled"),
                           key=lambda c: (c["dx_at"], c["id"]))
        for case in cancelled:
            cid = case["id"]
            if case["disease"] not in per_case:
                continue
            if (not store.alert_sent("new", case_id=cid)
                    or store.alert_sent("cancel", case_id=cid)):
                continue
            if not _gate(store, now, "cancel", f"case {cid}", case_id=cid):
                continue
            messages = cancel_message(_with_area(case, area_lookup), settings.base_url)
            if _try_send(store, sender, messages, "cancel", cid):
                sent += 1
        return sent


# ---------------------------------------------------------------- cluster

class Alerted(list):
    """รายการ cluster id ที่แจ้งสำเร็จ (เทียบเท่า list ธรรมดา) + `deferred` = True เมื่อไม่ได้ประเมินเลย
    เพราะล็อกแจ้งเตือนไม่ว่างและผู้เรียกสั่ง wait=False"""

    deferred = False


def evaluate_clusters(store: Store, sender: Sender, settings: Settings,
                      area_lookup: AreaLookup | None = None,
                      now: datetime | None = None, *, wait: bool = True) -> Alerted:
    """หา cluster จากหมุดที่ยืนยันแล้ว เทียบกับ cluster เดิม แล้วแจ้งกลุ่มใหม่/กลุ่มที่ขยาย
    กลุ่มที่ซ้อน (ใช้สมาชิกร่วมกัน) กับ cluster เดิม = กลุ่มเดียวกัน (หลายอัน → id ต่ำสุด)
    งบลองซ้ำนับต่อ (cluster, ขนาดที่จะแจ้ง) จึงเริ่มใหม่เมื่อกลุ่มโต
    คืน cluster id ที่แจ้งสำเร็จในรอบนี้

    wait=False: ถ้าล็อกแจ้งเตือนถูกถือโดยผู้อื่น (เช่น scheduler กำลังส่ง LINE ช้า) คืนรายการว่างที่ติด
    `.deferred = True` ทันทีโดยไม่ประเมินอะไร — เว็บใช้แบบนี้เพื่อไม่ค้างคำขอ; scheduler ประเมินทุกรอบอยู่แล้ว"""
    if not _LOCK.acquire(blocking=wait):
        out = Alerted()
        out.deferred = True
        return out
    try:
        return _evaluate_locked(store, sender, settings, area_lookup, now or datetime.now())
    finally:
        _LOCK.release()


def _evaluate_locked(store: Store, sender: Sender, settings: Settings,
                     area_lookup: AreaLookup | None, now: datetime) -> Alerted:
    alerted = Alerted()
    for rule in settings.enabled_rules():
        _evaluate_rule(store, sender, settings, rule, area_lookup, now, alerted)
    return alerted


def _evaluate_rule(store: Store, sender: Sender, settings: Settings, rule,
                   area_lookup: AreaLookup | None, now: datetime, alerted: Alerted) -> None:
    """ประเมิน cluster ของหนึ่งโรค — pin_radius: หมุดยืนยันแล้วในรัศมี · village: เคส active หมู่บ้านเดียวกัน."""
    if rule.group_by == "village":
        cases = store.active_cases(rule.key)
        groups = find_village_clusters(cases, rule.days, rule.min_cases)
    else:
        cases = store.pinned_cases(disease=rule.key)
        groups = find_clusters(cases, rule.radius_m, rule.days, rule.min_cases)
    by_id = {c["id"]: c for c in cases}
    groups.sort(key=lambda g: (min(by_id[i]["dx_at"] for i in g), min(g)))

    known = {c["id"]: c for c in store.clusters(rule.key)}
    for group in groups:
        members = sorted(group)
        dxs = [by_id[i]["dx_at"] for i in members]
        first, last = min(dxs), max(dxs)

        overlap = sorted(cid for cid, c in known.items()
                         if group & set(c["member_case_ids"]))
        if overlap:
            cid = overlap[0]
            row = known[cid]
            if (sorted(row["member_case_ids"]), row["first_dx_at"],
                    row["last_dx_at"]) != (members, first, last):
                store.update_cluster(cid, members, first, last)
                row.update(member_case_ids=members, first_dx_at=first, last_dx_at=last)
        else:
            cid = store.save_cluster(members, first, last, disease=rule.key)
            row = {"id": cid, "member_case_ids": members, "first_dx_at": first,
                   "last_dx_at": last, "alerted_size": 0}
            known[cid] = row

        size = len(members)
        if size <= row["alerted_size"]:
            continue
        if not _gate(store, now, "cluster", f"cluster {cid} size {size}",
                     cluster_id=cid, size=size):
            continue
        label = region_label([by_id[i] for i in members], area_lookup)
        messages = cluster_message(
            size, label, first, last, rule.min_cases, cid,
            settings.base_url, updated=row["alerted_size"] > 0,
            radius_m=rule.radius_m, days=rule.days, disease_key=rule.key, group_by=rule.group_by)
        try:
            sender(messages)
        except Exception as exc:  # noqa: BLE001
            _safe(store.record_cluster_alert, cid, size, False,
                  _response_code(exc), _error_text(exc))
            continue
        if _safe(store.record_cluster_alert, cid, size, True, None, None):
            row["alerted_size"] = size
        alerted.append(cid)
