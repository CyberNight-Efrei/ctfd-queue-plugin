// Loaded by CTFd after the challenge creation template is rendered.
(function () {
  function bindScoringSettings(form) {
    var select = form.querySelector("#queue-scoring-function");
    if (!select || form.dataset.queueScoringBound === "1") return;
    form.dataset.queueScoringBound = "1";

    var staticPanel = form.querySelector("#queue-static-value-panel");
    var dynamicPanel = form.querySelector("#queue-dynamic-value-panel");
    var staticValue = form.querySelector("#queue-static-value");
    var dynamicFields = [
      form.querySelector("#queue-initial"),
      form.querySelector("#queue-decay"),
      form.querySelector("#queue-minimum")
    ].filter(Boolean);

    function sync() {
      var dynamic = select.value !== "static";
      if (staticPanel) staticPanel.hidden = dynamic;
      if (dynamicPanel) dynamicPanel.hidden = !dynamic;
      if (staticValue) {
        staticValue.disabled = dynamic;
        staticValue.required = !dynamic;
      }
      dynamicFields.forEach(function (field) {
        field.disabled = !dynamic;
        field.required = dynamic;
      });
    }

    select.addEventListener("change", sync);
    sync();
  }

  function bindQueueSettings() {
    document.querySelectorAll("form").forEach(function (form) {
      bindScoringSettings(form);
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
