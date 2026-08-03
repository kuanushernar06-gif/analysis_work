(function () {
  const addBtn = document.getElementById("add-rating-btn");
  const rowsContainer = document.getElementById("rating-rows");
  const emptyHint = document.getElementById("rating-empty-hint");
  const form = document.getElementById("import-form");
  if (!addBtn || !rowsContainer || !form) return;

  function updateEmptyHint() {
    if (!emptyHint) return;
    emptyHint.style.display = rowsContainer.children.length ? "none" : "block";
  }

  function addRow() {
    const row = document.createElement("div");
    row.className = "rating-row";
    row.innerHTML = [
      '<div class="form-grid">',
      '  <label class="col-span-2">',
      "    Рейтинг сілтемесі (Google Sheets)",
      '    <input type="text" name="sheet_url" required autocomplete="off" placeholder="https://docs.google.com/spreadsheets/d/...">',
      "  </label>",
      '  <div><button type="button" class="btn btn-danger btn-small remove-row-btn">Жою</button></div>',
      "</div>",
    ].join("\n");

    row.querySelector(".remove-row-btn").addEventListener("click", () => {
      row.remove();
      updateEmptyHint();
    });

    rowsContainer.appendChild(row);
    updateEmptyHint();
    row.querySelector('input[name="sheet_url"]').focus();
  }

  form.addEventListener("submit", (e) => {
    if (!rowsContainer.children.length) {
      e.preventDefault();
      alert("Кемінде бір рейтинг сілтемесін қосыңыз.");
    }
  });

  addBtn.addEventListener("click", addRow);
  updateEmptyHint();
})();
