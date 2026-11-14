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

  // Переключатель темы: светлая или тёмная, выбор хранится в браузере.
  // Подпись кнопки говорит, что произойдёт по нажатию.
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function labelThemeButtons() {
    var text = currentTheme() === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (b) {
      b.setAttribute("title", text);
      b.setAttribute("aria-label", text);
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-theme-toggle]");
    if (!btn) return;
    var next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("dawg-replay-theme", next); } catch (err) { /* без сохранения */ }
    labelThemeButtons();
  });
  document.addEventListener("DOMContentLoaded", labelThemeButtons);

  // Панель запуска на странице скрипта открывается якорем #launch (CSS :target).
  // Здесь только фокус внутрь панели, возврат фокуса и закрытие по Esc.
  var opener = null;
  function sheetOpen() { return window.location.hash === "#launch" && document.getElementById("launch"); }
  function focusSheet() {
    var sheet = sheetOpen();
    if (!sheet) return;
    var first = sheet.querySelector("select, input:not([type=hidden]), textarea, button");
    (first || sheet).focus();
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest('a[href$="#launch"]');
    if (a) opener = a;
  });
  window.addEventListener("hashchange", function () {
    if (sheetOpen()) focusSheet();
    else if (opener) { opener.focus(); opener = null; }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && sheetOpen()) {
      window.location.hash = "";  // :target снимается, hashchange вернёт фокус
    }
  });
  document.addEventListener("DOMContentLoaded", focusSheet);
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", connect);
})();
