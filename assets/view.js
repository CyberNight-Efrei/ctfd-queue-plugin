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

  function api(path, method, body) {
    var options = {
      method: method || "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" }
    };
    if (body !== undefined) options.body = JSON.stringify(body);
    return CTFd.fetch(path, options).then(function (response) {
      if (!response || response.success === false) {
        var message = response && (response.message || response.description);
        throw new Error(message || "The queue request failed.");
      }
      return response.data;
    });
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "unknown";
    seconds = Math.max(0, Math.floor(Number(seconds)));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    if (hours > 0) return hours + "h " + minutes + "m";
    if (minutes > 0) return minutes + "m " + secs + "s";
    return secs + "s";
  }

  function setText(element, value) {
    if (element) element.textContent = value;
  }

  function render(status) {
    currentStatus = status;
    var panel = document.getElementById("queue-plugin-panel");
    var message = document.getElementById("queue-status-message");
    var details = document.getElementById("queue-details");
    var action = document.getElementById("queue-action-button");
    var flagInput = document.getElementById("queue-flag-input");
    var submit = document.getElementById("challenge-submit");
    if (!panel || !action || !message) return;

    action.disabled = false;
    action.className = "btn w-100";
    if (flagInput) flagInput.style.display = "none";
    if (submit) submit.style.display = "none";
    if (details) details.textContent = "";

    switch (status.state) {
      case "waiting":
        action.textContent = "Leave queue";
        action.classList.add("btn-danger");
        message.className = "alert alert-danger mb-2";
        setText(message, "You are registered and waiting for your turn.");
        var detailParts = [];
        if (status.show_position && status.position) {
          detailParts.push("Position: " + status.position);
        }
        if (status.show_eta && status.estimated_wait_seconds !== undefined) {
          detailParts.push("Estimated wait: " + formatDuration(status.estimated_wait_seconds));
        }
        setText(details, detailParts.join(" · "));
        break;
      case "selected":
        action.textContent = "Selected — you may participate";
        action.classList.add("btn-success");
        action.disabled = true;
        message.className = "alert alert-success mb-2";
        setText(message, status.selected_message || "You have been selected! You may now attempt this challenge.");
        if (flagInput) flagInput.style.display = "";
        if (submit) submit.style.display = "";
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
    if (!action || !idElement || action.dataset.queueBound === "1") return;
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
  }

  if (window.CTFd && CTFd.lib && CTFd.lib.$) {
    CTFd.lib.$(bind);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
