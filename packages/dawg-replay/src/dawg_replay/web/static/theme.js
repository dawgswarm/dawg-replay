// Тема до отрисовки страницы: выбор человека из localStorage, иначе системная.
(function () {
  try {
    var t = localStorage.getItem("dawg-replay-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* хранилище недоступно: системная тема */ }
})();
