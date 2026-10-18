// Loaded by CTFd after the challenge creation template is rendered.
(function () {
  function bindQueueSettings() {
    document.querySelectorAll("form").forEach(function (form) {
      if (form.dataset.queueSettingsBound === "1") return;
      if (!form.querySelector("[data-queue-bool]")) return;
      form.dataset.queueSettingsBound = "1";
      form.addEventListener("submit", function () {
        form.querySelectorAll("[data-queue-bool]").forEach(function (checkbox) {
          var hidden = form.querySelector('input[type="hidden"][name="' + checkbox.dataset.queueBool + '"]');
          if (hidden) hidden.value = checkbox.checked ? "true" : "false";
        });
      }, true);
    });
  }
  bindQueueSettings();
  if (window.MutationObserver && document.body) {
    new MutationObserver(bindQueueSettings).observe(document.body, { childList: true, subtree: true });
  }
})();
