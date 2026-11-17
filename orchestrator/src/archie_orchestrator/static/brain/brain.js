// Brain browser: after a page load, scroll the sidebar so the current file is in view.
// The sidebar scrolls on its own (brain.css), so only it moves, never the window.
(function () {
  "use strict";

  var side = document.querySelector(".brain-side");
  var current = side && side.querySelector('[aria-current="page"]');
  if (!current || side.scrollHeight <= side.clientHeight) {
    return;
  }
  var top = current.getBoundingClientRect().top - side.getBoundingClientRect().top;
  side.scrollTop += top - side.clientHeight / 3;
})();
