"""static/map.js — ตรวจโครงของเลเยอร์เส้นขอบ/ปีก่อน (อ่านเป็นข้อความ; ไม่มีตัวรัน JS ในชุดเทสต์)"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS_PATH = Path(__file__).resolve().parent.parent / "static" / "map.js"
JS = JS_PATH.read_text(encoding="utf-8")


def strip_comments(src):
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)


CODE = strip_comments(JS)


def test_map_js_uses_textcontent_not_innerhtml_for_popups():
    assert "innerHTML" not in CODE and "insertAdjacentHTML" not in CODE
    assert "textContent" in CODE


def test_map_js_fits_boundary_only_without_cluster():
    m = re.search(r"function\s+applyBoundaryFit\s*\(\)\s*\{(.*?)\n  \}", CODE, re.S)
    assert m, "ต้องมี applyBoundaryFit()"
    body = m.group(1)
    assert "selected_cluster === null" in body and "fitBounds" in body and "boundaryBounds" in body
    assert "animate: false" in body
    # การซูมเริ่มต้นของเคส/กลุ่มต้องไม่แอนิเมชัน ไม่งั้น Leaflet ทิ้งการซูมพอดีอำเภอที่มาทีหลัง
    fit_body = re.search(r"function\s+fit\s*\(.*?\n  \}", CODE, re.S).group(0)
    assert fit_body.count("fitBounds") == 2 and fit_body.count("animate: false") == 2
    show_body = re.search(r"function\s+show\s*\(.*?\n  \}", CODE, re.S).group(0)
    assert "applyBoundaryFit()" in show_body


def test_map_js_layer_failures_do_not_touch_case_layers():
    assert "function layerNote" in CODE and "layer-note" in CODE
    # layers.clearLayers() อยู่ที่ตัวจัดการ error ของการวาดเคสเท่านั้น (ครั้งเดียว)
    assert CODE.count("layers.clearLayers()") == 1
    boundary_part = CODE.split("function loadBoundary")[1].split("function loadPast")[0]
    past_part = CODE.split("function loadPast")[1].split("load().then(")[0]
    for part in (boundary_part, past_part):
        assert "layerNote(" in part and "layers.clearLayers" not in part and "fail(" not in part


def test_map_js_past_radius_formula_color_and_back_order():
    assert "Math.sqrt" in CODE and "Math.min(40," in CODE.replace(" ", "")
    assert "#f97316" in CODE and "bringToBack" in CODE


def test_map_js_layers_start_only_after_cases_load_and_use_urls_from_data():
    assert "data.boundary_url" in CODE and "data.past_url" in CODE
    assert "L.geoJSON" in CODE and "interactive: false" in CODE


@pytest.mark.skipif(shutil.which("node") is None, reason="ไม่มี node ใน PATH")
def test_map_js_has_valid_syntax():
    r = subprocess.run(["node", "--check", str(JS_PATH)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_map_js_past_summary_hides_zero_when_year_not_computed():
    summary = re.search(r"function\s+pastSummary\s*\(.*?\n  \}", CODE, re.S).group(0)
    assert "computed_at === null" in summary        # ไม่มีข้อมูลที่คำนวณได้ -> ไม่โชว์ "0 ราย" เป็นตัวเลขจริง
    assert "centroid_error" in summary              # ดึงพิกัดไม่สำเร็จ -> ไม่โชว์ "ไม่มีพิกัด N หมู่บ้าน"
    load_past = CODE.split("function loadPast")[1].split("load().then(")[0]
    assert "pastSummary(body)" in load_past and "pastNoteEl.hidden" in load_past


# ---------------------------------------------------------------- ตำบลแยกสี + หมุดสถานพยาบาล
def fn_body(name):
    m = re.search(r"function\s+" + name + r"\s*\(.*?\n  \}", CODE, re.S)
    assert m, f"ต้องมีฟังก์ชัน {name}()"
    return m.group(0)


def test_tambon_layer_is_non_interactive_and_sent_to_back():
    body = fn_body("drawTambons")
    assert "interactive: false" in body and "bringToBack" in body


def test_tambon_label_uses_dom_element_not_html_string():
    body = fn_body("drawTambons")
    assert "createElement" in body and "textContent" in body
    assert "innerHTML" not in CODE
    assert not re.search(r"html:\s*[\"'`]", CODE)          # divIcon รับ DOM element เท่านั้น ไม่ใช่สตริง HTML


def test_tambon_palette_has_at_least_eight_distinct_colors():
    m = re.search(r"var\s+TAMBON_COLORS\s*=\s*\[(.*?)\];", CODE, re.S)
    assert m, "ต้องมี TAMBON_COLORS"
    colors = re.findall(r"#[0-9a-fA-F]{6}", m.group(1))
    assert len({c.lower() for c in colors}) >= 8 and len(colors) == len(set(c.lower() for c in colors))
    used = {"#1d6ff2", "#16a34a", "#dc2626", "#f97316", "#9a3412", "#0b2e8a", "#7f1d1d"}
    assert not used & {c.lower() for c in colors}            # ไม่ชนสีหมุดเคส/cluster/วงปีก่อน/เส้นขอบ
    edges = re.search(r"var\s+TAMBON_EDGES\s*=\s*\[(.*?)\];", CODE, re.S)
    assert edges and len(re.findall(r"#[0-9a-fA-F]{6}", edges.group(1))) == len(colors)


def test_tambon_legend_built_with_dom_chips():
    body = fn_body("drawTambons")
    assert "legend-tambon" in CODE and "style.background" in body


def test_facility_icons_built_from_dom():
    body = fn_body("facilityIcon")
    assert "createElement" in body and "textContent" in body and "L.divIcon" in body
    assert "pane" in CODE and "facilityPane" in CODE            # ชั้นแยกใต้หมุดเคส ไม่บังการคลิกเคส


def test_facility_dragend_sends_put_with_csrf_header_and_reverts_on_failure():
    assert "dragend" in CODE
    put = fn_body("putFacility")
    assert '"PUT"' in put and "X-Requested-With" in put and "dengue" in put and "application/json" in put
    assert "__CODE__" in put and "encodeURIComponent" in put
    drag = fn_body("onFacilityDragEnd")
    assert "putFacility(" in drag
    # คืนตำแหน่งเดิม (setLatLng) ต้องอยู่ในตัวจัดการ "ล้มเหลว" (callback ที่สองของ .then) ไม่ใช่ฝั่งสำเร็จ
    success, failure = re.search(r"\.then\(function \(\) \{(.*?)\}, function \(err\) \{(.*?)\}\);", drag, re.S).groups()
    assert "setLatLng" in failure and "setLatLng" not in success
    assert "f.lat = ll.lat" in success                                   # ฝั่งสำเร็จจดตำแหน่งใหม่


def test_facility_editing_hidden_unless_editable():
    body = fn_body("setupFacilityEditing")
    assert "editable === true" in body and "facilityAdminEl" in body
    assert 'getElementById("facility-admin")' in CODE
    # ปุ่ม/รายการแก้ไขโผล่เฉพาะเมื่อ editable; ผู้อื่นเห็นแค่ข้อความ "ยังไม่มีพิกัด"
    assert "facility-note" in CODE


def test_new_layers_failures_do_not_touch_case_layers():
    for name in ("loadTambons", "loadFacilities"):
        body = fn_body(name)
        assert "layerNote(" in body and "layers.clearLayers" not in body and "fail(" not in body, name
    assert CODE.count("layers.clearLayers()") == 1


def test_new_layers_start_after_cases_load():
    tail = CODE.split("load().then(")[1]
    assert tail.index("show(body)") < tail.index("loadTambons()") < tail.index("loadFacilities()")


def test_layer_toggles_wired():
    assert "toggle-tambon" in CODE and "toggle-facility" in CODE and "removeLayer" in CODE and "addLayer" in CODE


def test_placing_a_pin_updates_the_unplaced_note():
    place = fn_body("startPlacing")
    assert "refreshUnplacedNote(" in place                    # ข้อความ "ยังไม่มีพิกัด" ต้องไม่ค้างหลังวางหมุดสำเร็จ
    note = fn_body("refreshUnplacedNote")
    assert "facilityNoteEl.hidden = true" in note and "facilityNoteEl.textContent" in note
