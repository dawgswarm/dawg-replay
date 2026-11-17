/* Проекты (M6): холст редактора и экран запуска проекта. Без сборщика и библиотек.
   Политика безопасности страницы запрещает встроенные стили и обработчики в разметке:
   положение узлов задаётся через el.style, события вешаются отсюда. Весь текст проходит через esc(). */
(function () {
  "use strict";

  // Импорт файла на списке проектов: форма уходит сразу после выбора файла.
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t && t.matches && t.matches("input[data-autosubmit]") && t.form) {
      if (t.form.requestSubmit) t.form.requestSubmit(); else t.form.submit();
    }
  });

  var root = document.getElementById("pe");
  if (!root) return;

  var MODE = root.dataset.mode; // edit | run
  var NAME = root.dataset.project;
  var DRAFT = root.dataset.draft === "1";
  var meta = document.querySelector('meta[name="csrf-token"]');
  var CSRF = meta ? meta.getAttribute("content") : "";
  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };
  var canvas = $("#pe-canvas"), world = $("#pe-world"), wires = $("#pe-wires"), insp = $("#pe-insp"), pop = $("#pe-pop");

  var ROWS = { first: "первая строка", each: "каждая строка", all: "вся колонка" };
  var WHEN = [["partial", "Готово или частично"], ["ok", "Только готово"], ["any", "Любой исход"]];
  var OPS = [["equals", "равно"], ["not_equals", "не равно"], ["contains", "содержит"], ["not_contains", "не содержит"],
    ["gt", "больше"], ["lt", "меньше"], ["empty", "пусто"], ["not_empty", "не пусто"]];
  var OP_WORD = {}; OPS.forEach(function (o) { OP_WORD[o[0]] = o[1]; });
  var STEP = { pending: "ждёт", starting: "запускается", running: "идёт", ok: "готово", partial: "частично", failed: "упал",
    not_started: "не стартовал", cancelled: "отменён", skipped: "пропущен", off_branch: "не по этой ветке" };
  var LIMIT_ROWS = 4;

  var S = {
    project: null, scripts: {}, version: null, problems: [], sel: null, tab: "mine", q: "",
    view: { x: 40, y: 30, k: 1 }, expanded: {}, split: {}, run: null, saving: null, dirty: false, lastGood: null,
    error: null
  };

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function plural(n, one, few, many) {
    n = Math.abs(n) % 100; var d = n % 10;
    if (n > 10 && n < 20) return many;
    if (d === 1) return one;
    if (d >= 2 && d <= 4) return few;
    return many;
  }
  function req(method, url, body) {
    var opt = { method: method, headers: { "X-CSRF-Token": CSRF }, credentials: "same-origin" };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, status: r.status, data: d }; });
    });
  }

  // --- модель ---------------------------------------------------------------------------

  function script(n) { return S.scripts[n.script] || null; }
  function node(id) { return S.project.nodes.find(function (n) { return n.id === id; }) || null; }
  function nodeTitle(n) {
    if (!n) return "";
    if (n.title) return n.title;
    var s = script(n), base = s ? s.title : n.script;
    // Узел скрипта один: хватает названия скрипта. Несколько: «скрипт: пресет», чтобы их различать.
    var same = S.project.nodes.filter(function (x) { return x.script === n.script; }).length;
    return same > 1 && n.preset ? base + ": " + n.preset : base;
  }
  function presetOf(n) {
    var s = script(n);
    if (!s || !n.preset) return null;
    return s.presets.find(function (p) { return p.name === n.preset; }) || null;
  }
  function mapsInto(nodeId, param) {
    var out = [];
    S.project.edges.forEach(function (e, ei) {
      if (e.to !== nodeId) return;
      (e.map || []).forEach(function (m, mi) { if (m.param === param) out.push({ e: e, ei: ei, m: m, mi: mi }); });
    });
    return out;
  }
  function valueText(p, v) {
    if (v === true) return "да";
    if (v === false) return "нет";
    if (Array.isArray(v)) return v.map(function (x) { return choiceTitle(p, x); }).join(", ");
    if (v && typeof v === "object") return v.name || "файл";
    return choiceTitle(p, v);
  }
  function choiceTitle(p, v) {
    if (p && p.choices) {
      var c = p.choices.find(function (c) { return c.value === v; });
      if (c) return c.title;
    }
    return v == null ? "" : String(v);
  }

  // Откуда значение входа: стрелка, затем на узле, затем пресет, затем по умолчанию.
  function resolve(n, p) {
    var into = mapsInto(n.id, p.name);
    if (into.length) {
      var x = into[0], src = node(x.e.from), t = nodeTitle(src);
      if (x.m.kind === "file") return { src: "edge", v: "", tag: into.length > 1 ? "от " + into.length + " узлов" : "от «" + t + "»" };
      if (x.m.kind === "value") return { src: "vedge", v: "", tag: "от «" + t + "»" };
      return { src: "vedge", v: "", tag: (x.m.column || "?") + ", " + ROWS[x.m.rows || "first"] };
    }
    if (n.values && Object.prototype.hasOwnProperty.call(n.values, p.name)) return { src: "node", v: valueText(p, n.values[p.name]), tag: "на узле" };
    var pr = presetOf(n);
    if (pr) {
      if (pr.secret.indexOf(p.name) >= 0) return { src: "preset", v: "••••", tag: "пресет" };
      if (Object.prototype.hasOwnProperty.call(pr.values, p.name)) return { src: "preset", v: valueText(p, pr.values[p.name]), tag: "пресет" };
      if (pr.files[p.name]) return { src: "preset", v: pr.files[p.name], tag: "пресет" };
    }
    var sc = script(n);
    if (p.secret && sc && (sc.secrets_set || []).indexOf(p.name) >= 0) return { src: "preset", v: "••••", tag: "сохранён" };
    if (p.default != null) return { src: "def", v: valueText(p, p.default), tag: "по умолч." };
    if (p.required) return { src: "empty", v: "", tag: "не задано" };
    return { src: "none", v: "—", tag: "" };
  }
  function isBad(n, p) { return p.required && resolve(n, p).src === "empty"; }

  function localBlockers() {
    var out = [];
    S.project.nodes.forEach(function (n) {
      var s = script(n);
      if (!s) return;
      s.params.forEach(function (p) {
        if (isBad(n, p)) out.push({ level: "blocker", node: n.id, param: p.name, text: nodeTitle(n) + ": не задан «" + p.title + "»" });
      });
    });
    return out;
  }
  function allProblems() {
    var server = S.problems.filter(function (p) { return p.level !== "blocker"; });
    var blockers = localBlockers();
    // Пустые обязательные входы считает и сервер (dr-120): свой список с узлом и входом важнее.
    S.problems.forEach(function (p) {
      if (p.level === "blocker" && !blockers.some(function (b) { return b.node === p.node && b.param === p.param; })) blockers.push(p);
    });
    return server.filter(function (p) { return p.level === "error"; }).concat(blockers,
      server.filter(function (p) { return p.level === "warning"; }));
  }

  // --- отрисовка верхней строки ------------------------------------------------------------

  function renderTop() {
    if (MODE !== "edit") return;
    var c = $("#pe-check"), run = $("#pe-run"), list = allProblems();
    var errors = list.filter(function (p) { return p.level === "error"; });
    var blockers = list.filter(function (p) { return p.level === "blocker"; });
    var warns = list.filter(function (p) { return p.level === "warning"; });
    var first = errors[0] || blockers[0] || warns[0];
    if (S.error) {
      c.className = "pe-check bad"; c.textContent = "● " + S.error;
    } else if (first) {
      c.className = "pe-check " + (first.level === "warning" ? "warn" : "bad");
      var more = list.length - 1;
      c.textContent = "● " + first.text + (more > 0 ? " и ещё " + more : "");
    } else if (!S.project.nodes.length) {
      c.className = "pe-check warn"; c.textContent = DRAFT ? "● Новый проект: перетащите первый скрипт на холст" : "● Пустой проект: добавьте узлы";
    } else {
      c.className = "pe-check"; c.textContent = "● Проверено, ошибок нет";
    }
    var block = errors.length || blockers.length || !S.project.nodes.length;
    if (run) {
      run.disabled = !!block;
      run.title = errors.length ? "Сначала исправьте ошибки проверки" : blockers.length ? "Сначала заполните обязательные входы" : "";
    }
  }
  function setSaved(text, err) {
    var el = $("#pe-saved");
    if (!el) return;
    el.textContent = text; el.className = "pe-saved" + (err ? " err" : "");
  }

  // --- палитра ---------------------------------------------------------------------------

  function renderPalette() {
    var el = $("#pe-plist");
    if (!el) return;
    var q = S.q.toLowerCase();
    var list = Object.keys(S.scripts).map(function (k) { return S.scripts[k]; }).filter(function (s) {
      if (!s) return false;
      if ((S.tab === "builtin") !== !!s.builtin) return false;
      return !q || (s.title + " " + (s.site || "") + " " + s.name + " " + (s.description || "")).toLowerCase().indexOf(q) >= 0;
    });
    list.sort(function (a, b) { return (a.site || "").localeCompare(b.site || "") || a.title.localeCompare(b.title); });
    if (!list.length) {
      el.innerHTML = '<p class="pe-empty-list">' + (S.q ? "Ничего не нашлось. " : (S.tab === "mine" ? "Своих скриптов пока нет. " : "")) +
        "Новый узел может написать агент: опишите ему, что сделать с данными.</p>";
      return;
    }
    var html = "", group = null;
    list.forEach(function (s) {
      var g = S.tab === "mine" ? (s.site || "Без сайта") : null;
      if (g !== null && g !== group) { html += '<p class="pe-group">' + esc(g) + "</p>"; group = g; }
      var ins = s.params.length, outs = s.outputs.length;
      html += '<div class="pe-pitem" draggable="true" data-script="' + esc(s.name) + '">' +
        '<span class="pe-av hue-' + esc(s.cover.hue) + '" aria-hidden="true">' + esc(s.cover.letter) + "</span>" +
        "<span><b>" + esc(s.title) + '</b><span class="sub">' + ins + " " + plural(ins, "вход", "входа", "входов") + ", " +
        outs + " " + plural(outs, "выход", "выхода", "выходов") + "</span></span>" +
        '<button type="button" class="pe-plus" data-add="' + esc(s.name) + '" title="Добавить на холст" aria-label="Добавить «' + esc(s.title) + '» на холст">+</button></div>';
    });
    el.innerHTML = html;
    $$(".pe-pitem", el).forEach(function (it) {
      it.addEventListener("dragstart", function (e) { e.dataTransfer.setData("text/plain", it.dataset.script); });
    });
    $$("[data-add]", el).forEach(function (b) {
      b.addEventListener("click", function () {
        var r = canvas.getBoundingClientRect();
        addNode(b.dataset.add, (r.width / 2 - S.view.x) / S.view.k - 120, (r.height / 3 - S.view.y) / S.view.k);
        var pal = $("#pe-palette"); if (pal) pal.classList.remove("open");
      });
    });
  }

  // --- узлы ------------------------------------------------------------------------------

  function rank(n, p) {
    var r = resolve(n, p);
    return r.src === "edge" || r.src === "vedge" ? 0 : isBad(n, p) ? 1 : 2;
  }
  function stepOf(n) { return S.run && S.run.steps_by_node ? S.run.steps_by_node[n.id] || null : null; }

  function rowHtml(n, p) {
    var r = resolve(n, p), linked = r.src === "edge" || r.src === "vedge";
    var editable = MODE === "edit" && p.simple && !linked;
    var kind = p.transfer ? "file" : "val";
    var bad = isBad(n, p);
    var shown = r.v;
    var tag = r.tag ? '<span class="pe-src s-' + r.src + '">' + esc(r.tag) + "</span>" : "";
    var val = shown ? '<span class="val"' + (editable ? ' data-edit="' + esc(p.name) + '" tabindex="0" role="button" title="Изменить на узле"' : "") + ">" + esc(shown) + "</span>" :
      (editable ? '<span class="val" data-edit="' + esc(p.name) + '" tabindex="0" role="button" title="Задать на узле">…</span>' : "");
    return '<div class="pe-prow' + (editable ? " editable" : "") + (bad ? " bad" : "") + '" data-in="' + esc(p.name) + '" data-kind="' + kind + '">' +
      '<i class="pin ' + kind + (linked ? " on" : "") + '"></i><span class="pn" title="' + esc(p.title + " (" + p.type_word + ")") + '">' + esc(p.title) + "</span>" +
      '<span class="pv">' + val + tag + "</span></div>";
  }

  function outsHtml(n, s) {
    return s.outputs.map(function (o) {
      var key = n.id + "|" + o.name, open = !!S.split[key];
      var cols = o.kind === "file" && o.columns && o.columns.length ? o.columns : null;
      var btn = cols && MODE === "edit" ? '<button type="button" class="pe-cx" data-cx="' + esc(key) + '" aria-expanded="' + open + '" title="' +
        (open ? "Свернуть колонки" : "Показать каждую колонку отдельным выходом") + '">' +
        (open ? "свернуть" : cols.length + " " + plural(cols.length, "колонка", "колонки", "колонок")) + "</button>" : "";
      var what = o.kind === "value" ? (o.secret ? "секрет" : o.type_word || "значение") : (cols ? "таблица" : "файл");
      if (o.optional) what += ", не всегда";
      var subs = cols && open ? cols.map(function (c) {
        return '<div class="pe-orow sub" data-out="' + esc(o.name) + '" data-col="' + esc(c) + '"><span class="on">' + esc(c) + "</span><small>колонка</small>" +
          '<i class="pout val" tabindex="0" title="Тяните к входу"></i></div>';
      }).join("") : "";
      return '<div class="pe-orow" data-out="' + esc(o.name) + '">' + btn + '<span class="on" title="' + esc(o.title || o.name) + '">' + esc(o.name) +
        "</span><small>" + esc(what) + '</small><i class="pout ' + (o.kind === "value" ? "val" : "file") + '" tabindex="0" title="Тяните к входу другого узла"></i></div>' + subs;
    }).join("");
  }

  function footHtml(n) {
    if (MODE === "run") {
      var st = stepOf(n);
      if (!st) return '<div class="pe-node-foot"><span class="stchip">нет шага</span></div>';
      var rows = "";
      if (st.rows && st.rows.length) {
        rows = '<div class="rowruns">' + st.rows.map(function (r) {
          var mark = r.status === "ok" || r.reused ? " ✓" : r.status === "failed" ? " ✕" : r.status === "running" ? " …" : "";
          var cls = r.reused ? "reused" : r.status;
          var inner = esc(r.label || "#" + (r.index + 1)) + mark;
          return r.run_id ? '<a class="rr ' + cls + '" href="/runs/' + esc(r.run_id) + '" title="' + esc(r.status_title || STEP[r.status] || "") + '">' + inner + "</a>"
            : '<span class="rr ' + cls + '" title="' + esc(r.status_title || STEP[r.status] || "") + '">' + inner + "</span>";
        }).join("") + "</div>";
      }
      var label = st.rows_summary && st.rows_summary.text ? (STEP[st.status] || st.status) + ", " + st.rows_summary.text : (st.status_title || STEP[st.status] || st.status);
      if (st.reused) label += ", из прошлого запуска";
      return rows + '<div class="pe-node-foot"><span class="stchip ' + esc(st.status) + '">' + esc(label) + "</span>" +
        (st.reason ? '<span class="reason" title="' + esc(st.reason) + '">' + esc(st.reason) + "</span>" : "") +
        (st.run_id ? '<a href="/runs/' + esc(st.run_id) + '">журнал</a>' : "") + "</div>";
    }
    var loop = S.project.edges.find(function (e) { return e.to === n.id && (e.map || []).some(function (m) { return m.rows === "each"; }); });
    var last = S.lastRun && S.lastRun.steps_by_node ? S.lastRun.steps_by_node[n.id] : null;
    return '<div class="pe-node-foot">' + (last ? '<span class="stchip ' + esc(last.status) + '">' + esc(last.status_title || STEP[last.status]) + " в прошлый раз</span>" : "") +
      (loop ? '<span class="pe-loop" title="по разу на каждую строку, ' + (loop.parallel || 1) + ' одновременно">↻ по строкам</span>' : "") + "</div>";
  }

  function renderNodes() {
    $$(".pe-node, .pe-edge-chip, .pe-blank", world).forEach(function (x) { x.remove(); });
    var errNodes = {};
    S.problems.forEach(function (p) { if (p.level === "error" && p.node) errNodes[p.node] = true; });
    S.project.nodes.forEach(function (n) {
      var s = script(n);
      var div = document.createElement("div");
      var st = MODE === "run" ? stepOf(n) : null;
      div.className = "pe-node" + (S.sel && S.sel.kind === "node" && S.sel.id === n.id ? " sel" : "") + (errNodes[n.id] ? " has-error" : "") +
        (st ? " st-" + st.status : "");
      div.dataset.id = n.id;
      div.style.left = (n.x || 0) + "px"; div.style.top = (n.y || 0) + "px";
      var cov = s ? s.cover : { hue: "teal", letter: "?" };
      var head = '<div class="pe-node-head"><span class="pe-av hue-' + esc(cov.hue) + '" aria-hidden="true">' + esc(cov.letter) + "</span>" +
        "<div><b title=\"" + esc(nodeTitle(n)) + '">' + esc(nodeTitle(n)) + "</b>" +
        (n.preset ? '<span class="pe-preset">пресет «' + esc(n.preset) + "»</span>" : '<span class="pe-preset none">без пресета</span>') + "</div></div>";
      if (!s) {
        div.innerHTML = head + '<div class="pe-params"><div class="pe-prow bad"><span class="pn">скрипт «' + esc(n.script) + '» не найден</span></div></div>' + footHtml(n);
        world.appendChild(div);
        return;
      }
      var order = s.params.slice().sort(function (a, b) { return rank(n, a) - rank(n, b); });
      var open = !!S.expanded[n.id], visible = open ? order : order.slice(0, LIMIT_ROWS), rest = order.length - LIMIT_ROWS;
      var params = order.length ? '<div class="pe-params">' + visible.map(function (p) { return rowHtml(n, p); }).join("") +
        (rest > 0 ? '<button type="button" class="pe-more" data-more="' + esc(n.id) + '">' + (open ? "свернуть" : "ещё " + rest + " " + plural(rest, "вход", "входа", "входов")) + "</button>" : "") +
        "</div>" : "";
      var outs = s.outputs.length ? '<div class="pe-outs">' + outsHtml(n, s) + "</div>" : "";
      div.innerHTML = head + params + outs + footHtml(n);
      world.appendChild(div);
    });
    if (!S.project.nodes.length) {
      var b = document.createElement("div");
      b.className = "pe-blank";
      b.innerHTML = MODE === "edit" ? "<b>Пустой холст</b>Перетащите сюда скрипт из списка слева или нажмите «+» рядом с ним." : "<b>В проекте нет узлов</b>";
      world.appendChild(b);
      var r = canvas.getBoundingClientRect();
      b.style.left = ((r.width / 2 - S.view.x) / S.view.k) + "px"; b.style.top = ((r.height / 2.2 - S.view.y) / S.view.k) + "px";
    }
    $$("[data-more]", world).forEach(function (btn) {
      btn.addEventListener("click", function (ev) { ev.stopPropagation(); var id = btn.dataset.more; S.expanded[id] = !S.expanded[id]; renderNodes(); });
    });
    $$("[data-cx]", world).forEach(function (btn) {
      btn.addEventListener("click", function (ev) { ev.stopPropagation(); var k = btn.dataset.cx; S.split[k] = !S.split[k]; renderNodes(); });
    });
    $$(".val[data-edit]", world).forEach(function (v) {
      v.addEventListener("click", function (ev) { ev.stopPropagation(); startEdit(v); });
      v.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); startEdit(v); } });
    });
    renderWires();
    renderTop();
  }

  // --- провода ---------------------------------------------------------------------------

  function centerOf(el) {
    var r = el.getBoundingClientRect(), w = world.getBoundingClientRect();
    return { x: (r.left + r.width / 2 - w.left) / S.view.k, y: (r.top + r.height / 2 - w.top) / S.view.k };
  }
  function nodeEl(id) { return world.querySelector('.pe-node[data-id="' + CSS.escape(id) + '"]'); }
  function outPos(id, name, col) {
    var el = nodeEl(id);
    if (!el) return null;
    var dot = null;
    if (col) dot = el.querySelector('.pe-orow.sub[data-out="' + CSS.escape(name) + '"][data-col="' + CSS.escape(col) + '"] .pout');
    if (!dot) dot = el.querySelector('.pe-orow:not(.sub)[data-out="' + CSS.escape(name) + '"] .pout');
    if (!dot && name === "@main") dot = el.querySelector(".pe-orow:not(.sub) .pout");
    if (!dot) { var r = centerOf(el); return { x: r.x + el.offsetWidth / 2, y: r.y }; }
    return centerOf(dot);
  }
  function inPos(id, param) {
    var el = nodeEl(id);
    if (!el) return null;
    var pin = el.querySelector('.pe-prow[data-in="' + CSS.escape(param) + '"] .pin');
    if (pin) return centerOf(pin);
    var more = el.querySelector(".pe-more") || el.querySelector(".pe-node-head");
    var c = centerOf(more);
    return { x: c.x - more.offsetWidth / 2, y: c.y };
  }
  function curve(a, b) {
    var dx = Math.max(60, Math.abs(b.x - a.x) / 2);
    return "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " " + (b.x - dx) + "," + b.y + " " + b.x + "," + b.y;
  }
  function mapText(m) {
    if (m.kind === "file") return (m.file === "@main" ? "главный файл" : m.file) + " → " + paramTitle(m);
    if (m.kind === "value") return m.value + " → " + paramTitle(m);
    return m.column + " → " + paramTitle(m) + ", " + ROWS[m.rows || "first"];
  }
  function paramTitle(m) {
    if (!m.__to) return m.param;
    var s = script(m.__to), p = s && s.params.find(function (x) { return x.name === m.param; });
    return p ? p.title : m.param;
  }
  // Точка на кривой провода, где подпись не ложится на узлы: от середины к краям.
  function bezier(a, b, t) {
    var dx = Math.max(60, Math.abs(b.x - a.x) / 2), p1 = { x: a.x + dx, y: a.y }, p2 = { x: b.x - dx, y: b.y }, u = 1 - t;
    return { x: u * u * u * a.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * b.x,
      y: u * u * u * a.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * b.y };
  }
  function chipSpot(a, b, w, h) {
    var boxes = S.project.nodes.map(function (n) {
      var el = nodeEl(n.id);
      return el ? { x: n.x || 0, y: n.y || 0, w: el.offsetWidth, h: el.offsetHeight } : null;
    }).filter(Boolean);
    function clear(p) {
      return boxes.every(function (bx) {
        return p.x + w / 2 + 4 <= bx.x || p.x - w / 2 - 4 >= bx.x + bx.w || p.y + h / 2 + 4 <= bx.y || p.y - h / 2 - 4 >= bx.y + bx.h;
      });
    }
    var ts = [0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82, 0.1, 0.9];
    for (var i = 0; i < ts.length; i += 1) {
      var p = bezier(a, b, ts[i]);
      if (clear(p)) return p;
    }
    // Свободного места на проводе нет (узлы рядом): подпись над верхним краем узлов, которые она бы закрыла.
    var mid = bezier(a, b, 0.5);
    var over = boxes.filter(function (bx) {
      return !(mid.x + w / 2 + 4 <= bx.x || mid.x - w / 2 - 4 >= bx.x + bx.w || mid.y + h / 2 + 4 <= bx.y || mid.y - h / 2 - 4 >= bx.y + bx.h);
    });
    if (!over.length) return mid;
    return { x: mid.x, y: Math.min.apply(null, over.map(function (bx) { return bx.y; })) - h / 2 - 6 };
  }
  function edgeActive(e) {
    if (!S.run) return true;
    var st = S.run.steps_by_node[e.from];
    return !st || ["ok", "partial", "running", "starting", "pending"].indexOf(st.status) >= 0;
  }

  function renderWires() {
    var html = "";
    $$(".pe-edge-chip", world).forEach(function (x) { x.remove(); });
    S.project.edges.forEach(function (e, ei) {
      var maps = e.map && e.map.length ? e.map : [{ kind: "file", file: "@main", param: "", rows: "first" }];
      maps.forEach(function (m, mi) {
        m.__to = node(e.to);
        var a = outPos(e.from, m.kind === "value" ? m.value : m.file, m.kind === "column" ? m.column : null);
        var b = m.param ? inPos(e.to, m.param) : inPos(e.to, "");
        if (!a || !b) return;
        var on = S.sel && S.sel.kind === "edge" && S.sel.ei === ei && (S.sel.mi === mi || S.sel.mi == null);
        var k = m.kind === "file" ? "file" : "val", d = curve(a, b);
        var cls = k + (on ? " sel" : "") + (edgeActive(e) ? "" : " inactive");
        html += '<path class="pe-wire-hit" data-ei="' + ei + '" data-mi="' + mi + '" d="' + d + '"/>' +
          '<path class="pe-wire ' + cls + '" d="' + d + '"/><circle class="pe-wire-dot ' + k + (on ? " sel" : "") + '" cx="' + b.x + '" cy="' + b.y + '" r="4.5"/>';
        if (on) {
          var chip = document.createElement("button");
          chip.type = "button"; chip.className = "pe-edge-chip"; chip.textContent = mapText(m) + (e.if_file ? ", если есть " + e.if_file : "");
          world.appendChild(chip);
          var spot = chipSpot(a, b, chip.offsetWidth, chip.offsetHeight);
          chip.style.left = spot.x + "px"; chip.style.top = spot.y + "px";
        }
      });
    });
    wires.innerHTML = html + (S.temp ? '<path class="pe-wire temp" d="' + S.temp + '"/>' : "");
    $$("[data-ei]", wires).forEach(function (p) {
      p.addEventListener("click", function (ev) { ev.stopPropagation(); select({ kind: "edge", ei: +p.dataset.ei, mi: +p.dataset.mi }); });
    });
  }

  function applyView() {
    world.style.transform = "translate(" + S.view.x + "px," + S.view.y + "px) scale(" + S.view.k + ")";
  }
  function fit() {
    var nodes = S.project.nodes;
    var r = canvas.getBoundingClientRect();
    if (!nodes.length) { S.view = { x: 40, y: 30, k: 1 }; applyView(); return; }
    var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    nodes.forEach(function (n) {
      var el = nodeEl(n.id), w = el ? el.offsetWidth : 244, h = el ? el.offsetHeight : 200;
      minx = Math.min(minx, n.x || 0); miny = Math.min(miny, n.y || 0);
      maxx = Math.max(maxx, (n.x || 0) + w); maxy = Math.max(maxy, (n.y || 0) + h);
    });
    var pad = 40, k = Math.min(1, (r.width - pad * 2) / Math.max(1, maxx - minx), (r.height - pad * 2) / Math.max(1, maxy - miny));
    k = Math.max(r.width < 700 ? 0.6 : 0.35, k);
    S.view = { k: k, x: pad - minx * k + Math.max(0, (r.width - pad * 2 - (maxx - minx) * k) / 2), y: pad - miny * k };
    applyView();
  }
  function zoom(dir, cx, cy) {
    var r = canvas.getBoundingClientRect();
    if (cx == null) { cx = r.width / 2; cy = r.height / 2; }
    var k = Math.min(1.6, Math.max(0.35, S.view.k * (dir > 0 ? 1.15 : 1 / 1.15)));
    S.view.x = cx - (cx - S.view.x) * k / S.view.k; S.view.y = cy - (cy - S.view.y) * k / S.view.k; S.view.k = k;
    applyView();
  }

  // --- выбор и панель справа ----------------------------------------------------------------

  function select(sel) { S.sel = sel; closePop(); renderNodes(); renderInsp(); }

  function choiceList(items, current, attr) {
    return '<div class="pe-choices" role="radiogroup">' + items.map(function (it) {
      return '<button type="button" class="pe-choice" role="radio" aria-checked="' + (it[0] === current) + '" data-' + attr + '="' + esc(it[0]) + '">' +
        '<span class="radio"></span><span><b>' + esc(it[1]) + "</b>" + (it[2] ? '<span class="sub">' + esc(it[2]) + "</span>" : "") + "</span></button>";
    }).join("") + "</div>";
  }
  function seg(items, current, attr) {
    return '<div class="pe-segrow" role="group">' + items.map(function (it) {
      return '<button type="button" data-' + attr + '="' + esc(it[0]) + '" aria-pressed="' + (it[0] === current) + '">' + esc(it[1]) + "</button>";
    }).join("") + "</div>";
  }
  function stepper(value, attr, label) {
    return '<div class="pe-stepper" role="group" aria-label="' + esc(label) + '"><button type="button" data-' + attr + '="-1" aria-label="Меньше">−</button><span>' +
      esc(value) + '</span><button type="button" data-' + attr + '="1" aria-label="Больше">+</button></div>';
  }
  function on(sel, ev, fn) { $$(sel, insp).forEach(function (b) { b.addEventListener(ev, function (e) { fn(b, e); }); }); }

  function renderInsp() {
    if (!insp) return;
    if (MODE === "run") return renderRunInsp();
    var sel = S.sel;
    if (sel && sel.kind === "node" && node(sel.id)) return renderNodeInsp(node(sel.id));
    if (sel && sel.kind === "edge" && S.project.edges[sel.ei]) return renderEdgeInsp(S.project.edges[sel.ei], sel.ei);
    return renderProjectInsp();
  }

  function problemsHtml() {
    var list = allProblems();
    if (!list.length) return '<p class="pe-help">Проверка не нашла ошибок.</p>';
    return '<ul class="pe-problems">' + list.map(function (p, i) {
      return '<li class="' + esc(p.level) + '"' + (p.node ? ' data-goto="' + esc(p.node) + '" role="button" tabindex="0"' : "") + ">" + esc(p.text) + "</li>";
    }).join("") + "</ul>";
  }

  function renderProjectInsp() {
    var p = S.project;
    insp.innerHTML = '<div class="pe-insp-head"><small>Проект</small><h2>' + esc(p.title) + "</h2></div>" +
      '<div class="pe-sec"><h3>Описание</h3><textarea class="pe-box" id="pe-desc" rows="3" placeholder="Что делает проект, для кого">' + esc(p.description || "") + "</textarea></div>" +
      '<div class="pe-sec"><h3>Предел времени</h3><p class="pe-help">По истечении идущие шаги отменяются, проект «упал (таймаут)».</p>' +
      '<div class="pe-line2"><span class="l">Минут на весь запуск</span>' + stepper(p.timeout_minutes || "нет", "tmo", "Минут на запуск") + "</div></div>" +
      '<div class="pe-sec"><h3>Проверка</h3>' + problemsHtml() + "</div>" +
      '<div class="pe-sec"><p class="pe-help">Выберите узел или стрелку, чтобы настроить. Файл проекта: <a href="/projects/' + encodeURIComponent(NAME) + '/export">скачать</a>.</p></div>';
    var d = $("#pe-desc");
    d.addEventListener("change", function () { p.description = d.value.trim() || null; changed(); });
    on("[data-tmo]", "click", function (b) {
      var steps = [null, 30, 60, 120, 240, 480, 720, 1440], i = steps.indexOf(p.timeout_minutes || null);
      if (i < 0) i = 0;
      i = Math.max(0, Math.min(steps.length - 1, i + (+b.dataset.tmo)));
      p.timeout_minutes = steps[i]; changed(); renderInsp();
    });
    bindGoto();
  }
  function bindGoto() {
    on("[data-goto]", "click", function (b) { select({ kind: "node", id: b.dataset.goto }); });
  }

  function fieldHtml(n, p) {
    var r = resolve(n, p), linked = r.src === "edge" || r.src === "vedge";
    var box;
    if (linked) box = '<div class="pe-box ro">' + esc(r.tag) + "</div>";
    else if (p.secret) box = '<div class="pe-box ro">' + (r.src === "preset" ? "•••• из пресета" : "задаётся в пресете или стрелкой") + "</div>";
    else if (p.transfer) box = '<div class="pe-box ro">' + (r.v && r.v !== "—" ? esc(r.v) + " (" + esc(r.tag) + ")" : "протяните стрелку или задайте в пресете") + "</div>";
    else if (p.type === "date" || p.type === "time" || p.type === "datetime") {
      var t = p.type === "datetime" ? "datetime-local" : p.type, own = n.values && n.values[p.name];
      box = '<input class="pe-box" type="' + t + '" data-fin="' + esc(p.name) + '" value="' + esc(own && /^\d/.test(own) ? own : "") + '">' +
        (r.src !== "node" && r.v && r.v !== "—" ? '<p class="pe-help">Сейчас: ' + esc(r.v) + " (" + esc(r.tag) + "). Относительные даты задаются в пресете.</p>" : "");
    } else if (p.list && p.type !== "multi_enum") {
      var cur = n.values && Array.isArray(n.values[p.name]) ? n.values[p.name].join("\n") : "";
      box = '<textarea class="pe-box" rows="3" data-flist="' + esc(p.name) + '" placeholder="' + esc(r.v && r.v !== "—" ? r.v : "по одному на строку") + '">' + esc(cur) + "</textarea>";
    } else {
      box = '<button type="button" class="pe-box' + (r.v && r.v !== "—" ? "" : " ph") + '" data-fedit="' + esc(p.name) + '">' + esc(r.v && r.v !== "—" ? r.v : "задать") + "</button>";
    }
    var own2 = n.values && Object.prototype.hasOwnProperty.call(n.values, p.name);
    return '<div class="pe-pfield' + (isBad(n, p) ? " bad" : "") + '"><div class="lbl"><span>' + esc(p.title) + (p.required ? ", обязательно" : "") +
      '</span><span class="kind">' + esc(p.type_word) + (r.tag && !linked ? " · " + esc(r.tag) : "") + "</span></div>" + box +
      (own2 && !linked ? '<button type="button" class="clear" data-fclear="' + esc(p.name) + '">убрать значение узла</button>' : "") + "</div>";
  }

  function renderNodeInsp(n) {
    var s = script(n);
    if (!s) {
      insp.innerHTML = '<div class="pe-insp-head"><small>Узел</small><h2>' + esc(nodeTitle(n)) + '</h2></div><div class="pe-sec"><p class="pe-help">Скрипт «' +
        esc(n.script) + '» не зарегистрирован. Добавьте его в библиотеку или уберите узел.</p><button type="button" class="pe-danger" data-rmnode>Убрать узел из проекта</button></div>';
      on("[data-rmnode]", "click", function () { removeNode(n.id); });
      return;
    }
    var presets = [["", "Без пресета", "значения по умолчанию из скрипта"]].concat(s.presets.map(function (p) {
      var sum = Object.keys(p.values).slice(0, 2).map(function (k) {
        var par = s.params.find(function (x) { return x.name === k; });
        return (par ? par.title : k) + ": " + valueText(par, p.values[k]);
      }).join(", ");
      return [p.name, p.name, sum];
    }));
    var incoming = S.project.edges.filter(function (e) { return e.to === n.id; }).length;
    insp.innerHTML = '<div class="pe-insp-head"><small>Узел, скрипт «' + esc(s.title) + '»</small><h2 contenteditable="true" spellcheck="false" id="pe-ntitle" title="Переименовать узел">' +
      esc(nodeTitle(n)) + "</h2></div>" +
      '<div class="pe-sec"><h3>Входы</h3><p class="pe-help">Значение берётся по порядку: стрелка, на узле, пресет, по умолчанию. Правка здесь меняет только этот узел.</p>' +
      (s.params.length ? s.params.map(function (p) { return fieldHtml(n, p); }).join("") : '<p class="pe-help">У скрипта нет входов.</p>') + "</div>" +
      '<div class="pe-sec"><h3>Пресет</h3>' + choiceList(presets, n.preset || "", "preset") +
      '<p class="pe-help">Пресеты создаются на <a href="/scripts/' + encodeURIComponent(s.name) + '#launch">странице скрипта</a>.</p></div>' +
      (incoming > 1 ? '<div class="pe-sec"><h3>Когда запускать</h3><p class="pe-help">У узла ' + incoming + " " + plural(incoming, "входящая стрелка", "входящие стрелки", "входящих стрелок") + ".</p>" +
        seg([["all", "Когда готовы все"], ["any", "Хватит любого"]], n.wait || "all", "wait") + "</div>" : "") +
      '<div class="pe-sec"><button type="button" class="pe-danger" data-rmnode>Убрать узел из проекта</button></div>';
    var h = $("#pe-ntitle");
    h.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); h.blur(); } });
    h.addEventListener("blur", function () {
      var t = h.textContent.trim();
      var def = (function () { var c = clone(n); c.title = null; return nodeTitle(c); })();
      var next = t && t !== def ? t.slice(0, 120) : null;
      if (next !== (n.title || null)) { n.title = next; changed(); renderNodes(); }
    });
    on("[data-preset]", "click", function (b) { n.preset = b.dataset.preset || null; changed(); renderNodes(); renderInsp(); });
    on("[data-wait]", "click", function (b) { n.wait = b.dataset.wait; changed(); renderInsp(); });
    on("[data-rmnode]", "click", function () { removeNode(n.id); });
    on("[data-fclear]", "click", function (b) { delete n.values[b.dataset.fclear]; changed(); renderNodes(); renderInsp(); });
    on("[data-fedit]", "click", function (b) { editField(n, s.params.find(function (x) { return x.name === b.dataset.fedit; }), b, true); });
    on("[data-fin]", "change", function (b) { setValue(n, b.dataset.fin, b.value || null); });
    on("[data-flist]", "change", function (b) {
      var items = b.value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
      setValue(n, b.dataset.flist, items.length ? items : null);
    });
  }

  function setValue(n, name, v) {
    n.values = n.values || {};
    if (v === null || v === undefined || v === "") delete n.values[name]; else n.values[name] = v;
    changed(); renderNodes(); renderInsp();
  }
  function parseTyped(p, raw) {
    raw = raw.trim();
    if (!raw) return null;
    if (p.type === "int") { var i = parseInt(raw.replace(/\s/g, ""), 10); return isNaN(i) ? raw : i; }
    if (p.type === "float") { var f = parseFloat(raw.replace(/\s/g, "").replace(",", ".")); return isNaN(f) ? raw : f; }
    return raw;
  }

  // Правка значения: выбор и да/нет поповером, текст и число полем ввода.
  function editField(n, p, anchor, inPanel) {
    if (!p) return;
    if (p.type === "bool" || p.type === "enum" || p.type === "multi_enum") {
      var cur = n.values ? n.values[p.name] : undefined;
      var items = p.type === "bool" ? [[true, "да"], [false, "нет"]] : p.choices.map(function (c) { return [c.value, c.title]; });
      if (p.type === "multi_enum") {
        var set = Array.isArray(cur) ? cur.slice() : [];
        openPop(anchor, items.map(function (it) {
          return { text: (set.indexOf(it[0]) >= 0 ? "✓ " : "") + it[1], keep: true, pick: function () {
            var i = set.indexOf(it[0]); if (i >= 0) set.splice(i, 1); else set.push(it[0]);
            setValue(n, p.name, set.length ? set.slice() : null); editField(n, p, inPanel ? $('[data-fedit="' + CSS.escape(p.name) + '"]', insp) || anchor : anchor, inPanel);
          } };
        }));
        return;
      }
      openPop(anchor, items.map(function (it) {
        return { text: it[1], cur: cur === it[0], pick: function () { setValue(n, p.name, it[0]); } };
      }).concat(n.values && Object.prototype.hasOwnProperty.call(n.values, p.name) ? [{ text: "Как в пресете", sub: "убрать значение узла", pick: function () { setValue(n, p.name, null); } }] : []));
      return;
    }
    var shown = n.values && n.values[p.name] != null ? String(n.values[p.name]) : "";
    var input = document.createElement("input");
    input.type = "text"; input.value = shown;
    if (p.type === "int" || p.type === "float") input.inputMode = "decimal";
    input.className = inPanel ? "pe-box" : "";
    input.setAttribute("aria-label", p.title);
    anchor.replaceWith(input);
    input.focus(); input.select();
    var done = false;
    function finish(save) {
      if (done) return; done = true;
      if (save) setValue(n, p.name, parseTyped(p, input.value)); else { renderNodes(); renderInsp(); }
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
  }
  function startEdit(v) {
    var el = v.closest(".pe-node"), n = node(el.dataset.id), s = script(n);
    editField(n, s.params.find(function (x) { return x.name === v.dataset.edit; }), v, false);
  }

  // --- стрелка: панель ----------------------------------------------------------------------

  function outOf(n, name) {
    var s = script(n);
    if (!s) return null;
    return s.outputs.find(function (o) { return o.name === name; }) || null;
  }
  function paramOf(n, name) {
    var s = script(n);
    return s ? s.params.find(function (p) { return p.name === name; }) || null : null;
  }

  function mapCard(e, ei, m, mi) {
    var A = node(e.from), B = node(e.to), o = outOf(A, m.kind === "value" ? m.value : m.file), p = paramOf(B, m.param);
    var outName = m.kind === "value" ? m.value : (m.file === "@main" ? "главный файл" : m.file);
    var html = '<div class="pe-mapcard"><div class="pe-flow"><button type="button" class="pe-pill" data-pick-out="' + mi + '"><small>' + esc(nodeTitle(A)) + "</small>" +
      esc(outName) + (m.kind === "column" ? ", колонка" : "") + '</button><span class="pe-arrow" aria-hidden="true">→</span><button type="button" class="pe-pill" data-pick-in="' + mi + '"><small>' +
      esc(nodeTitle(B)) + "</small>" + esc(p ? p.title : m.param || "выберите вход") + "</button></div>";
    if (m.kind === "file" && o && o.columns && p && p.columns) {
      html += '<div class="pe-cols">' + p.columns.map(function (c) {
        var ok = o.columns.indexOf(c) >= 0;
        return '<span class="pe-col' + (ok ? "" : " miss") + '">' + (ok ? "✓ " : "✕ ") + esc(c) + "</span>";
      }).join("") + o.columns.filter(function (c) { return p.columns.indexOf(c) < 0; }).map(function (c) { return '<span class="pe-col extra">' + esc(c) + "</span>"; }).join("") + "</div>";
    }
    if (m.kind === "file" && p && p.type === "table") html += '<p class="pe-help">Вся таблица уходит во вход «' + esc(p.title) + "», скрипт проходит по строкам сам, за один запуск.</p>";
    if (m.kind === "value" && o) html += '<p class="pe-help">Значение «' + esc(o.name) + "» (" + esc(o.type_word || "значение") + ") подставится во вход перед стартом." +
      (p && p.secret ? " Секрет: в файл проекта не попадает, на экране скрыт." : "") + "</p>";
    if (m.kind === "column") {
      var cols = o && o.columns ? o.columns : [];
      html += '<p class="pe-help">Из какой колонки:</p>' + (cols.length ? '<div class="pe-colchips">' + cols.map(function (c) {
        return '<button type="button" class="pe-colchip" data-col="' + mi + '" data-colname="' + esc(c) + '" aria-pressed="' + (c === m.column) + '">' + esc(c) + "</button>";
      }).join("") + "</div>" : '<input class="pe-box" data-colin="' + mi + '" value="' + esc(m.column || "") + '" placeholder="название колонки">');
    }
    if (e.map.length > 1) html += '<button type="button" class="rm" data-rmmap="' + mi + '">убрать это соответствие</button>';
    return html + "</div>";
  }

  function renderEdgeInsp(e, ei) {
    var A = node(e.from), B = node(e.to);
    var maps = e.map || [];
    var col = maps.find(function (m) { return m.kind === "column"; });
    var hasTable = maps.some(function (m) { var p = paramOf(B, m.param); return m.kind === "file" && p && p.type === "table"; });
    var rowsSec = "";
    if (col) {
      var p = paramOf(B, col.param), list = p && p.list;
      var rows = [["first", "Первая строка"], ["each", "Каждая строка отдельно"]];
      if (list) rows.push(["all", "Вся колонка списком"]);
      var where = e.where;
      var each = maps.some(function (m) { return m.rows === "each"; });
      rowsSec = '<div class="pe-sec"><h3>Строки</h3><p class="pe-help">Во входе «' + esc(p ? p.title : col.param) + "» одно значение, а в колонке может быть много строк.</p>" +
        seg(rows, col.rows || "first", "rows") +
        '<button type="button" class="pe-toggle" role="switch" aria-checked="' + !!where + '" data-where><span class="pe-sw"></span><span class="t">Только строки по условию</span></button>' +
        (where ? '<div class="pe-cond"><button type="button" class="pe-pill" data-wcol>' + esc(where.column || "колонка") + '</button><button type="button" class="pe-pill" data-wop>' +
          esc(OP_WORD[where.op] || "равно") + "</button>" + (where.op === "empty" || where.op === "not_empty" ? "<span></span>" :
          '<input class="pe-box" data-wval value="' + esc(where.value == null ? "" : where.value) + '" placeholder="значение">') + "</div>" : "") +
        '<div class="pe-line2"><span class="l">Не больше строк</span>' + stepper(e.limit || "все", "lim", "Не больше строк") + "</div>" +
        (each ? '<div class="pe-line2"><span class="l">Одновременно</span>' + stepper(e.parallel || 1, "par", "Одновременно") + "</div>" +
          '<p class="pe-help pe-gap">Если один запуск упал</p>' + seg([["continue", "Продолжать остальные"], ["stop", "Остановить"]], e.on_error || "continue", "onerr") : "") +
        '<div class="pe-preview">' + (each ? "Узел «" + esc(nodeTitle(B)) + "» запустится по разу на каждую строку" + (e.limit ? " (не больше " + e.limit + ")" : "") +
          ", по " + (e.parallel || 1) + " одновременно. Результаты склеятся в одну таблицу с колонкой «" + esc(col.column || "") + "»."
          : col.rows === "all" ? "Вся колонка уйдёт списком, узел запустится один раз." : "Возьмётся значение «" + esc(col.column || "") + "» из первой строки" + (where ? " после условия" : "") + ". Узел запустится один раз.") +
        "</div></div>";
    }
    var srcOuts = (script(A) || { outputs: [] }).outputs.filter(function (o) { return o.kind === "file"; });
    insp.innerHTML = '<div class="pe-insp-head"><small>Стрелка</small><h2>' + esc(nodeTitle(A)) + " → " + esc(nodeTitle(B)) + "</h2></div>" +
      '<div class="pe-sec"><h3>Что передаётся</h3>' + maps.map(function (m, mi) { return mapCard(e, ei, m, mi); }).join("") +
      '<button type="button" class="pe-addmap" data-addmap>+ ещё соответствие</button></div>' + rowsSec +
      '<div class="pe-sec"><h3>Когда идти дальше</h3>' + seg(WHEN, e.when || "partial", "when") +
      '<p class="pe-help pe-gap">Только если источник отдал файл</p><button type="button" class="pe-box' + (e.if_file ? "" : " ph") + '" data-iffile>' +
      esc(e.if_file || "всегда, без условия") + "</button>" +
      (hasTable ? '<button type="button" class="pe-toggle" role="switch" aria-checked="' + !!e.source_column + '" data-srccol><span class="pe-sw"></span><span class="t">Колонка источника<small>при склейке таблиц добавить колонку с названием узла' +
        (e.source_column ? ": «" + esc(e.source_column) + "»" : "") + "</small></span></button>" : "") + "</div>" +
      '<div class="pe-sec"><button type="button" class="pe-danger" data-rmedge>Удалить стрелку</button></div>';

    on("[data-when]", "click", function (b) { e.when = b.dataset.when; changed(); renderInsp(); });
    on("[data-rmedge]", "click", function () { S.project.edges.splice(ei, 1); S.sel = null; changed(); renderNodes(); renderInsp(); });
    on("[data-rmmap]", "click", function (b) { e.map.splice(+b.dataset.rmmap, 1); S.sel.mi = 0; changed(); renderNodes(); renderInsp(); });
    on("[data-rows]", "click", function (b) { e.map.forEach(function (m) { if (m.kind === "column") m.rows = b.dataset.rows; }); changed(); renderNodes(); renderInsp(); });
    on("[data-col]", "click", function (b) { e.map[+b.dataset.col].column = b.dataset.colname; changed(); renderNodes(); renderInsp(); });
    on("[data-colin]", "change", function (b) { e.map[+b.dataset.colin].column = b.value.trim(); changed(); renderNodes(); renderInsp(); });
    on("[data-where]", "click", function () {
      e.where = e.where ? null : { column: (col && col.column) || "", op: "equals", value: "", ignore_case: true }; changed(); renderInsp();
    });
    on("[data-wcol]", "click", function (b) {
      var o = outOf(A, col.file), cols = o && o.columns ? o.columns : [];
      if (!cols.length) { var v = window.prompt("Колонка условия", e.where.column || ""); if (v != null) { e.where.column = v.trim(); changed(); renderInsp(); } return; }
      openPop(b, cols.map(function (c) { return { text: c, cur: c === e.where.column, pick: function () { e.where.column = c; changed(); renderInsp(); } }; }));
    });
    on("[data-wop]", "click", function (b) {
      openPop(b, OPS.map(function (o) { return { text: o[1], cur: o[0] === e.where.op, pick: function () { e.where.op = o[0]; changed(); renderInsp(); } }; }));
    });
    on("[data-wval]", "change", function (b) { e.where.value = b.value; changed(); });
    on("[data-lim]", "click", function (b) {
      var steps = [null, 1, 3, 5, 10, 20, 50, 100, 500], i = steps.indexOf(e.limit || null);
      if (i < 0) i = 0;
      e.limit = steps[Math.max(0, Math.min(steps.length - 1, i + (+b.dataset.lim)))]; changed(); renderInsp();
    });
    on("[data-par]", "click", function (b) { e.parallel = Math.max(1, Math.min(10, (e.parallel || 1) + (+b.dataset.par))); changed(); renderNodes(); renderInsp(); });
    on("[data-onerr]", "click", function (b) { e.on_error = b.dataset.onerr; changed(); renderInsp(); });
    on("[data-srccol]", "click", function () {
      if (e.source_column) e.source_column = null;
      else e.source_column = "Источник";
      changed(); renderInsp();
    });
    on("[data-iffile]", "click", function (b) {
      var items = [{ text: "Всегда, без условия", cur: !e.if_file, pick: function () { e.if_file = null; changed(); renderNodes(); renderInsp(); } }];
      srcOuts.forEach(function (o) {
        items.push({ text: o.name, sub: o.optional ? "бывает не всегда: подходит для ветвления" : "отдаётся всегда: стрелка будет активна всегда",
          cur: e.if_file === o.name, pick: function () { e.if_file = o.name; changed(); renderNodes(); renderInsp(); } });
      });
      openPop(b, items);
    });
    on("[data-pick-out]", "click", function (b) {
      var m = e.map[+b.dataset.pickOut], s = script(A);
      openPop(b, (s ? s.outputs : []).map(function (o) {
        return { text: o.name, sub: o.kind === "value" ? "значение, " + (o.type_word || "") : (o.columns ? o.columns.join(", ") : "файл"),
          cur: (m.kind === "value" ? m.value : m.file) === o.name, pick: function () { setMap(e, +b.dataset.pickOut, o.name, m.param, null); } };
      }));
    });
    on("[data-pick-in]", "click", function (b) {
      var m = e.map[+b.dataset.pickIn], s = script(B);
      openPop(b, (s ? s.params : []).map(function (p) {
        return { text: p.title, sub: p.type_word + (p.required ? ", обязательно" : ""), cur: m.param === p.name,
          pick: function () { setMap(e, +b.dataset.pickIn, m.kind === "value" ? m.value : m.file, p.name, m.kind === "column" ? m.column : null); } };
      }));
    });
    on("[data-addmap]", "click", function (b) {
      var sA = script(A), sB = script(B);
      if (!sA || !sB || !sA.outputs.length) return;
      var o = sA.outputs[0], p = sB.params.find(function (x) { return !e.map.some(function (m) { return m.param === x.name; }); }) || sB.params[0];
      if (!p) return;
      e.map.push(makeMap(o, p, null));
      S.sel.mi = e.map.length - 1; changed(); renderNodes(); renderInsp();
    });
  }

  function makeMap(o, p, col) {
    var m = { kind: "file", file: null, value: null, column: null, param: p.name, rows: "first" };
    if (o.kind === "value") { m.kind = "value"; m.value = o.name; return m; }
    m.file = o.name;
    if (!p.transfer) { m.kind = "column"; m.column = col || (o.columns ? o.columns[0] : ""); }
    return m;
  }
  function setMap(e, mi, outName, param, col) {
    var A = node(e.from), B = node(e.to), o = outOf(A, outName) || { name: outName, kind: "file" }, p = paramOf(B, param);
    if (!p) return;
    e.map[mi] = makeMap(o, p, col);
    dedupe(e, e.map[mi]);
    changed(); renderNodes(); renderInsp();
  }
  // Во вход-значение и одиночный файл идёт одна стрелка: прежние соответствия в этот вход снимаются.
  function dedupe(keepEdge, keepMap) {
    var B = node(keepEdge.to), p = paramOf(B, keepMap.param);
    if (p && (p.type === "table" || (p.type === "file" && p.multiple))) return;
    S.project.edges.forEach(function (e) {
      if (e.to !== keepEdge.to) return;
      e.map = (e.map || []).filter(function (m) { return m === keepMap || m.param !== keepMap.param; });
    });
    S.project.edges = S.project.edges.filter(function (e) { return e === keepEdge || (e.map && e.map.length); });
  }

  // --- поповер ----------------------------------------------------------------------------

  function openPop(anchor, items) {
    var r = anchor.getBoundingClientRect();
    pop.innerHTML = items.map(function (it, i) {
      return '<button type="button" role="option" class="opt' + (it.cur ? " cur" : "") + '" data-i="' + i + '"' + (it.disabled ? " disabled" : "") + ' aria-selected="' + !!it.cur + '">' +
        esc(it.text) + (it.sub ? "<small>" + esc(it.sub) + "</small>" : "") + "</button>";
    }).join("");
    pop.hidden = false;
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    var top = r.bottom + 6 + h > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6;
    pop.style.left = left + "px"; pop.style.top = top + "px";
    $$(".opt", pop).forEach(function (b) {
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var it = items[+b.dataset.i];
        if (!it.keep) closePop();
        it.pick();
      });
    });
    var first = pop.querySelector(".opt.cur") || pop.querySelector(".opt");
    if (first) first.focus();
    S.popAnchor = anchor;
  }
  function closePop() {
    if (!pop.hidden) { pop.hidden = true; pop.innerHTML = ""; if (S.popAnchor && document.body.contains(S.popAnchor)) S.popAnchor.focus(); S.popAnchor = null; }
  }
  document.addEventListener("pointerdown", function (ev) { if (!pop.hidden && !pop.contains(ev.target)) closePop(); }, true);
  document.addEventListener("keydown", function (ev) {
    if (pop.hidden) return;
    if (ev.key === "Escape") { ev.preventDefault(); closePop(); return; }
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      var opts = $$(".opt:not([disabled])", pop), i = opts.indexOf(document.activeElement);
      i = ev.key === "ArrowDown" ? (i + 1) % opts.length : (i - 1 + opts.length) % opts.length;
      if (opts[i]) opts[i].focus();
    }
  });

  // --- правка схемы ------------------------------------------------------------------------

  function newId(scriptName) {
    var base = scriptName.replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "node", id = base, i = 2;
    while (node(id)) { id = base + "-" + i; i += 1; }
    return id;
  }
  // Свободное место рядом с точкой: новый узел не ложится поверх других и не прячет их кружки.
  function freeSpot(x, y) {
    var W = 260, H = 240, boxes = S.project.nodes.map(function (n) {
      var el = nodeEl(n.id);
      return { x: n.x || 0, y: n.y || 0, w: el ? el.offsetWidth + 20 : W, h: el ? el.offsetHeight + 20 : H };
    });
    function free(px, py) {
      return boxes.every(function (b) { return px + W <= b.x || px >= b.x + b.w || py + H <= b.y || py >= b.y + b.h; });
    }
    for (var r = 0; r < 12; r += 1) {
      var cands = r === 0 ? [[0, 0]] : [[r * 140, 0], [0, r * 120], [r * 140, r * 120], [-r * 140, 0], [0, -r * 120]];
      for (var i = 0; i < cands.length; i += 1) {
        if (free(x + cands[i][0], y + cands[i][1])) return [x + cands[i][0], y + cands[i][1]];
      }
    }
    var maxx = Math.max.apply(null, boxes.map(function (b) { return b.x + b.w; }).concat([0]));
    return [maxx + 20, y];
  }
  function addNode(scriptName, x, y, exact) {
    var s = S.scripts[scriptName];
    if (!s) return;
    if (!exact) { var spot = freeSpot(x, y); x = spot[0]; y = spot[1]; }
    var n = { id: newId(scriptName), script: scriptName, preset: s.presets.length ? s.presets[0].name : null, values: {}, title: null,
      x: Math.round(x), y: Math.round(y), wait: "all" };
    S.project.nodes.push(n);
    changed(); select({ kind: "node", id: n.id });
  }
  function removeNode(id) {
    S.project.nodes = S.project.nodes.filter(function (n) { return n.id !== id; });
    S.project.edges = S.project.edges.filter(function (e) { return e.from !== id && e.to !== id; });
    S.sel = null; changed(); renderNodes(); renderInsp();
  }
  function connect(from, outName, col, to, param) {
    if (from === to) return;
    var A = node(from), B = node(to), o = outOf(A, outName), p = paramOf(B, param);
    if (!o || !p) return;
    if (o.kind === "value" && p.transfer) { flash("Значение нельзя передать во вход-файл: протяните его к входу-значению."); return; }
    var e = S.project.edges.find(function (x) { return x.from === from && x.to === to; });
    var ei;
    if (!e) {
      e = { from: from, to: to, when: "partial", if_file: null, map: [], source_column: null, limit: null, where: null, parallel: 1, on_error: "continue" };
      S.project.edges.push(e);
    }
    var m = makeMap(o, p, col);
    e.map = e.map.filter(function (x) { return x.param !== param; });
    e.map.push(m);
    dedupe(e, m);
    ei = S.project.edges.indexOf(e);
    changed(); select({ kind: "edge", ei: ei, mi: e.map.indexOf(m) });
  }
  function flash(text) {
    var f = $("#flash");
    if (!f) return;
    f.innerHTML = '<p class="notice notice-error" role="alert"></p>';
    f.firstChild.textContent = text;
  }

  // --- сохранение ---------------------------------------------------------------------------

  var saveTimer = null;
  function changed() {
    S.dirty = true; S.error = null;
    setSaved("Сохраняю…");
    renderTop();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 600);
  }
  function payload() {
    var p = clone(S.project);
    p.edges.forEach(function (e) { (e.map || []).forEach(function (m) { delete m.__to; }); });
    return p;
  }
  function save() {
    if (S.saving) { saveTimer = setTimeout(save, 300); return; }
    S.dirty = false;
    var body = { project: payload(), base_version: S.version };
    if (DRAFT) {
      if (!body.project.nodes.length) { setSaved("Черновик: проект сохранится, когда на холсте появится узел"); return; }
      body.create = true;
    }
    S.saving = req("POST", "/projects/" + encodeURIComponent(NAME) + "/save", body).then(function (r) {
      S.saving = null;
      if (r.ok) {
        S.version = r.data.version; S.problems = r.data.problems || []; S.lastGood = clone(body.project); S.error = null;
        if (DRAFT) { DRAFT = false; if (window.history && history.replaceState) history.replaceState(null, "", "/projects/" + encodeURIComponent(NAME)); }
        setSaved("Сохранено, версия " + S.version);
        renderNodes(); if (!(S.sel && S.sel.kind === "node" && document.activeElement && insp.contains(document.activeElement))) renderInsp();
        return;
      }
      var err = r.data && r.data.error ? r.data.error : { message: "Не удалось сохранить" };
      if (r.status === 409) {
        setSaved("Проект изменён в другом месте", true);
        S.error = "Проект поменяли в другом месте (агент или другая вкладка). Обновите страницу: ваши последние правки не сохранены";
        renderTop();
        return;
      }
      // Правка не прошла проверку: схема возвращается к последней сохранённой, причина видна.
      var problems = (err.problems || []).map(function (p) { return typeof p === "string" ? p : p.text; }).filter(Boolean);
      flash("Не сохранено: " + (problems[0] || err.message));
      if (S.lastGood) { S.project = clone(S.lastGood); S.sel = null; }
      setSaved("Правка отменена", true);
      renderNodes(); renderInsp();
    }).catch(function () {
      S.saving = null; setSaved("Нет связи с приложением, правка не сохранена", true); S.dirty = true;
    });
  }
  window.addEventListener("beforeunload", function (e) { if (S.dirty || S.saving) { e.preventDefault(); e.returnValue = ""; } });

  // --- мышь, касание, клавиатура -------------------------------------------------------------

  var drag = null;
  canvas.addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0) return;
    var dot = ev.target.closest(".pout");
    var nEl = ev.target.closest(".pe-node");
    if (dot && MODE === "edit") {
      var row = dot.closest(".pe-orow"), id = row.closest(".pe-node").dataset.id;
      drag = { kind: "wire", from: id, out: row.dataset.out, col: row.dataset.col || null, a: centerOf(dot) };
      canvas.setPointerCapture(ev.pointerId);
      ev.preventDefault(); return;
    }
    if (nEl) {
      if (ev.target.closest(".val, .pe-more, input, .pe-cx, a")) return;
      var n = node(nEl.dataset.id);
      if (!S.sel || S.sel.kind !== "node" || S.sel.id !== n.id) select({ kind: "node", id: n.id });
      if (MODE === "edit" && ev.target.closest(".pe-node-head")) {
        drag = { kind: "node", n: n, sx: ev.clientX, sy: ev.clientY, x: n.x || 0, y: n.y || 0, moved: false };
        canvas.setPointerCapture(ev.pointerId);
      }
      return;
    }
    if (ev.target.closest(".pe-zoom, .pe-addnode, .pe-legend, .pe-runbar, .pe-edge-chip, .pe-wire-hit, button, a")) return;
    drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY, x: S.view.x, y: S.view.y, moved: false };
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    if (drag.kind === "node") {
      drag.n.x = Math.round(drag.x + (ev.clientX - drag.sx) / S.view.k); drag.n.y = Math.round(drag.y + (ev.clientY - drag.sy) / S.view.k);
      drag.moved = true;
      var el = nodeEl(drag.n.id); el.style.left = drag.n.x + "px"; el.style.top = drag.n.y + "px";
      renderWires();
    } else if (drag.kind === "pan") {
      S.view.x = drag.x + ev.clientX - drag.sx; S.view.y = drag.y + ev.clientY - drag.sy; drag.moved = true;
      canvas.classList.add("panning"); applyView();
    } else {
      var w = world.getBoundingClientRect();
      var b = { x: (ev.clientX - w.left) / S.view.k, y: (ev.clientY - w.top) / S.view.k };
      S.temp = curve(drag.a, b); renderWires();
      var hit = document.elementFromPoint(ev.clientX, ev.clientY);
      $$(".pe-prow.hot", world).forEach(function (p) { p.classList.remove("hot"); });
      var row = hit && hit.closest ? hit.closest(".pe-prow[data-in]") : null;
      if (row && row.closest(".pe-node").dataset.id !== drag.from) row.classList.add("hot");
    }
  });
  canvas.addEventListener("pointerup", function (ev) {
    if (!drag) return;
    var d = drag; drag = null;
    canvas.classList.remove("panning");
    if (d.kind === "node" && d.moved) changed();
    if (d.kind === "pan" && !d.moved && S.sel) select(null);
    if (d.kind === "wire") {
      S.temp = null;
      $$(".pe-prow.hot", world).forEach(function (p) { p.classList.remove("hot"); });
      var hit = document.elementFromPoint(ev.clientX, ev.clientY);
      var row = hit && hit.closest ? hit.closest(".pe-prow[data-in]") : null;
      if (!row) {
        var nEl = hit && hit.closest ? hit.closest(".pe-node") : null;
        if (nEl && nEl.dataset.id !== d.from) return pickInput(nEl.dataset.id, d, ev);
        renderWires(); return;
      }
      connect(d.from, d.out, d.col, row.closest(".pe-node").dataset.id, row.dataset.in);
    }
  });
  // Стрелка брошена на узел мимо входа (или вход скрыт под «ещё N»): выбор входа поповером.
  function pickInput(to, d, ev) {
    renderWires();
    var B = node(to), s = script(B);
    if (!s) return;
    var anchor = nodeEl(to).querySelector(".pe-node-head");
    openPop(anchor, s.params.map(function (p) {
      return { text: p.title, sub: p.type_word + (p.required ? ", обязательно" : ""), pick: function () { connect(d.from, d.out, d.col, to, p.name); } };
    }));
  }
  canvas.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var r = canvas.getBoundingClientRect();
    if (ev.ctrlKey || ev.metaKey) zoom(ev.deltaY < 0 ? 1 : -1, ev.clientX - r.left, ev.clientY - r.top);
    else { S.view.x -= ev.deltaX; S.view.y -= ev.deltaY; applyView(); }
  }, { passive: false });
  canvas.addEventListener("click", function (ev) {
    var chip = ev.target.closest(".pe-edge-chip");
    if (chip) return;
  });
  canvas.addEventListener("dragover", function (e) { if (MODE === "edit") { e.preventDefault(); canvas.classList.add("drop"); } });
  canvas.addEventListener("dragleave", function () { canvas.classList.remove("drop"); });
  canvas.addEventListener("drop", function (e) {
    e.preventDefault(); canvas.classList.remove("drop");
    var name = e.dataTransfer.getData("text/plain"), w = world.getBoundingClientRect();
    addNode(name, (e.clientX - w.left) / S.view.k - 120, (e.clientY - w.top) / S.view.k - 24, true);
  });
  document.addEventListener("keydown", function (ev) {
    if (MODE !== "edit" || !pop.hidden) return;
    var t = ev.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA)$/.test(t.tagName))) return;
    if ((ev.key === "Delete" || ev.key === "Backspace") && S.sel) {
      if (S.sel.kind === "node") { ev.preventDefault(); removeNode(S.sel.id); }
      else if (S.sel.kind === "edge" && S.project.edges[S.sel.ei]) { ev.preventDefault(); S.project.edges.splice(S.sel.ei, 1); S.sel = null; changed(); renderNodes(); renderInsp(); }
    }
    if (ev.key === "Escape" && S.sel) select(null);
  });
  $$("[data-zoom]").forEach(function (b) {
    b.addEventListener("click", function () { var z = +b.dataset.zoom; if (z === 0) fit(); else zoom(z); });
  });
  var addBtn = $("#pe-addnode");
  if (addBtn) addBtn.addEventListener("click", function () { var p = $("#pe-palette"); if (p) { p.classList.toggle("open"); var q = $("#pe-q"); if (q && p.classList.contains("open")) q.focus(); } });
  var qEl = $("#pe-q");
  if (qEl) qEl.addEventListener("input", function () { S.q = qEl.value.trim(); renderPalette(); });
  $$("[data-tab]").forEach(function (b) {
    b.addEventListener("click", function () {
      S.tab = b.dataset.tab;
      $$("[data-tab]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
      renderPalette();
    });
  });
  var check = $("#pe-check");
  if (check) check.addEventListener("click", function () {
    var list = allProblems();
    if (list.length && list[0].node) select({ kind: "node", id: list[0].node }); else select(null);
  });
  var titleBtn = $("#pe-title");
  if (titleBtn && MODE === "edit") titleBtn.addEventListener("click", function () {
    var input = document.createElement("input");
    input.className = "pe-title-input"; input.value = S.project.title; input.maxLength = 120; input.setAttribute("aria-label", "Название проекта");
    titleBtn.replaceWith(input); input.focus(); input.select();
    var done = false;
    function fin(saveIt) {
      if (done) return; done = true;
      var v = input.value.trim();
      if (saveIt && v && v !== S.project.title) { S.project.title = v; changed(); document.title = v + ", проект, dawg-replay"; }
      titleBtn.textContent = S.project.title; input.replaceWith(titleBtn);
    }
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") fin(true); if (e.key === "Escape") fin(false); });
    input.addEventListener("blur", function () { fin(true); });
  });
  window.addEventListener("resize", function () { renderWires(); });

  // --- экран запуска --------------------------------------------------------------------------

  function renderRunbar() {
    var bar = $(".pe-runbar", canvas);
    if (!bar) { bar = document.createElement("div"); bar.className = "pe-runbar"; canvas.appendChild(bar); }
    var r = S.run, st = r.status;
    bar.innerHTML = "<span>Проект:</span>" +
      '<span class="stchip ' + esc(st) + '">' + esc(r.label) + "</span>" +
      (r.status === "running" ? '<button type="button" class="pe-ghost" data-stop>' + (r.cancel_requested ? "Останавливается…" : "Остановить") + "</button>" : "") +
      (r.can_retry ? '<button type="button" class="pe-primary" data-retry>↻ Перезапустить с упавшего</button>' : "") +
      '<a class="pe-ghost" href="/projects/' + encodeURIComponent(r.project) + '">К редактору</a>';
    var stop = $("[data-stop]", bar), retry = $("[data-retry]", bar);
    if (stop) stop.addEventListener("click", function () {
      stop.disabled = true;
      req("POST", "/projects/runs/" + encodeURIComponent(r.id) + "/stop").then(function (x) { if (x.ok) { S.run = x.data; renderRun(); } else flash((x.data.error || {}).message || "Не удалось остановить"); });
    });
    if (retry) retry.addEventListener("click", function () {
      retry.disabled = true;
      req("POST", "/projects/runs/" + encodeURIComponent(r.id) + "/retry").then(function (x) {
        if (x.ok && x.data.redirect) window.location.href = x.data.redirect; else { retry.disabled = false; flash((x.data.error || {}).message || "Перезапуск не начат"); }
      });
    });
  }
  function stepRow(n) {
    var st = stepOf(n);
    if (!st) return "";
    var inner = '<span class="grow">' + esc(nodeTitle(n)) + "<small>" + esc(st.reason || (st.reused ? "результат из прошлого запуска" : (st.rows_summary && st.rows_summary.text) || "")) +
      '</small></span><span class="stchip ' + esc(st.status) + '">' + esc(st.status_title || STEP[st.status]) + "</span>";
    return st.run_id ? '<a class="pe-steprow" href="/runs/' + esc(st.run_id) + '">' + inner + "</a>" : '<div class="pe-steprow">' + inner + "</div>";
  }
  function renderRunInsp() {
    var r = S.run, sel = S.sel && S.sel.kind === "node" ? node(S.sel.id) : null;
    var trig = r.trigger === "schedule" ? "по расписанию" : "вручную";
    var html = '<div class="pe-insp-head"><small>Запуск проекта ' + esc(trig) + (r.retry_of ? ", перезапуск с упавшего" : "") + "</small><h2>" + esc(S.project.title) + ": " + esc(r.label) + "</h2></div>";
    if (r.error) html += '<div class="pe-sec"><p class="pe-help">' + esc(r.error) + "</p></div>";
    if (sel) {
      var st = stepOf(sel);
      html += '<div class="pe-sec"><h3>Узел «' + esc(nodeTitle(sel)) + '»</h3><div class="pe-steplist">' + stepRow(sel) + "</div>";
      if (st && st.rows && st.rows.length) {
        html += '<h3 class="pe-gap">Строки</h3><div class="pe-steplist">' + st.rows.map(function (row) {
          var inner = '<span class="grow">' + esc(row.label || "#" + (row.index + 1)) + "<small>" + esc(row.reason || (row.reused ? "результат из прошлого запуска" : "")) +
            '</small></span><span class="stchip ' + esc(row.status) + '">' + esc(row.status_title || STEP[row.status]) + "</span>";
          return row.run_id ? '<a class="pe-steprow" href="/runs/' + esc(row.run_id) + '">' + inner + "</a>" : '<div class="pe-steprow">' + inner + "</div>";
        }).join("") + "</div>";
      }
      html += "</div>";
    }
    html += '<div class="pe-sec"><h3>Шаги</h3><div class="pe-steplist">' + S.project.nodes.map(stepRow).join("") + "</div></div>";
    insp.innerHTML = html;
  }
  function renderRun() { renderNodes(); renderInsp(); renderRunbar(); }
  function poll() {
    if (!S.run || S.run.status !== "running") return;
    setTimeout(function () {
      req("GET", "/projects/runs/" + encodeURIComponent(S.run.id) + "/_state").then(function (x) {
        if (x.ok) { S.run = x.data; renderRun(); }
        poll();
      }).catch(poll);
    }, 2000);
  }

  // --- старт ------------------------------------------------------------------------------

  function start(data) {
    S.project = data.project;
    S.scripts = {};
    Object.keys(data.scripts || {}).forEach(function (k) { if (data.scripts[k]) S.scripts[k] = data.scripts[k]; });
    S.version = data.version; S.problems = data.problems || [];
    S.lastGood = clone(S.project);
    if (data.last_run) { S.lastRun = data.last_run; }
    if (MODE === "run") { S.run = data.run; }
    renderPalette();
    renderNodes();
    fit();
    renderWires();
    if (MODE === "run") { renderRun(); poll(); } else { renderInsp(); setSaved(data.version ? "Версия " + data.version : ""); }
  }
  if (MODE === "run") {
    var raw = $("#pe-data");
    start(JSON.parse(raw.textContent));
  } else {
    var q = DRAFT ? "?new=1&title=" + encodeURIComponent(root.querySelector("#pe-title").textContent.trim()) : "";
    req("GET", "/projects/" + encodeURIComponent(NAME) + "/_data" + q).then(function (r) {
      if (!r.ok) { flash((r.data.error || {}).message || "Проект не загрузился"); return; }
      start(r.data);
    });
  }
})();
