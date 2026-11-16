// Карта связей: ставит узлы по data-x/data-y, рисует стрелки, двигает узлы и запоминает раскладку,
// протянутая от кружка узла стрелка создаёт связь. Встроенных стилей нет (CSP): положения задаются здесь.
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";
  var PAD = 40;
  var MOVE_SLOP = 4;

  function nodes(map) { return Array.prototype.slice.call(map.querySelectorAll("[data-node]")); }

  function byName(map) {
    var out = {};
    nodes(map).forEach(function (n) { out[n.getAttribute("data-node")] = n; });
    return out;
  }

  function box(n) {
    return { x: n.offsetLeft, y: n.offsetTop, w: n.offsetWidth, h: n.offsetHeight };
  }

  function place(map) {
    nodes(map).forEach(function (n) {
      n.style.left = (parseInt(n.getAttribute("data-x"), 10) || 0) + "px";
      n.style.top = (parseInt(n.getAttribute("data-y"), 10) || 0) + "px";
    });
  }

  function size(map) {
    var w = 0, h = 0;
    nodes(map).forEach(function (n) {
      var b = box(n);
      w = Math.max(w, b.x + b.w);
      h = Math.max(h, b.y + b.h);
    });
    map.style.width = (w + PAD * 3) + "px";
    map.style.height = (h + PAD * 2) + "px";
    var svg = map.querySelector(".lmap-edges");
    svg.setAttribute("width", w + PAD * 3);
    svg.setAttribute("height", h + PAD * 2);
  }

  // Точки на сторонах прямоугольников, обращённых друг к другу, и кривая между ними.
  function route(a, b, shift) {
    var ac = { x: a.x + a.w / 2, y: a.y + a.h / 2 };
    var bc = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
    var dx = bc.x - ac.x, dy = bc.y - ac.y;
    var p1, p2, n1, n2;
    if (Math.abs(dx) * (a.h / a.w) >= Math.abs(dy)) {
      var s = dx >= 0 ? 1 : -1;
      p1 = { x: ac.x + s * a.w / 2, y: ac.y + shift };
      p2 = { x: bc.x - s * b.w / 2, y: bc.y + shift };
      n1 = { x: s, y: 0 }; n2 = { x: -s, y: 0 };
    } else {
      var t = dy >= 0 ? 1 : -1;
      p1 = { x: ac.x + shift, y: ac.y + t * a.h / 2 };
      p2 = { x: bc.x + shift, y: bc.y - t * b.h / 2 };
      n1 = { x: 0, y: t }; n2 = { x: 0, y: -t };
    }
    var d = Math.max(40, Math.hypot(p2.x - p1.x, p2.y - p1.y) / 3);
    var c1 = { x: p1.x + n1.x * d, y: p1.y + n1.y * d };
    var c2 = { x: p2.x + n2.x * d, y: p2.y + n2.y * d };
    var mid = {
      x: 0.125 * p1.x + 0.375 * c1.x + 0.375 * c2.x + 0.125 * p2.x,
      y: 0.125 * p1.y + 0.375 * c1.y + 0.375 * c2.y + 0.125 * p2.y
    };
    return {
      d: "M" + p1.x + "," + p1.y + " C" + c1.x + "," + c1.y + " " + c2.x + "," + c2.y + " " + p2.x + "," + p2.y,
      mid: mid
    };
  }

  function draw(map) {
    var all = byName(map);
    var paths = map.querySelectorAll("[data-edge]");
    var pairs = {};
    Array.prototype.forEach.call(paths, function (p) {
      pairs[p.getAttribute("data-from") + "\u0000" + p.getAttribute("data-to")] = true;
    });
    Array.prototype.forEach.call(paths, function (p) {
      var from = all[p.getAttribute("data-from")], to = all[p.getAttribute("data-to")];
      var label = map.querySelector('[data-label-for="' + p.getAttribute("data-edge") + '"]');
      if (!from || !to) { p.setAttribute("d", ""); if (label) label.hidden = true; return; }
      // Встречные связи (А → Б и Б → А) разводятся, чтобы не лечь одна на другую.
      var back = pairs[p.getAttribute("data-to") + "\u0000" + p.getAttribute("data-from")];
      var r = route(box(from), box(to), back ? 9 : 0);
      p.setAttribute("d", r.d);
      if (label) {
        label.hidden = false;
        // Подпись не выходит за край карты: иначе её начало обрезается.
        var lx = Math.max(4, Math.min(map.offsetWidth - label.offsetWidth - 4, r.mid.x - label.offsetWidth / 2));
        label.style.left = Math.round(lx) + "px";
        label.style.top = Math.round(Math.max(4, r.mid.y - label.offsetHeight / 2)) + "px";
      }
    });
  }

  function saveLayout(positions) {
    if (!window.fetch) return;
    fetch("/links/_layout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ positions: positions })
    }).catch(function () { /* сервер остановлен: раскладка останется до перезагрузки страницы */ });
  }

  function point(map, e) {
    var r = map.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  function nodeAt(e) {
    var el = document.elementFromPoint(e.clientX, e.clientY);
    return el ? el.closest("[data-node]") : null;
  }

  function init(map) {
    if (map.getAttribute("data-ready")) return;
    map.setAttribute("data-ready", "1");
    place(map);
    size(map);
    draw(map);
    var draft = map.querySelector("[data-draft]");
    var drag = null;

    map.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      var port = e.target.closest("[data-port]");
      var node = e.target.closest("[data-node]");
      if (!node) return;
      e.preventDefault();
      var start = point(map, e);
      if (port) {
        drag = { kind: "link", node: node, start: start };
      } else {
        drag = { kind: "move", node: node, start: start, x: node.offsetLeft, y: node.offsetTop, moved: false };
      }
      map.setPointerCapture(e.pointerId);
    });

    map.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var p = point(map, e);
      if (drag.kind === "move") {
        var dx = p.x - drag.start.x, dy = p.y - drag.start.y;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < MOVE_SLOP) return;
        drag.moved = true;
        drag.node.classList.add("is-dragging");
        drag.node.style.left = Math.max(0, drag.x + dx) + "px";
        drag.node.style.top = Math.max(0, drag.y + dy) + "px";
        draw(map);
      } else {
        var b = box(drag.node);
        var x1 = b.x + b.w, y1 = b.y + b.h / 2;
        draft.hidden = false;
        draft.setAttribute("d", "M" + x1 + "," + y1 + " L" + p.x + "," + p.y);
        nodes(map).forEach(function (n) { n.classList.remove("is-drop"); });
        var over = nodeAt(e);
        if (over && over !== drag.node) over.classList.add("is-drop");
      }
    });

    function finish(e, cancelled) {
      if (!drag) return;
      var d = drag;
      drag = null;
      if (d.kind === "move") {
        d.node.classList.remove("is-dragging");
        if (!d.moved) {
          // Клик без перетаскивания: открыть настройки получателя.
          var btn = d.node.querySelector(".lnode-main");
          if (!cancelled && btn) btn.click();
          return;
        }
        var name = d.node.getAttribute("data-node");
        d.node.setAttribute("data-x", d.node.offsetLeft);
        d.node.setAttribute("data-y", d.node.offsetTop);
        size(map);
        draw(map);
        var pos = {};
        pos[name] = [d.node.offsetLeft, d.node.offsetTop];
        saveLayout(pos);
        return;
      }
      draft.hidden = true;
      nodes(map).forEach(function (n) { n.classList.remove("is-drop"); });
      var target = cancelled ? null : nodeAt(e);
      if (target && target !== d.node && window.htmx) {
        htmx.ajax("POST", "/links", {
          target: "#link-panel",
          values: { source: d.node.getAttribute("data-node"), target: target.getAttribute("data-node"), after: "ok" }
        });
      }
    }

    map.addEventListener("pointerup", function (e) { finish(e, false); });
    map.addEventListener("pointercancel", function (e) { finish(e, true); });
    // Кнопка узла открывается и с клавиатуры; мышиный клик уже обработан в pointerup.
    map.addEventListener("click", function (e) {
      if (e.detail > 0 && e.target.closest(".lnode-main")) { e.preventDefault(); e.stopPropagation(); }
    }, true);
  }

  function initAll() {
    var map = document.querySelector("[data-lmap]");
    if (map) init(map);
  }

  document.addEventListener("DOMContentLoaded", initAll);
  document.addEventListener("htmx:afterSettle", initAll);
  window.addEventListener("resize", function () {
    var map = document.querySelector("[data-lmap]");
    if (map) draw(map);
  });
})();
