"""บริการคำนวณ+แคชจำนวนรายรายปี พ.ศ.สำหรับหน้า `/trend` (สเปกข้อ 4).

ปีที่จบแล้วคำนวณครั้งเดียวแล้วเก็บ (คำนวณใหม่เมื่อรหัสโรค/วันกันซ้ำ/พื้นที่เปลี่ยน);
ปีปัจจุบันหมดอายุใน 24 ชม. หรือกด "คำนวณใหม่" (ไม่เกินทุก 5 นาที). ฐาน HOSxP ล่ม -> ใช้แคชเก่า
พร้อมธงเตือน ไม่ยก exception ถึงหน้าเว็บ และข้อความผิดพลาดไม่มีรายละเอียดของฐาน/SQL.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable

from trend import CountRow, aggregate, build_episodes, year_range, year_of

log = logging.getLogger("dengue.trend")

CURRENT_TTL = timedelta(hours=24)
SETTLE_GRACE = timedelta(days=45)   # หลังปี พ.ศ.จบ ยังคำนวณใหม่ทุก 24 ชม. จนพ้นช่วงลงรหัสวินิจฉัยช้า
REFRESH_MIN_S = 300
FAIL_BACKOFF_S = 300                # คำนวณล้มแล้วพักก่อนลองใหม่ (กันสแกนปี พ.ศ.ซ้ำทุกการรีโหลด)
AREA_TTL_S = 3600                   # แคชอำเภอตั้งต้นจากตาราง village
CENTROID_TTL_S = 3600               # จุดกลางหมู่บ้านที่พบ
CENTROID_MISS_TTL_S = 300           # หมู่บ้านที่ฐานตอบว่าไม่มีจุดกลาง (กันยิงฐานซ้ำทุกการรีโหลด)
NAMES_MISS_TTL_S = 300              # ชื่อพื้นที่ที่ไม่พบ/ถามไม่สำเร็จ ไม่ถามซ้ำภายในเวลานี้ (ฐานล่ม = ไม่ต้องรอ timeout ทุกคำขอ)
CENTROID_FAIL_BACKOFF_S = 60        # ถามฐานไม่สำเร็จ: พักสั้น ๆ ไม่จำเป็น "ไม่มีพิกัด" (ไม่ใส่แคชลบ)
LOCK_WAIT_S = 2.0                   # รอคำขออื่นที่กำลังคำนวณปี พ.ศ.เดียวกันได้ไม่เกินนี้ แล้วตอบ "กำลังคำนวณ"
ERROR_TEXT = "ดึงข้อมูลไม่สำเร็จ"


@dataclass(frozen=True)
class YearResult:
    year: int
    rows: list[CountRow]
    computed_at: datetime | None
    stale: bool
    error: str | None
    throttled: bool
    busy: bool = False


class TrendService:
    def __init__(self, store, source, settings, clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic, lock_wait: float = LOCK_WAIT_S):
        self.store, self.source, self.settings = store, source, settings
        self.clock, self.monotonic = clock, monotonic
        self.lock_wait = lock_wait
        self._default_area: tuple[tuple[str, ...], float] | None = None
        self._fail_until: dict[tuple[str, int], float] = {}   # คีย์ (โรค, ปี พ.ศ.)
        self._centroids: dict[str, tuple[tuple[float, float] | None, float]] = {}
        self._centroid_fail_until = 0.0
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, int], threading.Lock] = {}
        self._last_refresh: dict[tuple[str, int], float] = {}
        self._names: dict[str, str] = {}
        self._names_retry_at: dict[str, float] = {}   # ชื่อที่ฐานไม่มี/ถามไม่สำเร็จ: ไม่ถามซ้ำจนถึงเวลานี้

    def _lock_for(self, key: tuple[str, int]) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _area(self) -> tuple[str, ...]:
        if self.settings.area_amphoe:
            return tuple(self.settings.area_amphoe)
        cached = self._default_area
        if cached is not None and self.monotonic() - cached[1] < AREA_TTL_S:
            return cached[0]
        area = tuple(self.source.default_area_amphoe())
        self._default_area = (area, self.monotonic())
        return area

    def area(self) -> tuple[str, ...]:
        """รหัสอำเภอที่ถือว่า "ในเขต" (ค่าตั้ง หรือค่าตั้งต้นจากตาราง village) — ยกข้อผิดพลาดของฐานตามเดิม."""
        return self._area()

    def centroids(self, codes: Iterable[str]) -> dict[str, tuple[float, float]]:
        """จุดกลางหมู่บ้าน (รหัส 8 หลัก -> (lat, lng)) ไม่ยก; ใช้ centroid_lookup ถ้าต้องรู้ว่าฐานล้มหรือไม่."""
        return self.centroid_lookup(codes)[0]

    def centroid_lookup(self, codes: Iterable[str]) -> tuple[dict[str, tuple[float, float]], bool]:
        """(จุดกลางที่รู้, ถามฐานไม่สำเร็จ?) พร้อมแคช: ที่พบ 1 ชม., ที่ฐานตอบว่าไม่มี 5 นาที;
        ฐานล้ม = ไม่แคชเป็น "ไม่มีพิกัด" (พักถามใหม่ 60 วินาที) เพื่อให้ผู้ใช้แยก "ดึงไม่สำเร็จ" ออกจาก "ไม่มีข้อมูล"."""
        wanted = [c for c in dict.fromkeys(codes) if c]
        now = self.monotonic()
        with self._guard:
            missing = [c for c in wanted if c not in self._centroids or self._centroids[c][1] <= now]
        failed = False
        if missing:
            if now < self._centroid_fail_until:
                failed = True
            else:
                try:
                    found = dict(self.source.village_centroids(missing))
                except Exception as e:  # noqa: BLE001
                    log.warning("trend: ดึงจุดกลางหมู่บ้านไม่สำเร็จ (%s)", type(e).__name__)
                    found = None
                    failed = True
                    self._centroid_fail_until = now + CENTROID_FAIL_BACKOFF_S
                if found is not None:
                    now = self.monotonic()
                    with self._guard:
                        for c in missing:
                            if c in found:
                                self._centroids[c] = (found[c], now + CENTROID_TTL_S)
                            else:
                                self._centroids[c] = (None, now + CENTROID_MISS_TTL_S)
        with self._guard:
            return ({c: e[0] for c in wanted if (e := self._centroids.get(c)) and e[0] is not None}, failed)

    def _icds(self, disease: str) -> tuple[str, ...]:
        """รหัส ICD ของโรค (กฎที่ resolve แล้ว); โรคที่ไม่รู้จัก = ของไข้เลือดออกตามฟิลด์เดิม."""
        rule = self.settings.rule(disease)
        return tuple(rule.icds) if rule is not None else tuple(self.settings.icds)

    def _signature(self, area: tuple[str, ...], disease: str = "dengue") -> str:
        s = self.settings
        tag = "" if disease == "dengue" else disease + "|"
        return "v2|" + tag + ",".join(self._icds(disease)) + "|" + str(s.realert_days) + "|" + ",".join(sorted(area))

    def year(self, year: int, refresh: bool = False, disease: str = "dengue") -> YearResult:
        lock = self._lock_for((disease, year))
        if not lock.acquire(timeout=self.lock_wait):
            cached = self.store.trend_get(year, disease)
            if cached is not None:
                r = self._from_cache(year, cached, stale=False, error=None, throttled=False)
                return YearResult(year, r.rows, r.computed_at, False, None, False, busy=True)
            return YearResult(year, [], None, False, None, False, busy=True)
        try:
            return self._year_locked(year, refresh, disease)
        finally:
            lock.release()

    def _year_locked(self, year: int, refresh: bool, disease: str) -> YearResult:
        now = self.clock()
        slot = (disease, year)
        cached = self.store.trend_get(year, disease)
        current = year >= year_of(now.date())
        if refresh and current and cached is not None:
            last = self._last_refresh.get(slot)
            if last is not None and self.monotonic() - last < REFRESH_MIN_S:
                return self._from_cache(year, cached, stale=False, error=None, throttled=True)
        explicit = refresh and current
        if not explicit and self.monotonic() < self._fail_until.get(slot, 0.0):
            return self._failed(year, cached)
        try:
            area = self._area()
            sig = self._signature(area, disease)
            start, end = year_range(year)
            if cached is not None and not explicit:
                meta = cached[0]
                # ข้อมูลที่คำนวณไว้ขณะปี พ.ศ.ยังไม่จบ/ยังอยู่ในช่วงผ่อนผัน ไม่ถือว่านิ่ง — คำนวณใหม่ทุก 24 ชม. จนพ้นช่วงผ่อนผัน
                settled = meta["computed_at"].date() >= end + SETTLE_GRACE
                fresh = meta["signature"] == sig and (settled or now - meta["computed_at"] < CURRENT_TTL)
                if fresh:
                    return self._from_cache(year, cached, stale=False, error=None, throttled=False)
            if explicit:
                self._last_refresh[slot] = self.monotonic()
            end = min(end, now.date())
            rows = self.source.episode_rows(
                self._icds(disease), start - timedelta(days=self.settings.realert_days), end)
            counts = aggregate(build_episodes(rows, self.settings.realert_days, start, end, area))
            self.store.trend_put(year, sig, counts, now, disease)
            self._fail_until.pop(slot, None)
            return YearResult(year, counts, now, False, None, False)
        except Exception as e:  # noqa: BLE001 — ห้ามให้หน้าเว็บล้ม; log เฉพาะชนิด ไม่ใส่รายละเอียด
            log.warning("trend: คำนวณปี พ.ศ. %s ไม่สำเร็จ (%s)", year, type(e).__name__)
            self._fail_until[slot] = self.monotonic() + FAIL_BACKOFF_S
            return self._failed(year, cached)

    def _failed(self, year: int, cached) -> YearResult:
        if cached is not None:
            return self._from_cache(year, cached, stale=True, error=ERROR_TEXT, throttled=False)
        return YearResult(year, [], None, False, ERROR_TEXT, False)

    @staticmethod
    def _from_cache(year, cached, stale: bool, error: str | None, throttled: bool) -> YearResult:
        meta, rows = cached
        return YearResult(year, rows, meta["computed_at"], stale, error, throttled)

    def names(self, codes: Iterable[str]) -> dict[str, str]:
        wanted = list(dict.fromkeys(codes))
        now = self.monotonic()
        missing = [c for c in wanted if c and c not in self._names and self._names_retry_at.get(c, 0.0) <= now]
        if missing:
            try:
                self._names.update(self.source.address_names(missing))
            except Exception as e:  # noqa: BLE001
                log.warning("trend: ดึงชื่อพื้นที่ไม่สำเร็จ (%s)", type(e).__name__)
            # address_names จริงกลืนข้อผิดพลาดของฐานแล้วคืน {} จึงแยก "ล้ม" กับ "ไม่มีชื่อ" ไม่ได้ — ทั้งคู่พักไว้ 5 นาที
            until = self.monotonic() + NAMES_MISS_TTL_S
            for c in missing:
                if c not in self._names:
                    self._names_retry_at[c] = until
        return {c: self._names[c] for c in wanted if c in self._names}
