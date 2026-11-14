"""ค่าตั้งของแอปแจ้งเตือนไข้เลือดออก SRRT — อ่านจาก env (ดูสเปกข้อ 11)."""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Mapping
from urllib.parse import urlsplit

from diseases import DEFAULT_ENABLED, REGISTRY, DiseaseRule

DEFAULT_LAN_PREFIXES = (
    "10.,172.16.,172.17.,172.18.,172.19.,172.20.,172.21.,172.22.,172.23.,"
    "172.24.,172.25.,172.26.,172.27.,172.28.,172.29.,172.30.,172.31.,"
    "192.168.,127.,30.25."
)
DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MIN_SECRET_LEN = 16
START_FROM_FUTURE_DAYS = 7  # เกินนี้ถือว่าน่าจะพิมพ์ผิด (เช่นปี พ.ศ.)
# DENGUE_START_FROM ที่ยังไม่ตั้ง = วันที่เริ่มระบบ ซึ่งเลื่อนไปทุกครั้งที่ restart — setup_check และหน้า /settings เตือนด้วยข้อความเดียวกัน
MSG_START_FROM_UNSET = ("ยังไม่ได้ตั้ง DENGUE_START_FROM — ใช้วันที่เริ่มระบบทุกครั้งที่ restart "
                        "(เคสที่ลงรหัสย้อนหลังหลัง restart จะไม่ถูกตรวจ) ตั้งเป็นวัน go-live")


def looks_like_placeholder(value: str) -> bool:
    """ค่าที่ยังเป็นตัวแทนจาก .env.example (เช่น "<Client ID ของกลุ่ม>") — มี < และ > ทั้งคู่."""
    return "<" in value and ">" in value


def secret_key_problem(value: str) -> str | None:
    """เหตุผลที่ SECRET_KEY ใช้ไม่ได้ (ข้อความไทย ไม่มีตัวค่าเอง) หรือ None ถ้าใช้ได้."""
    value = value or ""
    if not value.strip():
        return "ยังไม่ได้ตั้ง SECRET_KEY"
    if looks_like_placeholder(value):
        return "SECRET_KEY ยังเป็นค่าตัวอย่าง <...> จาก .env.example"
    if len(value) < MIN_SECRET_LEN:
        return f"SECRET_KEY สั้นเกินไป (ต้องยาวอย่างน้อย {MIN_SECRET_LEN} ตัวอักษร)"
    return None


def base_url_problem(value: str) -> str | None:
    """เหตุผลที่ DENGUE_BASE_URL ใช้สร้างลิงก์ในข้อความ LINE ไม่ได้ (ข้อความไทย) หรือ None ถ้าใช้ได้.

    ต้องเป็น http(s)://host... ไม่ใช่ค่าตัวอย่าง <...> — ข้อความไม่มีตัวค่า (อย่างมากพิมพ์ชื่อ scheme สั้น ๆ)."""
    value = (value or "").strip()
    if not value:
        return "ยังไม่ได้ตั้ง DENGUE_BASE_URL"
    if looks_like_placeholder(value):
        return "DENGUE_BASE_URL ยังเป็นค่าตัวอย่าง <...> จาก .env.example"
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 — พอร์ตผิดรูปยก ValueError
    except ValueError:
        return "DENGUE_BASE_URL อ่านเป็น URL ไม่ได้"
    if parts.scheme not in ("http", "https"):
        shown = parts.scheme if re.fullmatch(r"[a-z][a-z0-9+.-]{0,15}", parts.scheme) else "(ไม่มี)"
        return f"DENGUE_BASE_URL ต้องขึ้นต้นด้วย http:// หรือ https:// (พบ scheme {shown})"
    if not host or any(c.isspace() for c in value):
        return "DENGUE_BASE_URL ไม่มีชื่อโฮสต์ที่ใช้ได้"
    return None


@dataclass(frozen=True)
class Settings:
    icds: tuple[str, ...]
    poll_seconds: int
    start_from: date
    cluster_min_cases: int
    cluster_radius_m: int
    cluster_days: int
    realert_days: int
    cancel_check_days: int
    area_amphoe: tuple[str, ...]
    base_url: str
    tile_url: str
    data_audited: bool
    db_path: str
    lan_prefixes: tuple[str, ...]
    secret_key: str = field(repr=False)
    moph_base_url: str
    moph_client_key: str = field(repr=False)
    moph_secret_key: str = field(repr=False)
    # "" ปกติ · "unset" = ไม่ได้ตั้ง/ว่าง (ใช้วันนี้ทุกครั้งที่เริ่มระบบ) ·
    # "invalid" = DENGUE_START_FROM อ่านเป็นวันที่ไม่ได้ (ใช้วันนี้แทน) ·
    # "future" = วันที่ห่างจากวันนี้เกิน 7 วัน (เช่นปี พ.ศ. 2569-11-15 — ใช้ค่านั้นตามที่ตั้ง)
    start_from_note: str = ""
    # กฎต่อโรคครบทุกโรคในทะเบียน (diseases.REGISTRY) — ว่าง = สร้าง Settings ตรง ๆ โดยไม่ผ่าน load_settings
    rules: tuple[DiseaseRule, ...] = ()

    def _legacy_rules(self) -> tuple[DiseaseRule, ...]:
        return (DiseaseRule("dengue", True, self.icds, self.cluster_min_cases, self.cluster_days,
                            self.cluster_radius_m),)

    def _resolved_rules(self) -> tuple[DiseaseRule, ...]:
        """กฎครบทุกโรค โดยกฎของไข้เลือดออกอ่านจากฟิลด์เดิม (icds, cluster_*) เสมอ — ฟิลด์เหล่านี้เป็นแหล่งจริงของ dengue."""
        if not self.rules:
            return self._legacy_rules()
        legacy = self._legacy_rules()[0]
        return tuple(dataclasses.replace(legacy, enabled=r.enabled) if r.key == "dengue" else r
                     for r in self.rules)

    def enabled_rules(self) -> tuple[DiseaseRule, ...]:
        """กฎของโรคที่เปิดอยู่ (ลำดับตามทะเบียน); ไม่มี rules = ไข้เลือดออกจากฟิลด์เดิม."""
        return tuple(r for r in self._resolved_rules() if r.enabled)

    def rule(self, key: str) -> DiseaseRule | None:
        for r in self._resolved_rules():
            if r.key == key:
                return r
        return None


