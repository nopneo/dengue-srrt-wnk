/* หน้า /settings: แก้ค่าตั้งที่ซ้อนทับ .env (ฐานข้อมูล HOSxP, MOPH Notify, วัน go-live, การยืนยันตัวเลข)
   ข้อมูลหน้ามาจากบล็อก JSON #settings-data (ไม่มีค่าลับ); ฟอร์มสร้างฝั่งเซิร์ฟเวอร์
   - ตรรกะล้วน (เลือกช่องที่ส่ง, แปลผล API เป็นข้อความ, ตัดสินว่าเริ่มระบบใหม่แล้ว) อยู่ใน settings_logic.js (ทดสอบด้วย node)
   - ข้อความทุกจุดตั้งด้วย textContent / createElement (ไม่ใช้ HTML string); ข้อความจากเซิร์ฟเวอร์ใช้เฉพาะ `message` ภาษาไทยคงที่
   - ค่าลับอยู่เฉพาะในช่อง input แล้วเข้าตัวคำขอเท่านั้น; ล้างช่องหลังบันทึก/ย้อนกลับสำเร็จ */
(function () {
  "use strict";

  var Logic = window.SettingsLogic;
  var M = Logic ? Logic.M : null;
  var CSRF_HEADER = "X-Requested-With", CSRF_VALUE = "dengue";
  var POLL_EVERY_MS = 2000, POLL_MAX_MS = 60000, POLL_REQUEST_MS = 5000, POST_TIMEOUT_MS = 40000;
  var DB_KEYS = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS"];
  var MOPH_KEYS = ["MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"];
  var GROUPS = { db: DB_KEYS, moph: MOPH_KEYS };
  var MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];
  var STEP_LOOK = {
    ok: ["✔", "ผ่าน", "pill-green"],
    warn: ["⚠", "เตือน", "pill-orange"],
    fail: ["✘", "ล้มเหลว", "pill-red"]
  };
  var CONFIRM_REVERT = "ย้อนกลับไปใช้ค่าก่อนหน้า? ระบบจะเริ่มใหม่เพื่อใช้ค่านั้น";

  var dataEl = document.getElementById("settings-data");
  var form = document.getElementById("settings-form");
  var statusEl = document.getElementById("settings-status");
  var errorEl = document.getElementById("settings-error");
  if (!dataEl || !form || !statusEl || !errorEl) { return; }
  if (!Logic) {
    errorEl.textContent = "โหลดสคริปต์หน้าตั้งค่าไม่ครบ โหลดหน้านี้ใหม่";
    return;
  }

  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { data = null; }
  if (!data || !Array.isArray(data.items)) {
    errorEl.textContent = "อ่านข้อมูลหน้าตั้งค่าไม่ได้ โหลดหน้านี้ใหม่";
    return;
  }

  var items = {};
  data.items.forEach(function (it) { items[it.key] = it; });
  var fields = {}, clears = {}, stepLists = {};
  Array.prototype.forEach.call(form.querySelectorAll("[data-key]"), function (n) { fields[n.getAttribute("data-key")] = n; });
  Array.prototype.forEach.call(form.querySelectorAll("[data-clear]"), function (n) { clears[n.getAttribute("data-clear")] = n; });
  Array.prototype.forEach.call(form.querySelectorAll("[data-steps]"), function (n) { stepLists[n.getAttribute("data-steps")] = n; });
  var ALL_KEYS = Object.keys(fields);
  var touched = {};   // ช่องที่ผู้ใช้แตะ (input/change) — ช่อง source "default" ส่งเฉพาะที่แตะ (ดู SettingsLogic.collect)
  var confirmInput = form.querySelector("[data-confirm]");
  var forceBox = document.getElementById("force");
  var forceWrap = document.getElementById("force-wrap");
  var btnRevert = document.getElementById("btn-revert");
  var buttons = Array.prototype.slice.call(form.querySelectorAll("button"));
  var warnEl = document.getElementById("start-from-warning");
  var hintEl = document.getElementById("hint-start-from");

  // ---------- ข้อความสถานะ (พื้นที่ role=status/alert แสดงอยู่เสมอ: เปลี่ยนข้อความ ไม่สลับ hidden) ----------
  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined) { n.textContent = text; }
    return n;
  }

  function clearStatus() {
    statusEl.textContent = "";
    errorEl.textContent = "";
  }

  function say(text, extra) {
    errorEl.textContent = "";
    statusEl.textContent = text;
    if (extra) { statusEl.appendChild(extra); }
  }

  function fail(text, extra) {
    statusEl.textContent = "";
    errorEl.textContent = text;
    if (extra) { errorEl.appendChild(extra); }
  }

  function reloadButton() {
    var b = node("button", "btn sf-reload", "โหลดหน้าใหม่");
    b.type = "button";
    b.addEventListener("click", function () { window.location.reload(); });
    return b;
  }

  function loginLink() {
    var a = node("a", "sf-reload", "ไปหน้าเข้าสู่ระบบ");
    a.href = data.login_url || "/login";
    return a;
  }

  // ---------- ไฮไลต์ช่องที่ผิด ----------
  function markInvalid(input, on) {
    if (!input) { return; }
    input.classList.toggle("sf-invalid", on);
    if (on) { input.setAttribute("aria-invalid", "true"); } else { input.removeAttribute("aria-invalid"); }
  }

  function flag(key) {
    var input = key === "confirm" ? confirmInput : fields[key];
    markInvalid(input, true);
    if (input && input.focus) { input.focus(); }
  }

  function clearFlags() {
    Array.prototype.forEach.call(form.querySelectorAll(".sf-invalid"), function (n) { markInvalid(n, false); });
  }

  // ---------- รายการขั้นตอนการทดสอบ ----------
  function hideSteps(name) {
    var ul = stepLists[name];
    if (!ul) { return; }
    while (ul.firstChild) { ul.removeChild(ul.firstChild); }
    ul.hidden = true;
  }

  function renderSteps(name, steps) {
    var ul = stepLists[name];
    if (!ul) { return; }
    hideSteps(name);
    if (!Array.isArray(steps) || !steps.length) { return; }
    steps.forEach(function (st) {
      var look = STEP_LOOK[st && st.status] || STEP_LOOK.warn;
      var li = node("li");
      li.appendChild(node("span", "pill " + look[2], look[0] + " " + look[1]));
      li.appendChild(node("span", "step-name", typeof st.name === "string" ? st.name : ""));
      li.appendChild(node("span", "step-detail", typeof st.detail === "string" ? st.detail : ""));
      ul.appendChild(li);
    });
    ul.hidden = false;
  }

  // ---------- เก็บค่าจากฟอร์ม (ตรรกะอยู่ใน SettingsLogic.collect) ----------
  function collect(keys) {
    var raw = {}, ticked = {};
    ALL_KEYS.forEach(function (k) {
      raw[k] = fields[k].value;
      ticked[k] = !!(clears[k] && clears[k].checked);
    });
    return Logic.collect(items, raw, ticked, keys, touched);
  }

  // ---------- เรียก API ----------
  function post(url, payload) {
    var headers = { "Content-Type": "application/json" };
    headers[CSRF_HEADER] = CSRF_VALUE;
    var ctl = window.AbortController ? new AbortController() : null;
    var timedOut = false;
    var timer = ctl ? setTimeout(function () { timedOut = true; ctl.abort(); }, POST_TIMEOUT_MS) : null;
    var opts = { method: "POST", credentials: "same-origin", headers: headers, body: JSON.stringify(payload) };
    if (ctl) { opts.signal = ctl.signal; }
    return fetch(url, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (timer) { clearTimeout(timer); }
        return { status: res.status, body: (body && typeof body === "object") ? body : {} };
      });
    }, function () {
      if (timer) { clearTimeout(timer); }
      return timedOut ? { timeout: true, status: 0, body: {} } : { network: true, status: 0, body: {} };
    });
  }

  var busy = false, polling = false, pollTimer = null, lastButton = null;

  function setBusy(on) {
    busy = on;
    buttons.forEach(function (b) { b.disabled = on; });
  }

  function begin(stepsName) {
    clearStatus();
    clearFlags();
    if (stepsName) { hideSteps(stepsName); }
    lastButton = document.activeElement && document.activeElement.tagName === "BUTTON" ? document.activeElement : null;
    setBusy(true);
  }

  function end() {
    if (polling) { return; }
    setBusy(false);
    // ปุ่มที่กดถูกปิดระหว่างส่งจึงเสียโฟกัส: คืนให้ (ถ้าไม่มีช่องที่ถูกไฮไลต์ดึงโฟกัสไปแล้ว)
    var active = document.activeElement;
    if (lastButton && !lastButton.hidden && (!active || active === document.body)) { lastButton.focus(); }
  }

  function showFailure(r) {
    var d = Logic.describeFailure(r, ALL_KEYS);
    var extra = null;
    if (d.action === "login") { extra = loginLink(); }
    else if (d.action === "reload") { extra = reloadButton(); }
    fail(d.message, extra);
    if (d.action === "offer_force") {
      renderSteps("save", d.steps);
      if (forceWrap) { forceWrap.hidden = false; }
    } else if (d.action === "hide_revert" && btnRevert) {
      btnRevert.hidden = true;
    }
    if (d.field) { flag(d.field); }
  }

  // ข้อผิดพลาดตอนแสดงผลหลังได้คำตอบแล้ว (ไม่ใช่เครือข่ายล่ม): บอกว่าบันทึกแล้วถ้าคำขอสำเร็จไปแล้ว
  function renderBroken(landed) {
    fail(landed ? M.renderAfterSave : M.render, reloadButton());
  }

  // ---------- ปุ่มทดสอบ ----------
  function showTestResult(name, r) {
    renderSteps(name, r.body.steps);
    var s = Logic.summarizeTest(!!r.body.ok, r.body.steps);
    if (s.kind === "fail") { fail(s.message); } else { say(s.message); }
  }

  function runTest(target) {
    if (busy) { return; }
    clearFlags();
    var c = collect(GROUPS[target]);
    if (target === "db" && Logic.needsDbPassword(c.values)) { fail(M.needDbPass); flag("DB_PASS"); return; }
    begin(target);
    post(data.test_url, { target: target, values: c.values }).then(function (r) {
      if (r.status === 200 && Array.isArray(r.body.steps)) { showTestResult(target, r); }
      else { showFailure(r); }
    }).catch(function () { renderBroken(false); }).then(end);
  }

  function runSendTest() {
    if (busy) { return; }
    clearFlags();
    var c = collect(MOPH_KEYS);
    if (!window.confirm(Logic.confirmSendText(Object.keys(c.values).length > 0))) { return; }
    begin("moph");
    post(data.send_test_url, { confirm: true, values: c.values }).then(function (r) {
      if (r.status === 200 && Array.isArray(r.body.steps)) {
        renderSteps("moph", r.body.steps);
        if (r.body.ok) { say("ส่งข้อความทดสอบแล้ว — ตรวจในกลุ่ม LINE"); }
        else { fail("ส่งข้อความทดสอบไม่สำเร็จ — ดูรายละเอียดด้านล่างปุ่มทดสอบ"); }
      } else {
        showFailure(r);
      }
    }).catch(function () { renderBroken(false); }).then(end);
  }

  // ---------- บันทึก / ย้อนกลับ ----------
  function clearSecretInputs() {
    Array.prototype.forEach.call(form.querySelectorAll('input[type="password"]'), function (i) { i.value = ""; });
    Array.prototype.forEach.call(form.querySelectorAll('input[type="checkbox"]'), function (b) { b.checked = false; });
    touched = {};
    if (forceWrap) { forceWrap.hidden = true; }
    updateWarning();
  }

  function stopPolling() {
    polling = false;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  // เริ่มระบบใหม่แล้ว = /healthz ตอบ JSON ที่ boot_id ต่างจากตอนโหลดหน้า (ไม่ดู ok: ok เป็น false ได้ในสภาพที่หน้านี้ใช้แก้)
  function pollHealth() {
    var started = Date.now();
    polling = true;
    function tick() {
      pollTimer = null;
      if (Date.now() - started >= POLL_MAX_MS) {
        stopPolling();
        fail(M.restartTimeout, reloadButton());
        end();
        return;
      }
      var ctl = window.AbortController ? new AbortController() : null;
      var timer = ctl ? setTimeout(function () { ctl.abort(); }, POLL_REQUEST_MS) : null;
      var opts = { credentials: "same-origin", cache: "no-store" };
      if (ctl) { opts.signal = ctl.signal; }
      fetch(data.healthz_url, opts).then(function (res) { return res.json(); })
        .then(function (j) { return j; }, function () { return null; })
        .then(function (j) {
          if (timer) { clearTimeout(timer); }
          if (Logic.isRestarted(data.boot_id, j)) {
            stopPolling();
            say(M.restartOk);
            window.location.reload();
          } else {
            pollTimer = setTimeout(tick, POLL_EVERY_MS);
          }
        });
    }
    pollTimer = setTimeout(tick, POLL_EVERY_MS);
  }

  function afterSuccess(body, kind) {
    clearSecretInputs();
    hideSteps("save");
    var text, names = [];
    if (kind === "save") {
      (Array.isArray(body.changed) ? body.changed : []).forEach(function (k) { if (items[k]) { names.push(k); } });
      if (!names.length) { say(M.noChange); return; }
      text = "บันทึกแล้ว: " + names.join(", ");
      if (btnRevert) { btnRevert.hidden = false; }   // มีค่าก่อนหน้าให้ย้อนกลับเฉพาะเมื่อบันทึกนั้นเปลี่ยนอะไรจริง
    } else {
      text = "ย้อนกลับค่าก่อนหน้าแล้ว";
    }
    if (body.restart === "scheduled") {
      say(text + " — " + M.restarting);
      pollHealth();
    } else if (body.restart === "manual") {
      say(text + " — " + M.restartManual, reloadButton());
    } else {
      say(text);
    }
  }

  function runSave() {
    if (busy) { return; }
    clearFlags();
    var c = collect(ALL_KEYS);
    var conflict = Logic.findConflict(c);
    if (conflict) { fail(M.conflict); flag(conflict); return; }
    if (!Object.keys(c.values).length && !c.clear.length) { say(M.noChange); return; }
    if (Logic.needsDbPassword(c.values)) { fail(M.needDbPass); flag("DB_PASS"); return; }
    var pw = confirmInput.value;
    if (pw === "") { fail(M.needPassword); flag("confirm"); return; }
    begin("save");
    var landed = false;
    post(data.save_url, { values: c.values, clear: c.clear, password: pw, force: !!(forceBox && forceBox.checked) })
      .then(function (r) {
        if (r.status === 200 && r.body.ok) { landed = true; afterSuccess(r.body, "save"); } else { showFailure(r); }
      }).catch(function () { renderBroken(landed); }).then(function () {
        confirmInput.value = "";   // รหัสผ่านของผู้ดูแลถูกส่งไปแล้ว: ไม่ค้างในช่อง ไม่ว่าผลเป็นอย่างไร
        end();
      });
  }

  function runRevert() {
    if (busy) { return; }
    clearFlags();
    var pw = confirmInput.value;
    if (pw === "") { fail(M.needPassword); flag("confirm"); return; }
    if (!window.confirm(CONFIRM_REVERT)) { return; }
    begin("save");
    var landed = false;
    post(data.revert_url, { password: pw }).then(function (r) {
      if (r.status === 200 && r.body.ok) { landed = true; afterSuccess(r.body, "revert"); } else { showFailure(r); }
    }).catch(function () { renderBroken(landed); }).then(function () {
      confirmInput.value = "";
      end();
    });
  }

  // ---------- คำเตือน DENGUE_START_FROM ----------
  function dayNumber(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    if (!m) { return null; }
    var y = +m[1], mo = +m[2], d = +m[3];
    var t = Date.UTC(y, mo - 1, d), dt = new Date(t);
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) { return null; }
    return Math.round(t / 86400000);
  }

  function mophPresent(key) {
    var typed = fields[key] ? fields[key].value : "";
    var cleared = clears[key] && clears[key].checked;
    return typed.trim() !== "" || (!cleared && items[key] && items[key].state === "set");
  }

  function updateWarning() {
    var input = fields.DENGUE_START_FROM;
    if (!input) { return; }
    var text = input.value.trim();
    var start = dayNumber(text), today = dayNumber(data.server_today);
    if (hintEl) {
      if (start !== null) {
        var p = text.split("-");
        hintEl.textContent = +p[0] > 2400
          ? "ปีนี้เป็น พ.ศ. — ต้องใช้ปี ค.ศ. (เช่น " + String(data.server_today).slice(0, 4) + ")"
          : "= " + (+p[2]) + " " + MONTHS[+p[1] - 1] + " " + (+p[0] + 543) + " (พ.ศ.)";
      } else {
        hintEl.textContent = text === "" ? "" : "รูปแบบต้องเป็น ปปปป-ดด-วว";
      }
    }
    if (!warnEl) { return; }
    // I-2: START_FROM ยังไม่ตั้ง (ใช้วันเริ่มระบบ เลื่อนทุกครั้งที่ restart) และช่องยังว่าง -> เตือนให้ตั้งเป็นวัน go-live
    if (data.start_from_default && text === "") {
      warnEl.textContent = data.start_from_unset_warning || "";
      return;
    }
    // ย้อนหลังเกิน 1 วัน และมีกุญแจ MOPH ครบทั้งสองตัว (ตั้งไว้แล้วหรือกำลังพิมพ์) -> เคสสะสมจะถูกส่งรวดเดียว
    var past = start !== null && today !== null && today - start > 1;
    var keys = MOPH_KEYS.every(mophPresent);
    if (past && keys) {
      warnEl.textContent = data.start_from_warning;
    } else {
      warnEl.textContent = "";
    }
  }

  // ---------- เชื่อมเหตุการณ์ ----------
  function onEdit(e) {
    var key = e.target && e.target.getAttribute ? e.target.getAttribute("data-key") : null;
    if (key) { touched[key] = true; }
    if (e.target && e.target.classList && e.target.classList.contains("sf-invalid")) { markInvalid(e.target, false); }
    updateWarning();
  }
  form.addEventListener("input", onEdit);
  form.addEventListener("change", onEdit);
  form.addEventListener("submit", function (e) { e.preventDefault(); runSave(); });
  document.getElementById("btn-test-db").addEventListener("click", function () { runTest("db"); });
  document.getElementById("btn-test-moph").addEventListener("click", function () { runTest("moph"); });
  document.getElementById("btn-send-test").addEventListener("click", runSendTest);
  if (btnRevert) { btnRevert.addEventListener("click", runRevert); }
  // กลับมาจากแคชหน้า (ปุ่มย้อนกลับของเบราว์เซอร์): ไม่ให้มีรหัส/กุญแจค้างในช่อง
  window.addEventListener("pageshow", function (e) { if (e.persisted) { clearSecretInputs(); } });

  updateWarning();
})();
