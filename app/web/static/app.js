// Bridges server events (SSE) to the DOM: progress bars and status badges update in
// place; nothing on the page reloads by itself.
(function () {
  function fmt(n) {
    var u = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  var es = new EventSource("/events");
  es.onmessage = function (m) {
    var e; try { e = JSON.parse(m.data); } catch (_) { return; }
    if (e.event === "file.progress") {
      document.querySelectorAll('[data-file-id="' + e.file_id + '"]').forEach(function (row) {
        var bar = row.querySelector(".bar > i");
        if (bar) bar.style.width = (100 * e.downloaded / e.total).toFixed(1) + "%";
        var t = row.querySelector(".progress-text");
        if (t) t.textContent = fmt(e.downloaded) + " / " + fmt(e.total) + " · " + fmt(e.speed) + "/s";
      });
      return;
    }
    if (e.event === "file.status") {
      document.querySelectorAll('[data-file-id="' + e.file_id + '"] .badge').forEach(function (b) {
        b.className = "badge " + e.status;
        b.textContent = e.status;
      });
      if (e.status === "done") {
        document.querySelectorAll('[data-file-id="' + e.file_id + '"] .bar > i').forEach(function (bar) {
          bar.style.width = "100%";
        });
      }
    }
  };
})();
