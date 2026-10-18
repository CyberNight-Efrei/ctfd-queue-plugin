/* Route private queue notifications through CTFd's native toast UI.
 *
 * The default CTFd "notification" event is broadcast to all connected clients.
 * Queue events use a separate SSE event name and are only rendered when the
 * payload's recipient is the current authenticated user.
 */
(function () {
  if (typeof EventSource === "undefined" || !window.init) return;

  var userId = Number(window.init.userId);
  if (!Number.isInteger(userId) || userId <= 0) return;

  var root = typeof window.init.urlRoot === "string" ? window.init.urlRoot : "";
  var source = new EventSource(root + "/events");

  source.addEventListener("queue_notification", function (event) {
    var payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_) {
      return;
    }

    if (!payload || Number(payload.user_id) !== userId) return;

    var notification = {
      id: payload.id,
      title: payload.title || "Queue update",
      content: payload.content || "",
      html: payload.html || payload.content || "",
      type: "toast",
      sound: false
    };
    var handlers = window.CTFd &&
      window.CTFd._functions &&
      window.CTFd._functions.events;

    if (handlers && typeof handlers.eventToast === "function") {
      handlers.eventToast(notification);
    } else if (handlers && typeof handlers.eventAlert === "function") {
      handlers.eventAlert(notification);
    }
  });

  source.addEventListener("error", function () {
    // EventSource reconnects automatically. Persistent notifications remain in
    // CTFd's notifications table even when live events are temporarily offline.
  });
})();
