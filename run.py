"""คำสั่งบรรทัดคำสั่งของแอปแจ้งเตือนไข้เลือดออก SRRT.

  python run.py --once              รันหนึ่งรอบจริง (อ่าน HOSxP, บันทึกเคส, ส่ง LINE)
  python run.py --once --dry-run    รันหนึ่งรอบโดยพิมพ์ Flex JSON ลง stdout ใช้ฐานชั่วคราว ไม่ส่ง ไม่บันทึกอะไร
  python run.py add-user <username> <srrt|viewer|admin>   เพิ่มผู้ใช้ (ถามรหัสผ่านทางแป้นพิมพ์ อย่างน้อย 10 ตัวอักษร)
  python run.py set-password <username>   เปลี่ยนรหัสผ่านผู้ใช้ที่มีอยู่ (ถามรหัสผ่านสองครั้ง)
  python run.py remove-user <username> [--yes]   ลบผู้ใช้ (ถามยืนยัน; session ที่ค้างใช้ต่อไม่ได้ทันที)
  python run.py backup [--keep N]   สำรองฐานข้อมูลแอปไปที่ <โฟลเดอร์ data>/backups/ (เก็บ N ชุดล่าสุด ค่าเริ่มต้น 14)
  python run.py rearm-alerts [--yes]  ให้ระบบลองส่ง LINE ใหม่กับรายการที่ส่งไม่สำเร็จ/เลิกส่งแล้ว (ถามยืนยัน)

รหัสออก: 0 สำเร็จ · 1 รอบล้มเหลว · 2 ใช้คำสั่งผิด
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping

import alerts
import boundary
import facilities
import scheduler
from auth import MAX_USERNAME_LEN
from config import Settings, load_settings
from envfile import load_dotenv_if_present
from hosxp import HosxpError
from settings_store import effective_env
from store import MIN_PASSWORD_LEN, ROLES, Store, backup_database
from wiring import build_runtime


def _settings(env: Mapping[str, str] | None) -> Settings:
    """Settings ที่ซ้อนค่าจากหน้าตั้งค่าแล้ว (env ที่ส่งมาเป็น base; None = environment ของโปรเซส)."""
    return load_settings(effective_env(env))


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _print_sender(messages: list[dict]) -> None:
    """ตัวส่งของ dry-run: พิมพ์ข้อความ Flex เป็น JSON แทนการส่ง (ไม่แตะเครือข่าย)."""
    print(json.dumps(messages, ensure_ascii=False, indent=2))


def _read_new_password() -> str | None:
    """ถามรหัสผ่านใหม่สองครั้ง; คืน None (พิมพ์เหตุผลลง stderr แล้ว) ถ้าว่าง/สั้นเกินไป/ไม่ตรงกัน."""
    password = getpass.getpass("รหัสผ่าน: ")
    if not password:
        _err("รหัสผ่านต้องไม่ว่าง")
        return None
    if len(password) < MIN_PASSWORD_LEN:
        _err(f"รหัสผ่านสั้นเกินไป (ต้องยาวอย่างน้อย {MIN_PASSWORD_LEN} ตัวอักษร)")
        return None
    if password != getpass.getpass("พิมพ์รหัสผ่านอีกครั้ง: "):
        _err("รหัสผ่านสองครั้งไม่ตรงกัน")
        return None
    return password


def _add_user(username: str, role: str, env: Mapping[str, str] | None) -> int:
    if role not in ROLES:
        _err(f"บทบาท {role!r} ไม่ถูกต้อง ใช้ได้เฉพาะ: {', '.join(ROLES)}")
        return 2
    username = username.strip()
    if not username or len(username) > MAX_USERNAME_LEN or any(c.isspace() for c in username):
        _err(f"ชื่อผู้ใช้ต้องไม่ว่าง ไม่มีช่องว่าง และยาวไม่เกิน {MAX_USERNAME_LEN} ตัวอักษร")
        return 2
    password = _read_new_password()
    if password is None:
        return 2
    store = Store(_settings(env).db_path)
    try:
        store.add_user(username, password, role)
    except ValueError:
        _err(f"บทบาทไม่ถูกต้อง ใช้ได้เฉพาะ: {', '.join(ROLES)}")
        return 2
    except sqlite3.IntegrityError:
        _err(f"มีผู้ใช้ชื่อ {username!r} อยู่แล้ว")
        return 2
    print(f"เพิ่มผู้ใช้ {username} ({role}) แล้ว")
    return 0


def _set_password(username: str, env: Mapping[str, str] | None) -> int:
    username = username.strip()
    store = Store(_settings(env).db_path)
    if store.user_role(username) is None:
        _err(f"ไม่พบผู้ใช้ชื่อ {username!r}")
        return 2
    password = _read_new_password()
    if password is None:
        return 2
    if not store.set_password(username, password):
        _err(f"ไม่พบผู้ใช้ชื่อ {username!r}")
        return 2
    print(f"เปลี่ยนรหัสผ่านของ {username} แล้ว")
    return 0


def _confirm_yes() -> bool:
    try:
        answer = input("พิมพ์ yes เพื่อยืนยัน: ")
    except EOFError:
        answer = ""
    return answer.strip() == "yes"


def _remove_user(username: str, yes: bool, env: Mapping[str, str] | None) -> int:
    username = username.strip()
    store = Store(_settings(env).db_path)
    role = store.user_role(username)
    if role is None:
        _err(f"ไม่พบผู้ใช้ชื่อ {username!r}")
        return 2
    print(f"จะลบผู้ใช้ {username} ({role}) — session ที่ล็อกอินค้างอยู่จะใช้งานต่อไม่ได้ทันที")
    if not yes and not _confirm_yes():
        _err("ยกเลิก ไม่ได้เปลี่ยนแปลงอะไร")
        return 2
    if not store.remove_user(username):
        _err(f"ไม่พบผู้ใช้ชื่อ {username!r}")
        return 2
    print(f"ลบผู้ใช้ {username} แล้ว")
    return 0


DEFAULT_BACKUP_KEEP = 14
BACKUP_PREFIX = "dengue-"
BACKUP_SUFFIX = ".db"


def _backup(keep: int, env: Mapping[str, str] | None, now: datetime | None = None) -> int:
    """สำรองฐานของแอปไปที่ <โฟลเดอร์ data>/backups/dengue-YYYYmmdd-HHMM.db แล้วลบชุดเก่าเกิน `keep` ชุด.

    ไฟล์/โฟลเดอร์สำรองมีข้อมูลระบุตัวตนทางอ้อม (HN รหัสผ่านแฮช) จึงตั้งสิทธิ์ 700/600 เท่าที่ระบบรองรับ
    ลบเฉพาะไฟล์ชื่อ dengue-*.db ในโฟลเดอร์นั้น; เรียงตามชื่อ (ตรงกับเวลา) ไม่ใช่เวลาแก้ไฟล์."""
    if keep < 1:
        _err("--keep ต้องเป็นจำนวนเต็มตั้งแต่ 1 ขึ้นไป")
        return 2
    db_path = _settings(env).db_path
    if not os.path.isfile(db_path):
        _err(f"ไม่พบฐานข้อมูลของแอปที่ {db_path} (ตรวจ DENGUE_DB_PATH / รันจากโฟลเดอร์แอป)")
        return 2
    bdir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(bdir, exist_ok=True)
    try:
        os.chmod(bdir, 0o700)
    except OSError:  # Windows/ระบบไฟล์ที่ไม่รองรับ
        pass
    name = f"{BACKUP_PREFIX}{(now or datetime.now()):%Y%m%d-%H%M}{BACKUP_SUFFIX}"
    target = os.path.join(bdir, name)
    try:
        backup_database(db_path, target)
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        _err(f"สำรองฐานไม่สำเร็จ ({type(exc).__name__})")
        return 1
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    old = sorted((f for f in os.listdir(bdir)
                  if f.startswith(BACKUP_PREFIX) and f.endswith(BACKUP_SUFFIX)), reverse=True)
    for stale in old[keep:]:
        try:
            os.remove(os.path.join(bdir, stale))
        except OSError as exc:
            _err(f"ลบสำเนาเก่า {stale} ไม่ได้ ({type(exc).__name__})")
    print(target)
    return 0


def _rearm_alerts(yes: bool, env: Mapping[str, str] | None) -> int:
    """ล้างตัวนับความล้มเหลวของรายการแจ้งเตือนที่ยังไม่เคยสำเร็จ ให้ตัวส่งในแอปลองใหม่ตามนโยบายปกติ.

    ใช้ฐานเดียวกับแอป (settings จาก build_runtime) ไม่เริ่ม scheduler และไม่ส่งอะไรเอง."""
    rt = build_runtime(env)
    h = alerts.health(rt.store, rt.settings)
    print(f"ตอนนี้ส่ง LINE ไม่สำเร็จ {h['failing']} รายการ (หยุดส่งแล้ว {h['given_up']} รายการ)")
    if not yes and not _confirm_yes():
        _err("ยกเลิก ไม่ได้เปลี่ยนแปลงอะไร")
        return 2
    n = rt.store.rearm_alerts()
    print(f"ตั้งให้ลองส่งใหม่ {n} รายการ — ระบบจะลองในรอบถัดไป (ข้อความเก่าจะถูกส่งเป็นข้อความใหม่)")
    return 0


def _import_facilities(path: str, force: bool, dry_run: bool, env: Mapping[str, str] | None) -> int:
    """นำเข้าพิกัด รพ./รพ.สต. จาก CSV (จับคู่ด้วยชื่อกับ hospcode ของ HOSxP). พิมพ์เฉพาะชื่อสถานที่และจำนวน."""
    try:
        rows, problems = facilities.read_csv(Path(path))
    except ValueError as e:
        _err(str(e))
        return 1
    rt = build_runtime(env)
    try:
        area = tuple(rt.settings.area_amphoe) or tuple(rt.source.default_area_amphoe())
        catalog = rt.source.facilities(area)
    except HosxpError as e:
        _err(f"ดึงรายชื่อสถานพยาบาลจาก HOSxP ไม่ได้ ({type(e).__name__})")
        return 1
    if not catalog:
        _err("ไม่พบ รพ./รพ.สต. ของอำเภอที่ใช้ในตาราง hospcode ของ HOSxP")
        return 2
    res = facilities.match_rows(rows, catalog)
    docs = [d for d in (boundary.load_boundary(c, boundary.BOUNDARY_DIR) for c in area) if d is not None]
    outside = [item["name"] for item, lat, lng in res.matched
               if docs and not any(boundary.point_in_boundary(lat, lng, d) for d in docs)]
    now = datetime.now()
    payload = [{**item, "lat": lat, "lng": lng} for item, lat, lng in res.matched]
    if dry_run:
        manual = {f["hospcode"] for f in rt.store.facility_all() if f["source"] == "manual"}
        skipped = [] if force else [p["hospcode"] for p in payload if p["hospcode"] in manual]
        written = len(payload) - len(skipped)
    else:
        written, skipped = rt.store.facility_bulk_upsert(payload, user="import-facilities", at=now,
                                                         overwrite_manual=force)
    names = {c["hospcode"]: c["name"] for c in catalog}
    print(("[dry-run: ไม่เขียนข้อมูล] " if dry_run else "") + f"นำเข้า {written} แห่ง")
    if skipped:
        print("ข้าม (ผู้ดูแลวางตำแหน่งเองแล้ว manual — ใช้ --force เพื่อทับ): " + ", ".join(names[c] for c in skipped))
    for label, items in (("จับคู่กับ HOSxP ไม่ได้", res.unmatched), ("ชื่อกำกวม/ซ้ำ", res.ambiguous),
                         ("พิกัดไม่ถูกต้อง", res.bad_coords), ("พิกัดอยู่นอกอำเภอ", outside),
                         ("ปัญหาในไฟล์", problems)):
        if items:
            print(f"{label}: " + "; ".join(items))
    if res.missing:
        print("ไม่มีพิกัดใน CSV (วางหมุดเองบนแผนที่ในโหมดผู้ดูแลได้): " + ", ".join(m["name"] for m in res.missing))
    return 0


def _once(dry_run: bool, env: Mapping[str, str] | None) -> int:
    if not dry_run:
        return _run_cycle(env, dry_run=False)
    _err("DRY RUN: ใช้ฐานชั่วคราว ไม่บันทึกอะไรลงฐานจริง")
    # อ่าน settings.json ของฐานจริงก่อนสลับ DENGUE_DB_PATH เป็นฐานชั่วคราว (ไม่งั้นค่าซ้อนทับหาย)
    eff = effective_env(env)
    tmp = tempfile.mkdtemp(prefix="dengue-dry-")
    try:
        eff["DENGUE_DB_PATH"] = os.path.join(tmp, "dry-run.db")
        return _run_cycle(eff, dry_run=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_cycle(env: Mapping[str, str] | None, dry_run: bool) -> int:
    rt = build_runtime(env)
    sender = _print_sender if dry_run else rt.sender
    if not dry_run:
        if not scheduler.acquire_lock(scheduler.lock_path_for(rt.settings)):
            _err("มี scheduler ของแอปทำงานอยู่แล้ว (กันแจ้งเตือนซ้ำ) — "
                 "ใช้ --dry-run เพื่อทดลอง หรือหยุดแอปก่อน")
            return 1
        if sender is None:
            _err("ยังไม่ได้ตั้งค่า MOPH Notify: ตรวจจับเคสอย่างเดียว ไม่ส่งแจ้งเตือน")
    result = scheduler.cycle_once(rt.store, rt.source, sender, rt.settings,
                                  datetime.now(), rt.area_lookup)
    if result.error:
        _err(f"รอบล้มเหลว: {result.error}")
        return 1
    _err(f"เคสใหม่ {len(result.new_case_ids)} · ตามอาการ {result.followup_count} · "
         f"ยกเลิก {len(result.cancelled_ids)} · คืนสถานะ {len(result.reactivated_ids)}")
    return 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    if env is None:  # รันจากบรรทัดคำสั่ง: โหลด ./.env (หรือ DENGUE_ENV_FILE) ที่ยังไม่ได้ตั้งใน environment
        load_dotenv_if_present()
    parser = argparse.ArgumentParser(prog="run.py", description="แจ้งเตือนไข้เลือดออก SRRT")
    parser.add_argument("--once", action="store_true", help="รันหนึ่งรอบแล้วจบ")
    parser.add_argument("--dry-run", action="store_true",
                        help="ใช้กับ --once: พิมพ์ข้อความแทนการส่ง ใช้ฐานชั่วคราว")
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser("add-user", help="เพิ่มผู้ใช้ (ถามรหัสผ่านทางแป้นพิมพ์)")
    add.add_argument("username")
    add.add_argument("role", help="|".join(ROLES))
    setpw = sub.add_parser("set-password", help="เปลี่ยนรหัสผ่านผู้ใช้ที่มีอยู่")
    setpw.add_argument("username")
    rm = sub.add_parser("remove-user", help="ลบผู้ใช้ (ถามยืนยัน)")
    rm.add_argument("username")
    rm.add_argument("--yes", action="store_true", help="ข้ามการถามยืนยัน (สำหรับสคริปต์)")
    bk = sub.add_parser("backup", help="สำรองฐานข้อมูลแอป (sqlite backup API) และลบชุดเก่า")
    bk.add_argument("--keep", type=int, default=DEFAULT_BACKUP_KEEP,
                    help=f"จำนวนชุดล่าสุดที่เก็บไว้ (ค่าเริ่มต้น {DEFAULT_BACKUP_KEEP})")
    rearm = sub.add_parser("rearm-alerts",
                           help="ให้ระบบลองส่ง LINE ใหม่กับรายการที่ส่งไม่สำเร็จ/เลิกส่งแล้ว")
    rearm.add_argument("--yes", action="store_true", help="ข้ามการถามยืนยัน (สำหรับสคริปต์)")
    imp = sub.add_parser("import-facilities", help="นำเข้าพิกัด รพ./รพ.สต. จาก CSV (จับคู่ด้วยชื่อกับ HOSxP)")
    imp.add_argument("csv", help="ไฟล์ CSV (UTF-8) ที่มีคอลัมน์ ชื่อหน่วยงาน/ละติจูด/ลองติจูด")
    imp.add_argument("--force", action="store_true", help="ทับตำแหน่งที่ผู้ดูแลวางเอง (manual) ด้วย")
    imp.add_argument("--dry-run", action="store_true", help="แสดงสรุปโดยไม่เขียนข้อมูล")
    args = parser.parse_args(argv)
    if args.command == "add-user":
        return _add_user(args.username, args.role, env)
    if args.command == "set-password":
        return _set_password(args.username, env)
    if args.command == "remove-user":
        return _remove_user(args.username, args.yes, env)
    if args.command == "backup":
        return _backup(args.keep, env)
    if args.command == "rearm-alerts":
        return _rearm_alerts(args.yes, env)
    if args.command == "import-facilities":
        return _import_facilities(args.csv, args.force, args.dry_run, env)
    if args.once:
        return _once(args.dry_run, env)
    parser.error("ต้องระบุ --once, add-user, set-password, remove-user, backup, rearm-alerts หรือ import-facilities")
    return 2  # pragma: no cover


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # ข้อความไทย/อีโมจิใน Flex ต้องไม่ล้มบนคอนโซลหรือการ redirect ของ Windows
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
