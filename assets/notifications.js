(function () {
  if (!window.init) return;

  var userId = Number(window.init.userId);
  if (!Number.isInteger(userId) || userId <= 0) return;

  var root = typeof window.init.urlRoot === "string" ? window.init.urlRoot : "";
  var base = "/api/v1/plugins/ctfd-queue";
  var pollInProgress = false;
  // Track display and acknowledgement separately. A failed ACK must be retried
  // without showing the same popup repeatedly in the current tab.
  var displayed = Object.create(null);
  var acknowledged = Object.create(null);
  var lastWarningAt = 0;
  var historyLink = null;

  function warn(message, error) {
    var now = Date.now();
    if (now - lastWarningAt < 60000) return;
    lastWarningAt = now;
    if (window.console && typeof console.warn === "function") {
      console.warn("[Queue Challenge] " + message, error || "");
    }
  }

  function request(path, method, body) {
    var options = {
      method: method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json"
      }
    };
    if (body !== undefined) options.body = JSON.stringify(body);

    var ctfd = window.CTFd;
    if (ctfd && typeof ctfd.fetch === "function") {
      return ctfd.fetch(path, options);
    }

    var requestPath = path;
    if (path.charAt(0) === "/" && root) {
      requestPath = root.replace(/\/+$/, "") + path;
    }
    var safeMethod = /^(GET|HEAD|OPTIONS|TRACE)$/i.test(options.method);
    var csrfNonce = window.init && window.init.csrfNonce;
    if (!safeMethod && csrfNonce) {
      options.headers["CSRF-Token"] = csrfNonce;
    }
    if (typeof window.fetch !== "function") {
      return Promise.reject(new Error("This browser does not support fetch()."));
    }
    return window.fetch(requestPath, options);
  }

  function api(path, method, body) {
    return request(path, method, body).then(function (response) {
      if (!response || typeof response.json !== "function") {
        throw new Error("The queue notification API returned an invalid response.");
      }
      return response.json().catch(function () {
        return null;
      }).then(function (payload) {
        if (!response.ok || !payload || payload.success !== true) {
          var message = payload && (payload.message || payload.description || payload.error);
          throw new Error(message || ("Queue notification request failed (HTTP " +
            (response.status || 0) + ")."));
        }
        return payload.data || {};
      });
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[ch];
    });
  }

  function historyPageUrl() {
    return root.replace(/\/+$/, "") + "/plugins/ctfd-queue/notifications";
  }

  function currentLocale() {
    // CTFd marks the currently selected language in its navbar language menu.
    var selectedLanguage = document.querySelector(
      'form[x-data="LanguageForm"] .dropdown-item.bg-primary[value]'
    );
    if (selectedLanguage) {
      return String(selectedLanguage.getAttribute("value") || "").toLowerCase();
    }

    // Fallback for custom themes without the standard CTFd language menu.
    var cookieMatch = document.cookie.match(/(?:^|;\s*)language=([^;]*)/);
    if (cookieMatch) {
      try { return decodeURIComponent(cookieMatch[1]).toLowerCase(); }
      catch (_) { return String(cookieMatch[1]).toLowerCase(); }
    }
    return String((navigator.languages && navigator.languages[0]) ||
      navigator.language || "en").toLowerCase();
  }

  function historyLinkLabel() {
    return currentLocale().indexOf("fr") === 0 ? "Notif. Fil" : "Queue Notif.";
  }

  function setHistoryLinkLabel() {
    if (!historyLink) return;
    var label = historyLink.querySelector("[data-queue-notification-label]");
    if (!label) {
      label = document.createElement("span");
      label.setAttribute("data-queue-notification-label", "1");
      // Preserve a pending-count badge if another script already added one.
      Array.prototype.slice.call(historyLink.childNodes).forEach(function (node) {
        if (node.nodeType === 3) node.remove();
      });
      historyLink.insertBefore(label, historyLink.firstChild);
    }
    label.textContent = historyLinkLabel();
  }

  function mountHistoryLink() {
    var existing = document.getElementById("queue-notification-history-link");
    if (existing) {
      historyLink = existing;
      setHistoryLinkLabel();
      return;
    }

    var navList = document.querySelector("#base-navbars ul.navbar-nav") ||
      document.querySelector("nav.navbar ul.navbar-nav") ||
      document.querySelector(".navbar ul.navbar-nav");
    if (!navList) return;

    var item = document.createElement("li");
    item.className = "nav-item";
    item.id = "queue-notification-history-nav-item";
    historyLink = document.createElement("a");
    historyLink.id = "queue-notification-history-link";
    historyLink.className = "nav-link";
    historyLink.href = historyPageUrl();
    var label = document.createElement("span");
    label.setAttribute("data-queue-notification-label", "1");
    label.textContent = historyLinkLabel();
    historyLink.appendChild(label);
    item.appendChild(historyLink);
    navList.appendChild(item);
  }

  function updateHistoryLink(pendingCount) {
    if (!historyLink) return;
    var count = Math.max(0, Number(pendingCount) || 0);
    var badge = historyLink.querySelector("[data-queue-notification-count]");
    if (!count) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "badge badge-pill badge-warning ml-1";
      badge.setAttribute("data-queue-notification-count", "1");
      badge.setAttribute("aria-label", count + " pending queue notifications");
      historyLink.appendChild(document.createTextNode(" "));
      historyLink.appendChild(badge);
    }
    badge.textContent = String(count);
    badge.setAttribute("aria-label", count + " pending queue notifications");
  }

  function formatHistoryDate(value) {
    if (!value) return "Date unavailable";
    var date = new Date(value);
    if (!Number.isFinite(date.getTime())) return String(value);
    return date.toLocaleString();
  }

  function mountHistoryPage() {
    var list = document.getElementById("queue-notifications-list");
    if (!list || list.dataset.queueHistoryBound === "1") return;
    list.dataset.queueHistoryBound = "1";

    var status = document.getElementById("queue-notifications-status");
    var summary = document.getElementById("queue-notifications-summary");
    var previous = document.getElementById("queue-notifications-previous");
    var next = document.getElementById("queue-notifications-next");
    var refresh = document.getElementById("queue-notifications-refresh");
    var pageSize = 50;
    var offset = 0;
    var total = 0;
    var pendingCount = 0;

    function renderItems(items) {
      list.textContent = "";
      if (!items.length) {
        var empty = document.createElement("div");
        empty.className = "alert alert-secondary";
        empty.textContent = total ? "No notifications on this page." : "You have no queue notifications yet.";
        list.appendChild(empty);
        return;
      }

      items.forEach(function (item) {
        var card = document.createElement("article");
        card.className = "card queue-notification-card";
        var body = document.createElement("div");
        body.className = "card-body";
        var headingRow = document.createElement("div");
        headingRow.className = "d-flex flex-wrap justify-content-between align-items-start";
        var heading = document.createElement("h5");
        heading.className = "card-title mb-1";
        heading.textContent = item.title || "Queue update";
        var state = document.createElement("span");
        state.className = "queue-notification-state " +
          (item.acknowledged
            ? "queue-notification-state-acknowledged"
            : "queue-notification-state-pending");
        state.textContent = item.acknowledged ? "Alert displayed" : "Waiting for delivery";
        headingRow.appendChild(heading);
        headingRow.appendChild(state);

        var date = document.createElement("small");
        date.className = "queue-notification-date";
        date.textContent = formatHistoryDate(item.date);

        var content = document.createElement("p");
        content.className = "queue-notification-content";
        content.textContent = item.content || "";

        body.appendChild(headingRow);
        body.appendChild(date);
        body.appendChild(content);
        card.appendChild(body);
        list.appendChild(card);
      });
    }

    function loadPage() {
      if (status) status.textContent = "Loading your queue notification history…";
      if (previous) previous.disabled = true;
      if (next) next.disabled = true;
      if (refresh) refresh.disabled = true;

      api(base + "/notifications/history?limit=" + pageSize + "&offset=" + offset, "GET")
        .then(function (data) {
          var items = Array.isArray(data.notifications) ? data.notifications : [];
          total = Math.max(0, Number(data.total) || 0);
          pendingCount = Math.max(0, Number(data.pending_count) || 0);
          renderItems(items);
          if (summary) {
            var start = total ? offset + 1 : 0;
            var end = Math.min(offset + items.length, total);
            summary.textContent = "Showing " + start + "–" + end + " of " + total +
              " notifications · " + pendingCount + " awaiting popup delivery";
          }
          if (status) status.textContent = "";
          if (previous) previous.disabled = offset <= 0;
          if (next) next.disabled = offset + pageSize >= total;
          if (refresh) refresh.disabled = false;
          updateHistoryLink(pendingCount);
        })
        .catch(function (error) {
          warn("Could not load queue notification history.", error);
          if (status) status.textContent = "Could not load notification history: " +
            (error && error.message ? error.message : "Unknown error");
          if (summary) summary.textContent = "";
          list.textContent = "";
          if (previous) previous.disabled = offset <= 0;
          if (next) next.disabled = true;
          if (refresh) refresh.disabled = false;
        });
    }

    if (refresh) {
      refresh.addEventListener("click", function () {
        loadPage();
      });
    }
    if (previous) {
      previous.addEventListener("click", function () {
        if (offset <= 0) return;
        offset = Math.max(0, offset - pageSize);
        loadPage();
      });
    }
    if (next) {
      next.addEventListener("click", function () {
        if (offset + pageSize >= total) return;
        offset += pageSize;
        loadPage();
      });
    }
    loadPage();
  }

  function isMasterTab() {
    var events = window.CTFd && window.CTFd.events;
    var controller = events && events.controller;
    return !(controller && controller.isMaster === false);
  }

  function playNotificationSound() {
    var events = window.CTFd && window.CTFd.events;
    if (events && events.howl && typeof events.howl.play === "function") {
      try {
        events.howl.play();
      } catch (error) {
        warn("Could not play the CTFd notification sound.", error);
      }
      return;
    }

    var sounds = window.init && window.init.eventSounds;
    if (typeof Audio !== "undefined" && Array.isArray(sounds) && sounds.length > 0) {
      try {
        var audio = new Audio(sounds[0]);
        var playback = audio.play();
        if (playback && typeof playback.catch === "function") {
          playback.catch(function (error) {
            warn("Browser refused to play the queue notification sound.", error);
          });
        }
      } catch (error) {
        warn("Could not start the queue notification sound.", error);
      }
    }
  }

  function renderNotification(item) {
    var ctfd = window.CTFd;
    var handlers = ctfd && ctfd._functions && ctfd._functions.events;
    var notification = {
      id: item.id,
      title: item.title || "Queue update",
      content: item.content || "",
      // Queue message content may include flags or configured text; treat it as plain text.
      html: escapeHtml(item.content || ""),
      type: "alert",
      sound: true
    };

    if (handlers && typeof handlers.eventAlert === "function") {
      handlers.eventAlert(notification);
    } else {
      window.alert(notification.title + "\n\n" + notification.content);
    }
    playNotificationSound();
  }

  function poll() {
    if (pollInProgress || !isMasterTab()) return;
    pollInProgress = true;

    api(base + "/notifications", "GET")
      .then(function (data) {
        var items = Array.isArray(data.notifications) ? data.notifications : [];
        updateHistoryLink(data.pending_count === undefined ? items.length : data.pending_count);
        var chain = Promise.resolve();

        items.forEach(function (item) {
          var id = Number(item && item.id);
          if (!Number.isInteger(id) || id <= 0 || acknowledged[id]) return;

          chain = chain.then(function () {
            if (acknowledged[id]) return;
            // Display at most once per tab, but retry the ACK until the server
            // confirms it; this avoids duplicate popups after transient failures.
            if (!displayed[id]) {
              renderNotification(item);
              displayed[id] = true;
            }
            return api(base + "/notifications/" + id + "/ack", "POST", {})
              .then(function () {
                acknowledged[id] = true;
              });
          }).catch(function (error) {
            warn("Could not acknowledge or render a private queue notification.", error);
          });
        });

        return chain;
      })
      .catch(function (error) {
        warn("Could not retrieve private queue notifications.", error);
      })
      .then(function () {
        pollInProgress = false;
      });
  }

  mountHistoryLink();
  mountHistoryPage();
  poll();
  window.setInterval(poll, 3000);
})();
