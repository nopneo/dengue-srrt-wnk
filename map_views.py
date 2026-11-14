"""แผนที่รวม (`/map`) และ `GET /api/map.json` — สเปกข้อ 8.

JSON มีเฉพาะ id/พิกัด/สถานะหมุด/เวลาวินิจฉัย/ในเขต/cluster/ลิงก์ไปหน้าเคส
(ไม่มี HN ชื่อ เบอร์ ที่อยู่ เพศ อายุ หรือ visit key) — ข้อมูลติดต่อดูได้ที่หน้าเคสเท่านั้น.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from flask import Flask, jsonify, render_template, request, url_for

import trend
from auth import login_required
from boundary import BOUNDARY_DIR, load_boundaries, load_tambons_many
from case_views import OSM_ATTRIBUTION
from config import DEFAULT_TILE_URL
from disease_views import current_rule
from past_outbreak import build_past, village_codes
from store import Store

DAYS_PRESETS = (7, 14, 30, 90)
DAYS_RANGE = (1, 365)
BOUNDARY_CACHE_S = 600
PAST_YEARS = 3          # ปี พ.ศ.ก่อนหน้าที่เลือกดูได้ (ปีปัจจุบัน −1 … −3)
NO_SOURCE_TEXT = "ยังไม่ได้เชื่อมต่อฐานข้อมูล HOSxP"


def past_year_choices(today: date) -> list[int]:
    """ปี พ.ศ.ที่ดูย้อนหลังได้ (ใหม่ → เก่า) ไม่รวมปีปัจจุบัน."""
    return trend.recent_years(today, PAST_YEARS + 1)[1:]


def parse_past(raw: str | None, today: date) -> int | None:
    """ปี พ.ศ.ใน {ปีปัจจุบัน−1..−3} เท่านั้น; ผิดรูป/นอกรายการ -> None."""
    text = (raw or "").strip()
    if not (text.isascii() and text.isdigit()):
        return None
    year = int(text)
    return year if year in past_year_choices(today) else None


def parse_days(raw: str | None, default: int) -> int:
    """จำนวนวันย้อนหลัง 1-365; ผิดรูป/นอกช่วงใช้ค่าเริ่มต้น (ไม่ล้ม)."""
    try:
        days = int((raw or "").strip())
    except ValueError:
        return default
    return days if DAYS_RANGE[0] <= days <= DAYS_RANGE[1] else default


def parse_cluster(raw: str | None, clusters: list[dict]) -> int | None:
    """id ของ cluster ที่บันทึกไว้จริงเท่านั้น; ผิดรูป/ไม่มีในฐาน -> None."""
    try:
        cluster_id = int((raw or "").strip())
    except ValueError:
        return None
    return cluster_id if any(c["id"] == cluster_id for c in clusters) else None


def _cluster_of(clusters: list[dict], selected: int | None) -> dict[int, int]:
    """case_id -> id ของ cluster ที่บันทึกไว้ซึ่งมีเคสนั้น. ถ้าอยู่หลาย cluster ใช้ตัวที่
    ถูกเลือก (ถ้ามีเคสนั้น) ไม่เช่นนั้นตัวที่ id มากสุด (ไม่ขึ้นกับลำดับในลิสต์)."""
    owner: dict[int, int] = {}
    for c in clusters:
        for case_id in c["member_case_ids"]:
            owner[case_id] = max(owner.get(case_id, c["id"]), c["id"])
    for c in clusters:
        if c["id"] == selected:
            for case_id in c["member_case_ids"]:
                owner[case_id] = selected
    return owner


def map_cases(store: Store, days: int, selected: int | None,
              clusters: list[dict], disease: str | None = None) -> list[dict]:
    """เคสใช้งานที่มีหมุด (estimated/confirmed) และวินิจฉัยภายใน `days` วันล่าสุด.

    เมื่อเลือก cluster: รวมสมาชิกที่ยังใช้งานและมีหมุดของ cluster นั้นทุกคนด้วย แม้อยู่นอก
    ช่วงวัน (ทำเครื่องหมาย outside_window) — กันกลุ่มที่เลือกหายเงียบ ๆ."""
    owner = _cluster_of(clusters, selected)
    since = date.today() - timedelta(days=days)
    forced: set[int] = set()
    for c in clusters:
        if c["id"] == selected:
            forced = set(c["member_case_ids"])
    out = []
    for c in store.pinned_cases(since=None if forced else since, disease=disease):
        if c["status"] != "active" or c["pin_state"] not in ("estimated", "confirmed"):
            continue
        outside = c["dx_at"].date() < since
        if outside and c["id"] not in forced:
            continue
        out.append({
            "id": c["id"], "lat": c["lat"], "lng": c["lng"],
            "pin_state": c["pin_state"], "dx_at": c["dx_at"].isoformat(),
            "in_area": c["in_area"], "cluster_id": owner.get(c["id"]),
            "outside_window": outside,
            "url": url_for("case_detail", case_id=c["id"]),
        })
    return out


def _map_radius(rule) -> int:
    """รัศมีวงบนแผนที่: โรคจัดกลุ่มตามหมุดใช้รัศมีของกฎ; โรคจัดกลุ่มตามหมู่บ้านไม่มีวง (0)."""
    return rule.radius_m if rule.group_by == "pin_radius" else 0


def register_map_routes(app: Flask) -> None:
    @app.get("/map")
    @login_required
    def map_view():
        store: Store = app.config["STORE"]
        settings = app.config["SETTINGS"]
        rule = current_rule(settings, request.args, store)
        dparam = rule.key if len(settings.enabled_rules()) > 1 else None
        radius_m = _map_radius(rule)
        days = parse_days(request.args.get("days"), rule.days)
        clusters = store.clusters(rule.key)
        selected = parse_cluster(request.args.get("cluster"), clusters)
        outside_notice = selected is not None and any(
            c["outside_window"] for c in map_cases(store, days, selected, clusters, rule.key))
        is_osm = settings.tile_url == DEFAULT_TILE_URL
        today: date = app.config["CLOCK"]().date()
        past = parse_past(request.args.get("past"), today)
        data = {
            "api_url": url_for("map_json", days=days, cluster=selected, d=dparam),
            "radius_m": radius_m,
            "tile_url": settings.tile_url,
            "attribution": OSM_ATTRIBUTION if is_osm else "",
            "selected_cluster": selected,
            "days": days,
            "login_url": url_for("login", next=url_for("map_view", days=days,
                                                       cluster=selected, past=past, d=dparam)),
            "boundary_url": url_for("boundary_json"),
            "past_url": url_for("past_json", year=past, d=dparam) if past is not None else None,
            "past": past,
            "tambon_url": url_for("tambons_json"),
            "facility_url": url_for("facilities_json"),
            # เทมเพลต URL ของ PUT (JS แทน __CODE__ ด้วยรหัสสถานพยาบาล) — รวม prefix ของ proxy แล้ว
            "facility_put_url": url_for("facility_put", hospcode="__CODE__"),
        }
        presets = list(DAYS_PRESETS)
        custom_days = days if days not in presets else None
        return render_template(
            "map.html", page_title="แผนที่รวม", map_data=data, days=days,
            presets=presets, custom_days=custom_days, selected=selected,
            radius_m=radius_m, show_osm_attribution=is_osm,
            outside_notice=outside_notice, past=past, past_years=past_year_choices(today))

    @app.get("/api/map.json")
    @login_required
    def map_json():
        store: Store = app.config["STORE"]
        settings = app.config["SETTINGS"]
        rule = current_rule(settings, request.args, store)
        days = parse_days(request.args.get("days"), rule.days)
        clusters = store.clusters(rule.key)
        selected = parse_cluster(request.args.get("cluster"), clusters)
        return jsonify(cases=map_cases(store, days, selected, clusters, rule.key),
                       radius_m=_map_radius(rule), days=days)

    def _area_codes() -> list[str]:
        """รหัสอำเภอที่ถือว่า "ในเขต": ค่าตั้ง มิฉะนั้นค่าเริ่มต้นจากตาราง village (ฐานล่ม = ไม่มีรหัส ไม่ใช่ error)."""
        codes = list(app.config["SETTINGS"].area_amphoe)
        svc = app.config.get("TREND")
        if not codes and svc is not None:
            try:
                codes = list(svc.area())
            except Exception:  # noqa: BLE001 — เลเยอร์เสริม ฐานล่ม = ไม่มีเส้นขอบ ไม่ใช่ error
                codes = []
        return codes

    def _boundary_payload(kind: str, loader):
        """เส้นขอบ/ตำบลของรหัสอำเภอในค่าตั้งเท่านั้น (ไม่รับชื่อไฟล์จากคำขอ) แคชต่อโปรเซส 10 นาทีแยกตามชนิด."""
        codes = _area_codes()
        if not codes:
            return jsonify(features=[], missing=[])
        mono = app.config.get("MONOTONIC", time.monotonic)
        cache = app.config.setdefault("BOUNDARY_CACHE", {})
        key = (kind, tuple(codes), str(BOUNDARY_DIR))
        hit = cache.get(key)
        if hit is not None and mono() < hit[0]:
            return jsonify(hit[1])
        features, missing = loader(codes, BOUNDARY_DIR)
        payload = {"features": features, "missing": missing}
        cache[key] = (mono() + BOUNDARY_CACHE_S, payload)
        return jsonify(payload)

    @app.get("/api/boundary.json")
    @login_required
    def boundary_json():
        """เส้นขอบอำเภอที่ใช้ (ไฟล์ boundaries/<รหัส>.geojson)."""
        return _boundary_payload("district", load_boundaries)

    @app.get("/api/tambons.json")
    @login_required
    def tambons_json():
        """ขอบเขตตำบลของอำเภอที่ใช้ (ไฟล์ boundaries/<รหัส>.tambon.geojson)."""
        return _boundary_payload("tambon", load_tambons_many)

    @app.get("/api/past.json")
    @login_required
    def past_json():
        """จำนวนรายต่อหมู่บ้านของปี พ.ศ.ก่อนหน้า (เฉพาะจำนวนนับ + จุดกลางหมู่บ้าน)."""
        today: date = app.config["CLOCK"]().date()
        year = parse_past(request.args.get("year"), today)
        if year is None:
            return jsonify(error="ปี พ.ศ.ไม่ถูกต้อง"), 400
        svc = app.config.get("TREND")
        if svc is None:
            return jsonify(year=year, total=0, villages=[], unmapped={"villages": 0, "cases": 0},
                           computed_at=None, stale=False, error=NO_SOURCE_TEXT, centroid_error=False)
        result = svc.year(year, disease=current_rule(app.config["SETTINGS"], request.args).key)
        codes = village_codes(result.rows)
        cents, cent_failed = svc.centroid_lookup(codes) if codes else ({}, False)
        return jsonify(build_past(year, result, cents, svc.names(codes) if codes else {},
                                  centroid_error=cent_failed))

