/* หน้า /check: แผนที่จุดกลางหมู่บ้านของ รพ. (ตรวจด้วยตา) + ปุ่มยืนยัน/ล้างการยืนยันใน localStorage
   ข้อมูลหน้าอยู่ในบล็อก JSON #check-data (ชื่อหมู่บ้าน/พิกัดเท่านั้น); ข้อความทุกจุดตั้งด้วย textContent */
(function () {
  "use strict";

  var KEY = "dengue_audited";   // แถบเตือนใน base.html ซ่อนเมื่อค่านี้เป็น "1"

  // ---------- ปุ่มยืนยัน ----------
  var stateEl = document.getElementById("audit-state");
  var btnOn = document.getElementById("btn-audit");
  var btnOff = document.getElementById("btn-unaudit");

  function banner() { return document.querySelector('[data-testid="audit-banner"]'); }

  function say(text, isError) {
    if (!stateEl) { return; }
    stateEl.textContent = text;
    stateEl.className = "map-msg" + (isError ? " map-msg-error" : "");
    stateEl.hidden = false;
  }

  function isAudited() {
    try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; }
  }

  function showState() {
    say(isAudited() ? "เบราว์เซอร์นี้ยืนยันแล้วว่าตรวจกับข้อมูล รพ. นี้แล้ว (แถบเตือนถูกซ่อน)"
                    : "ยังไม่ได้ยืนยันในเบราว์เซอร์นี้", false);
  }

  function setAudited(on) {
    var ok = true;
    try {
      if (on) { localStorage.setItem(KEY, "1"); } else { localStorage.removeItem(KEY); }
    } catch (e) { ok = false; }
    if (!ok) { say("เบราว์เซอร์ไม่อนุญาตให้บันทึกการยืนยัน (ปิดการเก็บข้อมูลไซต์อยู่)", true); return; }
    var b = banner();
    if (b) { b.hidden = on; }
    showState();
  }

  if (btnOn) { btnOn.addEventListener("click", function () { setAudited(true); }); }
  if (btnOff) { btnOff.addEventListener("click", function () { setAudited(false); }); }
  if (stateEl) { showState(); }

  // ---------- แผนที่จุดกลางหมู่บ้าน ----------
  var dataEl = document.getElementById("check-data");
  var mapEl = document.getElementById("map");
  var msgEl = document.getElementById("check-map-msg");
  if (!dataEl || !mapEl) { return; }

  function mapFail(text) {
    if (!msgEl) { return; }
    msgEl.textContent = text;
    msgEl.hidden = false;
  }

  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { mapFail("อ่านข้อมูลหน้าแผนที่ไม่ได้"); return; }
  if (!window.L) { mapFail("โหลดแผนที่ไม่สำเร็จ"); return; }

  var map = L.map(mapEl).setView([data.center.lat, data.center.lng], data.center.zoom);
  // referrerPolicy "origin": เซิร์ฟเวอร์ tile (OSM) ต้องการ Referer แต่ส่งเฉพาะ origin
  // ไม่ส่ง path /check (หัวทั้งแอปเป็น same-origin จึงไม่มี Referer ถ้าไม่ตั้งตรงนี้)
  L.tileLayer(data.tile_url, {
    maxZoom: 19, attribution: data.attribution || "", referrerPolicy: "origin"
  }).addTo(map);

  var villages = data.villages || [];
  if (!villages.length) { mapFail("ไม่มีจุดกลางหมู่บ้านให้แสดง (village_gis_location ไม่มีพิกัดของหมู่บ้านใน รพ.)"); return; }

  var layer = L.featureGroup().addTo(map);
  villages.forEach(function (v) {
    var popup = document.createElement("div");
    var title = document.createElement("strong");
    title.textContent = v.name;
    var line = document.createElement("div");
    line.textContent = "รหัส " + v.code + " · " + v.lat.toFixed(5) + ", " + v.lng.toFixed(5);
    popup.appendChild(title);
    popup.appendChild(line);

    var label = document.createElement("span");
    label.textContent = v.name;

    L.circleMarker([v.lat, v.lng], {
      radius: 8, color: "#0b2a5b", weight: 2, fillColor: "#1d6ff2", fillOpacity: 0.85
    }).bindPopup(popup).bindTooltip(label, { permanent: true, direction: "top", offset: [0, -6] })
      .addTo(layer);
  });
  var bounds = layer.getBounds();
  if (bounds.isValid()) { map.fitBounds(bounds.pad(0.15), { maxZoom: 15 }); }
})();
