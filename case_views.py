"""หน้าเคส (`/case/<id>`) และ API ปักหมุด (`POST /api/case/<id>/pin`) — สเปกข้อ 5, 6, 8.

ข้อมูลติดต่อผู้ป่วย (ชื่อ/เบอร์/ที่อยู่) ดึงสดจาก HOSxP ต่อการเปิดหน้า แสดงเฉพาะใน HTML
(autoescape) ไม่เก็บ ไม่บันทึก log ไม่ใส่ใน <title> / URL / JSON ของ JavaScript.
"""
from __future__ import annotations

import logging
import math

from flask import Flask, abort, jsonify, render_template, request, session, url_for

from alerts import evaluate_clusters
from auth import api_write_guard, has_role, login_required, role_required
from config import DEFAULT_TILE_URL
from diseases import DISEASES
from geo import haversine_m
from store import PIN_STATES, Store
from webfmt import icd_dot

log = logging.getLogger(__name__)

# ขอบเขตประเทศไทย (หยาบ) — กันพิกัด 0,0 / สลับ lat-lng / นอกประเทศก่อนเข้าฐาน
LAT_RANGE = (5.0, 21.0)
LNG_RANGE = (97.0, 106.0)
PIN_SOURCES = ("manual", "gps")
DEFAULT_CENTER = {"lat": 14.35, "lng": 101.90, "zoom": 12}
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
CONTACT_ERROR = "ดึงข้อมูลจาก HOSxP ไม่ได้ในขณะนี้"


