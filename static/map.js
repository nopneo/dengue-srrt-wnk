/* แผนที่รวม: หมุดทุกเคส active ในช่วงวัน สีตามสถานะหมุด + ไฮไลต์สมาชิก cluster ที่เลือก
   ข้อมูลหน้าอยู่ในบล็อก JSON #map-data; พิกัดเคสมาจาก api_url (ไม่มี HN/ชื่อ/เบอร์/ที่อยู่) */
(function () {
  "use strict";

  var COLORS = { estimated: "#1d6ff2", confirmed: "#16a34a", cluster: "#dc2626" };
  var PAST_FILL = "#f97316", PAST_EDGE = "#9a3412";      // วงกลมการระบาดปีก่อน (ส้ม ต่างจากหมุดเคสปัจจุบัน)
  // ตำบลแยกสี (พื้นอ่อน 8 สี ไม่ชนสีหมุดเคส/cluster/วงปีก่อน/เส้นขอบ) + สีขอบเข้มกว่าเล็กน้อยของแต่ละสี
  var TAMBON_COLORS = ["#a78bfa", "#fbbf24", "#22d3ee", "#f472b6", "#a3e635", "#94a3b8", "#fb7185", "#2dd4bf"];
  var TAMBON_EDGES = ["#6d4fd6", "#b8860b", "#0e8aa0", "#c0307f", "#5f9a0a", "#5b6b82", "#cc3550", "#12877c"];
  var BOUNDARY_STYLE = { color: "#0b2e8a", weight: 2.5, dashArray: "8 6", fill: false, interactive: false };
  var LABELS = { estimated: "ประมาณ", confirmed: "ยืนยันแล้ว" };
  var MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];
  var DEFAULT_VIEW = { lat: 14.35, lng: 101.90, zoom: 12 };

  var dataEl = document.getElementById("map-data");
  var mapEl = document.getElementById("map");
  var alertEl = document.getElementById("map-alert");
  var emptyEl = document.getElementById("map-empty");
  var countEl = document.getElementById("map-count");
  var pastNoteEl = document.getElementById("past-note");
  var layerNoteEl = document.getElementById("layer-note");
  var legendBoundaryEl = document.getElementById("legend-boundary");
  var legendTambonEl = document.getElementById("legend-tambon");
  var legendFacilityEl = document.getElementById("legend-facility");
  var facilityAdminEl = document.getElementById("facility-admin");
  var facilityEditBtn = document.getElementById("facility-edit");
  var facilityUnplacedEl = document.getElementById("facility-unplaced");
  var facilityNoteEl = document.getElementById("facility-note");
  var facilityStatusEl = document.getElementById("facility-status");
  var toggleTambonEl = document.getElementById("toggle-tambon");
  var toggleFacilityEl = document.getElementById("toggle-facility");
  if (!dataEl || !mapEl) { return; }

  function fail(text, loginUrl) {
    if (!alertEl) { return; }
    alertEl.textContent = text;
    if (loginUrl) {   // เซสชันหมดอายุ: ลิงก์ไปหน้า login (URL จากเซิร์ฟเวอร์ รวม prefix แล้ว)
      var link = document.createElement("a");
      link.href = loginUrl;
      link.textContent = "เข้าสู่ระบบ";
      alertEl.appendChild(document.createTextNode(" "));
      alertEl.appendChild(link);
    }
    alertEl.className = "map-msg map-msg-error";
    alertEl.hidden = false;
  }

  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { fail("อ่านข้อมูลหน้าแผนที่ไม่ได้"); return; }
  if (!window.L) { fail("โหลดแผนที่ไม่สำเร็จ"); return; }

  var map = L.map(mapEl).setView([DEFAULT_VIEW.lat, DEFAULT_VIEW.lng], DEFAULT_VIEW.zoom);
  // referrerPolicy "origin": เซิร์ฟเวอร์ tile (OSM) ต้องการ Referer แต่ส่งเฉพาะ origin
  // ไม่ส่ง path ของหน้า (หัวทั้งแอปเป็น same-origin จึงไม่มี Referer ถ้าไม่ตั้งตรงนี้)
  L.tileLayer(data.tile_url, {
    maxZoom: 19, attribution: data.attribution || "", referrerPolicy: "origin"
  }).addTo(map);
  var layers = L.featureGroup().addTo(map);   // เลเยอร์เคสทั้งหมด (ล้างได้ถ้าวาดพลาดกลางคัน)
  var boundaryBounds = null;                  // ขอบเขตอำเภอ (ถ้ามีไฟล์) ใช้ซูมพอดีเมื่อไม่ได้เลือกกลุ่ม
  // ชั้นแยกใต้ชั้นวาดเคส (overlayPane = 400): ป้ายตำบล 380 และหมุดสถานพยาบาล 390 — ไม่บังการคลิกเคส/วงปีก่อน
  map.createPane("tambonLabelPane").style.zIndex = 380;
  map.createPane("facilityPane").style.zIndex = 390;
  var tambonLayer = L.layerGroup();
  var tambonLabels = L.layerGroup();
  var facilityLayer = L.featureGroup();
  var facilityMarkers = {};                   // รหัสสถานพยาบาล -> marker
  var facilityEditing = false;
  var placingHandler = null;

  // เลเยอร์เสริม (เส้นขอบ/ปีก่อน) ล้มได้โดยไม่กระทบเคสปัจจุบัน: แจ้งในแถบของเลเยอร์เท่านั้น
  function layerNote(text) {
    if (!layerNoteEl) { return; }
    var line = document.createElement("div");
    line.textContent = text;
    layerNoteEl.appendChild(line);
    layerNoteEl.hidden = false;
  }

  function applyBoundaryFit() {
    if (data.selected_cluster === null && boundaryBounds) {   // เลือกกลุ่ม = ซูมที่สมาชิกกลุ่มตามเดิม
      map.fitBounds(boundaryBounds, { padding: [16, 16], animate: false });
    }
  }

  // ISO "2026-11-08T09:00:00" -> "8 พ.ย. 2569" (ตัดจากสตริงตรง ๆ ไม่ผ่านเขตเวลาเบราว์เซอร์)
  function thaiDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    if (!m) { return "-"; }
    return parseInt(m[3], 10) + " " + MONTHS[parseInt(m[2], 10) - 1] + " " + (parseInt(m[1], 10) + 543);
  }

  function popupFor(c) {
    var box = document.createElement("div");
    var title = document.createElement("div");
    var link = document.createElement("a");
    link.href = c.url;
    link.textContent = "เปิดเคส";
    title.appendChild(link);
    var line = document.createElement("div");
    line.textContent = thaiDate(c.dx_at) + " · " + (LABELS[c.pin_state] || "-") +
      " · " + (c.in_area ? "ในเขต" : "นอกเขต");
    box.appendChild(title);
    box.appendChild(line);
    return box;
  }

  function render(cases) {
    var selected = data.selected_cluster;
    var members = L.featureGroup();   // วงรัศมีของสมาชิกกลุ่ม (ไว้คำนวณขอบเขต)
    // วาดเคสทั่วไปก่อน แล้วสมาชิกกลุ่มทับด้านบน
    var order = cases.slice().sort(function (a, b) {
      return (selected !== null && a.cluster_id === selected ? 1 : 0) -
             (selected !== null && b.cluster_id === selected ? 1 : 0);
    });
    order.forEach(function (c) {
      var ll = [c.lat, c.lng];
      var isMember = selected !== null && c.cluster_id === selected;
      var color = COLORS[c.pin_state] || COLORS.estimated;
      var dot;
      if (isMember) {
        if (data.radius_m > 0) {   // โรคจัดกลุ่มตามหมู่บ้านไม่มีวงรัศมี
          L.circle(ll, {
            radius: data.radius_m, color: COLORS.cluster, weight: 2,
            fillColor: COLORS.cluster, fillOpacity: 0.1, interactive: false
          }).addTo(layers).addTo(members);
        }
        dot = L.circleMarker(ll, {
          radius: 8, weight: 2, color: "#7f1d1d", fillColor: COLORS.cluster, fillOpacity: 0.95
        });
        if (!(data.radius_m > 0)) { dot.addTo(members); }
      } else {
        if (selected === null && c.pin_state === "confirmed" && data.radius_m > 0) {
          L.circle(ll, {   // ไม่เลือกกลุ่ม: เฉพาะหมุดยืนยันมีวงบาง ๆ (ประมาณไม่วง กันรก)
            radius: data.radius_m, color: color, weight: 1, fillColor: color,
            fillOpacity: 0.05, interactive: false
          }).addTo(layers);
        }
        dot = L.circleMarker(ll, {
          radius: selected === null ? 7 : 5, weight: 1.5, color: "#0b2e8a",
          fillColor: color, fillOpacity: selected === null ? 0.9 : 0.6
        });
      }
      dot.bindPopup(popupFor(c)).addTo(layers);
    });
    return members;
  }

  // ตั้งมุมมองเริ่มต้นแบบไม่แอนิเมชันทั้งหมด (animate:false): Leaflet ทิ้งคำสั่งซูมที่มาระหว่างแอนิเมชันซูมที่ยังวิ่งอยู่
  // จึงถ้า fit(cases) แอนิเมชันอยู่ การซูมพอดีอำเภอที่ตามมาตอนโหลดเส้นขอบเสร็จจะหาย (ขึ้นกับจังหวะเครือข่าย)
  function fit(cases, members) {
    if (members.getLayers().length) {   // สมาชิกกลุ่ม: ให้เห็นวงรัศมีเต็ม
      map.fitBounds(members.getBounds(), { maxZoom: 18, padding: [24, 24], animate: false });
    } else if (cases.length) {
      map.fitBounds(L.latLngBounds(cases.map(function (c) { return [c.lat, c.lng]; })),
        { maxZoom: 16, padding: [32, 32], animate: false });
    }
  }

  function ApiError(message, loginUrl) { this.message = message; this.loginUrl = loginUrl || null; }

  // ขั้นดึง+แปลงข้อมูล (ข้อผิดพลาดเครือข่าย/สถานะ/JSON เสีย) แยกจากขั้นวาด
  function load() {
    return fetch(data.api_url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (res.status === 401) { throw new ApiError("เซสชันหมดอายุ", data.login_url); }
        if (!res.ok) { throw new ApiError("โหลดข้อมูลแผนที่ไม่สำเร็จ (" + res.status + ")"); }
        return res.json().then(null, function () { throw new ApiError("อ่านข้อมูลแผนที่ไม่ได้"); });
      }, function () { throw new ApiError("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง"); })
      .then(function (body) {
        if (!body || !Array.isArray(body.cases)) { throw new ApiError("อ่านข้อมูลแผนที่ไม่ได้"); }
        return body;
      });
  }

  function show(body) {
    var cases = body.cases;
    if (!cases.length) { if (emptyEl) { emptyEl.hidden = false; } return; }
    var members = render(cases);
    fit(cases, members);
    applyBoundaryFit();   // ถ้าเส้นขอบโหลดมาก่อนเคส ให้ซูมพอดีอำเภอทับการซูมที่เคส
    if (countEl) {
      countEl.textContent = "แสดง " + cases.length + " เคส ในช่วง " + body.days + " วันล่าสุด";
      countEl.hidden = false;
    }
  }

  function loadJson(url) {
    return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (!res.ok) { throw new Error("status " + res.status); }
        return res.json();
      });
  }

  function loadBoundary() {
    if (!data.boundary_url) { return; }
    loadJson(data.boundary_url).then(function (body) {
      if (!body || !Array.isArray(body.features) || !body.features.length) { return; }   // ไม่มีไฟล์: ทำงานเหมือนเดิม
      var layer = L.geoJSON({ type: "FeatureCollection", features: body.features },
                            { style: function () { return BOUNDARY_STYLE; }, interactive: false }).addTo(map);
      var bounds = layer.getBounds();
      if (bounds.isValid()) { boundaryBounds = bounds; }
      if (legendBoundaryEl) { legendBoundaryEl.hidden = false; }
      applyBoundaryFit();
    }).catch(function () {
      layerNote("โหลดเส้นขอบอำเภอไม่สำเร็จ");
    });
  }

  function pastPopup(v, year) {
    var box = document.createElement("div");
    box.textContent = v.name + " · ปี พ.ศ. " + year + ": " + v.cases + " ราย";
    return box;
  }

  function pastSummary(body) {
    // ไม่มีข้อมูลที่คำนวณได้ (ฐานล่ม/กำลังคำนวณ): แสดงเฉพาะแถบข้อผิดพลาด ห้ามโชว์ "0 ราย" ให้อ่านเป็นตัวเลขจริง
    if (body.computed_at === null) { return null; }
    var text = "ปี พ.ศ. " + body.year + ": " + body.total + " ราย";
    if (body.centroid_error) {
      text += " (ยังไม่แสดงวงกลม)";   // ถามจุดกลางไม่สำเร็จ ≠ ไม่มีพิกัดในตาราง
    } else {
      text += " ใน " + body.villages.length + " หมู่บ้าน";
      if (body.unmapped && body.unmapped.cases > 0) {
        text += " · ไม่มีพิกัด " + (body.unmapped.villages > 0 ? body.unmapped.villages + " หมู่บ้าน " : "") +
                "(" + body.unmapped.cases + " ราย)";
      }
    }
    if (body.stale && body.computed_at) { text += " · ข้อมูลที่เก็บไว้เมื่อ " + thaiDate(body.computed_at); }
    return text;
  }

  function loadPast() {
    if (!data.past_url) { return; }
    loadJson(data.past_url).then(function (body) {
      if (!body || !Array.isArray(body.villages)) { throw new Error("shape"); }
      var pastLayer = L.featureGroup().addTo(map);
      body.villages.forEach(function (v) {
        var c = L.circleMarker([v.lat, v.lng], {
          radius: Math.min(40, 6 + 4 * Math.sqrt(v.cases)), weight: 1.5, color: PAST_EDGE,
          fillColor: PAST_FILL, fillOpacity: 0.35
        });
        c.bindPopup(pastPopup(v, body.year)).addTo(pastLayer);
      });
      pastLayer.eachLayer(function (l) { l.bringToBack(); });   // อยู่ใต้หมุดเคสปัจจุบัน
      var summary = pastSummary(body);
      if (pastNoteEl && summary !== null) {
        pastNoteEl.textContent = summary;
        pastNoteEl.hidden = false;
      }
      if (body.error) { layerNote("ปี พ.ศ. " + body.year + ": " + body.error); }
    }).catch(function () {
      layerNote("โหลดข้อมูลการระบาดปี พ.ศ.ก่อนหน้าไม่สำเร็จ");
    });
  }

  // ---- ตำบลแยกสี ----
  function ringCentroid(ring) {
    var a = 0, cx = 0, cy = 0;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      var f = ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
      a += f; cx += (ring[j][0] + ring[i][0]) * f; cy += (ring[j][1] + ring[i][1]) * f;
    }
    if (a === 0) { return [ring[0][0], ring[0][1], 0]; }
    return [cx / (3 * a), cy / (3 * a), Math.abs(a / 2)];
  }

  function labelPoint(geometry) {     // จุดกึ่งกลาง (centroid) ของ polygon ที่ใหญ่ที่สุด
    var polys = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;
    var best = null;
    polys.forEach(function (rings) {
      var c = ringCentroid(rings[0]);
      if (best === null || c[2] > best[2]) { best = c; }
    });
    return best ? [best[1], best[0]] : null;
  }

  function drawTambons(features) {
    var sorted = features.slice().sort(function (a, b) {
      return String(a.properties.code) < String(b.properties.code) ? -1 : 1;
    });
    if (legendTambonEl) { legendTambonEl.textContent = ""; }
    sorted.forEach(function (f, i) {
      var fill = TAMBON_COLORS[i % TAMBON_COLORS.length], edge = TAMBON_EDGES[i % TAMBON_EDGES.length];
      var shape = L.geoJSON(f, {
        style: function () { return { color: edge, weight: 1.5, fillColor: fill, fillOpacity: 0.22, interactive: false }; },
        interactive: false
      });
      shape.addTo(tambonLayer);
      var at = labelPoint(f.geometry);
      if (at) {
        var label = document.createElement("span");
        label.className = "tambon-label";
        label.textContent = f.properties.name;
        L.marker(at, {
          pane: "tambonLabelPane", interactive: false, keyboard: false,
          icon: L.divIcon({ className: "tambon-label-wrap", html: label, iconSize: [0, 0] })
        }).addTo(tambonLabels);
      }
      if (legendTambonEl) {
        var li = document.createElement("li");
        var chip = document.createElement("span");
        chip.className = "chip";
        chip.style.background = fill;
        chip.style.borderColor = edge;
        li.appendChild(chip);
        li.appendChild(document.createTextNode(f.properties.name));
        legendTambonEl.appendChild(li);
      }
    });
    var on = !toggleTambonEl || toggleTambonEl.checked;
    if (on) { map.addLayer(tambonLayer); map.addLayer(tambonLabels); }
    tambonLayer.eachLayer(function (g) { if (g.bringToBack) { g.bringToBack(); } });   // ใต้ทุกชั้น
    if (legendTambonEl) { legendTambonEl.hidden = false; }
  }

  // ---- หมุดสถานพยาบาล ----
  function facilityIcon(kind) {
    var el = document.createElement("span");
    var hospital = kind === "hospital";
    el.className = "fac " + (hospital ? "fac-hospital" : "fac-rpst");
    el.textContent = hospital ? "รพ." : "+";
    return L.divIcon({ className: "fac-wrap", html: el,
                       iconSize: hospital ? [30, 22] : [20, 20], iconAnchor: hospital ? [15, 11] : [10, 10] });
  }

  function facilityPopup(f) {
    var box = document.createElement("div");
    var title = document.createElement("strong");
    title.textContent = f.name;
    var line = document.createElement("div");
    line.textContent = (f.kind === "hospital" ? "โรงพยาบาล" : "รพ.สต.") +
      " · ต." + (f.tmb_name || f.tmb);
    box.appendChild(title);
    box.appendChild(line);
    return box;
  }

  function facilityStatus(text, isError) {
    if (!facilityStatusEl) { return; }
    facilityStatusEl.textContent = text;
    facilityStatusEl.className = isError ? "map-msg map-msg-error" : "map-hint";
    facilityStatusEl.hidden = false;
  }

  function putFacility(code, lat, lng) {
    var url = data.facility_put_url.replace("__CODE__", encodeURIComponent(code));
    return fetch(url, {
      method: "PUT", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-Requested-With": "dengue" },
      body: JSON.stringify({ lat: lat, lng: lng })
    }).then(function (res) {
      return res.json().then(null, function () { return {}; }).then(function (body) {
        if (!res.ok) { throw new Error(body && body.message ? body.message : "บันทึกตำแหน่งไม่สำเร็จ"); }
        return body;
      });
    });
  }

  function onFacilityDragEnd(marker) {
    var f = marker.facility, ll = marker.getLatLng();
    putFacility(f.code, ll.lat, ll.lng).then(function () {
      f.lat = ll.lat; f.lng = ll.lng;
      facilityStatus("บันทึกตำแหน่ง " + f.name + " แล้ว", false);
    }, function (err) {
      marker.setLatLng([f.lat, f.lng]);     // คืนตำแหน่งเดิม
      facilityStatus((err && err.message) || "บันทึกตำแหน่งไม่สำเร็จ", true);
    });
  }

  function addFacilityMarker(f) {
    var m = L.marker([f.lat, f.lng], { pane: "facilityPane", icon: facilityIcon(f.kind), draggable: false,
                                        keyboard: true, title: f.name });
    m.facility = f;
    m.bindPopup(facilityPopup(f));
    m.on("dragend", function () { onFacilityDragEnd(m); });
    m.addTo(facilityLayer);
    facilityMarkers[f.code] = m;
    if (facilityEditing && m.dragging) { m.dragging.enable(); }
    return m;
  }

  function setFacilityEditing(on) {
    facilityEditing = on;
    if (facilityEditBtn) {
      facilityEditBtn.setAttribute("aria-pressed", on ? "true" : "false");
      facilityEditBtn.textContent = on ? "เสร็จสิ้นการจัดตำแหน่ง" : "จัดตำแหน่งสถานพยาบาล";
    }
    Object.keys(facilityMarkers).forEach(function (code) {
      var m = facilityMarkers[code];
      if (m.dragging) { if (on) { m.dragging.enable(); } else { m.dragging.disable(); } }
    });
    if (facilityUnplacedEl) { facilityUnplacedEl.hidden = !on; }
    if (!on && placingHandler) { map.off("click", placingHandler); placingHandler = null; }
    facilityStatus(on ? "ลากหมุดเพื่อย้ายตำแหน่ง — บันทึกอัตโนมัติเมื่อปล่อย" : "ปิดโหมดจัดตำแหน่งแล้ว", false);
  }

  var unplacedItems = [];                    // สถานพยาบาลที่ยังไม่มีพิกัด (ลดลงเมื่อวางหมุดสำเร็จ)

  function refreshUnplacedNote() {
    if (!facilityNoteEl) { return; }
    if (!unplacedItems.length) { facilityNoteEl.hidden = true; return; }
    facilityNoteEl.textContent = "สถานพยาบาลที่ยังไม่มีพิกัด: " +
      unplacedItems.map(function (u) { return u.name; }).join(", ");
    facilityNoteEl.hidden = false;
  }

  function startPlacing(item, button) {
    if (placingHandler) { map.off("click", placingHandler); }
    var handler = function (e) {
      map.off("click", handler);
      placingHandler = null;
      button.classList.remove("on");
      putFacility(item.code, e.latlng.lat, e.latlng.lng).then(function () {
        addFacilityMarker({ code: item.code, name: item.name, kind: item.kind, tmb: item.tmb || "", tmb_name: null,
                            lat: e.latlng.lat, lng: e.latlng.lng, source: "manual" });
        if (button.parentNode) { button.parentNode.parentNode.removeChild(button.parentNode); }
        unplacedItems = unplacedItems.filter(function (u) { return u.code !== item.code; });
        refreshUnplacedNote();
        facilityStatus("วางหมุด " + item.name + " แล้ว", false);
      }, function (err) {
        facilityStatus((err && err.message) || "บันทึกตำแหน่งไม่สำเร็จ", true);
      });
    };
    placingHandler = handler;
    button.classList.add("on");
    facilityStatus("คลิกบนแผนที่เพื่อวางหมุด " + item.name, false);
    map.once("click", handler);
  }

  function setupFacilityEditing(body) {
    var unplaced = body.unplaced || [];
    unplacedItems = unplaced.slice();
    refreshUnplacedNote();
    if (body.editable === true && facilityAdminEl && facilityEditBtn) {   // ผู้ดูแลเท่านั้น (เซิร์ฟเวอร์ตรวจซ้ำที่ PUT)
      facilityAdminEl.hidden = false;
      if (facilityUnplacedEl) {
        facilityUnplacedEl.textContent = "";
        facilityUnplacedEl.hidden = true;
        unplaced.forEach(function (u) {
          var li = document.createElement("li");
          li.appendChild(document.createTextNode(u.name));
          var btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = "วางหมุด";
          btn.addEventListener("click", function () { startPlacing(u, btn); });
          li.appendChild(btn);
          facilityUnplacedEl.appendChild(li);
        });
      }
      facilityEditBtn.addEventListener("click", function () { setFacilityEditing(!facilityEditing); });
    }
  }

  function loadTambons() {
    if (!data.tambon_url) { return; }
    loadJson(data.tambon_url).then(function (body) {
      if (!body || !Array.isArray(body.features) || !body.features.length) { return; }   // ไม่มีไฟล์: ทำงานเหมือนเดิม
      drawTambons(body.features);
    }).catch(function () {
      layerNote("โหลดขอบเขตตำบลไม่สำเร็จ");
    });
  }

  function loadFacilities() {
    if (!data.facility_url) { return; }
    loadJson(data.facility_url).then(function (body) {
      if (!body || !Array.isArray(body.facilities)) { throw new Error("shape"); }
      body.facilities.forEach(addFacilityMarker);
      var on = !toggleFacilityEl || toggleFacilityEl.checked;
      if (on) { map.addLayer(facilityLayer); }
      if (legendFacilityEl && body.facilities.length) { legendFacilityEl.hidden = false; }
      setupFacilityEditing(body);
    }).catch(function () {
      layerNote("โหลดหมุดสถานพยาบาลไม่สำเร็จ");
    });
  }

  if (toggleTambonEl) {
    toggleTambonEl.addEventListener("change", function () {
      if (toggleTambonEl.checked) { map.addLayer(tambonLayer); map.addLayer(tambonLabels); }
      else { map.removeLayer(tambonLayer); map.removeLayer(tambonLabels); }
    });
  }
  if (toggleFacilityEl) {
    toggleFacilityEl.addEventListener("change", function () {
      if (toggleFacilityEl.checked) { map.addLayer(facilityLayer); } else { map.removeLayer(facilityLayer); }
    });
  }

  load().then(function (body) {
    try {
      show(body);
      loadBoundary();   // เลเยอร์เสริมเริ่มหลังเคสวาดเสร็จ — ล้มแล้วเคสยังอยู่
      loadPast();
      loadTambons();
      loadFacilities();
    } catch (e) {   // วาดพลาด: ล้างของที่วาดค้าง ไม่ให้แสดงข้อความว่าเชื่อมต่อไม่ได้
      layers.clearLayers();
      if (countEl) { countEl.hidden = true; }
      fail("แสดงแผนที่ไม่สำเร็จ ลองรีเฟรชหน้า");
    }
  }, function (err) {
    fail(err instanceof ApiError ? err.message : "เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง",
         err instanceof ApiError ? err.loginUrl : null);
  });
})();
