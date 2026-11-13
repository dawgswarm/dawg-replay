// dawg-replay: CSRF-заголовок для htmx и живое обновление списков по SSE.
// Встроенных скриптов на страницах нет: CSP разрешает только файлы с этого сервера.
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";

  document.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["X-CSRF-Token"] = csrf;
  });

  // Ответ с ошибкой сервера (403 после перезапуска, 500) показываем человеку.
  document.addEventListener("htmx:responseError", function (e) {
    var status = e.detail.xhr.status;
    var text = status === 403
      ? "Сервер не принял запрос. Если вы перезапускали dawg-replay ui, откройте новую ссылку из терминала."
      : "Сервер ответил ошибкой " + status + ". Повторите действие или посмотрите вывод dawg-replay ui в терминале.";
    var box = document.getElementById("global-error");
    if (!box) {
      box = document.createElement("p");
      box.id = "global-error";
      box.className = "notice notice-error";
      box.setAttribute("role", "alert");
      document.querySelector("main").prepend(box);
    }
    box.textContent = text;
  });

  // Списки с data-live-runs обновляются, когда меняются запуски (в том числе начатые из CLI).
  var source = null;
  function connect() {
    if (source || !document.querySelector("[data-live-runs]") || !window.EventSource) return;
    source = new EventSource("/events/runs");
    var first = true;
    source.addEventListener("change", function () {
      if (first) { first = false; return; }  // первое значение: страница и так свежая
      if (window.htmx) htmx.trigger(document.body, "runs-changed");
    });
  }
  function disconnect() {
    if (source) { source.close(); source = null; }
  }
  document.addEventListener("DOMContentLoaded", connect);
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", connect);
})();
