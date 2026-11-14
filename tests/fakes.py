"""ของปลอมสำหรับเทสต์ (ใช้ร่วมกันหลาย task): FakeSource แทน HosxpSource, FakeSender แทนตัวส่ง MOPH Notify."""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from hosxp import DxRow, HosxpError  # noqa: F401  (HosxpError ให้เทสต์อื่น import ผ่านที่นี่ได้)
from trend import VisitRow, year_of


def make_row(visit_key: str = "VN:1", hn: str = "000001", icd10: str = "A90",
             dx_at: datetime = datetime(2026, 11, 8, 9, 0),
             chw: str | None = "40", amp: str | None = "01",
             tmb: str | None = "01", moo: str | None = "3",
             house_no: str | None = "12", sex: str | None = "1",
             age_years: int | None = 30) -> DxRow:
    visit_type = "IPD" if visit_key.startswith("AN:") else "OPD"
    return DxRow(visit_key=visit_key, visit_type=visit_type, hn=hn, icd10=icd10,
                 dx_at=dx_at, chw=chw, amp=amp, tmb=tmb, moo=moo,
                 house_no=house_no, sex=sex, age_years=age_years)


def default_schema() -> dict[str, dict[str, str]]:
    """โครงสร้างตารางที่ครบตามที่แอปอ่าน (ใช้เป็นค่าเริ่มต้นของ FakeSource.schema)."""
    import setup_check
    schema = {}
    for table, cols in setup_check.REQUIRED_COLUMNS.items():
        schema[table] = {c: ("varchar" if c in ("vn", "an") else "text") for c in cols}
    return schema


DEFAULT_POINTS = [
    {"code": "30250100", "name": "หมู่ 1 ทดสอบ", "lat": 14.3663, "lng": 101.8087},
    {"code": "30250111", "name": "หมู่ 11 ทดสอบ", "lat": 14.4200, "lng": 101.8493},
]


