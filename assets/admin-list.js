(function () {
  var root = document.getElementById("queue-admin-list");
  if (!root) return;

  var base = "/api/v1/plugins/ctfd-queue";
  var listFeedback = document.getElementById("queue-list-feedback");
  var allRows = Array.prototype.slice.call(
    root.querySelectorAll(".queue-challenge-row")
  );

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function showFeedback(message, isError) {
    if (!listFeedback) return;
    listFeedback.style.display = "";
    listFeedback.className = "alert " + (isError ? "alert-danger" : "alert-success");
    listFeedback.textContent = message;
  }

  function fetchRequest(path, options) {
    var ctfd = window.CTFd;
    if (ctfd && typeof ctfd.fetch === "function") return ctfd.fetch(path, options);

    var requestPath = path;
    var urlRoot = window.init && typeof window.init.urlRoot === "string"
      ? window.init.urlRoot
      : "";
    if (path.charAt(0) === "/" && urlRoot) {
      requestPath = urlRoot.replace(/\/+$/, "") + path;
    }

    var method = String(options.method || "GET").toUpperCase();
    var safeMethod = method === "GET" || method === "HEAD" || method === "OPTIONS" || method === "TRACE";
    var csrfNonce = window.init && window.init.csrfNonce;
    if (!safeMethod && csrfNonce && options.headers &&
        !options.headers["CSRF-Token"]) {
      options.headers["CSRF-Token"] = csrfNonce;
    }
    if (typeof window.fetch !== "function") {
      return Promise.reject(new Error("This browser does not support fetch()."));
    }
    return window.fetch(requestPath, options);
  }

  function request(path, method, body) {
    var options = {
      method: method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" }
    };
    if (body !== undefined) options.body = JSON.stringify(body);

    return fetchRequest(path, options).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (payload) {
        if (!response.ok || !payload || payload.success !== true) {
          var message = payload && (payload.message || payload.description || payload.error);
          if (!message && payload && payload.errors) {
            message = typeof payload.errors === "string"
              ? payload.errors
              : JSON.stringify(payload.errors);
          }
          throw new Error(message || (response.status ? "HTTP " + response.status : "Request failed."));
        }
        return payload.data;
      });
    });
  }

  function getRow(challengeId) {
    return allRows.find(function (row) {
      return Number(row.dataset.challengeId) === Number(challengeId);
    });
  }

  function challengeName(row) {
    var title = row && row.querySelector(".queue-challenge-title");
    return title ? title.textContent.trim() : "Challenge " + (row ? row.dataset.challengeId : "");
  }

  function updateRowState(challengeId, state) {
    var row = getRow(challengeId);
    if (!row || !state) return;
    var waiting = row.querySelector(".queue-waiting-count");
    var selected = row.querySelector(".queue-selected-count");
    if (waiting && Array.isArray(state.waiting)) waiting.textContent = state.waiting.length;
    if (selected && Array.isArray(state.selected)) selected.textContent = state.selected.length;
  }

  function updateSummaryCounts() {
    var waitingTotal = 0;
    var selectedTotal = 0;
    allRows.forEach(function (row) {
      waitingTotal += Number((row.querySelector(".queue-waiting-count") || {}).textContent) || 0;
      selectedTotal += Number((row.querySelector(".queue-selected-count") || {}).textContent) || 0;
    });
    var waitingEl = document.getElementById("queue-total-waiting");
    var selectedEl = document.getElementById("queue-total-selected");
    if (waitingEl) waitingEl.textContent = waitingTotal;
    if (selectedEl) selectedEl.textContent = selectedTotal;
  }

  function advanceOne(challengeId) {
    return request(base + "/admin/challenges/" + challengeId + "/advance", "POST", {})
      .then(function (data) {
        if (data && data.state) updateRowState(challengeId, data.state);
        return data;
      });
  }

  function selectedCheckboxes() {
    return Array.prototype.slice.call(
      root.querySelectorAll(".queue-challenge-checkbox:checked")
    );
  }

  function updateSelectionUi() {
    var selectedCount = selectedCheckboxes().length;
    var count = document.getElementById("queue-selected-challenge-count");
    var button = document.getElementById("queue-advance-selected");
    if (count) count.textContent = selectedCount;
    if (button) button.disabled = selectedCount === 0;
    syncSelectVisible();
  }

  function visibleRows() {
    return allRows.filter(function (row) { return !row.hidden; });
  }

  function syncSelectVisible() {
    var selector = document.getElementById("queue-select-visible");
    if (!selector) return;
    var rows = visibleRows();
    var checked = rows.filter(function (row) {
      var checkbox = row.querySelector(".queue-challenge-checkbox");
      return checkbox && checkbox.checked;
    }).length;
    selector.checked = rows.length > 0 && checked === rows.length;
    selector.indeterminate = checked > 0 && checked < rows.length;
  }

  function applyFilters() {
    var nameInput = document.getElementById("queue-filter-name");
    var visibilityInput = document.getElementById("queue-filter-visibility");
    var modeInput = document.getElementById("queue-filter-mode");
    if (!nameInput || !visibilityInput || !modeInput) return;

    var nameFilter = (nameInput.value || "").trim().toLocaleLowerCase();
    var visibilityFilter = visibilityInput.value;
    var modeFilter = modeInput.value;
    var shown = 0;

    allRows.forEach(function (row) {
      var matchesName = !nameFilter ||
        (row.dataset.challengeName || "").toLocaleLowerCase().indexOf(nameFilter) !== -1;
      var matchesVisibility = visibilityFilter === "all" ||
        row.dataset.visibility === visibilityFilter;
      var matchesMode = modeFilter === "all" || row.dataset.mode === modeFilter;
      row.hidden = !(matchesName && matchesVisibility && matchesMode);
      if (!row.hidden) shown += 1;
    });

    var empty = document.getElementById("queue-no-filter-results");
    if (empty) empty.hidden = shown !== 0;
    var resultCount = document.getElementById("queue-filter-result-count");
    if (resultCount) resultCount.textContent = shown + " of " + allRows.length + " challenges";
    syncSelectVisible();
  }

  function copyUsingFallback(text) {
    var textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.setAttribute("aria-hidden", "true");
    textarea.style.position = "fixed";
    textarea.style.top = "0";
    textarea.style.left = "-10000px";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    var copied = false;
    try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
    textarea.remove();
    if (!copied) throw new Error("Clipboard access was blocked by the browser.");
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text).catch(function () {
        copyUsingFallback(text);
      });
    }
    try {
      copyUsingFallback(text);
      return Promise.resolve();
    } catch (error) {
      return Promise.reject(error);
    }
  }

  function copyFlag(challengeId, button) {
    var row = getRow(challengeId);
    var name = challengeName(row);
    button.disabled = true;
    request(base + "/admin/challenges/" + challengeId + "/flag")
      .then(function (data) {
        if (!data || typeof data.content !== "string" || !data.content) {
          throw new Error("No copyable flag is configured for this challenge.");
        }
        // Do not render or log the flag; hold it only long enough to copy it.
        return copyToClipboard(data.content);
      })
      .then(function () {
        button.textContent = "Copied ✓";
        showFeedback("Flag copied for " + name + ".", false);
        window.setTimeout(function () { button.textContent = "Copy flag"; }, 1800);
      })
      .catch(function (error) {
        showFeedback("Could not copy the flag for " + name + ": " + error.message, true);
      })
      .then(function () { button.disabled = false; });
  }

  function setAdvanceButtonsDisabled(disabled) {
    root.querySelectorAll('[data-action="advance"]').forEach(function (button) {
      button.disabled = disabled;
    });
    var bulk = document.getElementById("queue-advance-selected");
    if (bulk) bulk.disabled = disabled || selectedCheckboxes().length === 0;
  }

  function advanceMany(ids) {
    if (!ids.length) return Promise.resolve({ succeeded: [], failed: [] });
    var outcome = { succeeded: [], failed: [] };

    function next(index) {
      if (index >= ids.length) return Promise.resolve(outcome);
      var id = ids[index];
      return advanceOne(id).then(function () {
        outcome.succeeded.push(id);
      }).catch(function (error) {
        outcome.failed.push({ id: id, message: error.message || "Request failed." });
      }).then(function () {
        return next(index + 1);
      });
    }
    return next(0);
  }

  function reportAdvanceResults(outcome) {
    updateSummaryCounts();
    updateSelectionUi();
    var succeeded = outcome.succeeded.length;
    var failed = outcome.failed.length;
    if (failed === 0) {
      showFeedback("Advanced " + succeeded + (succeeded === 1 ? " queue." : " queues."), false);
    } else {
      var details = outcome.failed.map(function (failure) {
        return challengeName(getRow(failure.id)) + ": " + failure.message;
      }).join(" | ");
      showFeedback("Advanced " + succeeded + " queue(s); " + failed + " failed. " + details, true);
    }
  }

  root.addEventListener("change", function (event) {
    if (event.target.matches(".queue-challenge-checkbox")) updateSelectionUi();
  });

  root.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var challengeId = Number(button.dataset.challengeId);
    if (!Number.isInteger(challengeId) || challengeId <= 0) return;

    if (button.dataset.action === "advance") {
      button.disabled = true;
      advanceOne(challengeId)
        .then(function () {
          updateSummaryCounts();
          showFeedback("Queue advanced for " + challengeName(getRow(challengeId)) + ".", false);
        })
        .catch(function (error) {
          showFeedback("Could not advance " + challengeName(getRow(challengeId)) + ": " + error.message, true);
        })
        .then(function () { button.disabled = false; });
    } else if (button.dataset.action === "copy-flag") {
      copyFlag(challengeId, button);
    }
  });

  var nameFilter = document.getElementById("queue-filter-name");
  var visibilityFilter = document.getElementById("queue-filter-visibility");
  var modeFilter = document.getElementById("queue-filter-mode");
  if (nameFilter) nameFilter.addEventListener("input", applyFilters);
  if (visibilityFilter) visibilityFilter.addEventListener("change", applyFilters);
  if (modeFilter) modeFilter.addEventListener("change", applyFilters);

  var reset = document.getElementById("queue-filter-reset");
  if (reset) {
    reset.addEventListener("click", function () {
      nameFilter.value = "";
      visibilityFilter.value = "all";
      modeFilter.value = "all";
      applyFilters();
    });
  }

  var selectVisible = document.getElementById("queue-select-visible");
  if (selectVisible) {
    selectVisible.addEventListener("change", function () {
      visibleRows().forEach(function (row) {
        var checkbox = row.querySelector(".queue-challenge-checkbox");
        if (checkbox) checkbox.checked = selectVisible.checked;
      });
      updateSelectionUi();
    });
  }

  var bulkAdvance = document.getElementById("queue-advance-selected");
  if (bulkAdvance) {
    bulkAdvance.addEventListener("click", function () {
      var ids = selectedCheckboxes().map(function (checkbox) { return Number(checkbox.value); });
      if (!ids.length) return;
      if (!window.confirm("Advance the queue for " + ids.length + " selected challenge(s)?")) return;

      setAdvanceButtonsDisabled(true);
      showFeedback("Advancing " + ids.length + " selected queue(s)…", false);
      advanceMany(ids).then(function (outcome) {
        selectedCheckboxes().forEach(function (checkbox) { checkbox.checked = false; });
        reportAdvanceResults(outcome);
      }).catch(function (error) {
        showFeedback("Bulk queue advance failed: " + error.message, true);
      }).then(function () {
        setAdvanceButtonsDisabled(false);
        updateSelectionUi();
      });
    });
  }

  applyFilters();
  updateSelectionUi();
  updateSummaryCounts();
})();
