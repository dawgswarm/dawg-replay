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

  // Знак в шапке анимирован, пока есть идущие запуски: класс runs-active на body.
  // Сервер ставит его при отрисовке, дальше его обновляет поток /events/runs или,
  // на страницах без этого потока, редкий опрос /events/active.
  function setRunsActive(n) {
    document.body.classList.toggle("runs-active", n > 0);
  }

  // Списки с data-live-runs обновляются, когда меняются запуски (в том числе начатые из CLI).
  var source = null;
  function connect() {
    if (source || !document.querySelector("[data-live-runs]") || !window.EventSource) {
      if (!source) pollActive();
      return;
    }
    source = new EventSource("/events/runs");
    var first = true;
    source.addEventListener("change", function (e) {
      setRunsActive(parseInt(String(e.data).split(":")[0], 10) || 0);
      if (first) { first = false; return; }  // первое значение: страница и так свежая
      if (window.htmx) htmx.trigger(document.body, "runs-changed");
    });
  }
  var POLL_MS = 10000;
  function pollActive() {
    if (!window.fetch) return;
    window.setTimeout(function tick() {
      if (document.hidden) { window.setTimeout(tick, POLL_MS); return; }
      fetch("/events/active", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) setRunsActive(d.running); })
        .catch(function () { /* сервер остановлен: знак просто стоит */ })
        .then(function () { window.setTimeout(tick, POLL_MS); });
    }, POLL_MS);
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

  // Импорт скрипта: файл можно перетащить на поле или выбрать; проверка начинается сразу.
  function dropzone(e) { return e.target.closest && e.target.closest("[data-dropzone]"); }
  function submitZone(zone) { if (window.htmx) htmx.trigger(zone, "submit"); }
  document.addEventListener("dragover", function (e) {
    var zone = dropzone(e);
    if (!zone) return;
    e.preventDefault();
    zone.classList.add("is-over");
  });
  document.addEventListener("dragleave", function (e) {
    var zone = dropzone(e);
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("is-over");
  });
  document.addEventListener("drop", function (e) {
    var zone = dropzone(e);
    if (!zone) return;
    e.preventDefault();
    zone.classList.remove("is-over");
    var input = zone.querySelector("input[type=file]");
    if (!input || !e.dataTransfer || !e.dataTransfer.files.length) return;
    input.files = e.dataTransfer.files;
    submitZone(zone);
  });
  document.addEventListener("change", function (e) {
    var zone = dropzone(e);
    if (zone && e.target.type === "file" && e.target.files.length) submitZone(zone);
  });
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", connect);

  // Регулярный запуск: видны только поля выбранного «Как часто». Без скрипта видны все.
  function syncScheduleForm(form) {
    var kind = form.querySelector("select[name=_kind]");
    if (!kind) return;
    Array.prototype.forEach.call(form.querySelectorAll("[data-when]"), function (el) {
      el.hidden = el.getAttribute("data-when").split(" ").indexOf(kind.value) < 0;
    });
  }
  function syncAllScheduleForms() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-schedule-form]"), syncScheduleForm);
  }
  document.addEventListener("change", function (e) {
    var form = e.target.closest && e.target.closest("[data-schedule-form]");
    if (form && e.target.name === "_kind") syncScheduleForm(form);
  });
  document.addEventListener("DOMContentLoaded", syncAllScheduleForms);
  document.addEventListener("htmx:afterSettle", syncAllScheduleForms);
})();

// Вопросы скриптов посреди запуска (kit.ask): уведомление браузера, если человек разрешил.
// Пока идут запуски, раз в 5 секунд спрашиваем /events/asks; о каждом вопросе сообщаем один раз.
(function () {
  "use strict";
  var supported = "Notification" in window;
  var seen = {};
  var first = true;
  try { seen = JSON.parse(window.sessionStorage.getItem("dawg-asks-seen") || "{}") || {}; } catch (e) { seen = {}; }

  function remember() {
    try { window.sessionStorage.setItem("dawg-asks-seen", JSON.stringify(seen)); } catch (e) { /* не страшно */ }
  }

  function offerButtons() {
    var show = supported && Notification.permission === "default";
    Array.prototype.forEach.call(document.querySelectorAll("[data-notify-offer]"), function (el) { el.hidden = !show; });
  }

  function notify(q) {
    var n = new Notification("Скрипт ждёт ответа: " + q.title, { body: q.question, tag: "dawg-ask-" + q.id });
    n.onclick = function () {
      window.focus();
      window.location.href = "/runs/" + encodeURIComponent(q.run) + "#run-ask";
      n.close();
    };
  }

  function check() {
    if (!window.fetch || !supported || Notification.permission !== "granted") return Promise.resolve();
    if (!document.body.classList.contains("runs-active") && !document.querySelector("[data-ask-id]")) return Promise.resolve();
    return fetch("/events/asks", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var quiet = first && !document.hidden;  // вопрос уже на экране, когда страница открылась
        first = false;
        d.questions.forEach(function (q) {
          if (seen[q.id]) return;
          seen[q.id] = 1;
          if (!quiet && (document.hidden || !document.hasFocus())) notify(q);
        });
        remember();
      })
      .catch(function () { /* сервер остановлен */ });
  }

  function loop() {
    check().then(function () { window.setTimeout(loop, 5000); });
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-notify-enable]") : null;
    if (!btn || !supported) return;
    Notification.requestPermission().then(function () { offerButtons(); check(); });
  });
  document.addEventListener("DOMContentLoaded", function () { offerButtons(); loop(); });
  document.addEventListener("htmx:afterSettle", offerButtons);
  document.addEventListener("dawg:panels", offerButtons);
})();

