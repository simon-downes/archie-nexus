// Overlays for the session log viewer: tool call details and subagent transcripts.
//
// Rows carry data-dialog="<dialog id>". Clicking one opens that <dialog> as a
// modal on top of any already open, so a tool inside a subagent opens over the
// subagent's transcript. Escape closes only the top one. Each dialog's header
// shows a breadcrumb built from the open stack: the
// session, then each open level's data-crumb. Clicking a crumb closes the levels
// above it.
//
// Live updates (live.js) replace a tool's row and dialog by id and then
// dispatch "viewer:fragment". A replaced dialog that was open is reopened in
// place, so the user keeps reading the updated content.
(function () {
  "use strict";

  var stack = []; // open dialog ids, bottom first
  var sessionLabel = (document.querySelector(".session-id") || {}).textContent || "Session";
  sessionLabel = sessionLabel.trim().split(/\s/)[0] || "Session";

  function byId(id) {
    return document.getElementById(id);
  }

  function crumbLabel(dialog) {
    return dialog.getAttribute("data-crumb") || "Details";
  }

  function renderCrumbs() {
    stack.forEach(function (id, depth) {
      var dialog = byId(id);
      if (!dialog) {
        return;
      }
      var nav = dialog.querySelector(".crumbs");
      if (!nav) {
        return;
      }
      nav.textContent = "";
      var levels = [{ label: sessionLabel, depth: -1 }];
      for (var i = 0; i <= depth; i++) {
        var level = byId(stack[i]);
        levels.push({ label: level ? crumbLabel(level) : "…", depth: i });
      }
      levels.forEach(function (level, n) {
        if (n > 0) {
          var sep = document.createElement("span");
          sep.className = "crumb-sep";
          sep.textContent = "›";
          nav.appendChild(sep);
        }
        var current = level.depth === depth;
        var el = document.createElement(current ? "span" : "button");
        el.className = current ? "crumb is-current" : "crumb";
        el.textContent = level.label;
        if (!current) {
          el.type = "button";
          el.setAttribute("data-crumb-depth", String(level.depth));
        }
        nav.appendChild(el);
      });
    });
  }

  function open(id) {
    var dialog = byId(id);
    if (!dialog || dialog.open) {
      return;
    }
    stack.push(id);
    renderCrumbs();
    dialog.showModal();
  }

  // Close every level above `depth` (-1 closes all).
  function closeAbove(depth) {
    while (stack.length > depth + 1) {
      var dialog = byId(stack[stack.length - 1]);
      if (dialog && dialog.open) {
        dialog.close(); // the close listener pops the stack
      } else {
        stack.pop();
      }
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) {
      return;
    }
    var crumb = target.closest("[data-crumb-depth]");
    if (crumb) {
      closeAbove(parseInt(crumb.getAttribute("data-crumb-depth"), 10));
      return;
    }
    if (target.closest("[data-close]")) {
      closeAbove(stack.length - 2);
      return;
    }
    var row = target.closest("[data-dialog]");
    if (row) {
      open(row.getAttribute("data-dialog"));
      return;
    }
    // A click on the backdrop lands on the dialog element itself.
    if (target.tagName === "DIALOG" && target.open && stack[stack.length - 1] === target.id) {
      var box = target.getBoundingClientRect();
      var inside =
        event.clientX >= box.left && event.clientX <= box.right &&
        event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) {
        closeAbove(stack.length - 2);
      }
    }
  });

  // Escape closes the top level. Handled here rather than by the dialog's native
  // cancel: Chrome does not deliver close requests to a dialog that a live swap
  // reopened without a user gesture, and one key press must close one level.
  var escapeAt = -1;
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !stack.length) {
      return;
    }
    event.preventDefault();
    escapeAt = event.timeStamp;
    closeAbove(stack.length - 2);
  });
  document.addEventListener(
    "cancel",
    function (event) {
      if (!(event.target instanceof HTMLDialogElement)) {
        return;
      }
      event.preventDefault();
      if (event.timeStamp - escapeAt > 100) {
        closeAbove(stack.length - 2);
      }
    },
    true
  );

  // dialog.close() (and any native close) ends here.
  document.addEventListener(
    "close",
    function (event) {
      var dialog = event.target;
      if (!(dialog instanceof HTMLDialogElement)) {
        return;
      }
      var at = stack.lastIndexOf(dialog.id);
      if (at !== -1 && !dialog.hasAttribute("data-reopening")) {
        stack.splice(at, 1);
      }
    },
    true
  );

  // A live swap replaced elements that may hold open dialogs. Reopen them in
  // stack order: from the lowest replaced level up, every level is closed and
  // reopened, so nested overlays keep their stacking order.
  function restore() {
    stack = stack.filter(function (id) {
      return byId(id);
    });
    var first = -1;
    for (var i = 0; i < stack.length; i++) {
      if (!byId(stack[i]).open) {
        first = i;
        break;
      }
    }
    if (first === -1) {
      return;
    }
    stack.slice(first).forEach(function (id) {
      var dialog = byId(id);
      if (dialog.open) {
        dialog.setAttribute("data-reopening", "");
        dialog.close();
        dialog.removeAttribute("data-reopening");
      }
    });
    stack.slice(first).forEach(function (id) {
      byId(id).showModal();
    });
    renderCrumbs();
  }

  document.addEventListener("viewer:fragment", restore);
})();
