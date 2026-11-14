"""ทะเบียนโรค (diseases.py) และกฎต่อโรคใน Settings — สเปกข้อ 2 และ 4.5"""
import dataclasses

import pytest

from config import Settings, load_settings
from diseases import DEFAULT_ENABLED, DISEASES, REGISTRY, DiseaseRule

EXPECTED = {
    "dengue": ("ไข้เลือดออก", ("A90", "A91"), "per_case_pin", "pin_radius", 2, 14, 100),
    "lepto": ("เล็ปโตสไปโรสิส", ("A27",), "per_case_pin", "pin_radius", 2, 14, 100),
    "malaria": ("มาลาเรีย", ("B50", "B51", "B52", "B53", "B54"), "per_case_pin", "pin_radius", 2, 14, 100),
    "chik": ("ชิคุนกุนยา", ("A92",), "per_case_pin", "pin_radius", 2, 14, 100),
    "hfmd": ("มือเท้าปาก", ("B084",), "cluster", "village", 2, 7, 100),
    "varicella": ("สุกใส", ("B01",), "cluster", "village", 2, 21, 100),
    "diarrhea": ("อุจจาระร่วง/อาหารเป็นพิษ", tuple(f"A0{i}" for i in range(10)), "cluster", "village", 5, 3, 100),
}


def test_registry_matches_spec_table():
    assert [d.key for d in REGISTRY] == list(EXPECTED)
    for d in REGISTRY:
        name, icds, mode, group_by, n, days, radius = EXPECTED[d.key]
        assert (d.name_th, d.icd_prefixes, d.mode, d.group_by, d.min_cases, d.days, d.radius_m) == \
            (name, icds, mode, group_by, n, days, radius)
    assert DISEASES == {d.key: d for d in REGISTRY}
    assert DEFAULT_ENABLED == ("dengue",)


def test_registry_keys_unique_and_prefixes_do_not_overlap():
    assert len({d.key for d in REGISTRY}) == len(REGISTRY)
    pairs = [(d.key, p) for d in REGISTRY for p in d.icd_prefixes]
    for i, (ka, pa) in enumerate(pairs):
        for kb, pb in pairs[i + 1:]:
            assert not (pa.startswith(pb) or pb.startswith(pa)), (ka, pa, kb, pb)


def test_registry_colors_modes_are_valid():
    for d in REGISTRY:
        assert d.mode in ("per_case_pin", "cluster") and d.group_by in ("pin_radius", "village")
        for c in (d.color, d.cluster_color):
            assert len(c) == 2 and all(x.startswith("#") and len(x) == 7 for x in c)
    assert DISEASES["dengue"].cluster_color == ("#ff4d5e", "#dc2626")     # ค่าเดิมของข้อความ cluster
    assert DISEASES["dengue"].color == ("#1440b8", "#0b2e8a")


def test_dengue_registry_defaults_equal_legacy_settings_defaults():
    s = load_settings({})
    d = DISEASES["dengue"]
    assert (s.icds, s.cluster_min_cases, s.cluster_days, s.cluster_radius_m) == \
        (d.icd_prefixes, d.min_cases, d.days, d.radius_m)


def test_default_enabled_rules_only_dengue():
    s = load_settings({})
    assert [r.key for r in s.enabled_rules()] == ["dengue"]
    assert s.enabled_rules()[0].icds == ("A90", "A91")
    assert [r.key for r in s.rules] == list(EXPECTED) and [r.enabled for r in s.rules].count(True) == 1


def test_diseases_env_enables_in_registry_order_and_drops_unknown():
    s = load_settings({"DENGUE_DISEASES": "dengue, HFMD ,bogus"})
    assert [r.key for r in s.enabled_rules()] == ["dengue", "hfmd"]
    s2 = load_settings({"DENGUE_DISEASES": "hfmd,dengue"})
    assert [r.key for r in s2.enabled_rules()] == ["dengue", "hfmd"]
    assert s2.rule("hfmd").enabled and not s2.rule("lepto").enabled


@pytest.mark.parametrize("raw", ["", "bogus", " , "])
def test_diseases_env_empty_or_unusable_falls_back_to_dengue(raw):
    assert [r.key for r in load_settings({"DENGUE_DISEASES": raw}).enabled_rules()] == ["dengue"]


def test_per_disease_env_overrides_and_bad_values_use_registry_defaults():
    s = load_settings({"DENGUE_DISEASES": "hfmd,diarrhea", "DENGUE_HFMD_DAYS": "10",
                       "DENGUE_HFMD_MIN_CASES": "3", "DENGUE_HFMD_ICDS": "b084, b08",
                       "DENGUE_DIARRHEA_DAYS": "0", "DENGUE_DIARRHEA_MIN_CASES": "abc",
                       "DENGUE_DIARRHEA_RADIUS_M": "-5"})
    h, d = s.rule("hfmd"), s.rule("diarrhea")
    assert (h.days, h.min_cases, h.icds) == (10, 3, ("B084", "B08"))
    assert (d.days, d.min_cases, d.radius_m) == (3, 5, 100)


def test_legacy_dengue_env_only_affects_dengue():
    s = load_settings({"DENGUE_DISEASES": "dengue,hfmd", "DENGUE_ICDS": "A91",
                       "DENGUE_CLUSTER_MIN_CASES": "4", "DENGUE_CLUSTER_RADIUS_M": "150", "DENGUE_CLUSTER_DAYS": "7"})
    assert (s.rule("dengue").icds, s.rule("dengue").min_cases, s.rule("dengue").radius_m, s.rule("dengue").days) == \
        (("A91",), 4, 150, 7)
    assert s.rule("hfmd").icds == ("B084",) and s.rule("hfmd").min_cases == 2


def test_rule_exposes_disease_mode_group_by_and_unknown_key():
    s = load_settings({})
    r = s.rule("hfmd")
    assert r.disease is DISEASES["hfmd"] and r.mode == "cluster" and r.group_by == "village"
    assert s.rule("nope") is None


def test_settings_built_without_rules_falls_back_to_legacy_dengue_rule():
    s = load_settings({"DENGUE_ICDS": "A91", "DENGUE_CLUSTER_DAYS": "9"})
    bare = dataclasses.replace(s, rules=())
    (r,) = bare.enabled_rules()
    assert isinstance(r, DiseaseRule) and (r.key, r.icds, r.days, r.min_cases, r.radius_m) == ("dengue", ("A91",), 9, 2, 100)
    assert bare.rule("dengue") is not None
