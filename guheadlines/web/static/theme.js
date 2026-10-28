// Light or dark: the visitor's choice (the switch in the top bar), else
// their device setting, else dark. Runs before the page is drawn.
(function () {
  var root = document.documentElement, key = "theme";
  function device() {
    if (!window.matchMedia) return null;
    if (matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    if (matchMedia("(prefers-color-scheme: light)").matches) return "light";
    return null;
  }
  function saved() {
    try {
      var theme = localStorage.getItem(key);
      return theme === "light" || theme === "dark" ? theme : null;
    } catch (e) { return null; }
  }
  root.dataset.theme = saved() || device() || "dark";

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    function show() {
      button.setAttribute("aria-checked", root.dataset.theme === "dark" ? "true" : "false");
    }
    button.addEventListener("click", function () {
      var theme = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = theme;
      try {
        // Remember a choice only when it differs from the device setting,
        // so switching back means "follow my device" again.
        if (theme === (device() || "dark")) localStorage.removeItem(key);
        else localStorage.setItem(key, theme);
      } catch (e) {}
      show();
    });
    if (window.matchMedia) {  // follow the device while no choice is saved
      var query = matchMedia("(prefers-color-scheme: dark)");
      var follow = function () { if (!saved()) { root.dataset.theme = device() || "dark"; show(); } };
      if (query.addEventListener) query.addEventListener("change", follow);
      else if (query.addListener) query.addListener(follow);
    }
    show();
    button.hidden = false;
  });
})();
