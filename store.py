"""ฐานข้อมูลของแอป (SQLite) — ดูสเปกข้อ 9.

เก็บเฉพาะคีย์ รหัสโรค วันเวลา รหัสหมู่บ้าน บ้านเลขที่ เพศ อายุ และพิกัด
ไม่เก็บชื่อ เบอร์โทร หรือที่อยู่เต็ม
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from trend import CountRow

ROLES = ("srrt", "viewer", "admin")
MIN_PASSWORD_LEN = 10  # run.py add-user / set-password บังคับ (Store รับรหัสสั้นกว่านี้ได้ในเทสต์)
PIN_STATES = ("estimated", "confirmed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS "case" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_key TEXT NOT NULL UNIQUE,
    visit_type TEXT NOT NULL,
    hn TEXT NOT NULL,
    icd10 TEXT NOT NULL,
    dx_at TEXT NOT NULL,
    village_code TEXT,
    house_no TEXT,
    sex TEXT,
    age_years INTEGER,
    in_area INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    pin_state TEXT NOT NULL DEFAULT 'none',
    alerted_at TEXT,
    absent_cycles INTEGER NOT NULL DEFAULT 0,
    disease TEXT NOT NULL DEFAULT 'dengue'
);
CREATE INDEX IF NOT EXISTS idx_case_hn ON "case"(hn);
CREATE INDEX IF NOT EXISTS idx_case_dx ON "case"(dx_at);

CREATE TABLE IF NOT EXISTS pin (
    case_id INTEGER PRIMARY KEY REFERENCES "case"(id),
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hn_location (
    hn TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS house_location (
    village_code TEXT NOT NULL,
    house_no TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (village_code, house_no)
);

CREATE TABLE IF NOT EXISTS cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_case_ids TEXT NOT NULL,
    first_dx_at TEXT NOT NULL,
    last_dx_at TEXT NOT NULL,
    alerted_size INTEGER NOT NULL DEFAULT 0,
    alerted_at TEXT,
    disease TEXT NOT NULL DEFAULT 'dengue'
);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER REFERENCES "case"(id),
    cluster_id INTEGER REFERENCES cluster(id),
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    response_code INTEGER,
    error TEXT,
    size INTEGER,
    rearmed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_case ON alert_log(kind, case_id);
CREATE INDEX IF NOT EXISTS idx_alert_cluster ON alert_log(kind, cluster_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    case_id INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS "user" (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL
);

-- ที่ตั้งสถานพยาบาล (รพ./รพ.สต.) บนแผนที่รวม: เฉพาะที่ตั้งสถานพยาบาล (สาธารณะ) ไม่มีข้อมูลผู้ป่วย
-- วันที่เริ่มติดตามโรค (ไม่ใช่ไข้เลือดออก): โรคที่เพิ่งเปิดไม่ย้อนแจ้งเคสก่อนวันนี้; ปิดโรคแล้วแถวนี้ถูกลบ
CREATE TABLE IF NOT EXISTS disease_enabled (
    disease TEXT PRIMARY KEY,
    enabled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facility (
    hospcode TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    tmb TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- แคชของหน้าเปรียบเทียบปี พ.ศ. (/trend) แยกต่อโรค: เฉพาะจำนวนนับต่อเดือน/พื้นที่ ไม่มี HN ชื่อ หรือที่อยู่รายคน
-- (ใช้ชื่อตารางใหม่ _d — ตาราง trend_cache/trend_meta ของ 1.4.0 ไม่ถูกแตะ เพื่อถอยกลับโค้ด 1.4.0 ได้)
CREATE TABLE IF NOT EXISTS trend_cache_d (
    disease TEXT NOT NULL DEFAULT 'dengue',
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    in_area INTEGER NOT NULL,
    tmb TEXT NOT NULL,
    moo TEXT NOT NULL,
    cases INTEGER NOT NULL,
    PRIMARY KEY (disease, year, month, in_area, tmb, moo)
);
CREATE TABLE IF NOT EXISTS trend_meta_d (
    disease TEXT NOT NULL DEFAULT 'dengue',
    year INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    signature TEXT NOT NULL,
    PRIMARY KEY (disease, year)
);
"""


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """แฮช pbkdf2 สำหรับเทียบเมื่อไม่พบผู้ใช้ (คำนวณครั้งเดียวแบบ lazy) — ให้เวลาเท่าผู้ใช้จริง."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("dummy-password-for-timing",
                                             method="pbkdf2:sha256")
    return _DUMMY_HASH


def backup_database(src_path: str, dest_path: str) -> None:
    """สำรองฐานด้วย sqlite3 backup API (ได้สำเนาที่สอดคล้องแม้แอปกำลังเขียน — ห้ามใช้ cp ไฟล์สด)
    เปิดการเชื่อมต่อใหม่ทั้งสองฝั่งต่อครั้ง (ไม่ถือมือจับค้าง) เขียนไฟล์ชั่วคราวแล้ว rename เมื่อ
    `PRAGMA integrity_check` ผ่าน; ไฟล์ปลายทางเดิมที่ชื่อซ้ำถูกแทนที่ทั้งไฟล์.
    ยก sqlite3.Error / RuntimeError เมื่อสำเร็จไม่ได้ (ไม่ทิ้งไฟล์ชั่วคราว)."""
    tmp = dest_path + ".tmp"
    Path(tmp).unlink(missing_ok=True)
    src = sqlite3.connect(f"{Path(src_path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("สำเนาไม่ผ่าน integrity_check")
        finally:
            dst.close()
        os.replace(tmp, dest_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    finally:
        src.close()


def _add_column(con, sql: str) -> None:
    """ALTER ... ADD COLUMN ที่ทนโปรเซสอื่นเพิ่มคอลัมน์เดียวกันไปก่อน (เริ่มแอปพร้อมกัน)."""
    try:
        con.execute(sql)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _case_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["dx_at"] = _dt(d["dx_at"])
    d["alerted_at"] = _dt(d["alerted_at"])
    d["in_area"] = bool(d["in_area"])
    return d


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            cols = [r["name"] for r in con.execute("PRAGMA table_info(alert_log)")]
            if "size" not in cols:  # ฐานเก่าที่สร้างก่อนมีคอลัมน์ size
                con.execute("ALTER TABLE alert_log ADD COLUMN size INTEGER")
            if "rearmed" not in cols:  # ฐานเก่าก่อนมีคำสั่ง rearm-alerts
                con.execute("ALTER TABLE alert_log ADD COLUMN rearmed"
                            " INTEGER NOT NULL DEFAULT 0")
            case_cols = [r["name"] for r in con.execute('PRAGMA table_info("case")')]
            if "absent_cycles" not in case_cols:  # ฐานเก่าก่อนมีตัวนับรอบที่รหัสหาย (R24)
                con.execute('ALTER TABLE "case" ADD COLUMN absent_cycles'
                            " INTEGER NOT NULL DEFAULT 0")
            if "disease" not in case_cols:  # ฐานเก่าก่อนรองรับหลายโรค: เคสเดิมทั้งหมดเป็นไข้เลือดออก
                _add_column(con, 'ALTER TABLE "case" ADD COLUMN disease TEXT NOT NULL DEFAULT \'dengue\'')
            cluster_cols = [r["name"] for r in con.execute("PRAGMA table_info(cluster)")]
            if "disease" not in cluster_cols:
                _add_column(con, "ALTER TABLE cluster ADD COLUMN disease TEXT NOT NULL DEFAULT 'dengue'")
            con.execute('CREATE INDEX IF NOT EXISTS idx_case_disease_dx ON "case"(disease, dx_at)')

    @contextmanager
    def _connect(self):
        """เชื่อมต่อใหม่ต่อการเรียก (ปลอดภัยกับเธรดตัวตรวจจับ + เว็บ); commit เมื่อสำเร็จ."""
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    # ---- cases ----
    def add_case(self, visit_key: str, visit_type: str, hn: str, icd10: str,
                 dx_at: datetime, village_code: str | None,
                 house_no: str | None, sex: str | None,
                 age_years: int | None, in_area: bool,
                 disease: str = "dengue") -> int | None:
        house = house_no.strip() if house_no else None
        with self._connect() as con:
            cur = con.execute(
                'INSERT OR IGNORE INTO "case" (visit_key, visit_type, hn, icd10,'
                " dx_at, village_code, house_no, sex, age_years, in_area, disease)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (visit_key, visit_type, hn, icd10, dx_at.isoformat(),
                 village_code, house or None, sex, age_years, int(in_area), disease),
            )
            return cur.lastrowid if cur.rowcount else None

    def get_case(self, case_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute('SELECT * FROM "case" WHERE id=?', (case_id,)).fetchone()
        return _case_dict(row) if row else None

    def active_cases(self, disease: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM \"case\" WHERE status='active'", []
        if disease is not None:
            sql += " AND disease=?"
            args.append(disease)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY dx_at, id", args).fetchall()
        return [_case_dict(r) for r in rows]

    def list_cases(self, since: date, status: str | None = None,
                   in_area: bool | None = None, disease: str | None = None) -> list[dict]:
        sql = 'SELECT * FROM "case" WHERE dx_at >= ?'
        args: list = [since.isoformat()]
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        if in_area is not None:
            sql += " AND in_area=?"
            args.append(int(in_area))
        if disease is not None:
            sql += " AND disease=?"
            args.append(disease)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY dx_at DESC, id DESC", args).fetchall()
        return [_case_dict(r) for r in rows]

    def active_counts(self, since: date) -> dict[str, int]:
        """จำนวนเคส active ต่อโรคที่วินิจฉัยตั้งแต่ `since` (เฉพาะโรคที่มีเคส) — ใช้กับตัวเลขบนแท็บเลือกโรค."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT disease, COUNT(*) AS n FROM \"case\" WHERE status='active' AND dx_at >= ?"
                " GROUP BY disease", (since.isoformat(),)).fetchall()
        return {r["disease"]: r["n"] for r in rows}

    def disease_enabled_at(self, key: str, now: datetime) -> datetime:
        """วันที่เริ่มติดตามโรค `key` (บันทึก `now` ถ้ายังไม่มี) — ใช้กั้นไม่ให้ย้อนแจ้งเคสก่อนเปิดโรค."""
        with self._connect() as con:
            con.execute("INSERT OR IGNORE INTO disease_enabled (disease, enabled_at) VALUES (?,?)",
                        (key, now.isoformat(timespec="seconds")))
            row = con.execute("SELECT enabled_at FROM disease_enabled WHERE disease=?", (key,)).fetchone()
        return datetime.fromisoformat(row["enabled_at"])

    def forget_disabled_diseases(self, enabled_keys) -> None:
        """ลบวันเริ่มติดตามของโรคที่ไม่อยู่ในรายการที่เปิดอยู่ (เปิดใหม่ภายหลังเริ่มนับใหม่)."""
        keys = list(enabled_keys)
        with self._connect() as con:
            con.execute("DELETE FROM disease_enabled WHERE disease NOT IN (%s)" % ",".join("?" * len(keys)), keys)

    def cluster_disease(self, cluster_id: int) -> str | None:
        with self._connect() as con:
            row = con.execute("SELECT disease FROM cluster WHERE id=?", (cluster_id,)).fetchone()
        return row["disease"] if row else None

    # ---- สถานพยาบาลบนแผนที่ ----
    def facility_all(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM facility ORDER BY CASE kind WHEN 'hospital' THEN 0 ELSE 1 END,"
                               " name, hospcode").fetchall()
        return [dict(r) for r in rows]

    def facility_get(self, code: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM facility WHERE hospcode=?", (code,)).fetchone()
        return dict(row) if row else None

    _FACILITY_UPSERT = (
        "INSERT INTO facility (hospcode, name, kind, tmb, lat, lng, source, updated_at, updated_by)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(hospcode) DO UPDATE SET name=excluded.name, kind=excluded.kind, tmb=excluded.tmb,"
        " lat=excluded.lat, lng=excluded.lng, source=excluded.source, updated_at=excluded.updated_at,"
        " updated_by=excluded.updated_by")

    def facility_upsert(self, code: str, name: str, kind: str, tmb: str, lat: float, lng: float,
                        source: str, user: str, at: datetime) -> None:
        with self._connect() as con:
            con.execute(self._FACILITY_UPSERT,
                        (code, name, kind, tmb, lat, lng, source, at.isoformat(timespec="seconds"), user))

    def facility_bulk_upsert(self, rows: list[dict], user: str, at: datetime,
                             overwrite_manual: bool) -> tuple[int, list[str]]:
        """นำเข้า (source='csv') ใน transaction เดียว; แถวที่ผู้ดูแลวางเอง (source='manual') ไม่ถูกทับถ้าไม่ overwrite_manual.
        คืน (จำนวนที่เขียน, รหัสที่ข้ามเพราะเป็น manual); แถวใดผิด -> ยกข้อผิดพลาดและไม่เขียนอะไรเลย."""
        stamp = at.isoformat(timespec="seconds")
        written, skipped = 0, []
        with self._connect() as con:
            manual = {r["hospcode"] for r in con.execute("SELECT hospcode FROM facility WHERE source='manual'")}
            for r in rows:
                if r["hospcode"] in manual and not overwrite_manual:
                    skipped.append(r["hospcode"])
                    continue
                con.execute(self._FACILITY_UPSERT, (r["hospcode"], r["name"], r["kind"], r["tmb"], r["lat"],
                                                    r["lng"], "csv", stamp, user))
                written += 1
        return written, skipped

    # ---- แคชหน้าเปรียบเทียบปี พ.ศ. ----
    def trend_get(self, year: int, disease: str = "dengue") -> tuple[dict, list[CountRow]] | None:
        with self._connect() as con:
            meta = con.execute("SELECT computed_at, signature FROM trend_meta_d WHERE year=? AND disease=?",
                               (year, disease)).fetchone()
            if meta is None:
                return None
            rows = con.execute("SELECT month, in_area, tmb, moo, cases FROM trend_cache_d"
                               " WHERE year=? AND disease=? ORDER BY month, in_area DESC, tmb, moo",
                               (year, disease)).fetchall()
        return ({"computed_at": datetime.fromisoformat(meta["computed_at"]),
                 "signature": meta["signature"]},
                [CountRow(r["month"], bool(r["in_area"]), r["tmb"], r["moo"], r["cases"]) for r in rows])

    def trend_put(self, year: int, signature: str, rows: list[CountRow], computed_at: datetime,
                  disease: str = "dengue") -> None:
        """แทนที่แคชของปี พ.ศ.นั้น (เฉพาะโรคนั้น) ทั้งปีใน transaction เดียว."""
        with self._connect() as con:
            con.execute("DELETE FROM trend_cache_d WHERE year=? AND disease=?", (year, disease))
            con.executemany(
                "INSERT INTO trend_cache_d (disease, year, month, in_area, tmb, moo, cases) VALUES (?,?,?,?,?,?,?)",
                [(disease, year, r.month, int(r.in_area), r.tmb, r.moo, r.cases) for r in rows])
            con.execute("INSERT INTO trend_meta_d (disease, year, computed_at, signature) VALUES (?,?,?,?)"
                        " ON CONFLICT(disease, year) DO UPDATE SET computed_at=excluded.computed_at,"
                        " signature=excluded.signature",
                        (disease, year, computed_at.isoformat(timespec="seconds"), signature))

    def recent_case_for_hn(self, hn: str, since: datetime, disease: str | None = None) -> dict | None:
        sql, args = "SELECT * FROM \"case\" WHERE hn=? AND status='active' AND dx_at >= ?", [hn, since.isoformat()]
        if disease is not None:
            sql += " AND disease=?"
            args.append(disease)
        with self._connect() as con:
            row = con.execute(sql + " ORDER BY dx_at DESC, id DESC LIMIT 1", args).fetchone()
        return _case_dict(row) if row else None

    def mark_cancelled(self, case_id: int) -> None:
        with self._connect() as con:
            con.execute("UPDATE \"case\" SET status='cancelled' WHERE id=?", (case_id,))

    def update_absent_cycles(self, counts: dict[int, int]) -> None:
        """ตั้งตัวนับ "รอบที่รหัสวินิจฉัยหายติดกัน" ของหลายเคสใน transaction เดียว (id -> ค่าใหม่)."""
        if not counts:
            return
        with self._connect() as con:
            con.executemany('UPDATE "case" SET absent_cycles=? WHERE id=?',
                            [(n, cid) for cid, n in counts.items()])

    def reactivate_case(self, case_id: int) -> bool:
        """คืนสถานะเคสที่ถูกยกเลิก (รหัสกลับมา): status=active, absent_cycles=0, หมุดคงเดิม.
        คืน True เมื่อเปลี่ยนจริง (เคสต้องเป็น cancelled อยู่)."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE \"case\" SET status='active', absent_cycles=0"
                " WHERE id=? AND status='cancelled'", (case_id,))
            return cur.rowcount > 0

    # ---- pins ----
    def set_pin(self, case_id: int, lat: float, lng: float, state: str,
                source: str, user: str) -> None:
        if state not in PIN_STATES:
            raise ValueError(f"state must be one of {PIN_STATES}")
        now = _now()
        with self._connect() as con:
            case = con.execute(
                'SELECT hn, village_code, house_no FROM "case" WHERE id=?', (case_id,)
            ).fetchone()
            if case is None:
                raise ValueError(f"case {case_id} not found")
            old = con.execute(
                "SELECT lat, lng, state, source FROM pin WHERE case_id=?", (case_id,)
            ).fetchone()
            con.execute(
                "INSERT INTO pin (case_id, lat, lng, source, state, set_by, set_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(case_id) DO UPDATE SET lat=excluded.lat,"
                " lng=excluded.lng, source=excluded.source, state=excluded.state,"
                " set_by=excluded.set_by, set_at=excluded.set_at",
                (case_id, lat, lng, source, state, user, now),
            )
            con.execute('UPDATE "case" SET pin_state=? WHERE id=?', (state, case_id))
            if state == "confirmed":
                con.execute(
                    "INSERT INTO hn_location (hn, lat, lng, confirmed_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(hn) DO UPDATE SET lat=excluded.lat, lng=excluded.lng,"
                    " confirmed_at=excluded.confirmed_at",
                    (case["hn"], lat, lng, now),
                )
                house = (case["house_no"] or "").strip()
                if case["village_code"] and house:
                    con.execute(
                        "INSERT INTO house_location (village_code, house_no, lat, lng,"
                        " confirmed_at) VALUES (?,?,?,?,?)"
                        " ON CONFLICT(village_code, house_no) DO UPDATE SET"
                        " lat=excluded.lat, lng=excluded.lng,"
                        " confirmed_at=excluded.confirmed_at",
                        (case["village_code"], house, lat, lng, now),
                    )
            detail = {
                "before": dict(old) if old else None,
                "after": {"lat": lat, "lng": lng, "state": state, "source": source},
            }
            con.execute(
                "INSERT INTO audit_log (at, user, action, case_id, detail)"
                " VALUES (?,?,?,?,?)",
                (now, user, "set_pin", case_id, json.dumps(detail, ensure_ascii=False)),
            )

    def get_pin(self, case_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM pin WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["set_at"] = _dt(d["set_at"])
        return d

    def default_pin(self, hn: str, village_code: str | None,
                    house_no: str | None) -> tuple[float, float, str] | None:
        house = (house_no or "").strip()
        with self._connect() as con:
            if village_code and house:
                row = con.execute(
                    "SELECT lat, lng FROM house_location WHERE village_code=? AND house_no=?",
                    (village_code, house),
                ).fetchone()
                if row:
                    return row["lat"], row["lng"], "house_memory"
            row = con.execute(
                "SELECT lat, lng FROM hn_location WHERE hn=?", (hn,)
            ).fetchone()
        return (row["lat"], row["lng"], "hn_memory") if row else None

    def pinned_cases(self, since: date | None = None, disease: str | None = None) -> list[dict]:
        sql = ('SELECT c.*, p.lat AS lat, p.lng AS lng, p.source AS pin_source'
               ' FROM "case" c JOIN pin p ON p.case_id = c.id')
        args: list = []
        where = []
        if since is not None:
            where.append("c.dx_at >= ?")
            args.append(since.isoformat())
        if disease is not None:
            where.append("c.disease = ?")
            args.append(disease)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY c.dx_at, c.id", args).fetchall()
        return [_case_dict(r) for r in rows]

    def audit(self, case_id: int | None = None) -> list[dict]:
        """แถว audit_log เรียงตามลำดับเกิด (กรองตามเคสได้); detail ถอดจาก JSON แล้ว."""
        sql, args = "SELECT * FROM audit_log", []
        if case_id is not None:
            sql += " WHERE case_id=?"
            args.append(case_id)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    def add_audit(self, action: str, user: str, detail: str | None = None,
                  case_id: int | None = None) -> None:
        """เขียนแถว audit_log ทั่วไป (ที่ไม่ใช่ปักหมุด). `detail` เก็บเป็น JSON string เหมือนแถว set_pin
        เพื่อให้ `audit()` ถอดได้ — ผู้เรียกต้องไม่ส่งความลับ/ค่าที่ผู้ใช้กรอกมาใน action หรือ detail."""
        with self._connect() as con:
            con.execute(
                "INSERT INTO audit_log (at, user, action, case_id, detail) VALUES (?,?,?,?,?)",
                (_now(), user, action, case_id,
                 None if detail is None else json.dumps(detail, ensure_ascii=False)))

    # ---- users ----
    def add_user(self, username: str, password: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        with self._connect() as con:
            con.execute(
                'INSERT INTO "user" (username, password_hash, role) VALUES (?,?,?)',
                (username, generate_password_hash(password, method="pbkdf2:sha256"), role),
            )

    def user_role(self, username: str) -> str | None:
        """บทบาทปัจจุบันในฐาน หรือ None ถ้าไม่มีผู้ใช้นี้ (ใช้ตรวจ session ทุกคำขอ)."""
        with self._connect() as con:
            row = con.execute('SELECT role FROM "user" WHERE username=?', (username,)).fetchone()
        return row["role"] if row else None

    def set_password(self, username: str, password: str) -> bool:
        """เปลี่ยนรหัสผ่านของผู้ใช้ที่มีอยู่ (pbkdf2:sha256); False ถ้าไม่มีผู้ใช้นี้."""
        with self._connect() as con:
            cur = con.execute(
                'UPDATE "user" SET password_hash=? WHERE username=?',
                (generate_password_hash(password, method="pbkdf2:sha256"), username))
            return cur.rowcount > 0

    def remove_user(self, username: str) -> bool:
        """ลบผู้ใช้; False ถ้าไม่มีผู้ใช้นี้. session ที่ค้างของผู้ใช้นี้ใช้ต่อไม่ได้ (ตรวจทุกคำขอ)."""
        with self._connect() as con:
            cur = con.execute('DELETE FROM "user" WHERE username=?', (username,))
            return cur.rowcount > 0

    def check_user(self, username: str, password: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                'SELECT password_hash, role FROM "user" WHERE username=?', (username,)
            ).fetchone()
        # ไม่พบผู้ใช้ก็ยังคำนวณ pbkdf2 เทียบแฮมจำลอง กัน timing เดาว่ามี username นี้หรือไม่
        stored = row["password_hash"] if row else _dummy_hash()
        ok = check_password_hash(stored, password)
        return row["role"] if (row and ok) else None

    # ---- alerts ----
    def log_alert(self, kind: str, case_id: int | None, cluster_id: int | None,
                  ok: bool, response_code: int | None, error: str | None,
                  size: int | None = None) -> int:
        with self._connect() as con:
            return self._insert_alert(con, kind, case_id, cluster_id, ok,
                                      response_code, error, size)

    @staticmethod
    def _insert_alert(con, kind, case_id, cluster_id, ok, response_code, error, size):
        cur = con.execute(
            "INSERT INTO alert_log (case_id, cluster_id, kind, sent_at, ok,"
            " response_code, error, size) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, cluster_id, kind, _now(), int(ok), response_code, error, size),
        )
        return cur.lastrowid

    @staticmethod
    def _alert_where(kind: str, case_id: int | None, cluster_id: int | None,
                     size: int | None = None):
        sql = "kind=?"
        args: list = [kind]
        if case_id is not None:
            sql += " AND case_id=?"
            args.append(case_id)
        if cluster_id is not None:
            sql += " AND cluster_id=?"
            args.append(cluster_id)
        if size is not None:
            sql += " AND size=?"
            args.append(size)
        return sql, args

    def alert_attempts(self, kind: str, case_id: int | None = None,
                       cluster_id: int | None = None) -> int:
        where, args = self._alert_where(kind, case_id, cluster_id)
        with self._connect() as con:
            return con.execute(
                f"SELECT COUNT(*) FROM alert_log WHERE {where}", args
            ).fetchone()[0]

    def alert_sent(self, kind: str, case_id: int | None = None,
                   cluster_id: int | None = None) -> bool:
        where, args = self._alert_where(kind, case_id, cluster_id)
        with self._connect() as con:
            return con.execute(
                f"SELECT COUNT(*) FROM alert_log WHERE ok=1 AND {where}", args
            ).fetchone()[0] > 0

    def alert_failures(self, kind: str, case_id: int | None = None,
                       cluster_id: int | None = None,
                       size: int | None = None) -> tuple[int, datetime | None]:
        """(จำนวนครั้งที่ล้มเหลวนับตั้งแต่ครั้งที่สำเร็จล่าสุดของคีย์เดียวกัน,
        เวลาที่ล้มเหลวครั้งล่าสุด หรือ None) — คีย์ = kind + case/cluster (+ size)
        แถวที่ถูก re-arm (rearmed=1) ไม่นับ: ผู้ดูแลสั่งให้เริ่มนับใหม่จากศูนย์"""
        where, args = self._alert_where(kind, case_id, cluster_id, size)
        with self._connect() as con:
            n, last = con.execute(
                f"SELECT COUNT(*), MAX(sent_at) FROM alert_log WHERE ok=0 AND rearmed=0"
                f" AND {where}"
                f" AND id > COALESCE((SELECT MAX(id) FROM alert_log"
                f" WHERE ok=1 AND {where}), 0)",
                args + args,
            ).fetchone()
        return n, _dt(last)

    def alert_health(self, max_total: int, case_diseases=None, cluster_diseases=None) -> dict:
        """สรุปการแจ้งเตือนที่ยังส่งไม่สำเร็จ (จำนวนนับล้วน ไม่มี id/HN/ข้อความ) สำหรับแถบเตือนเว็บและ /healthz

        นับ "รายการ" ที่ครั้งล่าสุดล้มเหลวและยังไม่เคยสำเร็จ: `new`/`cancel` ต่อเคส (เฉพาะเคสที่ยัง
        ต้องแจ้งอยู่ — new = เคส active, cancel = เคส cancelled ที่เคยแจ้ง new สำเร็จ, reactivate = เคส active ที่เคยแจ้ง cancel
        สำเร็จแต่ยังไม่แจ้งคืนสถานะ) และ `cluster`
        ต่อ (cluster, ขนาด) ที่ยังไม่เกิน alerted_size (ขนาดที่ถูกแทนด้วยขนาดใหม่ที่แจ้งสำเร็จแล้ว
        ไม่มีใครลองซ้ำ จึงไม่นับ). given_up = รายการที่ล้มเหลวมาแล้ว >= max_total ครั้ง
        (ตรงกับที่ตัวส่งเลิกลอง) — รายการที่ไม่มี ok เลย จำนวนล้มเหลวติดกัน = จำนวนแถวล้มเหลว
        (ไม่นับแถวที่ถูก re-arm โดย rearm_alerts)

        case_diseases/cluster_diseases: นับเฉพาะโรคเหล่านี้ (None = ทุกโรค) — โรคที่ปิดหรือไม่แจ้งรายเคสไม่ทำให้แถบเตือนค้าง

        หมายเหตุ: ตัวเลขของ cluster เป็นค่าประมาณในบางกรณีที่พบยาก — ขนาดที่ล้มเหลวและสูงกว่า
        alerted_size หลังสมาชิกถูกยกเลิก (กลุ่มหดแล้วไม่มีใครลองขนาดนั้นอีก) และแถวเก่าของ cluster
        ที่ถูกรวมเข้า id ต่ำกว่า อาจทำให้แถบเตือนค้างจนกว่าจะ re-arm"""
        def _in(col, keys):
            return "" if keys is None else f" AND {col} IN ({','.join('?' * len(keys))})"
        cf, clf = _in("c.disease", case_diseases), _in("cl.disease", cluster_diseases)
        params = [*(case_diseases or ()), *(case_diseases or ()), *(case_diseases or ()), *(cluster_diseases or ())]
        sql = f"""
            SELECT COUNT(*) FROM alert_log a JOIN "case" c ON c.id = a.case_id
            WHERE a.kind='new' AND a.ok=0 AND a.rearmed=0 AND c.status='active'{cf}
              AND NOT EXISTS (SELECT 1 FROM alert_log o WHERE o.kind='new'
                              AND o.case_id=a.case_id AND o.ok=1)
            GROUP BY a.case_id
            UNION ALL
            SELECT COUNT(*) FROM alert_log a JOIN "case" c ON c.id = a.case_id
            WHERE a.kind='cancel' AND a.ok=0 AND a.rearmed=0 AND c.status='cancelled'{cf}
              AND EXISTS (SELECT 1 FROM alert_log o WHERE o.kind='new'
                          AND o.case_id=a.case_id AND o.ok=1)
              AND NOT EXISTS (SELECT 1 FROM alert_log o WHERE o.kind='cancel'
                              AND o.case_id=a.case_id AND o.ok=1)
            GROUP BY a.case_id
            UNION ALL
            SELECT COUNT(*) FROM alert_log a JOIN "case" c ON c.id = a.case_id
            WHERE a.kind='reactivate' AND a.ok=0 AND a.rearmed=0 AND c.status='active'{cf}
              AND EXISTS (SELECT 1 FROM alert_log o WHERE o.kind='cancel'
                          AND o.case_id=a.case_id AND o.ok=1)
              AND NOT EXISTS (SELECT 1 FROM alert_log o WHERE o.kind='reactivate'
                              AND o.case_id=a.case_id AND o.ok=1)
            GROUP BY a.case_id
            UNION ALL
            SELECT COUNT(*) FROM alert_log a LEFT JOIN cluster cl ON cl.id = a.cluster_id
            WHERE a.kind='cluster' AND a.ok=0 AND a.rearmed=0 AND a.size IS NOT NULL{clf}
              AND a.size > COALESCE(cl.alerted_size, 0)
              AND NOT EXISTS (SELECT 1 FROM alert_log o WHERE o.kind='cluster'
                              AND o.cluster_id=a.cluster_id AND o.size=a.size AND o.ok=1)
            GROUP BY a.cluster_id, a.size
        """
        with self._connect() as con:
            counts = [r[0] for r in con.execute(sql, params)]
        return {"failing": len(counts),
                "given_up": sum(1 for n in counts if n >= max_total)}

    def rearm_alerts(self) -> int:
        """ให้ผู้ดูแลสั่ง "ลองส่งใหม่" ให้ทุกรายการที่ยังไม่เคยส่งสำเร็จ: ทำเครื่องหมาย rearmed=1 ที่
        แถวล้มเหลวทุกแถวของรายการที่ไม่มีแถว ok เลย (ตัวตนรายการ = kind + case_id + cluster_id + size
        เช่นเดียวกับ alert_health) แล้วนโยบายลองซ้ำเริ่มนับจากศูนย์ (ลองทุกรอบ 5 ครั้ง แล้วทุก 30 นาที
        รวมสูงสุด 20). ไม่ลบประวัติ. คืนจำนวน "รายการ" (ไม่ใช่จำนวนแถว) ที่ re-arm — ทำซ้ำได้ผล 0
        ทำใน transaction เดียว (BEGIN IMMEDIATE กันตัวส่งเขียนแทรกระหว่างนับกับอัปเดต)"""
        no_ok = ("NOT EXISTS (SELECT 1 FROM alert_log o WHERE o.ok=1 AND o.kind=a.kind"
                 " AND o.case_id IS a.case_id AND o.cluster_id IS a.cluster_id"
                 " AND o.size IS a.size)")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            items = con.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT a.kind, a.case_id, a.cluster_id,"
                f" a.size FROM alert_log a WHERE a.ok=0 AND a.rearmed=0 AND {no_ok})"
            ).fetchone()[0]
            con.execute(
                f"UPDATE alert_log AS a SET rearmed=1 WHERE a.ok=0 AND a.rearmed=0 AND {no_ok}")
            return items

    def alerts(self, kind: str | None = None, case_id: int | None = None,
               cluster_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alert_log WHERE 1=1", []
        for col, val in (("kind", kind), ("case_id", case_id), ("cluster_id", cluster_id)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sent_at"] = _dt(d["sent_at"])
            d["ok"] = bool(d["ok"])
            out.append(d)
        return out

    # ---- clusters ----
    def save_cluster(self, member_ids: list[int], first_dx_at: datetime,
                     last_dx_at: datetime, disease: str = "dengue") -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO cluster (member_case_ids, first_dx_at, last_dx_at, disease)"
                " VALUES (?,?,?,?)",
                (json.dumps(list(member_ids)), first_dx_at.isoformat(),
                 last_dx_at.isoformat(), disease),
            )
            return cur.lastrowid

    def clusters(self, disease: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM cluster", []
        if disease is not None:
            sql += " WHERE disease=?"
            args.append(disease)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["member_case_ids"] = json.loads(d["member_case_ids"])
            d["first_dx_at"] = _dt(d["first_dx_at"])
            d["last_dx_at"] = _dt(d["last_dx_at"])
            d["alerted_at"] = _dt(d["alerted_at"])
            out.append(d)
        return out

    def update_cluster(self, cluster_id: int, member_ids: list[int],
                       first_dx_at: datetime, last_dx_at: datetime) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE cluster SET member_case_ids=?, first_dx_at=?, last_dx_at=?"
                " WHERE id=?",
                (json.dumps(list(member_ids)), first_dx_at.isoformat(),
                 last_dx_at.isoformat(), cluster_id),
            )

    def record_cluster_alert(self, cluster_id: int, size: int, ok: bool,
                             response_code: int | None, error: str | None) -> None:
        """บันทึกผลแจ้ง cluster (พร้อมขนาด) และ — เมื่อสำเร็จ — อัปเดต alerted_size
        ใน transaction เดียวกัน"""
        with self._connect() as con:
            self._insert_alert(con, "cluster", None, cluster_id, ok, response_code,
                               error, size)
            if ok:
                con.execute(
                    "UPDATE cluster SET alerted_size=?, alerted_at=? WHERE id=?",
                    (size, _now(), cluster_id),
                )

    def update_cluster_alerted(self, cluster_id: int, size: int) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE cluster SET alerted_size=?, alerted_at=? WHERE id=?",
                (size, _now(), cluster_id),
            )