class FakeSource:
    """แทน HosxpSource: rows = วินิจฉัยที่ HOSxP คืน, cancelled = visit_key ที่ถูกถอนรหัสแล้ว.

    failure = exception ที่ต้องยกเมื่อเรียกเมธอด; fail_methods = ชื่อเมธอดที่ยก (None = ทุกเมธอด)
    """

    def __init__(self, rows: Iterable[DxRow] = (), centroids: dict | None = None,
                 area: tuple[str, ...] = ("4001",), contacts: dict | None = None,
                 cancelled: Iterable[str] = (), failure: Exception | None = None,
                 fail_methods: Iterable[str] | None = None,
                 visit_rows: Iterable[VisitRow] = (), names: dict | None = None,
                 facilities: Iterable[dict] | None = None):
        self.rows = list(rows)
        self.centroids = dict(centroids or {})
        self.area = tuple(area)
        self.contacts = dict(contacts or {})
        self.cancelled = set(cancelled)
        self.failure = failure
        self.fail_methods = None if fail_methods is None else set(fail_methods)
        # ---- หน้า /trend: visit ย้อนหลังรายคน (ใช้รวมเป็นจำนวนนับ) และชื่อพื้นที่ ----
        self.visit_rows = list(visit_rows)
        self.names = dict(names or {})
        self.episode_calls: list[tuple] = []
        self.episode_attempts = 0       # รวมครั้งที่ล้ม
        self.default_area_calls = 0
        self.centroid_requests: list[list[str]] = []
        self.facility_catalog = [dict(f) for f in (facilities or [])]   # รายชื่อ รพ./รพ.สต. จาก hospcode
        self.facilities_calls = 0
        self.address_names_calls = 0
        self.visits_by_year: dict[int, int] = {}   # ปี พ.ศ. -> visits_in_ranges (ไม่ตั้ง = นับจาก visit_rows หรือ 5)
        self.last_since: datetime | None = None
        self.last_icds: tuple[str, ...] | None = None
        self.icds_seen: list[tuple[str, ...]] = []   # ชุดรหัสที่ถูกถามด้วย new_dx (ตรวจว่าโรคที่ปิดไม่ถูก query)
        self.fail_icds: set[tuple[str, ...]] = set()  # ชุดรหัสที่ให้ล้มด้วย `failure` (จำลองโรคเดียวอ่านไม่ได้)
        # ---- ข้อมูลสำหรับ setup_check / หน้า /check (เฉพาะจำนวนนับและโครงสร้าง) ----
        self.schema = default_schema()
        self.now = datetime.now().replace(microsecond=0)
        self.read_only = True
        self.timeout_ok = True
        self.write_privs: list[str] = []
        self.thai = ["หมู่บ้านทดสอบ"]
        self.latest_visit: date | None = date.today()
        self.villages = {"total": 9, "with_centroid": 9}
        self.points = [dict(p) for p in DEFAULT_POINTS]
        self.icd_found = {"A90": 8, "A911": 2}
        self.area_found = {"4001": {"visits": 9, "mapped": 8}}

    def _maybe_fail(self, method: str) -> None:
        if self.failure is not None and (
                self.fail_methods is None or method in self.fail_methods):
            raise self.failure

    def new_dx(self, since: datetime, icds: tuple[str, ...]) -> list[DxRow]:
        self._maybe_fail("new_dx")
        if tuple(icds) in self.fail_icds:
            raise self.failure
        self.last_since, self.last_icds = since, tuple(icds)
        self.icds_seen.append(tuple(icds))
        prefixes = tuple(i.replace(".", "") for i in icds)
        # cancelled = ถูกถอนรหัสแล้ว: HOSxP จริงจะไม่คืนแถวนั้นจาก new_dx ด้วย
        return [r for r in self.rows
                if r.dx_at >= since and r.icd10.startswith(prefixes)
                and r.visit_key not in self.cancelled]

    def dx_present_many(self, visit_keys: list[str],
                        icds: tuple[str, ...]) -> set[str]:
        self._maybe_fail("dx_present_many")
        if tuple(icds) in self.fail_icds:
            raise self.failure
        if not visit_keys:
            return set()
        if not any(i.replace(".", "").strip() for i in icds):
            raise HosxpError("no disease codes configured")
        return {k for k in visit_keys if k not in self.cancelled}

    def dx_present(self, visit_key: str, icds: tuple[str, ...]) -> bool:
        self._maybe_fail("dx_present")
        return visit_key in self.dx_present_many([visit_key], icds)

    def contact(self, hn: str) -> dict:
        self._maybe_fail("contact")
        return self.contacts.get(hn, {"name": "", "phone": "", "address": ""})

    def village_centroid(self, code: str) -> tuple[float, float] | None:
        self._maybe_fail("village_centroid")
        return self.centroids.get(code)

    def default_area_amphoe(self) -> tuple[str, ...]:
        self.default_area_calls += 1
        self._maybe_fail("default_area_amphoe")
        return self.area

    # ---- setup_check / หน้า /check ----
    def server_now(self) -> datetime:
        self._maybe_fail("server_now")
        return self.now

    def statement_timeout_supported(self) -> bool:
        self._maybe_fail("statement_timeout_supported")
        return self.timeout_ok

    def session_read_only(self) -> bool:
        self._maybe_fail("session_read_only")
        return self.read_only

    def write_privileges(self) -> list[str]:
        self._maybe_fail("write_privileges")
        return list(self.write_privs)

    def table_columns(self, tables: Iterable[str]) -> dict[str, dict[str, str]]:
        self._maybe_fail("table_columns")
        return {t: dict(c) for t, c in self.schema.items() if t in set(tables)}

    def thai_samples(self) -> list[str]:
        self._maybe_fail("thai_samples")
        return list(self.thai)

    def latest_visit_date(self) -> date | None:
        self._maybe_fail("latest_visit_date")
        return self.latest_visit

    def village_counts(self) -> dict:
        self._maybe_fail("village_counts")
        return dict(self.villages)

    def village_points(self) -> list[dict]:
        self._maybe_fail("village_points")
        return [dict(p) for p in self.points]

    def icd_counts(self, icds: tuple[str, ...], since: date) -> dict[str, int]:
        self._maybe_fail("icd_counts")
        prefixes = tuple(i.replace(".", "") for i in icds)
        return {k: v for k, v in self.icd_found.items() if k.startswith(prefixes)}

    def area_counts(self, icds: tuple[str, ...], since: date) -> dict[str, dict]:
        self._maybe_fail("area_counts")
        return {k: dict(v) for k, v in self.area_found.items()}

    def episode_rows(self, icds: tuple[str, ...], start: date, end: date) -> list[VisitRow]:
        self.episode_attempts += 1
        self._maybe_fail("episode_rows")
        self.episode_calls.append((tuple(icds), start, end))
        return [r for r in self.visit_rows if start <= r.day <= end]

    def visits_in_ranges(self, icds: tuple[str, ...],
                         ranges: list[tuple[date, date]]) -> list[int]:
        self._maybe_fail("visits_in_ranges")
        out = []
        for start, end in ranges:
            year = year_of(start)
            if year in self.visits_by_year:
                out.append(self.visits_by_year[year])
            elif self.visit_rows:
                out.append(sum(1 for r in self.visit_rows if start <= r.day <= end))
            else:
                out.append(5)   # ค่าเริ่มต้น "สุขภาพดี" ให้เทสต์เดิมที่ไม่เกี่ยวกับ /trend ไม่ติด ⚠
        return out

    def village_centroids(self, codes: Iterable[str]) -> dict[str, tuple[float, float]]:
        codes = list(codes)
        self.centroid_requests.append(codes)
        self._maybe_fail("village_centroids")   # เมธอดจริงยก HosxpError เมื่อฐานล้ม (แยกจาก "ไม่มีพิกัด")
        return {c: self.centroids[c] for c in codes if c in self.centroids}

    def facilities(self, area: tuple[str, ...]) -> list[dict]:
        self.facilities_calls += 1
        self._maybe_fail("facilities")
        return [dict(f) for f in self.facility_catalog]

    def address_names(self, codes: Iterable[str]) -> dict[str, str]:
        self.address_names_calls += 1
        self._maybe_fail("address_names")
        return {c: self.names[c] for c in codes if c in self.names}


class FakeSender:
    """ตัวส่งปลอม: เรียกด้วย messages (list[dict]) บันทึกไว้ใน calls.

    ตั้ง error เป็น exception ใด ๆ (เช่น NotifyError) เพื่อให้ยกทุกครั้งที่ส่ง
    """

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> None:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error

    @property
    def sent(self) -> list[dict]:
        """ข้อความทั้งหมดที่เรียกส่ง (รวมครั้งที่ยก error) เรียงตามลำดับ."""
        return [m for call in self.calls for m in call]
