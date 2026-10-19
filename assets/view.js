// Queue UI plus CTFd's standard challenge submission hook.
CTFd._internal.challenge.data = undefined;
CTFd._internal.challenge.renderer = null;
CTFd._internal.challenge.preRender = function () {};
CTFd._internal.challenge.render = null;
CTFd._internal.challenge.postRender = function () {};

CTFd._internal.challenge.submit = function (preview) {
  var challengeId = parseInt(CTFd.lib.$("#challenge-id").val(), 10);
  var submission = CTFd.lib.$("#challenge-input").val();
  var body = { challenge_id: challengeId, submission: submission };
  var params = {};
  if (preview) params.preview = true;
  return CTFd.api.post_challenge_attempt(params, body);
};

(function () {
  var currentStatus = null;
  var busy = false;
  var liveEtaSeconds = null;

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
        var invalidResponse = new Error("The queue request failed.");
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
            message = response.status ? "HTTP " + response.status : "The queue request failed.";
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

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "unknown";
    seconds = Math.max(0, Math.floor(Number(seconds)));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    var mm = String(minutes).padStart(2, "0");
    var ss = String(secs).padStart(2, "0");
    if (hours > 0) return hours + "h" + mm + "m" + ss + "s";
    if (minutes > 0) return minutes + "m" + ss + "s";
    return secs + "s";
  }

  function renderWaitingDetails() {
    if (!currentStatus || currentStatus.state !== "waiting") return;
    var parts = [];
    if (currentStatus.show_position && currentStatus.position) {
      parts.push("Position: " + currentStatus.position);
    }
    if (currentStatus.show_eta && liveEtaSeconds !== null) {
      parts.push("Estimated wait: " + formatDuration(liveEtaSeconds));
    }
    setText(document.getElementById("queue-details"), parts.join(" · "));
  }

  function tickEstimatedWait() {
    if (!currentStatus || currentStatus.state !== "waiting" ||
        !currentStatus.show_eta || liveEtaSeconds === null) return;
    if (liveEtaSeconds > 0) liveEtaSeconds -= 1;
    renderWaitingDetails();
  }

  function setText(element, value) {
    if (element) element.textContent = value;
  }

  function render(status) {
    currentStatus = status;
    liveEtaSeconds = status && status.state === "waiting" && status.show_eta &&
      status.estimated_wait_seconds !== undefined &&
      status.estimated_wait_seconds !== null &&
      Number.isFinite(Number(status.estimated_wait_seconds))
      ? Math.max(0, Math.floor(Number(status.estimated_wait_seconds)))
      : null;
    var panel = document.getElementById("queue-plugin-panel");
    var message = document.getElementById("queue-status-message");
    var details = document.getElementById("queue-details");
    var action = document.getElementById("queue-action-button");
    var flagInput = document.getElementById("queue-flag-input");
    var submit = document.getElementById("challenge-submit");
    if (!panel || !action || !message) return;

    action.disabled = false;
    action.className = "btn w-100";
    if (flagInput) flagInput.style.display = "";
    if (submit) submit.style.display = "";
    if (details) details.textContent = "";

    switch (status.state) {
      case "waiting":
        action.textContent = "Leave queue";
        action.classList.add("btn-danger");
        message.className = "alert alert-danger mb-2";
        setText(message, "You are registered and waiting for your turn.");
        renderWaitingDetails();
        break;
      case "selected":
        action.textContent = "Selected, you may now participate";
        action.classList.add("btn-success");
        action.disabled = true;
        message.className = "alert alert-success mb-2";
        setText(message, status.selected_message || "You have been selected! You may now attempt this challenge.");
        if (details && status.round_number) setText(details, "Selection round " + status.round_number);
        break;
      case "registered_by_teammate":
        action.textContent = "Team already in queue";
        action.classList.add("btn-secondary");
        action.disabled = true;
        message.className = "alert alert-info mb-2";
        setText(message, "A teammate (" + (status.username || "another participant") + ") has already registered your team.");
        break;
      case "completed":
        action.textContent = "Challenge completed";
        action.classList.add("btn-secondary");
        action.disabled = true;
        message.className = "alert alert-info mb-2";
        setText(message, "You have already completed this queue challenge.");
        break;

      case "needs_team":
        action.textContent = "Join a team first";
        action.classList.add("btn-secondary");
        action.disabled = true;
        message.className = "alert alert-warning mb-2";
        setText(message, "You must join a team before entering this queue.");
        break;
      default:
        action.textContent = "Join queue";
        action.classList.add("btn-primary");
        message.className = "alert alert-secondary mb-2";
        setText(message, "Register in the queue to be considered for the next draw.");
        break;
    }
  }

  function refresh() {
    var idElement = document.getElementById("queue-challenge-id");
    if (!idElement) return Promise.resolve();
    var challengeId = parseInt(idElement.value, 10);
    return api("/api/v1/plugins/ctfd-queue/challenges/" + challengeId + "/status")
      .then(render)
      .catch(function (error) {
        logApiFailure(error, "status refresh");
        var message = document.getElementById("queue-status-message");
        var action = document.getElementById("queue-action-button");
        if (message) {
          message.className = "alert alert-warning mb-2";
          setText(message, error.message);
        }
        if (action) {
          action.disabled = true;
          action.textContent = "Queue unavailable";
        }
      });
  }

  function bind() {
    var action = document.getElementById("queue-action-button");
    var idElement = document.getElementById("queue-challenge-id");
    var panel = document.getElementById("queue-plugin-panel");
    if (!action || !idElement || action.dataset.queueBound === "1") return;
    var currentUserId = Number(window.init && window.init.userId);
    if (!Number.isInteger(currentUserId) || currentUserId <= 0) {
      action.disabled = true;
      action.textContent = "Sign in to join";
      var loginMessage = document.getElementById("queue-status-message");
      if (loginMessage) {
        loginMessage.className = "alert alert-info mb-2";
        setText(loginMessage, "Sign in to register in this challenge queue.");
      }
      return;
    }
    if (panel && panel.dataset.preview === "true") {
      action.disabled = true;
      action.textContent = "Queue disabled in preview";
      var previewMessage = document.getElementById("queue-status-message");
      if (previewMessage) {
        previewMessage.className = "alert alert-info mb-2";
        setText(previewMessage, "Queue participation is disabled in CTFd's administrator preview.");
      }
      return;
    }
    action.dataset.queueBound = "1";
    action.addEventListener("click", function () {
      if (busy || !currentStatus) return;
      var challengeId = parseInt(idElement.value, 10);
      var shouldLeave = currentStatus.state === "waiting";
      if (!shouldLeave && currentStatus.state !== "not_joined") return;
      busy = true;
      action.disabled = true;
      var endpoint = "/api/v1/plugins/ctfd-queue/challenges/" + challengeId + "/" + (shouldLeave ? "leave" : "join");
      api(endpoint, "POST", {})
        .then(render)
        .catch(function (error) {
          logApiFailure(error, shouldLeave ? "leave queue" : "join queue");
          var message = document.getElementById("queue-status-message");
          if (message) {
            message.className = "alert alert-warning mb-2";
            setText(message, error.message);
          }
        })
        .then(function () {
          busy = false;
          if (currentStatus) action.disabled = currentStatus.state !== "waiting" && currentStatus.state !== "not_joined";
          return refresh();
        });
    });
    refresh();
    window.setInterval(function () {
      if (document.getElementById("queue-action-button")) refresh();
    }, 10000);
    window.setInterval(tickEstimatedWait, 1000);
  }

  if (window.CTFd && CTFd.lib && CTFd.lib.$) {
    CTFd.lib.$(bind);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
