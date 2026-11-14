/* แท็บเลือกโรค: จำโรคล่าสุดต่อเครื่อง (localStorage) — ใช้ไม่ได้ก็ทำงานต่อได้ (ค่า ?d= ใน URL เป็นตัวหลัก) */
(function () {
  var KEY = "dengue_disease";
  var tabs = document.querySelector(".disease-tabs");
  if (!tabs) return;
  var links = [].slice.call(tabs.querySelectorAll("[data-disease]"));
  var current = tabs.getAttribute("data-current");
  var hasParam = /[?&]d=/.test(location.search);
  // ลิงก์จากข้อความ LINE (?cluster=N) เลือกโรคจากกลุ่มเอง — ห้ามเด้งไปโรคที่จำไว้
  var hasCluster = /[?&]cluster=/.test(location.search);
  try {
    var saved = localStorage.getItem(KEY);
    if (!hasParam && !hasCluster && saved && saved !== current) {
      for (var i = 0; i < links.length; i++) {
        if (links[i].getAttribute("data-disease") === saved) {
          // คง query เดิม (days/past ฯลฯ) แล้วเติม d
          location.replace(location.pathname + location.search + (location.search ? "&" : "?") + "d=" + encodeURIComponent(saved));
          return;
        }
      }
    }
    if (hasParam) localStorage.setItem(KEY, current);
  } catch (e) { /* พื้นที่เก็บข้อมูลถูกปิด */ }
  links.forEach(function (a) {
    a.addEventListener("click", function () {
      try { localStorage.setItem(KEY, a.getAttribute("data-disease")); } catch (e) { /* ข้าม */ }
    });
  });
})();
