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

  function api(path, method, body) {
    var options = {
      method: method || "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" }
    };
    if (body !== undefined) options.body = JSON.stringify(body);
    return CTFd.fetch(path, options).then(function (response) {
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
    seconds = Math.max(0, Number(seconds) || 0);
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.floor(seconds % 60);
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + s + "s";
    return s + "s";
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
          entry.id + '">Revoke selection</button></td></tr>';
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
    document.getElementById("queue-elapsed").textContent = challenge.has_drawn ? duration(challenge.elapsed_seconds) : "No draw yet";
    document.getElementById("queue-mode").textContent = challenge.queue_mode === "automatic" ? "Automatic" : "Manual";
    document.getElementById("queue-batch-size").textContent = challenge.batch_size;
    document.getElementById("queue-interval").textContent = challenge.interval_seconds + " seconds";
    document.getElementById("queue-next-draw").textContent =
      challenge.queue_mode === "automatic" ? duration(challenge.next_draw_in_seconds) : "Manual";
    renderEntries("queue-selected-rows", state.selected, true);
    renderEntries("queue-waiting-rows", state.waiting, false);
  }

  function refresh() {
    return api(base + "/admin/challenges/" + challengeId + "/state")
      .then(renderState)
      .catch(function (error) {
        logApiFailure(error, "admin state refresh");
        showFeedback(error.message, true);
      });
  }

  function runAction(path, method, body, successMessage) {
    return api(path, method, body).then(function (data) {
      if (data && data.challenge && data.waiting) renderState(data);
      else if (data && data.state) renderState(data.state);
      showFeedback(successMessage, false);
      return refresh();
    }).catch(function (error) {
      logApiFailure(error, "admin queue action");
      showFeedback(error.message, true);
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
      if (!window.confirm("Remove this participant from the queue or revoke their selection?")) {
        button.disabled = false;
        return;
      }
      runAction(base + "/admin/entries/" + entryId, "DELETE", undefined, "Participant removed.")
        .then(function () { button.disabled = false; });
    }
  });

  var addForm = document.getElementById("queue-add-user-form");
  if (addForm) {
    addForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var input = document.getElementById("queue-add-user-id");
      var userId = parseInt(input.value, 10);
      if (!Number.isInteger(userId) || userId < 1) {
        showFeedback("Enter a valid CTFd user ID.", true);
        return;
      }
      runAction(base + "/admin/challenges/" + challengeId + "/entries", "POST", {
        user_id: userId
      }, "Participant added to the queue.").then(function () { input.value = ""; });
    });
  }

  refresh();
  window.setInterval(refresh, 15000);
})();
