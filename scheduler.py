"""ตัวตั้งเวลา: วนรันตัวตรวจจับ -> ส่งเคสค้าง -> ประเมิน cluster ทุก poll_seconds (สเปกข้อ 4, 12).

- `acquire_lock`: ล็อกระดับ OS บนไฟล์ในโฟลเดอร์ข้อมูล กัน gunicorn หลาย worker/หลายโปรเซสรันซ้ำ
  ถือไว้ตลอดอายุโปรเซส ระบบปล่อยให้เองเมื่อโปรเซสจบ
- ไม่ log ข้อมูลผู้ป่วย: log เฉพาะชื่อชนิด exception
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Callable

import alerts
import detector
from app import sanitize_error, set_cycle_error
from config import Settings
from detector import CycleResult
from store import Store

log = logging.getLogger(__name__)

NO_SENDER_NOTICE = "ยังไม่ได้ตั้งค่า MOPH Notify"
MIN_POLL_SECONDS = 0.05  # กันวนถี่เกินไปเมื่อตั้ง poll_seconds เป็น 0

try:  # Windows
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
except ImportError:  # POSIX
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

_HELD: dict[str, object] = {}  # path -> file handle ที่ถือล็อกอยู่ (ต้องไม่ถูกเก็บกวาดทิ้ง)
_HELD_GUARD = threading.Lock()


def acquire_lock(path: str) -> bool:
    """ขอล็อกแบบ exclusive ไม่รอ; True = ได้ล็อก (ถือจนโปรเซสจบ), False = มีผู้อื่นถืออยู่."""
    key = os.path.normcase(os.path.abspath(path))
    with _HELD_GUARD:
        if key in _HELD:
            return False
        os.makedirs(os.path.dirname(key), exist_ok=True)
        fh = open(key, "a+b")
        if not _try_lock(fh):
            fh.close()
            return False
        _HELD[key] = fh
        return True


def release_all_locks() -> None:
    """ปล่อยล็อกที่โปรเซสนี้ถืออยู่ทั้งหมด (สำหรับเทสต์; ใช้งานจริงปล่อยเองตอนโปรเซสจบ)."""
    with _HELD_GUARD:
        for fh in _HELD.values():
            fh.close()  # ปิดไฟล์ = ปล่อยล็อก OS
        _HELD.clear()


def lock_path_for(settings: Settings) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(settings.db_path)), "scheduler.lock")


def cycle_once(store: Store, source, sender, settings: Settings, now: datetime,
               area_lookup: Callable[[str], str | None] | None = None) -> CycleResult:
    """หนึ่งรอบ: ตรวจจับ -> ส่งเคสใหม่/ยกเลิกที่ค้าง -> ประเมิน cluster.

    แต่ละขั้นแยกกัน: ขั้นใดยก exception ก็ยังทำขั้นที่เหลือต่อ (บั๊กขั้นเดียวต้องไม่ปิดการแจ้งเตือนทั้งหมด)
    result.error = ข้อผิดพลาดแรกของรอบ (ของตัวตรวจจับ หรือชื่อชนิด exception ของขั้นที่ยก — ไม่มีข้อความ/ข้อมูลผู้ป่วย)
    ส่งเคสที่ค้างและลองซ้ำเสมอ แม้ HOSxP ล้มในรอบนี้
    sender เป็น None = ยังไม่ได้ตั้งค่า MOPH Notify: ข้ามขั้นแจ้งเตือน (ยังสร้างเคสตามปกติ)
    """
    try:
        result = detector.run_cycle(store, source, settings, now)
    except Exception as exc:  # noqa: BLE001
        log.error("cycle: ขั้นตรวจจับล้มเหลว (%s)", type(exc).__name__)
        result = CycleResult(error=type(exc).__name__)
    else:
        if result.error:
            log.warning("cycle: ตรวจจับไม่สำเร็จ (%s)", sanitize_error(result.error))
    if sender is not None:
        for name, step in (("ส่งเคสค้าง", alerts.send_pending),
                           ("ประเมิน cluster", alerts.evaluate_clusters)):
            try:
                step(store, sender, settings, area_lookup, now)
            except Exception as exc:  # noqa: BLE001
                log.error("cycle: ขั้น%sล้มเหลว (%s)", name, type(exc).__name__)
                result.error = result.error or type(exc).__name__
    return result


def start_scheduler(app, store: Store, source, sender, settings: Settings,
                    area_lookup: Callable[[str], str | None] | None = None
                    ) -> threading.Thread | None:
    """เริ่มเธรด daemon หนึ่งตัว (เฉพาะโปรเซสที่ได้ล็อก) คืน None ถ้าไม่ได้ล็อก.

    หยุดได้ด้วย app.config["SCHEDULER_STOP"].set() (ใช้ในเทสต์ และโดย hook รีสตาร์ตของหน้า /settings ซึ่งรอให้รอบ
    ปัจจุบันจบ — join app.config["SCHEDULER_THREAD"] — ก่อนส่ง SIGTERM กันฆ่ากลางการส่ง LINE)
    """
    if not acquire_lock(lock_path_for(settings)):
        log.info("scheduler: มีโปรเซสอื่นถือล็อกอยู่ ไม่เริ่มตัวตั้งเวลาในโปรเซสนี้")
        return None
    stop = threading.Event()
    app.config["SCHEDULER_STOP"] = stop
    standing_error = NO_SENDER_NOTICE if sender is None else None
    if standing_error:
        set_cycle_error(app, standing_error)  # แจ้งเตือนปิดอยู่: ให้ /healthz เห็นตั้งแต่ต้น
    interval = max(float(settings.poll_seconds), MIN_POLL_SECONDS)

    def loop() -> None:
        while not stop.is_set():
            try:  # ทั้งตัวรอบและการบันทึกสถานะอยู่ในการป้องกันเดียว เธรดต้องไม่ตาย
                try:
                    result = cycle_once(store, source, sender, settings, datetime.now(),
                                        area_lookup)
                    # ไม่มีตัวส่งยังเป็นข้อบกพร่องถาวร จึงไม่ถูกล้างด้วยรอบที่ตรวจจับสำเร็จ
                    error = result.error or standing_error
                except Exception as exc:  # noqa: BLE001
                    log.error("scheduler: รอบล้มเหลว (%s)", type(exc).__name__)
                    error = exc
                set_cycle_error(app, error)
                app.config["CYCLE_STATE"]["last_cycle_at"] = datetime.now().isoformat(
                    timespec="seconds")
            except Exception as exc:  # noqa: BLE001
                log.error("scheduler: บันทึกสถานะรอบไม่สำเร็จ (%s)", type(exc).__name__)
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="dengue-scheduler", daemon=True)
    app.config["SCHEDULER_THREAD"] = thread
    thread.start()
    return thread
