// หน้า /trend: กดลูกศรหน้าตำบลเพื่อแสดง/ซ่อนแถวหมู่บ้าน (ไม่มี JS ก็ยังอ่านตารางหลักได้)
(function () {
  document.querySelectorAll(".area-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.getAttribute("data-key");
      var open = btn.getAttribute("aria-expanded") !== "true";
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "▾" : "▸";
      document.querySelectorAll('tr.area-child[data-parent="' + key + '"]').forEach(function (tr) {
        tr.hidden = !open;
      });
    });
  });
})();
