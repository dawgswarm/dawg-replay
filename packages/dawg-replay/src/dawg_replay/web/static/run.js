// dawg-replay, экран запуска: живое обновление по SSE и фильтр журнала.
// Панели рисует сервер (шаблоны Jinja, всё экранировано); здесь они только заменяются.
(function () {
  "use strict";
  var root = document.querySelector(".run[data-run-id]");
  if (!root) return;
  var runId = root.getAttribute("data-run-id");
  var log = document.getElementById("run-log");
  var source = null;
  var done = false;

  function fromHtml(html) {
    var tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    return tpl.content.firstElementChild;
  }

  function replacePanel(name, html) {
    var old = document.getElementById("run-" + name);
    var node = fromHtml(html);
    if (!old || !node) return;
    // Панель починки не перерисовываем без изменений: иначе закроется раскрытый текст для агента.
    if (name === "repair" && old.getAttribute("data-key") === node.getAttribute("data-key")) return;
    // Тот же вопрос скрипта: не трогаем, иначе пропадёт уже набранный ответ.
    if (name === "ask" && old.getAttribute("data-ask-id") && old.getAttribute("data-ask-id") === node.getAttribute("data-ask-id")) return;
    // Уведомление «папка открыта» живёт внутри панели файлов: не теряем его при обновлении.
    var keep = old.querySelector("#files-notice");
    old.replaceWith(node);
    if (keep && node.querySelector("#files-notice")) node.querySelector("#files-notice").replaceWith(keep);
    if (window.htmx) htmx.process(node);
  }

  function nearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  function appendLog(items) {
    if (!log || !items.length) return;
    var stick = nearBottom(log);
    items.forEach(function (html) {
      var node = fromHtml(html);
      if (node) log.appendChild(node);
    });
    // Длинный журнал не должен тормозить страницу: на экране держим последние 2000 строк.
    while (log.children.length > 2000) log.removeChild(log.firstElementChild);
    if (stick) log.scrollTop = log.scrollHeight;
    updateEmpty();
  }

  function updateEmpty() {
    if (!log) return;
    var min = log.getAttribute("data-min");
    var order = ["debug", "info", "warn", "error"];
    var shown = Array.prototype.some.call(log.children, function (li) {
      var m = /lvl-(\w+)/.exec(li.className);
      var lvl = m ? m[1] : "info";
      return lvl === "raw" || order.indexOf(lvl) >= order.indexOf(min);
    });
    root.classList.toggle("log-is-empty", !shown);
  }

  function connect() {
    if (source || done || !root.hasAttribute("data-live") || !window.EventSource) return;
    var after = root.getAttribute("data-after") || "0";
    source = new EventSource("/runs/" + encodeURIComponent(runId) + "/events?after=" + encodeURIComponent(after));
    source.addEventListener("patch", function (e) {
      var data = JSON.parse(e.data);
      Object.keys(data.panels || {}).forEach(function (name) { replacePanel(name, data.panels[name]); });
      appendLog(data.log || []);
      document.dispatchEvent(new CustomEvent("dawg:panels"));
      if (e.lastEventId) root.setAttribute("data-after", e.lastEventId);
    });
    source.addEventListener("resync", function () {
      disconnect();
      window.location.reload();
    });
    source.addEventListener("done", function () {
      done = true;
      disconnect();
    });
  }

  function disconnect() {
    if (source) { source.close(); source = null; }
  }

  var select = document.getElementById("log-level");
  if (select && log) {
    select.addEventListener("change", function () {
      log.setAttribute("data-min", select.value);
      updateEmpty();
    });
  }

  // «Починить с агентом»: копирование текста для агента. Панель перерисовывается, поэтому делегирование.
  function copyStatus(btn, text) {
    var box = btn.parentNode && btn.parentNode.querySelector("[data-copy-status]");
    if (box) box.textContent = text;
  }

  function copyFallback(area) {
    area.focus();
    area.select();
    try { return document.execCommand("copy"); } catch (e) { return false; }
  }

  root.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-copy]") : null;
    if (!btn) return;
    var area = document.getElementById(btn.getAttribute("data-copy"));
    if (!area) return;
    var ok = "Скопировано. Вставьте в Claude Code или Codex.";
    var manual = "Не удалось скопировать: текст выделен, нажмите Cmd+C или Ctrl+C.";
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(area.value).then(function () { copyStatus(btn, ok); }, function () {
        copyStatus(btn, copyFallback(area) ? ok : manual);
      });
    } else {
      copyStatus(btn, copyFallback(area) ? ok : manual);
    }
  });

  function openRepair() {
    if (window.location.hash !== "#repair") return;
    var box = document.getElementById("repair");
    if (!box) return;
    box.open = true;
    box.scrollIntoView({ block: "center" });
  }

  openRepair();
  window.addEventListener("hashchange", openRepair);

  if (log) log.scrollTop = log.scrollHeight;
  updateEmpty();
  connect();
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", connect);
})();
