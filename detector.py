"""ตัวตรวจจับ: หนึ่งรอบ = ดึงวินิจฉัยใหม่ -> สร้างเคส/นับตามอาการ -> ตรวจเคสที่รหัสหาย (สเปกข้อ 5).

ลำดับ: อ่านจาก HOSxP ให้ครบก่อน (วินิจฉัยใหม่, อำเภอในเขต, เคสที่ยังมีวินิจฉัยอยู่) แล้วค่อยเขียนลงฐานของแอป
ถ้าการอ่านเหล่านั้นล้มด้วย HosxpError จะคืน result.error และไม่แก้เคสใดเลย (ไม่สร้าง ไม่ยกเลิก ไม่แตะตัวนับ)
ยกเว้นพิกัดกลางหมู่บ้าน: เป็นข้อมูลเสริม ถ้าอ่านไม่ได้ก็ยังสร้างเคสแต่ไม่มีหมุด (บันทึก warning)

การยกเลิกเคส (R24) — คำตอบที่ "สำเร็จแต่ว่าง/ไม่ครบ" ของ HOSxP (replica กำลัง reload, ต่อผิดฐาน,
แพทย์ลบแล้วเพิ่มรหัสใหม่คนละรอบ) ต้องไม่ล้างเคสทั้งหมด:
  1. debounce: รหัสต้องหายติดกัน CANCEL_AFTER_ABSENT_CYCLES รอบ (นับใน case.absent_cycles)
     เจอรหัสเมื่อไรตัวนับกลับเป็น 0; แถวที่ new_dx คืนในรอบนี้นับว่ามีรหัสทันที
  2. guard: ถ้ารอบนี้จะยกเลิก >= 2 เคส และมากกว่าครึ่งของเคส active ในช่วงตรวจ = ผิดปกติ
     ไม่ยกเลิกอะไรเลยในรอบนั้น (ตัวนับยังเพิ่ม) และตั้ง result.error ให้ /healthz เห็น; ประเมินใหม่ทุกรอบ
  3. เคสที่ถูกยกเลิกแล้วแต่รหัสของ visit นั้นกลับมาใน new_dx จะถูกคืนสถานะเป็น active (หมุดคงเดิม)
  4. ตรวจเฉพาะเคสที่ dx_at อยู่ในช่วง DENGUE_CANCEL_CHECK_DAYS วันล่าสุด (เคสเก่ากว่านั้นไม่ถูกตรวจซ้ำ)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from config import Settings
from geo import village_code
from hosxp import DxRow, HosxpError
from store import Store

log = logging.getLogger(__name__)

CANCEL_AFTER_ABSENT_CYCLES = 3
MASS_CANCEL_MIN = 2  # ยกเลิกน้อยกว่านี้ไม่ถือว่า "จำนวนมาก" (เหลือเฉพาะเงื่อนไข "เกินครึ่ง")
MASS_CANCEL_NOTICE = "ข้ามการยกเลิกจำนวนมากผิดปกติ"


@dataclass
class CycleResult:
    new_case_ids: list[int] = field(default_factory=list)
    followup_count: int = 0
    cancelled_ids: list[int] = field(default_factory=list)
    reactivated_ids: list[int] = field(default_factory=list)
    error: str | None = None


def _in_area(row: DxRow, area: tuple[str, ...]) -> bool:
    if not row.chw or not row.amp:
        return False
    return (row.chw + row.amp) in area


def run_cycle(store: Store, source, settings: Settings, now: datetime) -> CycleResult:
    """รันหนึ่งรอบ วนทีละโรคที่เปิด (settings.enabled_rules()). `now` ใช้กำหนดช่วงตรวจการหายของรหัส.

    แต่ละโรคแยกกันทั้ง debounce, guard ยกเลิกจำนวนมาก และการอ่านที่ล้ม: โรคหนึ่งอ่านไม่ได้ = โรคนั้นไม่แก้เคสใดเลย
    ส่วนโรคอื่นยังทำต่อ (result.error เก็บข้อผิดพลาดแรกของรอบ). visit เดียวที่มีรหัสหลายโรคเป็นของโรคที่มาก่อนในทะเบียน.
    """
    result = CycleResult()
    since = datetime.combine(settings.start_from, time.min)
    check_from = now - timedelta(days=settings.cancel_check_days)
    try:
        area = settings.area_amphoe or source.default_area_amphoe()
    except HosxpError as e:
        result.error = str(e) or "HosxpError"
        return result
    # visit_key ไม่ซ้ำทั้งระบบ: ชุดนี้รวมทุกโรคเพื่อกัน visit เดียวเกิดเคสซ้ำข้ามโรค
    known = {c["visit_key"] for c in store.list_cases(settings.start_from)}
    rules = settings.enabled_rules()
    store.forget_disabled_diseases([r.key for r in rules])   # ปิดโรคแล้วเปิดใหม่ = เริ่มนับใหม่ ไม่ย้อนแจ้ง
    multi = len(rules) > 1
    for rule in rules:
        rule_since = since
        if rule.key != "dengue":
            # โรคที่เพิ่งเปิดไม่ย้อนแจ้งเคสก่อนวันเปิด (ไข้เลือดออกใช้ DENGUE_START_FROM ตามเดิม)
            rule_since = max(since, datetime.combine(store.disease_enabled_at(rule.key, now).date(), time.min))
        label = f"{rule.disease.name_th}: " if multi else ""
        _run_rule(store, source, settings, rule, area, rule_since, check_from, known, result, label)
    return result


def _run_rule(store: Store, source, settings: Settings, rule, area: tuple[str, ...],
              since: datetime, check_from: datetime, known: set[str], result: CycleResult,
              label: str = "") -> None:
    try:
        rows = source.new_dx(since, rule.icds)
        # เคสที่ยังใช้งานในช่วงตรวจต้องมีวินิจฉัยอยู่จริง; ไม่อยู่ในผล = ยังไม่ถือว่าถูกถอนทันที (debounce)
        watched = [c for c in store.active_cases(rule.key) if c["dx_at"] >= check_from]
        present = source.dx_present_many([c["visit_key"] for c in watched], rule.icds)
    except HosxpError as e:
        result.error = result.error or f"{label}{str(e) or 'HosxpError'}"
        return

    seen = {r.visit_key for r in rows}
    cases_by_key = {c["visit_key"]: c
                    for c in store.list_cases(settings.start_from, disease=rule.key)}
    window = timedelta(days=settings.realert_days)
    for row in sorted(rows, key=lambda r: (r.dx_at, r.visit_key, r.icd10)):
        if row.dx_at < since:
            continue  # ก่อนวันเริ่ม
        if row.visit_key in known:
            # visit นี้เป็นเคสอยู่แล้ว (เช่น รหัสที่สองใน visit เดียว); ถ้าเคยถูกยกเลิกแต่รหัสของโรคเดียวกันกลับมา = คืนสถานะ
            case = cases_by_key.get(row.visit_key)
            if (case is not None and case["status"] == "cancelled"
                    and store.reactivate_case(case["id"])):
                case["status"] = "active"
                result.reactivated_ids.append(case["id"])
            continue
        if store.recent_case_for_hn(row.hn, since=row.dx_at - window, disease=rule.key) is not None:
            result.followup_count += 1  # มาตามอาการ: ไม่สร้างเคส ไม่แจ้งซ้ำ
            continue
        code = village_code(row.chw, row.amp, row.tmb, row.moo)
        pin = store.default_pin(row.hn, code, row.house_no)
        if pin is not None:
            lat, lng, pin_source = pin
        else:
            try:
                centroid = source.village_centroid(code) if code else None
            except HosxpError as e:
                # หมุดเป็นข้อมูลเสริม: ยังสร้างเคสโดยไม่มีหมุด (pin_state คง "none")
                log.warning("village_centroid ล้มเหลวสำหรับเคส %s: %s", row.visit_type, e)
                centroid = None
            lat, lng, pin_source = (*centroid, "village_centroid") if centroid else (None,) * 3
        case_id = store.add_case(
            row.visit_key, row.visit_type, row.hn, row.icd10, row.dx_at, code,
            row.house_no, row.sex, row.age_years, _in_area(row, area), disease=rule.key)
        if case_id is None:
            continue
        known.add(row.visit_key)
        result.new_case_ids.append(case_id)
        if lat is not None:
            # ตั้งเฉพาะเคสที่เพิ่งสร้าง จึงไม่มีทางทับหมุด "ยืนยัน" ของเคสอื่น/เคสเดิม
            store.set_pin(case_id, lat, lng, "estimated", pin_source, "system")

    _apply_absence(store, watched, present | seen, result, label)


def _apply_absence(store: Store, watched: list[dict], present: set[str],
                   result: CycleResult, label: str = "") -> None:
    """อัปเดตตัวนับรอบที่รหัสหาย แล้วยกเลิกเคสที่หายติดกันครบรอบ (ภายใต้ guard จำนวนมาก)."""
    counts: dict[int, int] = {}
    to_cancel: list[dict] = []
    for case in watched:
        if case["visit_key"] in present:
            if case["absent_cycles"]:
                counts[case["id"]] = 0
            continue
        n = case["absent_cycles"] + 1
        counts[case["id"]] = n
        if n >= CANCEL_AFTER_ABSENT_CYCLES:
            to_cancel.append(case)
    store.update_absent_cycles(counts)
    if (len(to_cancel) >= MASS_CANCEL_MIN and len(to_cancel) * 2 > len(watched)):
        log.warning("detector: ข้ามการยกเลิก %d จาก %d เคส (มากผิดปกติ — น่าจะเป็นคำตอบจาก HOSxP ไม่ครบ)",
                    len(to_cancel), len(watched))
        result.error = result.error or f"{label}{MASS_CANCEL_NOTICE}"
        return
    for case in to_cancel:
        store.mark_cancelled(case["id"])
        result.cancelled_ids.append(case["id"])
