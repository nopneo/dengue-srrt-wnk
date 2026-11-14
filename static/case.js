/* หน้าเคส: แผนที่ Leaflet หมุดลากได้ + วงรัศมี + ปุ่มตำแหน่งปัจจุบัน/ยืนยันหมุด
   ข้อมูลมาจากบล็อก JSON #case-data เท่านั้น (ไม่มีชื่อ/เบอร์/ที่อยู่/HN) */
(function () {
  "use strict";

  var CSRF_HEADER = "X-Requested-With", CSRF_VALUE = "dengue";
  var COLORS = { estimated: "#1d6ff2", confirmed: "#16a34a" };
  var PILLS = {
    none: ["รอปักหมุด", "pill-orange"],
    estimated: ["ประมาณ", "pill-blue"],
    confirmed: ["ยืนยันแล้ว", "pill-green"]
  };
  var NO_GEO = "เบราว์เซอร์ไม่อนุญาตตำแหน่ง ใช้ลากหมุดบนแผนที่แทน";

  var dataEl = document.getElementById("case-data");
  var mapEl = document.getElementById("map");
  var msgEl = document.getElementById("pin-msg");
  if (!dataEl || !mapEl) { return; }

  function say(text, isError) {
    if (!msgEl) { return; }
    msgEl.textContent = text || "";
    msgEl.hidden = !text;
    msgEl.className = "map-msg" + (isError ? " map-msg-error" : "");
  }

  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { say("อ่านข้อมูลหน้าเคสไม่ได้", true); return; }
  if (!window.L) { say("โหลดแผนที่ไม่สำเร็จ", true); return; }

  var map = L.map(mapEl).setView([data.center.lat, data.center.lng], data.center.zoom);
  // referrerPolicy "origin": เซิร์ฟเวอร์ tile (OSM) ต้องการ Referer แต่ส่งเฉพาะ origin
  // ไม่ส่ง path /case/<id> (หัวทั้งแอปเป็น same-origin จึงไม่มี Referer ถ้าไม่ตั้งตรงนี้)
  L.tileLayer(data.tile_url, {
    maxZoom: 19, attribution: data.attribution || "", referrerPolicy: "origin"
  }).addTo(map);

  var state = data.pin.state;          // none | estimated | confirmed (ตามที่บันทึกในฐาน)
  var source = "manual";               // manual | gps (ของตำแหน่งที่กำลังจะยืนยัน)
  var marker = null, circle = null;

  function circleStyle(st) {
    var color = COLORS[st] || COLORS.estimated;
    return {
      radius: data.radius_m > 0 ? data.radius_m : 25, color: color, weight: 3,   // 0 = โรคกลุ่มก้อน: วงเล็กแค่ให้เห็นตำแหน่ง fillColor: color, fillOpacity: 0.12,
      dashArray: st === "confirmed" ? null : "8 6"   // ประมาณ/ร่าง = เส้นประ, ยืนยัน = เส้นทึบ
    };
  }

  function ensureMarker(latlng) {
    if (marker) { marker.setLatLng(latlng); circle.setLatLng(latlng); return; }
    marker = L.marker(latlng, { draggable: !!data.can_edit, keyboard: !!data.can_edit });
    circle = L.circle(latlng, circleStyle(state === "none" ? "estimated" : state));
    circle.addTo(map);
    marker.addTo(map);
    if (data.can_edit) {
      marker.on("drag", function () { circle.setLatLng(marker.getLatLng()); });
      marker.on("dragend", function () {
        source = "manual";
        circle.setStyle(circleStyle("estimated"));   // ย้ายแล้ว = ยังไม่ยืนยัน (เส้นประ)
        say("ย้ายหมุดแล้ว ยังไม่ได้ยืนยัน กด \"ยืนยันหมุด\" เพื่อบันทึก");
      });
    }
  }

  function setPill(st) {
    var el = document.getElementById("pin-pill");
    if (!el) { return; }
    el.textContent = PILLS[st][0];
    el.className = "pill " + PILLS[st][1];
  }

  if (data.pin.state !== "none" && data.pin.lat !== null) {
    ensureMarker([data.pin.lat, data.pin.lng]);
    map.fitBounds(circle.getBounds(), { maxZoom: 18, padding: [24, 24] });
  }

  // เคสอื่นในรัศมี: จุดเล็ก ๆ อ่านอย่างเดียว (ลิงก์ไปหน้าเคสนั้น)
  (data.neighbors || []).forEach(function (n) {
    var dot = L.circleMarker([n.lat, n.lng], {
      radius: 7, weight: 2, color: "#0b2e8a",
      fillColor: COLORS[n.pin_state] || COLORS.estimated, fillOpacity: 0.9
    }).addTo(map);
    var box = document.createElement("div");
    var link = document.createElement("a");
    link.href = n.url;
    link.textContent = "เคส #" + n.case_id;
    box.appendChild(link);
    box.appendChild(document.createTextNode(" · " + n.distance_m + " ม."));
    dot.bindPopup(box);
  });

  if (!data.can_edit) { return; }

  // ---- ส่วนแก้ไขหมุด (เฉพาะ srrt ขึ้นไป) ----
  var btnLocate = document.getElementById("btn-locate");
  var btnConfirm = document.getElementById("btn-confirm");

  map.on("click", function (e) {
    if (marker) { return; }            // มีหมุดแล้วให้ลากเอา กันคลิกพลาดย้ายหมุด
    ensureMarker(e.latlng.wrap());
    source = "manual";
    say("วางหมุดแล้ว ลากปรับตำแหน่งได้ แล้วกด \"ยืนยันหมุด\"");
  });

  btnLocate.addEventListener("click", function () {
    if (!navigator.geolocation || !window.isSecureContext) { say(NO_GEO, true); return; }
    say("กำลังหาตำแหน่ง...");
    navigator.geolocation.getCurrentPosition(function (pos) {
      var ll = L.latLng(pos.coords.latitude, pos.coords.longitude);
      ensureMarker(ll);
      source = "gps";
      circle.setStyle(circleStyle("estimated"));
      map.setView(ll, 18);
      say("ใช้ตำแหน่งปัจจุบันแล้ว ตรวจตำแหน่งแล้วกด \"ยืนยันหมุด\"");
    }, function () { say(NO_GEO, true); }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });

  btnConfirm.addEventListener("click", function () {
    if (!marker) { say("ยังไม่ได้วางหมุด คลิกบนแผนที่หรือใช้ตำแหน่งปัจจุบัน", true); return; }
    var ll = marker.getLatLng().wrap();
    btnConfirm.disabled = true;
    say("กำลังบันทึก...");
    var headers = { "Content-Type": "application/json" };
    headers[CSRF_HEADER] = CSRF_VALUE;
    fetch(data.pin_url, {
      method: "POST", credentials: "same-origin", headers: headers,
      body: JSON.stringify({ lat: ll.lat, lng: ll.lng, state: "confirmed", source: source })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { status: res.status, body: body };
      });
    }).then(function (r) {
      if (r.status === 200 && r.body.ok) {
        state = "confirmed";
        circle.setStyle(circleStyle("confirmed"));
        setPill("confirmed");
        var alerted = (r.body.clusters_alerted || []).length;
        say(alerted ? "ยืนยันหมุดแล้ว และส่งแจ้งเตือนกลุ่มผู้ป่วยแล้ว" : "ยืนยันหมุดแล้ว");
      } else if (r.status === 401) {
        say("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่", true);
      } else {
        say(r.body.message || "บันทึกหมุดไม่สำเร็จ (" + r.status + ")", true);
      }
    }).catch(function () {
      say("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง", true);
    }).then(function () { btnConfirm.disabled = false; });
  });
})();
