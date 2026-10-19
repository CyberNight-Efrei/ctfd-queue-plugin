(function () {
  if (!window.init) return;

  var userId = Number(window.init.userId);
  if (!Number.isInteger(userId) || userId <= 0) return;

  var root = typeof window.init.urlRoot === "string" ? window.init.urlRoot : "";
  var base = "/api/v1/plugins/ctfd-queue";
  var pollInProgress = false;
  var seen = Object.create(null);
  var lastWarningAt = 0;

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
      requestPath = root.replace(/\\/+$/, "") + path;
    }
    var safeMethod = /^(GET|HEAD|OPTIONS|TRACE)$/i.test(options.method);
    var csrfNonce = window.init && window.init.csrfNonce;
    if (!safeMethod && csrfNonce) {
      options.headers["CSRF-Token"] = csrfNonce;
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
        var chain = Promise.resolve();

        items.forEach(function (item) {
          var id = Number(item && item.id);
          if (!Number.isInteger(id) || id <= 0 || seen[id]) return;

          chain = chain.then(function () {
            if (seen[id]) return;
            // Acknowledge through an endpoint that verifies recipient_user_id before
            // rendering; notification contents are never included in a broadcast event.
            return api(base + "/notifications/" + id + "/ack", "POST", {})
              .then(function () {
                seen[id] = true;
                renderNotification(item);
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

  poll();
  window.setInterval(poll, 3000);
})();
