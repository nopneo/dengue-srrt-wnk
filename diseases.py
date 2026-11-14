"""ทะเบียนโรคที่แอปติดตาม (กำหนดในโค้ด) และกฎต่อโรคที่ resolve จาก env — สเปก 2026-11-10-multi-disease.

ไม่ import โมดูลอื่นของแอป (config.py เป็นผู้ import โมดูลนี้) — ข้อมูลล้วน ไม่มีข้อมูลผู้ป่วย.
ลำดับใน REGISTRY มีความหมาย: visit เดียวที่มีรหัสหลายโรคเป็นของโรคที่มาก่อน (visit_key ของเคสไม่ซ้ำทั้งระบบ).
"""
from __future__ import annotations

from dataclasses import dataclass

# ค่ากำหนดการแจ้งเตือน: per_case_pin = แจ้งทุกรายพร้อมหมุด · cluster = แจ้งเฉพาะเมื่อเป็นกลุ่มก้อน
MODES = ("per_case_pin", "cluster")
# การจัดกลุ่ม: pin_radius = หมุดที่ยืนยันแล้วอยู่ในรัศมี · village = หมู่บ้านเดียวกัน
GROUP_BYS = ("pin_radius", "village")

NAVY = ("#1440b8", "#0b2e8a")
RED = ("#ff4d5e", "#dc2626")


@dataclass(frozen=True)
class Disease:
    key: str
    name_th: str
    icd_prefixes: tuple[str, ...]
    color: tuple[str, str]            # gradient เริ่ม/จบ: หัวข้อความเคสและจุดบนแท็บ
    cluster_color: tuple[str, str]    # gradient หัวข้อความ cluster
    mode: str
    group_by: str
    min_cases: int
    days: int
    radius_m: int


def _d(key, name_th, icds, color, mode, group_by, min_cases, days, cluster_color=None) -> Disease:
    return Disease(key, name_th, tuple(icds), color, cluster_color or color, mode, group_by,
                   min_cases, days, 100)


REGISTRY: tuple[Disease, ...] = (
    _d("dengue", "ไข้เลือดออก", ("A90", "A91"), NAVY, "per_case_pin", "pin_radius", 2, 14, cluster_color=RED),
    _d("lepto", "เล็ปโตสไปโรสิส", ("A27",), ("#14b8a6", "#0f766e"), "per_case_pin", "pin_radius", 2, 14),
    _d("malaria", "มาลาเรีย", ("B50", "B51", "B52", "B53", "B54"), ("#a855f7", "#7c3aed"),
       "per_case_pin", "pin_radius", 2, 14),
    _d("chik", "ชิคุนกุนยา", ("A92",), ("#ffa62b", "#f97316"), "per_case_pin", "pin_radius", 2, 14),
    _d("hfmd", "มือเท้าปาก", ("B084",), ("#ec4899", "#be185d"), "cluster", "village", 2, 7),
    _d("varicella", "สุกใส", ("B01",), ("#22c55e", "#15803d"), "cluster", "village", 2, 21),
    _d("diarrhea", "อุจจาระร่วง/อาหารเป็นพิษ", tuple(f"A0{i}" for i in range(10)), ("#d97706", "#92400e"),
       "cluster", "village", 5, 3),
)
DISEASES: dict[str, Disease] = {d.key: d for d in REGISTRY}
DEFAULT_ENABLED = ("dengue",)


MODE_LABELS = {"per_case_pin": "แจ้งทุกราย + ปักหมุด", "cluster": "แจ้งเฉพาะเมื่อเป็นกลุ่มก้อน"}
GROUP_LABELS = {"pin_radius": "หมุดในรัศมี", "village": "หมู่บ้านเดียวกัน"}


def env_names(key: str) -> dict[str, str]:
    """ชื่อ env ที่ปรับกฎของโรคนั้น (ไข้เลือดออกใช้ชื่อเดิม) — แสดงในหน้า /settings และ /check."""
    if key == "dengue":
        return {"icds": "DENGUE_ICDS", "min_cases": "DENGUE_CLUSTER_MIN_CASES",
                "days": "DENGUE_CLUSTER_DAYS", "radius_m": "DENGUE_CLUSTER_RADIUS_M"}
    p = f"DENGUE_{key.upper()}_"
    return {"icds": p + "ICDS", "min_cases": p + "MIN_CASES", "days": p + "DAYS", "radius_m": p + "RADIUS_M"}


@dataclass(frozen=True)
class DiseaseRule:
    """กฎที่ใช้จริงของหนึ่งโรค (ทะเบียน + ค่าที่ env ปรับ)."""
    key: str
    enabled: bool
    icds: tuple[str, ...]
    min_cases: int
    days: int
    radius_m: int

    @property
    def disease(self) -> Disease:
        return DISEASES[self.key]

    @property
    def mode(self) -> str:
        return self.disease.mode

    @property
    def group_by(self) -> str:
        return self.disease.group_by
