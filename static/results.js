(function () {
  const btn = document.getElementById("clear-results-btn");
  const confirmArea = document.getElementById("clear-results-confirm");
  const cancelBtn = document.getElementById("clear-results-cancel");
  if (!btn || !confirmArea) return;

  btn.addEventListener("click", () => {
    btn.style.display = "none";
    confirmArea.style.display = "block";
  });

  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      confirmArea.style.display = "none";
      btn.style.display = "inline-block";
    });
  }
})();
