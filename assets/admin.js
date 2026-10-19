(function () {
  var root = document.getElementById("queue-admin-detail");
  if (!root) return;
  var challengeId = parseInt(root.dataset.challengeId, 10);
  var base = "/api/v1/plugins/ctfd-queue";

  var recentFailureLogs = {};

  function logApiFailure(error, operation) {
    if (!window.console) return;
    var path = error && error.queuePath ? error.queuePath : "(unknown path)";
    var status = error && Number.isFinite(Number(error.queueStatus))
      ? Number(error.queueStatus)
      : 0;
    var message = error && error.message ? error.message : "Unknown request error";
    var key = operation + "|" + path + "|" + status + "|" + message;
    var now = Date.now();
    if (recentFailureLogs[key] && now - recentFailureLogs[key] < 60000) return;
    recentFailureLogs[key] = now;
    var logger = status === 0 || status >= 500 ? console.error : console.warn;
    if (typeof logger === "function") {
      logger.call(console, "[Queue Challenge] " + operation + " failed", {
        path: path,
        status: status,
        message: message
      });
    }
  }

  function fetchRequest(path, options) {
    var ctfd = window.CTFd;
    if (ctfd && typeof ctfd.fetch === "function") {
      return ctfd.fetch(path, options);
    }

    // Admin plugin scripts may execute before CTFd.js is defined. Fall back
    // to native fetch while preserving CTFd's URL root and CSRF protection.
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

  function api(path, method, body) {
    var options = {
      method: method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" }
    };
    if (body !== undefined) options.body = JSON.stringify(body);
    return fetchRequest(path, options).then(function (response) {
      if (!response || typeof response.json !== "function") {
        var invalidResponse = new Error("Request failed.");
        invalidResponse.queuePath = path;
        invalidResponse.queueStatus = 0;
        throw invalidResponse;
      }
      return response.json().catch(function () {
        return null;
      }).then(function (payload) {
        if (!response.ok || !payload || payload.success !== true) {
          var message = payload && (payload.message || payload.description || payload.error);
          if (!message && payload && payload.errors) {
            if (typeof payload.errors === "string") {
              message = payload.errors;
            } else if (Array.isArray(payload.errors)) {
              message = payload.errors.join("; ");
            } else if (typeof payload.errors === "object") {
              message = Object.keys(payload.errors).map(function (key) {
                var value = payload.errors[key];
                return key + ": " + (Array.isArray(value) ? value.join(", ") : String(value));
              }).join("; ");
            }
          }
          if (!message) {
            message = response.status ? "HTTP " + response.status : "Request failed.";
          }
          var apiError = new Error(message);
          apiError.queuePath = path;
          apiError.queueStatus = Number(response.status) || 0;
          throw apiError;
        }
        return payload.data;
      });
    }).catch(function (error) {
      if (!error.queuePath) error.queuePath = path;
      if (!Number.isFinite(Number(error.queueStatus))) error.queueStatus = 0;
      throw error;
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function timeLabel(value) {
    if (!value) return "—";
    try { return new Date(value + (value.endsWith("Z") ? "" : "Z")).toLocaleString(); }
    catch (_) { return value; }
  }

  function duration(seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    return [h, m, s].map(function (part) {
      return String(part).padStart(2, "0");
    }).join(":");
  }

  var liveCounters = {
    elapsedSeconds: Math.max(0, Number(root.dataset.elapsedSeconds) || 0),
    nextDrawSeconds: Math.max(0, Number(root.dataset.nextDrawInSeconds) || 0),
    syncedAt: Date.now(),
    hasDrawn: root.dataset.hasDrawn === "true",
    automatic: root.dataset.queueMode === "automatic"
  };
  var refreshInProgress = false;
  var autoRefreshTimer = null;
  var drawRefreshTimer = null;
  var drawRefreshTriggered = false;
  var autoRefreshStorageKey = "queue-challenge-admin-auto-refresh:" + challengeId;

  function currentCounterValues() {
    var elapsedSinceSync = Math.max(0, Math.floor((Date.now() - liveCounters.syncedAt) / 1000));
    return {
      elapsedSeconds: liveCounters.elapsedSeconds + elapsedSinceSync,
      nextDrawSeconds: Math.max(0, liveCounters.nextDrawSeconds - elapsedSinceSync)
    };
  }

  function renderCounters() {
    var elapsed = document.getElementById("queue-elapsed");
    var nextDraw = document.getElementById("queue-next-draw");
    var current = currentCounterValues();
    if (elapsed) elapsed.textContent = liveCounters.hasDrawn
      ? duration(current.elapsedSeconds)
      : "No draw yet";
    if (nextDraw) nextDraw.textContent = liveCounters.automatic
      ? duration(current.nextDrawSeconds)
      : "Manual";
  }

  function tickCounters() {
    renderCounters();
    if (!liveCounters.automatic || refreshInProgress) return;

    var current = currentCounterValues();
    if (current.nextDrawSeconds > 0) {
      drawRefreshTriggered = false;
      return;
    }
    if (drawRefreshTriggered || drawRefreshTimer !== null) return;

    drawRefreshTriggered = true;
    refresh().then(function () {
      if (!liveCounters.automatic) return;
      // The scheduler can be a few seconds behind the visual countdown.
      // Retry until the backend reports the next round/countdown.
      if (currentCounterValues().nextDrawSeconds <= 0 && drawRefreshTimer === null) {
        drawRefreshTriggered = true;
        drawRefreshTimer = window.setTimeout(function () {
          drawRefreshTimer = null;
          drawRefreshTriggered = false;
          tickCounters();
        }, 5000);
      }
    });
  }

  function readAutoRefreshPreference() {
    try {
      return window.localStorage.getItem(autoRefreshStorageKey) === "true";
    } catch (_) {
      return false;
    }
  }

  function saveAutoRefreshPreference(enabled) {
    try {
      window.localStorage.setItem(autoRefreshStorageKey, enabled ? "true" : "false");
    } catch (_) {
      // The preference still applies for the current page if browser storage is disabled.
    }
  }

  function setAutoRefresh(enabled) {
    if (autoRefreshTimer !== null) {
      window.clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
    if (enabled) {
      autoRefreshTimer = window.setInterval(refresh, 120000);
    }
  }

  function showFeedback(message, isError) {
    var feedback = document.getElementById("queue-admin-feedback");
    if (!feedback) return;
    feedback.style.display = "";
    feedback.className = "alert " + (isError ? "alert-danger" : "alert-success");
    feedback.textContent = message;
  }

  function renderEntries(targetId, entries, isSelected) {
    var target = document.getElementById(targetId);
    if (!target) return;
    if (!entries.length) {
      target.innerHTML = '<tr><td colspan="' + (isSelected ? "5" : "5") + '" class="queue-empty-row">' +
        (isSelected ? "No participants are currently selected." : "The waiting queue is empty.") +
        "</td></tr>";
      return;
    }
    target.innerHTML = entries.map(function (entry) {
      var identity = escapeHtml(entry.username) + ' <span class="text-muted small">#' + escapeHtml(entry.user_id) + "</span>";
      var team = escapeHtml(entry.team_name || "—");
      if (isSelected) {
        return "<tr><td>" + identity + '</td><td>' + team + '</td><td>' +
          escapeHtml(entry.round_number || "—") + '</td><td>' + escapeHtml(timeLabel(entry.selected_at)) +
          '</td><td class="text-right"><button class="btn btn-sm btn-outline-danger" data-action="remove" data-entry-id="' +
          entry.id + '">Remove from queue</button></td></tr>';
      }
      return '<tr><td><span class="queue-position">' + escapeHtml(entry.position) + "</span>" + (Number(entry.position) === 1 ? ' <span class="queue-badge queue-badge-auto ml-2">Next</span>' : "") + "</td><td>" +
        identity + "</td><td>" + team + "</td><td>" + escapeHtml(timeLabel(entry.joined_at)) +
        '</td><td class="queue-row-actions">' +
        '<button class="btn btn-sm btn-outline-secondary" data-action="move-up" data-entry-id="' + entry.id + '" aria-label="Move up">↑</button>' +
        '<button class="btn btn-sm btn-outline-secondary" data-action="move-down" data-entry-id="' + entry.id + '" aria-label="Move down">↓</button>' +
        '<button class="btn btn-sm btn-outline-danger" data-action="remove" data-entry-id="' + entry.id + '">Remove</button>' +
        "</td></tr>";
    }).join("");
  }

  function renderState(state) {
    var challenge = state.challenge;
    document.getElementById("queue-waiting-count").textContent = state.waiting.length;
    document.getElementById("queue-selected-count").textContent = state.selected.length;
    document.getElementById("queue-round-number").textContent = challenge.round_number;
    liveCounters.hasDrawn = Boolean(challenge.has_drawn);
    liveCounters.elapsedSeconds = Math.max(0, Number(challenge.elapsed_seconds) || 0);
    liveCounters.automatic = challenge.queue_mode === "automatic";
    liveCounters.nextDrawSeconds = Math.max(0, Number(challenge.next_draw_in_seconds) || 0);
    liveCounters.syncedAt = Date.now();
    if (drawRefreshTimer !== null) {
      window.clearTimeout(drawRefreshTimer);
      drawRefreshTimer = null;
    }
    // A positive countdown means a new draw has been registered. Re-arm the
    // zero-crossing trigger for the next automatic round.
    drawRefreshTriggered = false;
    renderCounters();

    var mode = document.getElementById("queue-mode");
    var automatic = challenge.queue_mode === "automatic";
    mode.textContent = automatic ? "Automatic" : "Manual";
    mode.className = "queue-badge queue-mode-badge " +
      (automatic ? "queue-mode-automatic" : "queue-mode-manual");
    document.getElementById("queue-batch-size").textContent = challenge.batch_size;
    document.getElementById("queue-interval").textContent = challenge.interval_seconds + " seconds";
    renderEntries("queue-selected-rows", state.selected, true);
    renderEntries("queue-waiting-rows", state.waiting, false);
  }

  function refresh() {
    if (refreshInProgress) return Promise.resolve(false);

    refreshInProgress = true;
    var refreshButton = document.getElementById("queue-refresh");
    var refreshStatus = document.getElementById("queue-refresh-status");
    var idleLabel = "↻ Refresh";
    if (refreshButton) {
      idleLabel = refreshButton.getAttribute("data-idle-label") || refreshButton.innerHTML;
      refreshButton.setAttribute("data-idle-label", idleLabel);
      refreshButton.disabled = true;
      refreshButton.setAttribute("aria-busy", "true");
      refreshButton.innerHTML = '<span aria-hidden="true">↻</span> Refreshing…';
    }
    if (refreshStatus) refreshStatus.textContent = "Refreshing queue data…";

    var stateUrl = base + "/admin/challenges/" + challengeId + "/state?_refresh=" + Date.now();
    var timeoutId = null;
    var controller = typeof window.AbortController === "function"
      ? new window.AbortController()
      : null;
    var request = window.fetch(stateUrl, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      return response.json().catch(function () {
        return null;
      }).then(function (payload) {
        if (!response.ok || !payload || payload.success !== true || !payload.data) {
          var message = payload && (payload.message || payload.description || payload.error);
          if (!message && payload && payload.errors) {
            message = typeof payload.errors === "string"
              ? payload.errors
              : JSON.stringify(payload.errors);
          }
          var error = new Error(message || ("Queue refresh failed (HTTP " + response.status + ")."));
          error.queuePath = stateUrl;
          error.queueStatus = Number(response.status) || 0;
          throw error;
        }
        return payload.data;
      });
    }).catch(function (error) {
      if (!error.queuePath) error.queuePath = stateUrl;
      if (!Number.isFinite(Number(error.queueStatus))) error.queueStatus = 0;
      throw error;
    });

    var timeout = new Promise(function (_, reject) {
      timeoutId = window.setTimeout(function () {
        if (controller) controller.abort();
        var error = new Error("The queue API did not respond within 15 seconds.");
        error.queuePath = stateUrl;
        error.queueStatus = 0;
        reject(error);
      }, 15000);
    });

    return Promise.race([request, timeout])
      .then(function (state) {
        if (!state || !state.challenge || !Array.isArray(state.waiting) ||
            !Array.isArray(state.selected)) {
          var error = new Error("The queue API returned an invalid state response.");
          error.queuePath = stateUrl;
          error.queueStatus = 0;
          throw error;
        }
        renderState(state);
        if (refreshStatus) {
          refreshStatus.textContent = "Last refreshed at " + new Date().toLocaleTimeString();
        }
        return true;
      })
      .catch(function (error) {
        logApiFailure(error, "admin state refresh");
        if (refreshStatus) {
          refreshStatus.textContent = "Refresh failed at " + new Date().toLocaleTimeString();
        }
        showFeedback("Could not refresh queue data: " + (error.message || "Unknown error"), true);
        return false;
      })
      .then(function (succeeded) {
        if (timeoutId !== null) window.clearTimeout(timeoutId);
        refreshInProgress = false;
        if (refreshButton) {
          refreshButton.disabled = false;
          refreshButton.removeAttribute("aria-busy");
          refreshButton.innerHTML = idleLabel;
        }
        return succeeded;
      }, function (error) {
        // Keep cleanup guaranteed even if an unexpected exception escapes the handlers above.
        if (timeoutId !== null) window.clearTimeout(timeoutId);
        refreshInProgress = false;
        if (refreshButton) {
          refreshButton.disabled = false;
          refreshButton.removeAttribute("aria-busy");
          refreshButton.innerHTML = idleLabel;
        }
        logApiFailure(error, "admin state refresh cleanup");
        if (refreshStatus) {
          refreshStatus.textContent = "Refresh failed at " + new Date().toLocaleTimeString();
        }
        showFeedback("Could not refresh queue data: " + (error.message || "Unknown error"), true);
        return false;
      });
  }

  function runAction(path, method, body, successMessage) {
    return api(path, method, body).then(function (data) {
      if (data && data.challenge && data.waiting) renderState(data);
      else if (data && data.state) renderState(data.state);
      showFeedback(successMessage, false);
      return refresh().then(function () { return true; });
    }).catch(function (error) {
      logApiFailure(error, "admin queue action");
      showFeedback(error.message, true);
      return false;
    });
  }

  var refreshButton = document.getElementById("queue-refresh");
  if (refreshButton) {
    refreshButton.addEventListener("click", function (event) {
      event.preventDefault();
      refresh();
    });
  }

  var autoRefreshCheckbox = document.getElementById("queue-auto-refresh");
  if (autoRefreshCheckbox) {
    autoRefreshCheckbox.checked = readAutoRefreshPreference();
    setAutoRefresh(autoRefreshCheckbox.checked);
    autoRefreshCheckbox.addEventListener("change", function () {
      saveAutoRefreshPreference(autoRefreshCheckbox.checked);
      setAutoRefresh(autoRefreshCheckbox.checked);
      showFeedback(
        autoRefreshCheckbox.checked
          ? "Automatic refresh enabled (every 2 minutes)."
          : "Automatic refresh disabled.",
        false
      );
    });
  }

  var advance = document.getElementById("queue-advance");
  if (advance) {
    advance.addEventListener("click", function () {
      advance.disabled = true;
      runAction(base + "/admin/challenges/" + challengeId + "/advance", "POST", {}, "Queue advanced.")
        .then(function () { advance.disabled = false; });
    });
  }

  root.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var entryId = parseInt(button.dataset.entryId, 10);
    var action = button.dataset.action;
    button.disabled = true;
    if (action === "move-up" || action === "move-down") {
      runAction(base + "/admin/entries/" + entryId + "/move", "POST", {
        direction: action === "move-up" ? "up" : "down"
      }, "Queue order updated.").then(function () { button.disabled = false; });
    } else if (action === "remove") {
      if (!window.confirm("Remove this participant from the active queue?")) {
        button.disabled = false;
        return;
      }
      runAction(base + "/admin/entries/" + entryId, "DELETE", undefined, "Participant removed.")
        .then(function () { button.disabled = false; });
    }
  });

  var addForm = document.getElementById("queue-add-user-form");
  var lookupMode = document.getElementById("queue-add-lookup-mode");
  var usernameWrap = document.getElementById("queue-add-username-wrap");
  var userIdWrap = document.getElementById("queue-add-user-id-wrap");
  var usernameInput = document.getElementById("queue-add-username");
  var userIdInput = document.getElementById("queue-add-user-id");
  var suggestionList = document.getElementById("queue-user-suggestions");
  var suggestionTimer = null;
  var suggestionRequestId = 0;

  function updateLookupMode() {
    var byUsername = !lookupMode || lookupMode.value === "username";
    if (usernameWrap) usernameWrap.hidden = !byUsername;
    if (userIdWrap) userIdWrap.hidden = byUsername;
    if (usernameInput) {
      usernameInput.disabled = !byUsername;
      usernameInput.required = byUsername;
    }
    if (userIdInput) {
      userIdInput.disabled = byUsername;
      userIdInput.required = !byUsername;
    }
  }

  function fetchUsernameSuggestions() {
    if (!usernameInput || !suggestionList || !lookupMode || lookupMode.value !== "username") return;
    var query = usernameInput.value.trim();
    suggestionRequestId += 1;
    var requestId = suggestionRequestId;
    if (suggestionTimer !== null) window.clearTimeout(suggestionTimer);
    if (!query) {
      suggestionList.innerHTML = "";
      return;
    }

    suggestionTimer = window.setTimeout(function () {
      api(base + "/admin/users/suggest?q=" + encodeURIComponent(query))
        .then(function (data) {
          if (requestId !== suggestionRequestId ||
              usernameInput.value.trim() !== query ||
              lookupMode.value !== "username") return;
          var users = data && Array.isArray(data.users) ? data.users : [];
          suggestionList.innerHTML = users.map(function (user) {
            return '<option value="' + escapeHtml(user.username) + '"></option>';
          }).join("");
        })
        .catch(function (error) {
          logApiFailure(error, "username suggestions");
        });
    }, 180);
  }

  if (lookupMode) lookupMode.addEventListener("change", function () {
    updateLookupMode();
    if (suggestionList) suggestionList.innerHTML = "";
    if (lookupMode.value === "username") fetchUsernameSuggestions();
  });
  if (usernameInput) usernameInput.addEventListener("input", fetchUsernameSuggestions);
  updateLookupMode();

  if (addForm) {
    addForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var body;
      if (!lookupMode || lookupMode.value === "username") {
        var username = usernameInput ? usernameInput.value.trim() : "";
        if (!username) {
          showFeedback("Enter a valid CTFd username.", true);
          return;
        }
        body = { username: username };
      } else {
        var userId = userIdInput ? parseInt(userIdInput.value, 10) : NaN;
        if (!Number.isInteger(userId) || userId < 1) {
          showFeedback("Enter a valid CTFd user ID.", true);
          return;
        }
        body = { user_id: userId };
      }

      runAction(base + "/admin/challenges/" + challengeId + "/entries", "POST", body,
        "Participant added to the queue.").then(function (succeeded) {
          if (!succeeded) return;
          if (usernameInput) usernameInput.value = "";
          if (userIdInput) userIdInput.value = "";
          if (suggestionList) suggestionList.innerHTML = "";
        });
    });
  }

  // Keep the clock independent from API requests, including a stalled first refresh.
  renderCounters();
  window.setInterval(tickCounters, 1000);
  refresh();
})();
