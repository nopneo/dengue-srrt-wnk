"""ตัวจัดรูปแบบสำหรับหน้าเว็บ: วันที่ไทย (พ.ศ.), รหัสโรค, เพศ, พื้นที่, ป้ายหมุด/เขต.

ไม่ import app/auth (กัน circular import).
"""
from __future__ import annotations

from datetime import date, datetime

THAI_MONTHS = ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
               "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")
THAI_MONTHS_SHORT = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                     "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")
THAI_WEEKDAYS = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์")
ROLE_LABELS = {"srrt": "SRRT", "viewer": "ผู้ดู", "admin": "ผู้ดูแล"}


def thai_date(d: date | datetime | None, short: bool = True) -> str:
    """เช่น 8 พ.ย. 2569 (short) หรือ 8 พฤศจิกายน 2569."""
    if d is None:
        return "-"
    months = THAI_MONTHS_SHORT if short else THAI_MONTHS
    return f"{d.day} {months[d.month - 1]} {d.year + 543}"


def thai_datetime(d: datetime | None) -> str:
    if d is None:
        return "-"
    return f"{thai_date(d)} {d:%H:%M}"


def thai_date_long(d: date) -> str:
    return f"วัน{THAI_WEEKDAYS[d.weekday()]}ที่ {d.day} {THAI_MONTHS[d.month - 1]} {d.year + 543}"


def icd_dot(icd: str | None) -> str:
    """A911 -> A91.1 (ใส่จุดหลังตัวที่ 3 เมื่อยาวกว่า 3)."""
    icd = (icd or "").strip().upper()
    return f"{icd[:3]}.{icd[3:]}" if len(icd) > 3 else (icd or "-")


def sex_text(code: str | None) -> str:
    return {"1": "ชาย", "2": "หญิง"}.get((code or "").strip(), "-")


def area_text(village_code: str | None) -> str:
    """หมู่ที่จากรหัสหมู่บ้าน 8 หลัก (ตำแหน่ง 7-8); หมู่ 0 หรืออ่านไม่ได้ -> '-'."""
    code = (village_code or "").strip()
    try:
        moo = int(code[6:8])
    except ValueError:
        return "-"
    return f"หมู่ {moo}" if moo > 0 else "-"


def pin_pill(state: str | None) -> dict:
    """ป้ายหมุด: label + css class ของ pill."""
    if state == "confirmed":
        return {"label": "ยืนยันแล้ว", "css": "pill-green"}
    if state == "estimated":
        return {"label": "ประมาณ", "css": "pill-blue"}
    return {"label": "รอปักหมุด", "css": "pill-orange"}


def area_pill(in_area: bool) -> dict:
    if in_area:
        return {"label": "ในเขต", "css": "pill-navy"}
    return {"label": "นอกเขต", "css": "pill-red"}