// Таблица вручную («Ввести вручную» у параметра table). Поля без name: форма запуска их не отправляет,
// «Готово» собирает колонки и ячейки и шлёт на сервер, в ответ поле с таблицей, как после загрузки файла.
(function () {
  "use strict";
  var SECRET = /pass|парол|code|код|token|токен|secret|секрет|pin|ключ|key/i;

  function box(el) { return el.closest ? el.closest("[data-manual]") : null; }

  function columns(m) {
    var fixed = m.querySelectorAll("thead [data-col]");
    if (fixed.length) return Array.prototype.map.call(fixed, function (s) { return s.getAttribute("data-col"); });
    return Array.prototype.map.call(m.querySelectorAll("thead [data-col-input]"), function (i) { return i.value.trim(); });
  }

  function cellInput(name, secret) {
    var td = document.createElement("td");
    var input = document.createElement("input");
    input.type = secret && SECRET.test(name) ? "password" : "text";
    input.setAttribute("data-cell", "");
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("aria-label", name || "значение");
    td.appendChild(input);
    return td;
  }

  function syncTypes(m) {
    // Колонку назвали «пароль» или «code»: у секретной таблицы её ячейки становятся полями пароля.
    if (!m.getAttribute("data-secret")) return;
    var cols = columns(m);
    Array.prototype.forEach.call(m.querySelectorAll("tbody tr"), function (tr) {
      Array.prototype.forEach.call(tr.querySelectorAll("[data-cell]"), function (input, i) {
        input.type = SECRET.test(cols[i] || "") ? "password" : "text";
      });
    });
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    var open = t.closest && t.closest("[data-manual-open]");
    if (open) {
      var m0 = open.parentNode.querySelector("[data-manual]");
      if (!m0) return;
      m0.hidden = !m0.hidden;
      open.setAttribute("aria-expanded", m0.hidden ? "false" : "true");
      if (!m0.hidden) { var first = m0.querySelector("input"); if (first) first.focus(); }
      return;
    }
    var m = box(t);
    if (!m) return;
    var body = m.querySelector("tbody");
    if (t.closest("[data-manual-add-row]")) {
      var last = body.lastElementChild;
      var row = last.cloneNode(true);
      Array.prototype.forEach.call(row.querySelectorAll("[data-cell]"), function (i) { i.value = ""; });
      body.appendChild(row);
      var f = row.querySelector("[data-cell]");
      if (f) f.focus();
    } else if (t.closest("[data-manual-del-row]")) {
      var tr = t.closest("tr");
      if (body.children.length > 1) tr.remove();
      else Array.prototype.forEach.call(tr.querySelectorAll("[data-cell]"), function (i) { i.value = ""; });
    } else if (t.closest("[data-manual-add-col]")) {
      var th = document.createElement("th");
      th.scope = "col";
      var head = document.createElement("input");
      head.type = "text";
      head.setAttribute("data-col-input", "");
      head.placeholder = "колонка";
      head.spellcheck = false;
      head.setAttribute("aria-label", "Имя новой колонки");
      th.appendChild(head);
      var act = m.querySelector("thead .table-manual-act");
      act.parentNode.insertBefore(th, act);
      Array.prototype.forEach.call(body.children, function (r) {
        r.insertBefore(cellInput("", false), r.querySelector(".table-manual-act"));
      });
      head.focus();
    } else if (t.closest("[data-manual-done]")) {
      var cols = columns(m);
      var cells = [];
      Array.prototype.forEach.call(body.children, function (r) {
        Array.prototype.forEach.call(r.querySelectorAll("[data-cell]"), function (i) { cells.push(i.value); });
      });
      if (!window.htmx) return;
      htmx.ajax("POST", m.getAttribute("data-url"), {
        source: t, target: m.closest(".field"), swap: "outerHTML",
        values: { _cols: cols, _cells: cells },
      });
    }
  });

  document.addEventListener("input", function (e) {
    if (e.target.hasAttribute && e.target.hasAttribute("data-col-input")) {
      var m = box(e.target);
      if (m) syncTypes(m);
    }
  });

  // Enter в ячейке не отправляет форму запуска.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !box(e.target) || e.target.tagName !== "INPUT") return;
    e.preventDefault();
    var done = box(e.target).querySelector("[data-manual-done]");
    if (done) done.click();
  });
})();
