(function () {
  const previewBtn = document.getElementById("preview-btn");
  if (!previewBtn) return;

  const FIELDS = [
    { key: "student", label: "Оқушы аты-жөні", required: true },
    { key: "subject", label: "Пән", required: false },
    { key: "topic", label: "Тақырып", required: false },
    { key: "score", label: "Балл", required: true },
    { key: "max_score", label: "Максимум балл", required: false },
    { key: "curator", label: "Куратор", required: false },
  ];

  const GUESS = {
    student: ["оқушы", "аты", "фио", "тегі", "аты-жөні", "name", "student"],
    subject: ["пән", "предмет", "subject"],
    topic: ["тақырып", "тема", "topic"],
    score: ["балл", "ұпай", "score", "нәтиже", "балл саны"],
    max_score: ["максимум", "максимал", "жалпы балл", "max"],
    curator: ["куратор", "куратора", "curator", "мұғалім"],
  };

  function guessColumn(header, key) {
    const keywords = GUESS[key] || [];
    const lower = header.map((h) => h.toLowerCase());
    for (const kw of keywords) {
      const idx = lower.findIndex((h) => h.includes(kw));
      if (idx !== -1) return header[idx];
    }
    return "";
  }

  async function runPreview() {
    const sheetUrl = document.getElementById("sheet-url-input").value.trim();
    const statusEl = document.getElementById("preview-status");
    const mappingArea = document.getElementById("mapping-area");
    mappingArea.style.display = "none";
    statusEl.textContent = "";

    if (!sheetUrl) {
      statusEl.textContent = "Алдымен сілтемені енгізіңіз.";
      return;
    }

    statusEl.textContent = "Жүктелуде...";
    try {
      const resp = await fetch("/api/sheet-preview?sheet_url=" + encodeURIComponent(sheetUrl));
      const data = await resp.json();
      if (!data.ok) {
        statusEl.textContent = "Қате: " + data.error;
        return;
      }
      statusEl.textContent = `Табылды: ${data.total_rows} жол, ${data.header.length} баған.`;
      buildMappingUI(data.header, data.preview_rows, sheetUrl);
    } catch (err) {
      statusEl.textContent = "Қате шықты: " + err;
    }
  }

  function buildMappingUI(header, previewRows, sheetUrl) {
    const mappingFields = document.getElementById("mapping-fields");
    mappingFields.innerHTML = "";

    FIELDS.forEach((f) => {
      const wrap = document.createElement("label");
      wrap.textContent = f.label + (f.required ? " *" : " (міндетті емес)");
      const select = document.createElement("select");
      select.id = "select-" + f.key;

      const emptyOpt = document.createElement("option");
      emptyOpt.value = "";
      emptyOpt.textContent = "— таңдамау —";
      select.appendChild(emptyOpt);

      header.forEach((h) => {
        const opt = document.createElement("option");
        opt.value = h;
        opt.textContent = h;
        select.appendChild(opt);
      });

      const guess = guessColumn(header, f.key);
      if (guess) select.value = guess;

      wrap.appendChild(select);
      mappingFields.appendChild(wrap);
    });

    const table = document.getElementById("preview-table");
    let html = "<thead><tr>";
    header.forEach((h) => (html += `<th>${escapeHtml(h)}</th>`));
    html += "</tr></thead><tbody>";
    previewRows.forEach((row) => {
      html += "<tr>";
      row.forEach((cell) => (html += `<td>${escapeHtml(cell)}</td>`));
      html += "</tr>";
    });
    html += "</tbody>";
    table.innerHTML = html;

    document.getElementById("mapping-area").style.display = "block";

    const form = document.getElementById("import-form");
    form.onsubmit = () => {
      document.getElementById("import-sheet-url").value = sheetUrl;
      document.getElementById("import-fixed-curator").value =
        document.getElementById("fixed-curator-input").value.trim();
      document.getElementById("import-default-max-score").value =
        document.getElementById("default-max-score-input").value.trim();
      document.getElementById("col_student").value = document.getElementById("select-student").value;
      document.getElementById("col_subject").value = document.getElementById("select-subject").value;
      document.getElementById("col_topic").value = document.getElementById("select-topic").value;
      document.getElementById("col_score").value = document.getElementById("select-score").value;
      document.getElementById("col_max_score").value = document.getElementById("select-max_score").value;
      document.getElementById("col_curator").value = document.getElementById("select-curator").value;

      if (!document.getElementById("col_student").value || !document.getElementById("col_score").value) {
        alert("Кемінде 'Оқушы аты-жөні' және 'Балл' бағандарын таңдаңыз.");
        return false;
      }
      return true;
    };
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : str;
    return div.innerHTML;
  }

  previewBtn.addEventListener("click", runPreview);
})();
