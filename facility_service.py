"""บริการหมุด รพ./รพ.สต. บนแผนที่รวม: รายชื่อจาก HOSxP (แคช) + พิกัดใน SQLite + วาง/ย้ายหมุดโดยผู้ดูแล.

ข้อมูลเป็นที่ตั้งสถานพยาบาล (สาธารณะ) ไม่มีข้อมูลผู้ป่วย. รายชื่อจาก HOSxP เป็นข้อมูลเสริม: ฐานล่ม = ใช้แถวใน SQLite
ต่อได้ (ไม่ error) และการวางหมุดของสถานพยาบาลที่ยังไม่มีแถวต้องอยู่ในรายชื่อ (ไม่สร้างสถานพยาบาลใหม่จากคำขอ).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from facilities import valid_coords

log = logging.getLogger("dengue.facilities")

CATALOG_TTL_S = 3600
CATALOG_FAIL_BACKOFF_S = 60


@dataclass(frozen=True)
class PlaceResult:
    status: str     # ok | bad_coords | unknown | unavailable


class FacilityService:
    def __init__(self, store, source, settings, trend=None,
                 clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time.monotonic):
        self.store, self.source, self.settings, self.trend = store, source, settings, trend
        self.clock, self.monotonic = clock, monotonic
        self._catalog: list[dict] | None = None
        self._catalog_until = 0.0
        self._fail_until = 0.0

    def _area(self) -> tuple[str, ...]:
        if self.settings.area_amphoe:
            return tuple(self.settings.area_amphoe)
        return tuple(self.trend.area()) if self.trend is not None else ()

    def catalog(self) -> tuple[list[dict], bool]:
        """(รายชื่อ รพ./รพ.สต. จาก HOSxP, ถามฐานไม่สำเร็จ?) แคช 1 ชม.; ล้มเหลว = พัก 60 วินาที และคืนรายการเก่า (ถ้ามี)
        พร้อมธง failed — ไม่แคชความล้มเหลวเป็น "ไม่มีสถานพยาบาล"."""
        if self.source is None:
            return [], False
        now = self.monotonic()
        if self._catalog is not None and now < self._catalog_until:
            return self._catalog, False
        if now < self._fail_until:
            return (self._catalog or []), True
        try:
            area = self._area()
            catalog = self.source.facilities(area) if area else []
        except Exception as e:  # noqa: BLE001 — เลเยอร์เสริม ต้องไม่ทำให้ API ล้ม
            log.warning("facilities: ดึงรายชื่อสถานพยาบาลไม่สำเร็จ (%s)", type(e).__name__)
            self._fail_until = now + CATALOG_FAIL_BACKOFF_S
            return (self._catalog or []), True
        self._catalog, self._catalog_until = list(catalog), now + CATALOG_TTL_S
        return self._catalog, False

    def snapshot(self, editable: bool) -> dict:
        rows = self.store.facility_all()
        catalog, _failed = self.catalog()
        placed = {r["hospcode"] for r in rows}
        tmbs = sorted({r["tmb"] for r in rows})
        names: dict[str, str] = {}
        if tmbs and self.trend is not None:
            try:
                names = self.trend.names(tmbs)
            except Exception:  # noqa: BLE001
                names = {}
        return {
            "facilities": [{"code": r["hospcode"], "name": r["name"], "kind": r["kind"], "tmb": r["tmb"],
                            "tmb_name": names.get(r["tmb"]), "lat": r["lat"], "lng": r["lng"],
                            "source": r["source"]} for r in rows],
            "unplaced": [{"code": c["hospcode"], "name": c["name"], "kind": c["kind"]}
                         for c in catalog if c["hospcode"] not in placed],
            "editable": bool(editable),
        }

    def place(self, code: str, lat: object, lng: object, user: str) -> PlaceResult:
        coords = valid_coords(lat, lng)
        if coords is None:
            return PlaceResult("bad_coords")
        lat_f, lng_f = coords
        existing = self.store.facility_get(code)
        if existing is not None:
            ident = (existing["name"], existing["kind"], existing["tmb"])
        else:
            catalog, failed = self.catalog()
            item = next((c for c in catalog if c["hospcode"] == code), None)
            if item is None:
                return PlaceResult("unavailable" if failed else "unknown")
            ident = (item["name"], item["kind"], item["tmb"])
        self.store.facility_upsert(code, *ident, lat_f, lng_f, "manual", user, self.clock())
        self.store.add_audit("facility_place", user,
                             detail=json.dumps({"hospcode": code, "lat": lat_f, "lng": lng_f}))
        return PlaceResult("ok")
