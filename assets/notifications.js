/* Queue notifications use the same Alert + sound behavior as CTFd's
 * native notification handler, while keeping a distinct SSE event name.
 */
(function () {
  if (typeof EventSource === "undefined" || !window.init) return;

  var userId = Number(window.init.userId);
  if (!Number.isInteger(userId) || userId <= 0) return;

  var root = typeof window.init.urlRoot === "string" ? window.init.urlRoot : "";
  var source = new EventSource(root + "/events");

  function logNotificationWarning(message, error) {
    if (!window.console || typeof console.warn !== "function") return;
    if (error) {
      console.warn("[Queue Challenge] " + message, error);
    } else {
      console.warn("[Queue Challenge] " + message);
    }
  }

  function playNotificationSound() {
    var ctfd = window.CTFd;
    var events = ctfd && ctfd.events;

    // CTFd.js initializes Howler with the same eventSounds configured by the
    // active theme. Reuse it rather than introducing a second sound player.
    if (events && events.howl && typeof events.howl.play === "function") {
      try {
        events.howl.play();
      } catch (error) {
        logNotificationWarning("Could not play the native CTFd notification sound.", error);
      }
      return;
    }

    // Compatibility fallback for themes/CTFd versions that expose eventSounds
    // but not the Howler instance through CTFd.events.
    var sounds = window.init.eventSounds;
    if (typeof Audio !== "undefined" && Array.isArray(sounds) && sounds.length > 0) {
      try {
        var audio = new Audio(sounds[0]);
        var playback = audio.play();
        if (playback && typeof playback.catch === "function") {
          playback.catch(function (error) {
            logNotificationWarning(
              "Browser refused to play the queue notification sound.",
              error
            );
          });
        }
      } catch (error) {
        logNotificationWarning("Could not start the queue notification sound.", error);
      }
      return;
    }

    logNotificationWarning(
      "A queue notification requested sound, but no CTFd notification audio player is available."
    );
  }

  function renderNotification(payload) {
    var ctfd = window.CTFd;
    var handlers = ctfd && ctfd._functions && ctfd._functions.events;
    var notificationType = payload.type || "alert";
    var notification = {
      id: payload.id,
      title: payload.title || "Queue update",
      content: payload.content || "",
      html: payload.html || payload.content || "",
      type: notificationType,
      sound: payload.sound !== false
    };

    // CTFd uses one elected master tab for built-in notification rendering and
    // sound playback. Our custom SSE event must follow the same rule to avoid
    // duplicate popups/sounds when the participant has multiple tabs open.
    var events = ctfd && ctfd.events;
    var controller = events && events.controller;
    if (controller && controller.isMaster === false) return;

    if (notificationType === "alert" && handlers &&
        typeof handlers.eventAlert === "function") {
      handlers.eventAlert(notification);
    } else if (notificationType === "toast" && handlers &&
               typeof handlers.eventToast === "function") {
      handlers.eventToast(notification);
    } else if (notificationType === "background" && handlers &&
               typeof handlers.eventBackground === "function") {
      handlers.eventBackground(notification);
    } else if (notificationType === "alert") {
      // Fallback for a theme which does not expose CTFd's modal event handler.
      window.alert(notification.title + "\n\n" + notification.content);
    } else {
      logNotificationWarning(
        "CTFd has no renderer for queue notification type: " + notificationType
      );
    }

    if (notification.sound) {
      playNotificationSound();
    }
  }

  source.addEventListener("queue_notification", function (event) {
    var payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      logNotificationWarning("Could not parse a queue notification event.", error);
      return;
    }

    if (!payload || Number(payload.user_id) !== userId) return;
    renderNotification(payload);
  });

  var connectionErrorReported = false;

  source.addEventListener("open", function () {
    connectionErrorReported = false;
  });

  source.addEventListener("error", function () {
    // EventSource reconnects automatically. Log once per outage to avoid console spam.
    if (connectionErrorReported) return;
    connectionErrorReported = true;
    logNotificationWarning(
      "Live queue notification stream disconnected; browser reconnection is automatic."
    );
  });
})();
