"""อ่านข้อมูลจาก HOSxP (MariaDB) แบบอ่านอย่างเดียว — ไม่เขียนลงฐาน HOSxP เด็ดขาด.

- SQL ทุกคำสั่งใช้พารามิเตอร์ (%s) รหัสโรคมาจากอาร์กิวเมนต์ `icds` ไม่ฝังรหัสในข้อความ SQL
- `connect` คือฟังก์ชันที่ผู้เรียกให้มา (ควรเป็นการเชื่อมแบบอ่านอย่างเดียว) เปิดต่อหนึ่งเมธอดแล้วปิด
- ทุกความผิดพลาดของไดรเวอร์/เครือข่ายถูกห่อเป็น HosxpError
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable

import facilities as _fac
from trend import VisitRow


class HosxpError(Exception):
    """ต่อ HOSxP หรือ query ไม่ได้ (ผู้เรียกต้องไม่ตีความว่าข้อมูลหาย)."""


# ข้อความคงที่ของ wiring.make_connect (ไม่มีรหัส/โฮสต์/ข้อความไดรเวอร์) — หน้าตั้งค่าส่งต่อให้ผู้ใช้ได้
MSG_DB_NOT_CONFIGURED = "ยังไม่ได้ตั้งค่า DB_HOST / DB_USER"
MSG_READ_ONLY_FAILED = "ตั้งการเชื่อมต่อเป็นอ่านอย่างเดียวไม่สำเร็จ"  # ตามด้วย " (<ชื่อชนิด exception>)"


class HosxpTimeout(HosxpError):
    """คำสั่งถูกฐานตัดเพราะเกิน max_statement_time (เฉพาะคำสั่งตรวจก่อนใช้)."""

AUDIT_TIMEOUT_S = 15       # max_statement_time ของคำสั่งตรวจก่อนใช้ (วินาที)
TREND_TIMEOUT_S = 60       # ดึงแถวย้อนหลังทั้งปี พ.ศ.สำหรับหน้า /trend ใช้เวลามากกว่าคำสั่งตรวจ (gunicorn --timeout 120)
_ER_STATEMENT_TIMEOUT = 1969


@dataclass(frozen=True)
class DxRow:
    visit_key: str          # "VN:<vn>" หรือ "AN:<an>"
    visit_type: str         # "OPD" | "IPD"
    hn: str
    icd10: str              # ตามที่เก็บใน HOSxP (ไม่มีจุด เช่น A911)
    dx_at: datetime
    chw: str | None
    amp: str | None
    tmb: str | None
    moo: str | None
    house_no: str | None
    sex: str | None         # รหัส HOSxP ตามที่อ่านมา ('1' ชาย, '2' หญิง)
    age_years: int | None   # อายุ ณ วันวินิจฉัย; None ถ้าไม่มีวันเกิด


# ---- ตัวช่วยแปลงค่า ----
def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _to_time(value: Any) -> time:
    """คอลัมน์เวลาของ HOSxP อาจมาเป็น time, timedelta หรือข้อความ; NULL -> 00:00:00."""
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, time):
        return value.replace(microsecond=0)
    if isinstance(value, timedelta):
        secs = int(value.total_seconds()) % 86400
        return time(secs // 3600, secs % 3600 // 60, secs % 60)
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip()).replace(microsecond=0)
        except ValueError:
            return time(0, 0, 0)
    return time(0, 0, 0)


def _combine(d: Any, t: Any) -> datetime | None:
    day = _to_date(d)
    return datetime.combine(day, _to_time(t)) if day else None


def _age_years(birthday: Any, at: datetime) -> int | None:
    b = _to_date(birthday)
    if b is None:
        return None
    age = at.year - b.year - ((at.month, at.day) < (b.month, b.day))
    return age if 0 <= age <= 150 else None


def _like_params(icds: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """คืน (ส่วนเงื่อนไข SQL, พารามิเตอร์) เช่น '(icd10 LIKE %s OR icd10 LIKE %s)'."""
    cleaned = (i.replace(".", "").strip() for i in icds)
    prefixes = tuple(c + "%" for c in cleaned if c)  # คำนำหน้าว่างจะกลายเป็น '%' (ตรงทุกแถว) จึงตัดทิ้ง
    clause = "(" + " OR ".join(["{col} LIKE %s"] * len(prefixes)) + ")"
    return clause, prefixes


_OPD_SQL = (
    "SELECT d.vn, d.hn, d.icd10, COALESCE(d.vstdate, o.vstdate),"
    " COALESCE(d.vsttime, o.vsttime), p.chwpart, p.amppart, p.tmbpart, p.moopart,"
    " p.addrpart, p.sex, p.birthday"
    " FROM ovstdiag d"
    " LEFT JOIN ovst o ON o.vn = d.vn"
    " JOIN patient p ON p.hn = d.hn"
    " WHERE COALESCE(d.vstdate, o.vstdate) >= %s AND {like}"
    " ORDER BY 4, 5, d.vn"
)
_IPD_SQL = (
    "SELECT d.an, i.hn, d.icd10, i.regdate, i.regtime, p.chwpart, p.amppart,"
    " p.tmbpart, p.moopart, p.addrpart, p.sex, p.birthday"
    " FROM iptdiag d"
    " JOIN ipt i ON i.an = d.an"
    " JOIN patient p ON p.hn = i.hn"
    " WHERE i.regdate >= %s AND {like}"
    " ORDER BY i.regdate, i.regtime, d.an"
)


AREA_ERROR_BACKOFF_S = 60  # งด area_label หลังเกิด error (วินาที)
_CHUNK = 200  # จำนวนคีย์สูงสุดต่อหนึ่ง IN (...)


class HosxpSource:
    def __init__(self, connect: Callable[[], Any],
                 clock: Callable[[], float] = _time.monotonic):
        self._connect = connect
        self._clock = clock
        self._area_down_until = 0.0  # หลัง error ของ area_label งดลองต่อจนถึงเวลานี้
        self._area_cache: dict[str, str | None] = {}  # ชื่อตำบลคงที่ จึงแคชต่อรหัสได้

    # ---- ชั้นล่าง: รันคำสั่งหลายอันในการเชื่อมต่อเดียว ห่อทุก error เป็น HosxpError ----
    def _run(self, queries: list[tuple[str, tuple]],
             timeout_s: int | None = None) -> list[list[tuple]]:
        """รันหลายคำสั่งในการเชื่อมต่อเดียว; timeout_s -> SET SESSION max_statement_time (ถ้าฐานไม่รับ
        ก็ทำต่อโดยไม่ตั้ง — ไม่ถือเป็นข้อผิดพลาด)."""
        try:
            con = self._connect()
            try:
                out = []
                cur = con.cursor()
                try:
                    if timeout_s:
                        _set_statement_timeout(cur, timeout_s)
                    for sql, params in queries:
                        cur.execute(sql, params)
                        out.append(list(cur.fetchall()))
                finally:
                    cur.close()
                return out
            finally:
                con.close()
        except HosxpError:
            raise
        except Exception as e:  # noqa: BLE001 — ไดรเวอร์ใดก็ได้
            code = e.args[0] if e.args and isinstance(e.args[0], int) else None
            cls = HosxpTimeout if code == _ER_STATEMENT_TIMEOUT else HosxpError
            raise cls(f"{type(e).__name__}: {e}"[:300]) from e

    def _audit_run(self, queries: list[tuple[str, tuple]]) -> list[list[tuple]]:
        """คำสั่งตรวจก่อนใช้: ตัดที่ AUDIT_TIMEOUT_S วินาที (กันหน้า /check ค้าง)."""
        return self._run(queries, timeout_s=AUDIT_TIMEOUT_S)

    # ---- วินิจฉัยใหม่ ----
    def new_dx(self, since: datetime, icds: tuple[str, ...]) -> list[DxRow]:
        clause, like = _like_params(icds)
        if not like:
            return []
        params = (since.date(),) + like
        opd, ipd = self._run([
            (_OPD_SQL.format(like=clause.format(col="d.icd10")), params),
            (_IPD_SQL.format(like=clause.format(col="d.icd10")), params),
        ])
        rows = [self._row("OPD", r) for r in opd] + [self._row("IPD", r) for r in ipd]
        rows = [r for r in rows if r is not None]
        rows.sort(key=lambda r: (r.dx_at, r.visit_key))
        return rows

    @staticmethod
    def _row(visit_type: str, r: tuple) -> DxRow | None:
        key, hn, icd10, d, t, chw, amp, tmb, moo, addr, sex, birthday = r
        dx_at = _combine(d, t)
        if dx_at is None:
            return None
        prefix = "VN" if visit_type == "OPD" else "AN"
        return DxRow(
            visit_key=f"{prefix}:{str(key).strip()}", visit_type=visit_type,
            hn=str(hn).strip(), icd10=str(icd10).strip(), dx_at=dx_at,
            chw=_clean(chw), amp=_clean(amp), tmb=_clean(tmb), moo=_clean(moo),
            house_no=_clean(addr), sex=_clean(sex),
            age_years=_age_years(birthday, dx_at),
        )

    def dx_present(self, visit_key: str, icds: tuple[str, ...]) -> bool:
        """True ถ้า visit นั้นยังมีวินิจฉัยตามรหัสอยู่; False เฉพาะเมื่อ query สำเร็จและไม่พบแถว."""
        return visit_key in self.dx_present_many([visit_key], icds)

    def dx_present_many(self, visit_keys: list[str],
                        icds: tuple[str, ...]) -> set[str]:
        """ส่วนย่อยของ visit_keys ที่ยังมีวินิจฉัยตามรหัสอยู่ (หนึ่งการเชื่อมต่อต่อหนึ่งการเรียก).

        คีย์ที่ไม่อยู่ในผลลัพธ์ = query สำเร็จแล้วไม่พบ; ถ้า query ไม่สำเร็จจะยก HosxpError
        รหัสโรคว่างยก HosxpError (กันการตีความว่า "ไม่พบ" แล้วยกเลิกทุกเคส)
        """
        if not visit_keys:
            return set()
        clause, like = _like_params(icds)
        if not like:
            raise HosxpError("no disease codes configured")
        queries: list[tuple[str, tuple]] = []
        prefixes: list[str] = []
        for kind, table, col in (("VN", "ovstdiag", "vn"), ("AN", "iptdiag", "an")):
            numbers = [k.partition(":")[2] for k in visit_keys
                       if k.partition(":")[0] == kind]
            for i in range(0, len(numbers), _CHUNK):
                chunk = numbers[i:i + _CHUNK]
                queries.append((
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} IN ("
                    + ",".join(["%s"] * len(chunk)) + ") AND "
                    + clause.format(col="icd10"),
                    tuple(chunk) + like))
                prefixes.append(kind)
        bad = [k for k in visit_keys if k.partition(":")[0] not in ("VN", "AN")]
        if bad:
            raise ValueError(f"visit_key ต้องขึ้นต้น VN: หรือ AN: ได้ {bad[0]!r}")
        present: set[str] = set()
        for kind, rows in zip(prefixes, self._run(queries)):
            present.update(f"{kind}:{str(r[0]).strip()}" for r in rows)
        return present

    # ---- ข้อมูลผู้ติดต่อ (ดึงสด ไม่เก็บ) ----
    def contact(self, hn: str) -> dict:
        empty = {"name": "", "phone": "", "address": ""}
        (rows,) = self._run([(
            "SELECT pname, fname, lname, mobile_phone_number, hometel, informtel,"
            " addrpart, moopart, tmbpart, amppart, chwpart"
            " FROM patient WHERE hn = %s LIMIT 1", (hn,))])
        if not rows:
            return empty
        (pname, fname, lname, mobile, hometel, informtel,
         addr, moo, tmb, amp, chw) = rows[0]
        name = f"{_clean(pname) or ''}{_clean(fname) or ''} {_clean(lname) or ''}".strip()
        phone = next((p for p in (_clean(mobile), _clean(hometel), _clean(informtel)) if p), "")
        chw, amp, tmb = _clean(chw), _clean(amp), _clean(tmb)
        names = self._address_names(chw, amp, tmb)
        parts = []
        if _clean(addr):
            parts.append(f"บ้านเลขที่ {_clean(addr)}")
        moo_s = _clean(moo)
        if moo_s and moo_s.strip("0"):
            parts.append(f"หมู่ {moo_s}")
        for prefix, key, raw in (("ต.", (chw or "") + (amp or "") + (tmb or ""), tmb),
                                 ("อ.", (chw or "") + (amp or ""), amp),
                                 ("จ.", chw or "", chw)):
            label = names.get(key) or raw
            if label:
                parts.append(f"{prefix}{label}")
        return {"name": name, "phone": phone, "address": " ".join(parts)}

    def _address_names(self, chw: str | None, amp: str | None,
                       tmb: str | None) -> dict[str, str]:
        """ชื่อจังหวัด/อำเภอ/ตำบลจาก thaiaddress; ใช้ไม่ได้ก็คืน {} (ผู้เรียกใช้รหัสดิบแทน)."""
        ids = []
        if chw:
            ids.append(chw)
            if amp:
                ids.append(chw + amp)
                if tmb:
                    ids.append(chw + amp + tmb)
        if not ids:
            return {}
        sql = ("SELECT addressid, name FROM thaiaddress WHERE addressid IN ("
               + ",".join(["%s"] * len(ids)) + ")")
        try:
            (rows,) = self._run([(sql, tuple(ids))])
        except HosxpError:
            return {}
        return {str(a).strip(): n for a, n in ((a, _clean(n)) for a, n in rows) if n}

    # ---- หมู่บ้าน/พื้นที่ ----
    def area_label(self, village_code: str) -> str | None:
        """"ต.<ตำบล> ม.<n>" จากรหัสหมู่บ้าน 8 หลัก (chw+amp+tmb+moo); ไม่ทราบ/ผิดพลาด -> None ไม่ยกข้อผิดพลาด.

        ใช้เป็น area_lookup ของข้อความแจ้งเตือน; error ไม่ถูกแคชถาวร แต่งดลองใหม่ 60 วินาที; ไม่พบ แคชถาวร"""
        code = village_code
        if not (isinstance(code, str) and len(code) == 8
                and code.isascii() and code.isdigit()):
            return None
        if code in self._area_cache:
            return self._area_cache[code]
        if self._clock() < self._area_down_until:
            return None  # เพิ่งล้มเหลว: ไม่ต่อใหม่ทุกข้อความแจ้งเตือนระหว่าง HOSxP ล่ม
        try:
            (rows,) = self._run([(
                "SELECT name FROM thaiaddress WHERE addressid = %s LIMIT 1",
                (code[:6],))])
        except Exception:  # noqa: BLE001 — เสริมข้อมูลเท่านั้น ต้องไม่ทำให้การแจ้งเตือนล้ม
            self._area_down_until = self._clock() + AREA_ERROR_BACKOFF_S
            return None
        name = _clean(rows[0][0]) if rows else None
        moo = int(code[6:8])
        label = None
        if name:
            label = f"ต.{name} ม.{moo}" if moo > 0 else f"ต.{name}"
        self._area_cache[code] = label
        return label

    def village_centroid(self, code: str) -> tuple[float, float] | None:
        (rows,) = self._run([(
            "SELECT latitude, longitude FROM village_gis_location"
            " WHERE village_code = %s LIMIT 1", (code,))])
        if not rows or rows[0][0] is None or rows[0][1] is None:
            return None
        return float(rows[0][0]), float(rows[0][1])

    def default_area_amphoe(self) -> tuple[str, ...]:
        (rows,) = self._run([(
            "SELECT DISTINCT LEFT(address_id, 4) FROM village"
            " WHERE address_id IS NOT NULL", ())])
        codes = {str(r[0]).strip()[:4] for r in rows if r[0] is not None}
        return tuple(sorted(c for c in codes if len(c) == 4 and c.isascii() and c.isdigit()))

    # ---- ตรวจก่อนใช้ (setup_check / หน้า /check): คืนเฉพาะจำนวนนับและโครงสร้าง ไม่มีแถวผู้ป่วย ----
    def server_now(self) -> datetime:
        """เวลา NOW() ตามเขตเวลาของ session ฐาน HOSxP."""
        (rows,) = self._audit_run([("SELECT NOW()", ())])
        value = rows[0][0] if rows else None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise HosxpError("NOW() อ่านค่าไม่ได้") from None

    def statement_timeout_supported(self) -> bool:
        """True ถ้าฐานรับ SET SESSION max_statement_time (ต่อฐานไม่ได้ -> ยก HosxpError)."""
        try:
            con = self._connect()
        except HosxpError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HosxpError(f"{type(e).__name__}: {e}"[:300]) from e
        try:
            cur = con.cursor()
            try:
                return _set_statement_timeout(cur, AUDIT_TIMEOUT_S)
            finally:
                cur.close()
        finally:
            con.close()

    def session_read_only(self) -> bool:
        (rows,) = self._audit_run([("SELECT @@SESSION.tx_read_only", ())])
        return bool(rows and rows[0][0] is not None and int(rows[0][0]) == 1)

    def write_privileges(self) -> list[str]:
        """ชื่อสิทธิ์เขียน (เช่น INSERT, UPDATE) ที่บัญชีนี้มีบนฐานปัจจุบันหรือ *.* — ไม่คืนชื่อบัญชี/โฮสต์."""
        db_rows, grant_rows = self._audit_run([("SELECT DATABASE()", ()), ("SHOW GRANTS", ())])
        db = str(db_rows[0][0]) if db_rows and db_rows[0][0] is not None else ""
        found: list[str] = []
        for (line,) in grant_rows:
            m = _GRANT_RE.match(str(line))
            if not m or not _scope_covers_db(m.group(2), db):
                continue
            for priv in _split_privileges(m.group(1)):
                if priv in _WRITE_PRIVILEGES and priv not in found:
                    found.append(priv)
        return found

    def table_columns(self, tables) -> dict[str, dict[str, str]]:
        """{ตาราง: {คอลัมน์: ชนิดข้อมูล}} ของฐานปัจจุบันจาก information_schema (ตารางที่ไม่มีไม่อยู่ในผล)."""
        names = tuple(str(t).lower() for t in tables)
        if not names:
            return {}
        (rows,) = self._audit_run([(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = DATABASE() AND table_name IN ("
            + ",".join(["%s"] * len(names)) + ")", names)])
        out: dict[str, dict[str, str]] = {}
        for table, column, data_type in rows:
            out.setdefault(str(table).lower(), {})[str(column).lower()] = (
                str(data_type).lower())
        return out

    def thai_samples(self) -> list[str]:
        """ชื่อหมู่บ้านบางส่วน (ข้อความไทยที่ไม่ใช่ข้อมูลผู้ป่วย) ไว้ตรวจว่าอ่านภาษาไทยได้."""
        (rows,) = self._audit_run([(
            "SELECT village_name FROM village WHERE village_name IS NOT NULL LIMIT 20", ())])
        return [s for s in (_clean(r[0]) for r in rows) if s]

    def latest_visit_date(self) -> date | None:
        (rows,) = self._audit_run([("SELECT MAX(vstdate) FROM ovstdiag", ())])
        return _to_date(rows[0][0]) if rows else None

    def village_counts(self) -> dict:
        """{'total': หมู่บ้านในตาราง village, 'with_centroid': ที่มีพิกัดใน village_gis_location}."""
        (rows,) = self._audit_run([(
            "SELECT COUNT(DISTINCT v.village_code),"
            " COUNT(DISTINCT CASE WHEN g.village_code IS NOT NULL THEN v.village_code END)"
            " FROM village v LEFT JOIN village_gis_location g"
            " ON g.village_code = v.village_code" + _VALID_GIS_ON, ())])
        total, with_centroid = rows[0] if rows else (0, 0)
        return {"total": int(total or 0), "with_centroid": int(with_centroid or 0)}

    def village_centroids(self, codes: Iterable[str]) -> dict[str, tuple[float, float]]:
        """จุดกลางของหมู่บ้านตามรหัส 8 หลัก (village_gis_location) เฉพาะที่พิกัดใช้ได้ ไว้วางวงกลมปีก่อนบนแผนที่.
        ฐานล้ม/ช้า -> ยก HosxpError (ผู้เรียกต้องแยก "ดึงไม่สำเร็จ" ออกจาก "ไม่มีพิกัดในตาราง" — ไม่คืน {} เงียบ ๆ)."""
        valid = [c for c in dict.fromkeys(str(x).strip() for x in codes)
                 if len(c) == 8 and c.isascii() and c.isdigit()]
        if not valid:
            return {}
        queries = []
        for i in range(0, len(valid), _CHUNK):
            chunk = valid[i:i + _CHUNK]
            queries.append(("SELECT village_code, latitude, longitude FROM village_gis_location"
                            " WHERE village_code IN (" + ",".join(["%s"] * len(chunk)) + ")",
                            tuple(chunk)))
        results = self._run(queries)
        wanted = set(valid)
        out: dict[str, tuple[float, float]] = {}
        for rows in results:
            for code, lat, lng in rows:
                code_s = _clean(code)
                try:
                    lat_f, lng_f = float(lat), float(lng)
                except (TypeError, ValueError):
                    continue
                if (code_s not in wanted or not math.isfinite(lat_f) or not math.isfinite(lng_f)
                        or abs(lat_f) > 90 or abs(lng_f) > 180 or (lat_f == 0 and lng_f == 0)):
                    continue
                out[code_s] = (lat_f, lng_f)
        return out

    def village_points(self) -> list[dict]:
        """จุดกลางหมู่บ้านของ รพ. (village + village_gis_location) ที่พิกัดใช้ได้ ไว้ตรวจด้วยตา."""
        (rows,) = self._audit_run([(
            "SELECT v.village_code, v.village_name, g.latitude, g.longitude"
            " FROM village v JOIN village_gis_location g"
            " ON g.village_code = v.village_code ORDER BY v.village_code", ())])
        points, seen = [], set()
        for code, name, lat, lng in rows:
            code = _clean(code)
            try:
                lat_f, lng_f = float(lat), float(lng)
            except (TypeError, ValueError):
                continue
            if (not code or code in seen or not math.isfinite(lat_f)
                    or not math.isfinite(lng_f) or abs(lat_f) > 90 or abs(lng_f) > 180
                    or (lat_f == 0 and lng_f == 0)):
                continue
            seen.add(code)
            points.append({"code": code, "name": _clean(name) or code,
                           "lat": lat_f, "lng": lng_f})
        return points

    def icd_counts(self, icds: tuple[str, ...], since: date) -> dict[str, int]:
        """{รหัส ICD ที่เก็บจริง: จำนวน visit} ตั้งแต่ `since` เฉพาะรหัสที่ขึ้นต้นตาม icds."""
        clause, like = _like_params(icds)
        if not like:
            return {}
        where = clause.format(col="d.icd10")
        params = (since,) + like
        opd, opd_null, ipd = self._audit_run([
            ("SELECT d.icd10, COUNT(DISTINCT d.vn) FROM ovstdiag d"
             " WHERE d.vstdate >= %s AND " + where + " GROUP BY d.icd10", params),
            ("SELECT d.icd10, COUNT(DISTINCT d.vn) FROM ovstdiag d"
             " JOIN ovst o ON o.vn = d.vn"
             " WHERE d.vstdate IS NULL AND o.vstdate >= %s AND " + where
             + " GROUP BY d.icd10", params),
            ("SELECT d.icd10, COUNT(DISTINCT d.an) FROM iptdiag d"
             " JOIN ipt i ON i.an = d.an"
             " WHERE i.regdate >= %s AND " + where + " GROUP BY d.icd10", params),
        ])
        out: dict[str, int] = {}
        for code, n in list(opd) + list(opd_null) + list(ipd):
            key = (_clean(code) or "").upper()
            if key:
                out[key] = out.get(key, 0) + int(n or 0)
        return out

    def area_counts(self, icds: tuple[str, ...], since: date) -> dict[str, dict]:
        """{จังหวัด+อำเภอ (4 หลัก): {'visits': จำนวน visit, 'mapped': ที่จับคู่พิกัดหมู่บ้านได้}}."""
        clause, like = _like_params(icds)
        if not like:
            return {}
        where = clause.format(col="d.icd10")
        params = (since,) + like
        join = (" LEFT JOIN village_gis_location g ON g.village_code = CONCAT("
                "p.chwpart, p.amppart, p.tmbpart, LPAD(TRIM(p.moopart), 2, '0'))"
                " AND CAST(p.moopart AS UNSIGNED) > 0" + _VALID_GIS_ON)
        opd, opd_null, ipd = self._audit_run([
            ("SELECT CONCAT(p.chwpart, p.amppart), COUNT(DISTINCT d.vn),"
             " COUNT(DISTINCT CASE WHEN g.village_code IS NOT NULL THEN d.vn END)"
             " FROM ovstdiag d JOIN patient p ON p.hn = d.hn" + join
             + " WHERE d.vstdate >= %s AND " + where + " GROUP BY 1", params),
            ("SELECT CONCAT(p.chwpart, p.amppart), COUNT(DISTINCT d.vn),"
             " COUNT(DISTINCT CASE WHEN g.village_code IS NOT NULL THEN d.vn END)"
             " FROM ovstdiag d JOIN ovst o ON o.vn = d.vn"
             " JOIN patient p ON p.hn = d.hn" + join
             + " WHERE d.vstdate IS NULL AND o.vstdate >= %s AND " + where
             + " GROUP BY 1", params),
            ("SELECT CONCAT(p.chwpart, p.amppart), COUNT(DISTINCT d.an),"
             " COUNT(DISTINCT CASE WHEN g.village_code IS NOT NULL THEN d.an END)"
             " FROM iptdiag d JOIN ipt i ON i.an = d.an"
             " JOIN patient p ON p.hn = i.hn" + join
             + " WHERE i.regdate >= %s AND " + where + " GROUP BY 1", params),
        ])
        out: dict[str, dict] = {}
        for code, visits, mapped in list(opd) + list(opd_null) + list(ipd):
            slot = out.setdefault(_clean(code) or "", {"visits": 0, "mapped": 0})
            slot["visits"] += int(visits or 0)
            slot["mapped"] += int(mapped or 0)
        return out

    # ---- หน้าเปรียบเทียบปี พ.ศ. (/trend): แถวรายคนอยู่ในหน่วยความจำเพื่อรวมเป็นจำนวนนับแล้วทิ้ง ----
    def episode_rows(self, icds: tuple[str, ...], start: date, end: date) -> list[VisitRow]:
        """visit ของรหัส `icds` ในช่วง [start, end] พร้อมที่อยู่ตามทะเบียน (OPD แล้ว IPD)."""
        clause, like = _like_params(icds)
        if not like:
            return []
        params = (start, end) + like
        opd_params = (start, end, start, end) + like
        opd, ipd = self._run([
            ("SELECT DISTINCT d.hn, COALESCE(d.vstdate, o.vstdate), p.chwpart, p.amppart,"
             " p.tmbpart, p.moopart"
             " FROM ovstdiag d LEFT JOIN ovst o ON o.vn = d.vn"
             " JOIN patient p ON p.hn = d.hn"
             " WHERE (d.vstdate BETWEEN %s AND %s"
             " OR (d.vstdate IS NULL AND o.vstdate BETWEEN %s AND %s)) AND "
             + clause.format(col="d.icd10"), opd_params),
            ("SELECT DISTINCT i.hn, i.regdate, p.chwpart, p.amppart, p.tmbpart, p.moopart"
             " FROM iptdiag d JOIN ipt i ON i.an = d.an JOIN patient p ON p.hn = i.hn"
             " WHERE i.regdate BETWEEN %s AND %s AND " + clause.format(col="d.icd10"), params),
        ], timeout_s=TREND_TIMEOUT_S)
        out: list[VisitRow] = []
        for hn, day, chw, amp, tmb, moo in list(opd) + list(ipd):
            hn_s, day_d = _clean(hn), _to_date(day)
            if not hn_s or day_d is None:
                continue
            out.append(VisitRow(hn_s, day_d, _clean(chw), _clean(amp), _clean(tmb), _clean(moo)))
        return out

    def visits_in_ranges(self, icds: tuple[str, ...],
                         ranges: list[tuple[date, date]]) -> list[int]:
        """จำนวน visit (OPD + IPD) ของรหัส `icds` ในแต่ละช่วง [start, end] (การเชื่อมต่อเดียว)
        ไว้ตรวจว่าปี พ.ศ.นั้นมีข้อมูลไหม."""
        clause, like = _like_params(icds)
        if not like or not ranges:
            return [0] * len(ranges)
        where = clause.format(col="d.icd10")
        queries: list[tuple[str, tuple]] = []
        for start, end in ranges:
            params = (start, end) + like
            queries.append(("SELECT COUNT(DISTINCT d.vn) FROM ovstdiag d LEFT JOIN ovst o ON o.vn = d.vn"
                            " WHERE (d.vstdate BETWEEN %s AND %s"
                            " OR (d.vstdate IS NULL AND o.vstdate BETWEEN %s AND %s)) AND " + where,
                            (start, end, start, end) + like))
            queries.append(("SELECT COUNT(DISTINCT d.an) FROM iptdiag d JOIN ipt i ON i.an = d.an"
                            " WHERE i.regdate BETWEEN %s AND %s AND " + where, params))
        results = self._audit_run(queries)
        counts = [int(r[0][0] or 0) if r else 0 for r in results]
        return [counts[i] + counts[i + 1] for i in range(0, len(counts), 2)]

    def facilities(self, area: tuple[str, ...]) -> list[dict]:
        """รพ./รพ.สต. ของอำเภอใน `area` จาก hospcode: [{"hospcode","name","kind","tmb"}] (ไม่มีพิกัด — HOSxP ไม่เก็บ).
        คัดเฉพาะรหัส 5 หลักตัวเลข (ตัดรหัส 9 หลักที่ซ้ำรายการเดียวกัน) และชื่อขึ้นต้น "โรงพยาบาล"; ล้มเหลวยก HosxpError."""
        codes = [a for a in dict.fromkeys(str(x).strip() for x in area)
                 if len(a) == 4 and a.isascii() and a.isdigit()]
        if not codes:
            return []
        sql = ("SELECT hospcode, name, chwpart, amppart, tmbpart FROM hospcode"
               " WHERE CONCAT(chwpart, amppart) IN (" + ",".join(["%s"] * len(codes)) + ") AND name LIKE %s")
        (rows,) = self._run([(sql, tuple(codes) + (_fac.HOSPITAL_PREFIX + "%",))])
        out: dict[str, dict] = {}
        for code, name, chw, amp, tmb in rows:
            code_s, name_s = _clean(code), _clean(name)
            if not code_s or not (len(code_s) == 5 and code_s.isascii() and code_s.isdigit()) or not name_s:
                continue
            kind = _fac.classify(name_s)
            tmb6 = "".join(str(_clean(x) or "") for x in (chw, amp, tmb))
            if kind is None or not (len(tmb6) == 6 and tmb6.isascii() and tmb6.isdigit()):
                continue
            out.setdefault(code_s, {"hospcode": code_s, "name": name_s, "kind": kind, "tmb": tmb6})
        return [out[k] for k in sorted(out)]

    def address_names(self, codes: Iterable[str]) -> dict[str, str]:
        """ชื่อตำบล (รหัส 6 หลัก, thaiaddress) และหมู่บ้าน (รหัส 8 หลัก, village) -> ชื่อ;
        ผิดพลาดคืน {} (ผู้เรียกใช้รหัสดิบแทน). ชื่อสถานที่ ไม่ใช่ข้อมูลผู้ป่วย."""
        tambons: list[str] = []
        villages: list[str] = []
        for c in dict.fromkeys(str(x).strip() for x in codes):
            if c.isascii() and c.isdigit():
                if len(c) == 6:
                    tambons.append(c)
                elif len(c) == 8:
                    villages.append(c)
        queries: list[tuple[str, tuple]] = []
        for i in range(0, len(tambons), _CHUNK):
            chunk = tambons[i:i + _CHUNK]
            queries.append(("SELECT addressid, name FROM thaiaddress WHERE addressid IN ("
                            + ",".join(["%s"] * len(chunk)) + ")", tuple(chunk)))
        for i in range(0, len(villages), _CHUNK):
            chunk = villages[i:i + _CHUNK]
            queries.append(("SELECT village_code, village_name FROM village WHERE village_code IN ("
                            + ",".join(["%s"] * len(chunk)) + ")", tuple(chunk)))
        if not queries:
            return {}
        try:
            results = self._run(queries)
        except HosxpError:
            return {}
        names: dict[str, str] = {}
        for rows in results:
            for code, name in rows:
                code_s, name_s = _clean(code), _clean(name)
                if code_s and name_s:
                    names[code_s] = name_s
        return names


# เงื่อนไขพิกัดใน village_gis_location ที่ใช้ได้ (ไม่ NULL ไม่ว่าง) ต่อท้าย ON ของ LEFT JOIN
_VALID_GIS_ON = (" AND g.latitude IS NOT NULL AND g.longitude IS NOT NULL"
                 " AND TRIM(g.latitude) <> '' AND TRIM(g.longitude) <> ''")
_GRANT_RE = re.compile(r"^GRANT\s+(.*?)\s+ON\s+(.*?)\s+TO\s", re.I | re.S)
_SCOPE_RE = re.compile(r"^(\*|`(?:[^`]|``)*`)\.(\*|`(?:[^`]|``)*`)$")
_WRITE_PRIVILEGES = frozenset({
    "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "INDEX", "TRIGGER", "EVENT",
    "CREATE ROUTINE", "ALTER ROUTINE", "CREATE VIEW", "CREATE TEMPORARY TABLES",
    "ALL PRIVILEGES", "ALL", "SUPER", "FILE"})


def _set_statement_timeout(cur, seconds: int) -> bool:
    """ตั้ง max_statement_time ของ session (MariaDB, วินาที); ฐานไม่รับ -> False ไม่ยกข้อผิดพลาด."""
    try:
        cur.execute(f"SET SESSION max_statement_time = {int(seconds)}")
        return True
    except Exception:  # noqa: BLE001 — ไม่รองรับ/ไม่มีสิทธิ์: ทำต่อโดยไม่ตั้ง
        return False


def _split_privileges(text: str) -> list[str]:
    """แยกรายชื่อสิทธิ์ที่คั่นด้วยจุลภาค (ไม่ตัดจุลภาคในวงเล็บของสิทธิ์ระดับคอลัมน์)."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [" ".join(p.split()).upper() for p in parts if p.strip()]


def _scope_covers_db(scope: str, db: str) -> bool:
    """True ถ้า `ON <scope>` ครอบคลุมทั้งฐาน db (`*.*` หรือ `db`.*; ระดับตาราง/คอลัมน์ไม่นับ)."""
    m = _SCOPE_RE.match(scope.strip())
    if not m or m.group(2) != "*":
        return False
    if m.group(1) == "*":
        return True
    name = m.group(1)[1:-1].replace("``", "`")
    pattern, i = "", 0
    while i < len(name):
        ch = name[i]
        if ch == "\\" and i + 1 < len(name):
            pattern += re.escape(name[i + 1])
            i += 2
            continue
        pattern += ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
        i += 1
    return bool(db) and re.fullmatch(pattern, db, re.I) is not None
