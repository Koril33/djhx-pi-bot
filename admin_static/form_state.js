(() => {
  const forms = document.querySelectorAll('form[method="post"]');
  for (const form of forms) {
    form.addEventListener("submit", event => {
      if (form.dataset.submitting === "1") {
        event.preventDefault();
        return;
      }
      const submitter = event.submitter;
      if (submitter && submitter.name) {
        const chosen = document.createElement("input");
        chosen.type = "hidden";
        chosen.name = submitter.name;
        chosen.value = submitter.value;
        chosen.dataset.submitChoice = "1";
        form.append(chosen);
      }
      form.dataset.submitting = "1";
      const status = document.createElement("span");
      status.className = "submit-status";
      status.setAttribute("role", "status");
      status.textContent = "提交中，请稍候…";
      form.append(status);
      for (const button of form.querySelectorAll('button[type="submit"]')) {
        button.dataset.originalText = button.textContent;
        button.disabled = true;
        if (button === submitter) {
          button.textContent = button.dataset.busyLabel || "提交中…";
        }
      }
    });
  }
  window.addEventListener("pageshow", () => {
    for (const form of forms) {
      if (form.dataset.submitting !== "1") continue;
      delete form.dataset.submitting;
      for (const button of form.querySelectorAll('button[type="submit"]')) {
        button.disabled = false;
        if (button.dataset.originalText) button.textContent = button.dataset.originalText;
      }
      for (const item of form.querySelectorAll(".submit-status, [data-submit-choice]")) item.remove();
    }
  });
})();