class PinError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def _coord(value, name: str, bounds: tuple[float, float]) -> float:
    """ต้องเป็นตัวเลข JSON จริง (ไม่ใช่ string/bool/null/NaN/Infinity) และอยู่ในช่วง."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PinError("not_a_number", f"{name}ต้องเป็นตัวเลข")
    if isinstance(value, float) and not math.isfinite(value):
        raise PinError("not_a_number", f"{name}ต้องเป็นตัวเลขที่จำกัดค่า")
    if not bounds[0] <= value <= bounds[1]:
        raise PinError("out_of_range", f"{name}อยู่นอกประเทศไทย กรุณาตรวจพิกัด")
    return float(value)


def parse_pin_body(body) -> tuple[float, float, str, str]:
    """ตรวจ JSON ของคำขอปักหมุด -> (lat, lng, state, source) หรือยก PinError."""
    if not isinstance(body, dict):
        raise PinError("bad_json", "รูปแบบข้อมูลไม่ถูกต้อง")
    state = body.get("state")
    if state not in PIN_STATES:
        raise PinError("bad_state", "สถานะหมุดต้องเป็น estimated หรือ confirmed")
    source = body.get("source", "manual")
    if source not in PIN_SOURCES:
        raise PinError("bad_source", "แหล่งที่มาของหมุดต้องเป็น manual หรือ gps")
    lat_raw, lng_raw = body.get("lat"), body.get("lng")
    try:
        lat = _coord(lat_raw, "ละติจูด", LAT_RANGE)
        lng = _coord(lng_raw, "ลองจิจูด", LNG_RANGE)
    except PinError as err:
        if (err.code == "out_of_range" and _is_num(lat_raw) and _is_num(lng_raw)
                and LNG_RANGE[0] <= lat_raw <= LNG_RANGE[1]
                and LAT_RANGE[0] <= lng_raw <= LAT_RANGE[1]):
            raise PinError("swapped_lat_lng",
                           "ดูเหมือนสลับค่าละติจูดกับลองจิจูด กรุณาตรวจพิกัด") from None
        raise
    return lat, lng, state, source


def _is_num(v) -> bool:
    return (not isinstance(v, bool) and isinstance(v, (int, float))
            and (not isinstance(v, float) or math.isfinite(v)))


def _neighbors(store: Store, case: dict, pin: dict | None, radius_m: int) -> list[dict]:
    """เคสอื่นของ "โรคเดียวกัน" ที่ยังใช้งานและมีหมุด (ทุกสถานะ) ภายในรัศมีจากหมุดของเคสนี้ เรียงตามระยะ.
    รัศมี 0 (โรคจัดกลุ่มตามหมู่บ้าน) = ไม่มีเพื่อนบ้านตามรัศมี."""
    if pin is None or radius_m <= 0:
        return []
    out = []
    for c in store.pinned_cases(disease=case["disease"]):
        if c["id"] == case["id"] or c["status"] != "active":
            continue
        dist = haversine_m(pin["lat"], pin["lng"], c["lat"], c["lng"])
        if dist <= radius_m:
            out.append({"case": c, "distance_m": round(dist)})
    out.sort(key=lambda n: (n["distance_m"], n["case"]["id"]))
    return out


def _center(store: Store, case: dict, pin: dict | None) -> dict:
    if pin is not None:
        return {"lat": pin["lat"], "lng": pin["lng"], "zoom": 17}
    memory = store.default_pin(case["hn"], case["village_code"], case["house_no"])
    if memory is not None:
        return {"lat": memory[0], "lng": memory[1], "zoom": 16}
    pinned = store.pinned_cases(disease=case["disease"])
    if pinned:
        return {"lat": pinned[0]["lat"], "lng": pinned[0]["lng"], "zoom": 12}
    return dict(DEFAULT_CENTER)


def fetch_contact(source, hn: str) -> dict | None:
    """ดึงชื่อ/เบอร์/ที่อยู่สด; ใช้ไม่ได้ (ไม่มีแหล่ง/HosxpError/อื่น ๆ) -> None."""
    if source is None:
        return None
    try:
        data = source.contact(hn)
        return {k: (data[k].strip() if isinstance(data.get(k), str) else "")
                for k in ("name", "phone", "address")}
    except Exception as exc:  # noqa: BLE001 — ต้องไม่ทำให้หน้าเคส/แผนที่ล่ม
        log.warning("ดึงข้อมูลติดต่อไม่สำเร็จ (%s)", type(exc).__name__)  # ไม่ log ข้อมูลผู้ป่วย
        return None


def register_case_routes(app: Flask) -> None:
    @app.get("/case/<int:case_id>")
    @login_required
    def case_detail(case_id: int):
        store: Store = app.config["STORE"]
        settings = app.config["SETTINGS"]
        case = store.get_case(case_id)
        if case is None:
            abort(404)
        pin = store.get_pin(case_id)
        cancelled = case["status"] != "active"
        can_edit = has_role(session.get("role"), "srrt") and not cancelled
        rule = settings.rule(case["disease"])
        if rule is None:
            radius_m = settings.cluster_radius_m
        else:   # โรคจัดกลุ่มตามหมู่บ้านไม่ใช้รัศมีรอบหมุด
            radius_m = rule.radius_m if rule.group_by == "pin_radius" else 0
        disease = DISEASES.get(case["disease"])
        neighbors = _neighbors(store, case, pin, radius_m)
        contact = fetch_contact(app.config.get("SOURCE"), case["hn"])

        is_osm = settings.tile_url == DEFAULT_TILE_URL
        data = {
            "case_id": case_id,
            "can_edit": can_edit,
            "pin": {"lat": pin["lat"], "lng": pin["lng"], "state": case["pin_state"]}
            if pin else {"lat": None, "lng": None, "state": "none"},
            "radius_m": radius_m,
            "tile_url": settings.tile_url,
            "attribution": OSM_ATTRIBUTION if is_osm else "",
            "center": _center(store, case, pin),
            "pin_url": url_for("api_case_pin", case_id=case_id),
            "neighbors": [
                {"case_id": n["case"]["id"], "lat": n["case"]["lat"],
                 "lng": n["case"]["lng"], "pin_state": n["case"]["pin_state"],
                 "distance_m": n["distance_m"],
                 "url": url_for("case_detail", case_id=n["case"]["id"])}
                for n in neighbors],
        }
        title = f"เคส #{case_id} · {icd_dot(case['icd10'])} · {case['visit_type']}"
        return render_template(
            "case.html", page_title=title, case=case, cancelled=cancelled,
            can_edit=can_edit, contact=contact, contact_error=CONTACT_ERROR,
            neighbors=neighbors, radius_m=radius_m, disease_name=disease.name_th if disease else "",
            back_d=case["disease"] if len(settings.enabled_rules()) > 1 else None,
            show_osm_attribution=is_osm, case_data=data)

    # ชั้นนอกสุด role_required (401 ไม่ได้ login -> 403 ต่ำกว่า srrt; admin ผ่านได้) แล้ว api_write_guard
    # (415 ไม่ใช่ JSON -> 403 csrf) ตามลำดับ
    @app.post("/api/case/<int:case_id>/pin")
    @role_required("srrt")
    @api_write_guard
    def api_case_pin(case_id: int):
        store: Store = app.config["STORE"]
        case = store.get_case(case_id)
        if case is None:
            return jsonify(error="not_found", message="ไม่พบเคสนี้"), 404
        try:
            lat, lng, state, source = parse_pin_body(request.get_json(silent=True))
            if case["status"] != "active":
                raise PinError("case_cancelled", "เคสนี้ถูกยกเลิก ปักหมุดไม่ได้", 409)
            if state == "estimated" and case["pin_state"] == "confirmed":
                raise PinError("already_confirmed",
                               "หมุดนี้ยืนยันแล้ว เปลี่ยนเป็นประมาณไม่ได้", 409)
        except PinError as err:
            return jsonify(error=err.code, message=err.message), err.status

        store.set_pin(case_id, lat, lng, state, source, user=session["user"])

        # ประเมิน cluster แบบไม่รอล็อกแจ้งเตือน: ถ้า scheduler กำลังส่ง LINE (ช้า) จะไม่ค้างคำขอเว็บ —
        # scheduler ประเมิน cluster ทุกรอบอยู่แล้ว จึงไม่มีอะไรหาย (cluster_check = "deferred")
        alerted: list[int] = []
        check = "done"
        sender = app.config.get("SENDER")
        if state == "confirmed" and sender is not None:
            try:
                result = evaluate_clusters(
                    store, sender, app.config["SETTINGS"],
                    area_lookup=app.config.get("AREA_LOOKUP"), wait=False)
                alerted = list(result)
                if getattr(result, "deferred", False):
                    check = "deferred"
            except Exception as exc:  # noqa: BLE001 — หมุดบันทึกแล้ว ต้องตอบ ok
                check = "deferred"
                log.warning("ประเมิน cluster หลังยืนยันหมุดไม่สำเร็จ (%s)",
                            type(exc).__name__)
        return jsonify(ok=True, pin_state=state, lat=lat, lng=lng,
                       clusters_alerted=alerted, cluster_check=check)