def _positive_int(value: str, default: int) -> int:
    n = _int(value, default)
    return n if n > 0 else default


def _build_rules(get, dengue: tuple[tuple[str, ...], int, int, int]) -> tuple[DiseaseRule, ...]:
    """กฎครบทุกโรค: dengue ใช้ค่าจากชื่อ env เดิม (`dengue` = icds, min_cases, days, radius_m ที่อ่านแล้ว)
    โรคอื่นอ่าน DENGUE_<KEY>_ICDS/_MIN_CASES/_DAYS/_RADIUS_M — ค่าไม่ใช่เลขบวกใช้ค่าตั้งต้นของทะเบียน."""
    wanted = {k.lower() for k in _split(get("DENGUE_DISEASES"))}
    enabled = [d.key for d in REGISTRY if d.key in wanted] or list(DEFAULT_ENABLED)
    rules = []
    for d in REGISTRY:
        if d.key == "dengue":
            icds, n, days, radius = dengue
        else:
            p = f"DENGUE_{d.key.upper()}_"
            icds = _split(get(p + "ICDS"), upper=True) or d.icd_prefixes
            n = _positive_int(get(p + "MIN_CASES"), d.min_cases)
            days = _positive_int(get(p + "DAYS"), d.days)
            radius = _positive_int(get(p + "RADIUS_M"), d.radius_m)
        rules.append(DiseaseRule(d.key, d.key in enabled, icds, n, days, radius))
    return tuple(rules)


def _split(value: str, upper: bool = False) -> tuple[str, ...]:
    items = (p.strip() for p in value.split(","))
    return tuple((p.upper() if upper else p) for p in items if p)


def _lan_prefixes(value: str) -> tuple[str, ...]:
    """prefix ที่อยู่ LAN: ต่อ "." ท้ายให้ถ้าขาด ("10" -> "10.", "172.16" -> "172.16.")
    เพื่อให้เทียบทั้งเลขชุด ("10" จะได้ไม่ไปตรง 100.x.x.x)."""
    return tuple(p if p.endswith(".") else p + "." for p in _split(value))


def _int(value: str, default: int) -> int:
    """ค่าว่างหรืออ่านเป็นเลขไม่ได้ -> ใช้ค่าเริ่มต้น แทนการล้มด้วย ValueError."""
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env

    def get(key: str, default: str = "") -> str:
        return e.get(key, default)

    start_raw = get("DENGUE_START_FROM").strip()
    today = date.today()
    start_note = ""
    if not start_raw:
        start_from, start_note = today, "unset"
    else:
        try:
            start_from = date.fromisoformat(start_raw)
        except ValueError:
            start_from, start_note = today, "invalid"
        else:
            if start_from > today + timedelta(days=START_FROM_FUTURE_DAYS):
                start_note = "future"
    icds = _split(get("DENGUE_ICDS"), upper=True) or ("A90", "A91")
    min_cases = _int(get("DENGUE_CLUSTER_MIN_CASES"), 2)
    radius_m = _int(get("DENGUE_CLUSTER_RADIUS_M"), 100)
    cluster_days = _int(get("DENGUE_CLUSTER_DAYS"), 14)
    return Settings(
        icds=icds,
        poll_seconds=_int(get("DENGUE_POLL_SECONDS"), 120),
        start_from=start_from,
        cluster_min_cases=min_cases,
        cluster_radius_m=radius_m,
        cluster_days=cluster_days,
        realert_days=_int(get("DENGUE_REALERT_DAYS"), 21),
        cancel_check_days=_int(get("DENGUE_CANCEL_CHECK_DAYS"), 90),
        area_amphoe=_split(get("DENGUE_AREA_AMPHOE")),
        base_url=get("DENGUE_BASE_URL", "https://wnkhos.go.th/dengue"),
        tile_url=get("DENGUE_TILE_URL", DEFAULT_TILE_URL),
        data_audited=get("DENGUE_DATA_AUDITED", "0").strip().lower()
        in ("1", "true", "yes"),
        db_path=get("DENGUE_DB_PATH", "data/dengue.db"),
        lan_prefixes=_lan_prefixes(get("DENGUE_LAN_PREFIXES") or DEFAULT_LAN_PREFIXES),
        secret_key=get("SECRET_KEY"),
        moph_base_url=get("MOPH_BASE_URL", "https://morpromt2f.moph.go.th"),
        moph_client_key=get("MOPH_CLIENT_KEY"),
        moph_secret_key=get("MOPH_SECRET_KEY"),
        start_from_note=start_note,
        rules=_build_rules(get, (icds, min_cases, cluster_days, radius_m)),
    )
