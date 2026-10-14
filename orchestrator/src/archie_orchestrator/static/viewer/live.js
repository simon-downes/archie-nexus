// Live transcript updates for an active session.
//
// Opens an EventSource on #transcript's data-live-url and applies each
// "fragment" message with htmx.swap(): a plain element is appended to
// #transcript, and elements carrying hx-swap-oob are placed by id. The server
// sends persisted event ids as SSE ids; on reconnect the browser resends the
// last one as Last-Event-ID, and this script also passes it as ?after= so a
// fresh EventSource resumes from the same point.
(function () {
  "use strict";

  var transcript = document.getElementById("transcript");
  var stateEl = document.getElementById("live-state");
  var wrap = document.getElementById("live-wrap");
  if (!transcript || !transcript.dataset.liveUrl || !window.EventSource) {
    return;
  }

  var baseUrl = transcript.dataset.liveUrl;
  var lastId = transcript.dataset.lastEventId || "";
  var source = null;
  var retryMs = 1000;
  var finished = false;

  function setState(state) {
    if (stateEl) {
      stateEl.textContent = state;
    }
    if (wrap) {
      wrap.className = "live is-" + state;
    }
  }

  function apply(html) {
    var placeholder = document.getElementById("no-messages");
    if (placeholder) {
      placeholder.remove();
    }
    htmx.swap(transcript, html, { swapStyle: "beforeend" });
  }

  function atBottom() {
    var doc = document.documentElement;
    return window.innerHeight + window.scrollY >= doc.scrollHeight - 80;
  }

  function onFragment(event) {
    var follow = atBottom();
    if (event.lastEventId) {
      lastId = event.lastEventId;
    }
    apply(event.data);
    if (follow) {
      window.scrollTo(0, document.documentElement.scrollHeight);
    }
  }

  function close() {
    finished = true;
    if (source) {
      source.close();
    }
  }

  function connect() {
    var url = baseUrl + (lastId ? "?after=" + encodeURIComponent(lastId) : "");
    source = new EventSource(url);
    source.onopen = function () {
      retryMs = 1000;
      setState("live");
    };
    source.addEventListener("fragment", onFragment);
    source.addEventListener("end", function (event) {
      close();
      if (event.data) {
        apply(event.data);
      }
      setState("ended");
    });
    source.addEventListener("reload", function () {
      close();
      window.location.reload();
    });
    source.onerror = function () {
      if (finished) {
        return;
      }
      source.close();
      setState("reconnecting");
      // Partial streaming blocks are replaced once the final message arrives.
      document.querySelectorAll(".streaming").forEach(function (el) {
        el.classList.add("stale");
      });
      window.setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 30000);
    };
  }

  setState("connecting");
  // Keep an expanded tool row expanded when a live update replaces it.
  document.body.addEventListener("htmx:oobBeforeSwap", function (event) {
    var target = event.detail.target;
    var incoming = event.detail.fragment && event.detail.fragment.firstElementChild;
    if (target && target.tagName === "DETAILS" && target.open && incoming) {
      incoming.setAttribute("open", "");
    }
  });
  connect();
})();
