(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const ROUTE_META = {
    documents: { eyebrow: "Preparación", title: "Documentos" },
    setup: { eyebrow: "Configuración", title: "Diseña el test" },
    exam: { eyebrow: "Evaluación", title: "Examen" },
    results: { eyebrow: "Evaluación", title: "Resultados" },
    settings: { eyebrow: "ExamForge", title: "Ajustes" },
  };

  const DIFFICULTY_HINTS = {
    basic: "Recuerdo directo, definiciones y clasificaciones.",
    university: "Comprensión, relación de conceptos y aplicación.",
    competitive: "Matices, excepciones y distractores cercanos.",
  };

  const TYPE_LABELS = {
    multiple_choice: "Tipo test",
    true_false: "Verdadero / Falso",
    short_answer: "Respuesta corta",
  };

  const TYPE_SHORT = {
    multiple_choice: "Test",
    true_false: "V/F",
    short_answer: "Corta",
  };

  const state = {
    booted: false,
    settings: null,
    docs: [],
    selectedDocId: null,
    previewCache: new Map(),
    config: {
      count: 20,
      difficulty: "university",
      weights: { multiple_choice: 100, true_false: 0, short_answer: 0 },
      timerEnabled: true,
      timerMinutes: 30,
      immediateFeedback: false,
      chunkChars: 4500,
    },
    exam: null,
    answers: {},
    flags: new Set(),
    checked: new Set(),
    current: 0,
    submitted: false,
    results: null,
    reviewFilter: "all",
    coverage: null,
    coverageRequest: 0,
    generating: false,
    timerDeadline: null,
    timerInterval: null,
    timerWarned: false,
    route: "documents",
  };

  class ApiError extends Error {}

  async function apiCall(method, ...args) {
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api[method] !== "function") {
      throw new ApiError("La conexión con el proceso local todavía no está disponible. Espera un segundo e inténtalo de nuevo.");
    }
    let response;
    try {
      response = await api[method](...args);
    } catch (error) {
      throw new ApiError(cleanError(error));
    }
    if (response && response.ok === false) {
      throw new ApiError((response.error && response.error.message) || "La operación no se pudo completar.");
    }
    return response ? response.data : null;
  }

  function cleanError(error) {
    const raw = error instanceof Error ? error.message : String(error || "");
    return raw.replace(/^Error:\s*/i, "").replace(/^pywebview.*?:\s*/i, "");
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("es-ES").format(Number(value) || 0);
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1).replace(".", ",") + " KB";
    return (value / (1024 * 1024)).toFixed(1).replace(".", ",") + " MB";
  }

  function plural(count, singular, pluralForm) {
    return count === 1 ? singular : pluralForm;
  }

  function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function toast(message, kind = "info", timeout = 5000) {
    const region = $("#toast-region");
    const node = document.createElement("div");
    node.className = "toast" + (kind === "error" ? " is-error" : kind === "success" ? " is-success" : "");
    node.innerHTML = '<div class="toast-body">' + escapeHtml(message) + "</div>";
    region.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity 160ms ease";
      setTimeout(() => node.remove(), 180);
    }, timeout);
  }

  function confirmAction({ title, body, confirmLabel = "Continuar", danger = false }) {
    return new Promise((resolve) => {
      const modal = $("#modal");
      $("#modal-title").textContent = title;
      $("#modal-body").textContent = body;
      const button = $("#modal-confirm");
      button.textContent = confirmLabel;
      button.className = "btn " + (danger ? "btn-danger" : "btn-primary");
      const onClose = () => {
        modal.removeEventListener("close", onClose);
        resolve(modal.returnValue === "confirm");
      };
      modal.addEventListener("close", onClose);
      modal.returnValue = "cancel";
      modal.showModal();
    });
  }

  /* ------------------------------------------------------------------ */
  /* Boot and routing                                                    */
  /* ------------------------------------------------------------------ */

  async function boot() {
    if (state.booted) return;
    try {
      const data = await apiCall("bootstrap");
      state.settings = data.settings;
      applyTheme(state.settings.theme);
      hydrateSettings();
      applySettingsDefaults();
      syncSetupForm();
      renderProvider();
      state.booted = true;
      renderDocuments();
      go("documents");
      refreshCoverage();
    } catch (error) {
      toast(cleanError(error), "error", 9000);
    }
  }

  function applySettingsDefaults() {
    if (!state.settings) return;
    state.config.count = state.settings.default_question_count;
    state.config.timerMinutes = state.settings.default_timer_minutes;
    state.config.chunkChars = state.settings.chunk_chars;
    state.config.immediateFeedback = Boolean(state.settings.immediate_feedback);
  }

  function go(route) {
    if (!ROUTE_META[route]) return;
    if (route === "exam" && !state.exam) {
      toast("Genera un examen antes de abrirlo.", "error");
      return;
    }
    if (route === "results" && !state.submitted) {
      toast("Finaliza el examen para ver los resultados.", "error");
      return;
    }
    state.route = route;
    $$(".nav-item[data-route]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.route === route);
    });
    $$(".view").forEach((view) => {
      view.classList.toggle("is-active", view.dataset.view === route);
    });
    const meta = ROUTE_META[route];
    $("#view-eyebrow").textContent = meta.eyebrow;
    $("#view-title").textContent = meta.title;
    renderTopbarActions(route);
    if (route === "documents") renderDocuments();
    if (route === "setup") {
      renderCoverage();
      updateGenerateState();
    }
    if (route === "exam") renderExam();
    if (route === "results") renderResults();
    if (route === "settings") renderSettings();
    $("#content").scrollTop = 0;
  }

  function renderTopbarActions(route) {
    const container = $("#topbar-actions");
    if (route === "documents") {
      container.innerHTML = '<button class="btn btn-primary btn-sm" type="button" data-action="add-docs">Añadir documentos</button>';
    } else if (route === "setup") {
      container.innerHTML = '<button class="btn btn-secondary btn-sm" type="button" data-action="go-documents">Ver documentos</button>';
    } else if (route === "results") {
      container.innerHTML = '<button class="btn btn-secondary btn-sm" type="button" data-action="new-exam">Nuevo examen</button>';
    } else if (route === "settings") {
      container.innerHTML = '<button class="btn btn-secondary btn-sm" type="button" data-action="go-documents">Volver al material</button>';
    } else {
      container.innerHTML = "";
    }
  }

  /* ------------------------------------------------------------------ */
  /* Documents                                                           */
  /* ------------------------------------------------------------------ */

  async function pickDocuments() {
    try {
      const data = await apiCall("pick_documents");
      if (data && data.paths && data.paths.length) {
        await addDocuments(data.paths);
      }
    } catch (error) {
      toast(cleanError(error), "error");
    }
  }

  async function addDocuments(paths) {
    if (!paths || !paths.length) return;
    const browse = $("#browse-btn");
    browse.disabled = true;
    browse.textContent = "Leyendo…";
    try {
      const data = await apiCall("add_documents", paths);
      const added = data.documents || [];
      for (const summary of added) {
        const existing = state.docs.findIndex((doc) => doc.id === summary.id);
        if (existing >= 0) state.docs[existing] = summary;
        else state.docs.push(summary);
      }
      for (const failure of data.errors || []) {
        toast(failure.name + ": " + failure.message, "error", 8000);
      }
      if (added.length) {
        state.selectedDocId = added[added.length - 1].id;
        toast(added.length + " " + plural(added.length, "documento añadido", "documentos añadidos"), "success");
      }
      renderDocuments();
      renderPreview();
      updateNavState();
      await refreshCoverage();
    } catch (error) {
      toast(cleanError(error), "error", 8000);
    } finally {
      browse.disabled = false;
      browse.textContent = "Seleccionar archivos";
    }
  }

  function renderDocuments() {
    const list = $("#doc-list");
    list.innerHTML = state.docs
      .map((doc) => {
        const excluded = doc.excluded_count
          ? '<span class="chip chip-warn">' + doc.excluded_count + " excl.</span>"
          : "";
        return (
          '<li class="doc-item' +
          (doc.id === state.selectedDocId ? " is-selected" : "") +
          '" data-doc-id="' +
          escapeHtml(doc.id) +
          '" tabindex="0">' +
          '<span class="doc-icon">' +
          (doc.suffix === ".pdf" ? "PDF" : "MD") +
          "</span>" +
          '<span class="doc-copy">' +
          '<span class="doc-name">' +
          escapeHtml(doc.name) +
          "</span>" +
          '<span class="doc-meta">' +
          formatNumber(doc.words) +
          " palabras · " +
          formatNumber(doc.tokens) +
          " tokens" +
          "</span></span>" +
          '<span class="doc-badges">' +
          excluded +
          '<button class="icon-btn" type="button" data-remove-doc="' +
          escapeHtml(doc.id) +
          '" aria-label="Quitar documento"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>' +
          "</span></li>"
        );
      })
      .join("");

    const count = state.docs.length;
    $("#doc-count").textContent = count + " " + plural(count, "archivo", "archivos");
    $("#nav-doc-count").textContent = String(count);
    const totalWords = state.docs.reduce((sum, doc) => sum + doc.words, 0);
    const totalTokens = state.docs.reduce((sum, doc) => sum + doc.tokens, 0);
    const excluded = state.docs.reduce((sum, doc) => sum + doc.excluded_count, 0);
    $("#doc-summary").textContent = count
      ? formatNumber(totalWords) +
        " palabras · " +
        formatNumber(totalTokens) +
        " tokens estimados · " +
        excluded +
        " " +
        plural(excluded, "exclusión aplicada", "exclusiones aplicadas")
      : "Todavía no has añadido material.";
    if (!count) {
      state.selectedDocId = null;
      $("#preview-content").hidden = true;
      $("#preview-empty").hidden = false;
    }
    renderPreview();
    updateNavState();
  }

  async function selectDocument(id) {
    state.selectedDocId = id;
    renderDocuments();
    renderPreview();
  }

  async function renderPreview() {
    const doc = state.docs.find((item) => item.id === state.selectedDocId);
    if (!doc) {
      $("#preview-content").hidden = true;
      $("#preview-empty").hidden = false;
      return;
    }
    $("#preview-empty").hidden = true;
    $("#preview-content").hidden = false;
    $("#preview-name").textContent = doc.name;
    $("#preview-meta").textContent =
      (doc.pages ? doc.pages + " " + plural(doc.pages, "página", "páginas") + " · " : "") +
      formatBytes(doc.size_bytes) +
      " · " +
      formatNumber(doc.chars) +
      " caracteres";
    $("#preview-tokens").textContent = formatNumber(doc.tokens) + " tokens";
    $("#preview-metrics").innerHTML = [
      ["Palabras", formatNumber(doc.words)],
      ["Texto válido", formatNumber(doc.kept_chars) + " car."],
      ["Excluido", formatNumber(doc.excluded_chars) + " car."],
    ]
      .map(
        ([label, value]) =>
          '<div class="metric"><span class="metric-value">' + value + '</span><span class="metric-label">' + label + "</span></div>"
      )
      .join("");

    const exclusions = doc.exclusions || [];
    if (exclusions.length) {
      $("#preview-exclusions").innerHTML =
        '<div class="exclusion-head">' +
        '<span class="chip chip-warn">' +
        doc.excluded_count +
        " bloques descartados</span></div>" +
        "<ul>" +
        exclusions
          .slice(0, 5)
          .map(
            (item) =>
              "<li>" +
              escapeHtml(item.reason) +
              ' <span>· ' +
              escapeHtml(item.section || "Documento") +
              "</span></li>"
          )
          .join("") +
        "</ul>";
    } else {
      $("#preview-exclusions").innerHTML =
        '<div class="exclusion-head">Sin exclusiones detectadas</div><p class="field-hint">Se usará todo el texto extraído.</p>';
    }

    const cached = state.previewCache.get(doc.id);
    $("#preview-text").textContent = cached || doc.preview || "Cargando texto…";
    if (!cached) {
      try {
        const data = await apiCall("get_document", doc.id);
        state.previewCache.set(doc.id, data.content || "");
        if (state.selectedDocId === doc.id) {
          $("#preview-text").textContent = data.content || doc.preview || "";
          if (data.exclusions && !exclusions.length) {
            $("#preview-exclusions").innerHTML =
              '<div class="exclusion-head">Sin exclusiones detectadas</div><p class="field-hint">Se usará todo el texto extraído.</p>';
          }
        }
      } catch (error) {
        $("#preview-text").textContent = "No se pudo cargar la vista previa.";
      }
    }
  }

  async function removeDocument(id) {
    const doc = state.docs.find((item) => item.id === id);
    if (!doc) return;
    try {
      await apiCall("remove_document", id);
      state.docs = state.docs.filter((item) => item.id !== id);
      state.previewCache.delete(id);
      if (state.selectedDocId === id) state.selectedDocId = state.docs[0] ? state.docs[0].id : null;
      renderDocuments();
      await refreshCoverage();
      toast("Documento retirado de la sesión.");
    } catch (error) {
      toast(cleanError(error), "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Coverage and generation                                             */
  /* ------------------------------------------------------------------ */

  const scheduleCoverage = debounce(() => refreshCoverage(), 350);

  async function refreshCoverage() {
    if (!state.booted) return;
    if (!state.docs.length) {
      state.coverage = null;
      renderCoverage();
      updateGenerateState();
      return;
    }
    const requestId = ++state.coverageRequest;
    try {
      const data = await apiCall("preview_coverage", {
        question_count: state.config.count,
        weights: state.config.weights,
        chunk_chars: state.config.chunkChars,
      });
      if (requestId !== state.coverageRequest) return;
      state.coverage = data;
      renderCoverage();
    } catch (error) {
      if (requestId !== state.coverageRequest) return;
      state.coverage = null;
      renderCoverage();
      $("#provider-callout").innerHTML = '<div class="callout is-warning">No se pudo calcular el plan: ' + escapeHtml(cleanError(error)) + "</div>";
    }
    updateGenerateState();
  }

  function renderCoverage() {
    const empty = $("#coverage-empty");
    const content = $("#coverage-content");
    const chip = $("#coverage-chip");
    if (!state.coverage || !state.coverage.chunks.length) {
      empty.hidden = false;
      content.hidden = true;
      chip.textContent = "Sin calcular";
      chip.className = "chip";
      return;
    }
    empty.hidden = true;
    content.hidden = false;
    const chunks = state.coverage.chunks;
    chip.textContent = chunks.length + " " + plural(chunks.length, "bloque", "bloques");
    chip.className = "chip chip-accent";
    $("#coverage-summary").innerHTML = [
      ["Preguntas", state.coverage.question_count],
      ["Bloques", chunks.length],
      ["Material válido", formatNumber(state.coverage.valid_chars) + " car."],
    ]
      .map(([label, value]) => "<div><dt>" + label + "</dt><dd>" + value + "</dd></div>")
      .join("");
    $("#coverage-list").innerHTML = chunks
      .map((chunk) => {
        const sections = chunk.sections && chunk.sections.length ? chunk.sections.join(" · ") : "Material general";
        const types = countTypes(chunk.question_types);
        return (
          '<li class="coverage-item">' +
          '<span class="coverage-index">' +
          String(chunk.index).padStart(2, "0") +
          "</span>" +
          '<span class="coverage-section" title="' +
          escapeHtml(sections) +
          '">' +
          escapeHtml(sections) +
          '<span class="coverage-types">' +
          escapeHtml(types) +
          "</span></span>" +
          '<span class="coverage-meta">' +
          (chunk.question_count === 1 ? "1 preg." : chunk.question_count + " preg.") +
          "</span></li>"
        );
      })
      .join("");
  }

  function countTypes(types) {
    const counts = {};
    for (const type of types) counts[type] = (counts[type] || 0) + 1;
    return Object.entries(counts)
      .map(([type, count]) => (count > 1 ? count + " " : "") + (TYPE_SHORT[type] || type))
      .join(" · ");
  }

  function providerReady() {
    if (!state.settings) return false;
    if (state.settings.provider === "demo") return true;
    return Boolean(state.settings[state.settings.provider + "_key_set"]);
  }

  function renderProvider() {
    if (!state.settings) return;
    const provider = state.settings.provider;
    const labels = { groq: "Groq", gemini: "Google Gemini", demo: "Modo demostración" };
    $("#provider-label").textContent = labels[provider] || provider;
    const dot = $("#provider-dot");
    dot.className = "status-dot";
    if (provider === "demo") {
      dot.classList.add("is-demo");
      $("#provider-hint").textContent = "Sin conexión · heurístico";
    } else if (providerReady()) {
      dot.classList.add("is-ready");
      $("#provider-hint").textContent = "Clave configurada";
    } else {
      $("#provider-hint").textContent = "Falta la API key";
    }
    $$(".provider-option").forEach((option) => {
      const input = $("input", option);
      input.checked = input.value === provider;
    });
    $$("[data-key-status]").forEach((node) => {
      const name = node.dataset.keyStatus;
      if (state.settings[name + "_key_set"]) {
        node.textContent = "Clave guardada";
        node.className = "key-status is-set";
      } else {
        node.textContent = "Sin clave";
        node.className = "key-status";
      }
    });
  }

  function updateGenerateState() {
    if (!state.settings) return;
    const button = $("#generate-btn");
    const hint = $("#generate-hint");
    const ready = providerReady();
    const hasDocs = state.docs.length > 0;
    const weights = state.config.weights;
    const sum = Object.values(weights).reduce((total, value) => total + Number(value || 0), 0);
    button.disabled = !hasDocs || !ready || sum <= 0 || state.generating;
    if (!hasDocs) hint.textContent = "Necesitas al menos un documento.";
    else if (!ready) hint.textContent = "Configura una API key o activa el modo demostración.";
    else if (sum <= 0) hint.textContent = "Activa al menos un formato de pregunta.";
    else hint.textContent = "El examen se generará bloque a bloque.";

    const callout = $("#provider-callout");
    if (!ready) {
      callout.innerHTML =
        '<div class="callout is-warning"><strong>Falta la clave del proveedor.</strong><br>Puedes generarla gratis o probar el modo demostración. <button class="text-btn" type="button" data-route="settings">Abrir ajustes</button></div>';
    } else if (state.settings.provider === "demo") {
      callout.innerHTML =
        '<div class="callout"><strong>Modo demostración.</strong> Genera preguntas heurísticas en local. No valida la semántica como un modelo real.</div>';
    } else {
      const chunks = state.coverage ? state.coverage.chunks.length : 0;
      callout.innerHTML =
        '<div class="callout"><strong>' +
        escapeHtml(state.settings.provider === "groq" ? "Groq" : "Gemini") +
        "</strong> · " +
        escapeHtml(state.settings.provider === "groq" ? state.settings.groq_model : state.settings.gemini_model) +
        (chunks ? "<br>Se enviarán " + chunks + " " + plural(chunks, "bloque", "bloques") + " al proveedor." : "") +
        "</div>";
    }
  }

  function readConfig() {
    state.config.count = clamp(Number($("#question-count").value) || 1, 1, 100);
    state.config.timerMinutes = clamp(Number($("#timer-minutes").value) || 30, 1, 240);
    state.config.timerEnabled = $("#timer-toggle").checked;
    state.config.immediateFeedback = $("#immediate-feedback").checked;
    state.config.chunkChars = clamp(Number($("#chunk-chars").value) || 4500, 1200, 12000);
    state.config.weights = weightsFromForm();
    return {
      question_count: state.config.count,
      weights: state.config.weights,
      difficulty: state.config.difficulty,
      timer_minutes: state.config.timerEnabled ? state.config.timerMinutes : null,
      immediate_feedback: state.config.immediateFeedback,
      chunk_chars: state.config.chunkChars,
    };
  }

  function weightsFromForm() {
    const weights = {};
    $$("[data-format]").forEach((checkbox) => {
      const type = checkbox.dataset.format;
      const amount = $('[data-weight="' + type + '"]');
      weights[type] = checkbox.checked ? clamp(Number(amount.value) || 0, 0, 100) : 0;
    });
    return weights;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function syncSetupForm() {
    $("#question-count").value = String(state.config.count);
    $("#timer-minutes").value = String(state.config.timerMinutes);
    $("#timer-toggle").checked = state.config.timerEnabled;
    $("#timer-minutes").disabled = !state.config.timerEnabled;
    $("#immediate-feedback").checked = state.config.immediateFeedback;
    $("#chunk-chars").value = String(state.config.chunkChars);
    setWeights(state.config.weights);
    setDifficulty(state.config.difficulty);
    updateWeightStatus();
  }

  function setWeights(weights) {
    $$("[data-format]").forEach((checkbox) => {
      const type = checkbox.dataset.format;
      const value = Number(weights[type] || 0);
      checkbox.checked = value > 0;
      const input = $('[data-weight="' + type + '"]');
      input.disabled = value <= 0;
      input.value = String(value);
      $(".format-row[data-type='" + type + "']").classList.toggle("is-enabled", value > 0);
    });
    updateWeightStatus();
  }

  function normalizeWeights() {
    const active = $$("[data-format]").filter((item) => item.checked);
    if (!active.length) return;
    const even = Math.floor(100 / active.length);
    let remainder = 100 - even * active.length;
    active.forEach((checkbox) => {
      const input = $('[data-weight="' + checkbox.dataset.format + '"]');
      input.value = String(even + (remainder-- > 0 ? 1 : 0));
    });
    updateWeightStatus();
  }

  function updateWeightStatus() {
    const active = $$("[data-format]").filter((item) => item.checked);
    const sum = active.reduce((total, checkbox) => total + (Number($('[data-weight="' + checkbox.dataset.format + '"]').value) || 0), 0);
    const status = $("#weight-status");
    if (!active.length) {
      status.textContent = "Activa al menos un formato.";
    } else if (sum === 100) {
      status.textContent = "Reparto correcto: 100 %.";
    } else {
      status.textContent = "Suma actual " + sum + " %. Se normalizará al generar.";
    }
  }

  function setDifficulty(value) {
    state.config.difficulty = value;
    $$("[data-difficulty]").forEach((button) => {
      const active = button.dataset.difficulty === value;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-checked", active ? "true" : "false");
    });
    $("#difficulty-hint").textContent = DIFFICULTY_HINTS[value];
  }

  async function generateExam() {
    if (state.generating) return;
    const config = readConfig();
    if (!providerReady()) {
      toast("Configura la API key o activa el modo demostración.", "error");
      go("settings");
      return;
    }
    const sum = Object.values(config.weights).reduce((total, value) => total + Number(value), 0);
    if (sum <= 0) {
      toast("Activa al menos un formato de pregunta.", "error");
      return;
    }
    state.generating = true;
    updateGenerateState();
    const overlay = $("#generation-overlay");
    overlay.hidden = false;
    $("#generation-title").textContent = "Preparando el temario";
    $("#generation-message").textContent = "Dividiendo el material en bloques equilibrados.";
    $("#generation-bar").style.width = "0%";
    $("#generation-count").textContent = "0 / 0";
    try {
      const data = await apiCall("generate", config);
      state.exam = data.exam;
      state.answers = {};
      state.flags = new Set();
      state.checked = new Set();
      state.current = 0;
      state.submitted = false;
      state.results = null;
      state.reviewFilter = "all";
      startTimer(state.exam.config.timer_minutes);
      $("#nav-exam").disabled = false;
      $("#nav-results").disabled = true;
      $("#nav-exam-dot").hidden = false;
      overlay.hidden = true;
      renderExam();
      go("exam");
      toast("Examen listo: " + state.exam.questions.length + " preguntas.", "success");
    } catch (error) {
      overlay.hidden = true;
      toast(cleanError(error), "error", 9000);
    } finally {
      state.generating = false;
      updateGenerateState();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Exam player                                                         */
  /* ------------------------------------------------------------------ */

  function startTimer(minutes) {
    clearInterval(state.timerInterval);
    state.timerInterval = null;
    state.timerWarned = false;
    if (!minutes) {
      state.timerDeadline = null;
      updateTimerDisplay();
      return;
    }
    state.timerDeadline = Date.now() + minutes * 60000;
    updateTimerDisplay();
    state.timerInterval = setInterval(() => {
      updateTimerDisplay();
      if (state.timerDeadline && Date.now() >= state.timerDeadline && !state.submitted) {
        clearInterval(state.timerInterval);
        state.timerInterval = null;
        toast("Se agotó el tiempo. El examen se entrega automáticamente.", "error", 7000);
        submitExam(true);
      }
    }, 1000);
  }

  function updateTimerDisplay() {
    const container = $("#exam-timer");
    const value = $("#timer-value");
    if (!state.timerDeadline) {
      container.classList.remove("is-low");
      value.textContent = "Libre";
      return;
    }
    const remaining = Math.max(0, Math.floor((state.timerDeadline - Date.now()) / 1000));
    const hours = Math.floor(remaining / 3600);
    const minutes = Math.floor((remaining % 3600) / 60);
    const seconds = remaining % 60;
    value.textContent =
      (hours ? String(hours).padStart(2, "0") + ":" : "") +
      String(minutes).padStart(2, "0") +
      ":" +
      String(seconds).padStart(2, "0");
    container.classList.toggle("is-low", remaining <= 60);
    if (remaining <= 60 && remaining > 0 && !state.timerWarned) {
      state.timerWarned = true;
      toast("Queda menos de un minuto.", "info");
    }
  }

  function currentQuestion() {
    return state.exam ? state.exam.questions[state.current] : null;
  }

  function hasAnswer(question) {
    const value = state.answers[question.id];
    return question.type === "short_answer" ? Boolean(String(value || "").trim()) : value !== undefined && value !== null;
  }

  function isRevealed(question) {
    if (!state.exam.config.immediate_feedback) return false;
    if (question.type === "short_answer") return state.checked.has(question.id);
    return hasAnswer(question);
  }

  function renderExam() {
    if (!state.exam) return;
    $("#nav-exam-dot").hidden = false;
    $("#exam-title").textContent = state.exam.title;
    const total = state.exam.questions.length;
    const types = countTypes(state.exam.questions.map((question) => question.type));
    $("#exam-meta").textContent =
      total +
      " preguntas · " +
      types +
      " · " +
      (state.exam.provider === "demo" ? "demostración" : state.exam.model || state.exam.provider);
    renderQuestion();
    renderQuestionGrid();
    updateTimerDisplay();
  }

  function renderQuestion() {
    const question = currentQuestion();
    if (!question) return;
    const total = state.exam.questions.length;
    $("#question-type").textContent = TYPE_LABELS[question.type] || question.type;
    $("#question-index").textContent = "Pregunta " + (state.current + 1) + " de " + total;
    $("#question-prompt").textContent = question.prompt;
    const flagButton = $("#flag-btn");
    const flagged = state.flags.has(question.id);
    flagButton.setAttribute("aria-pressed", flagged ? "true" : "false");
    $("span", flagButton).textContent = flagged ? "Marcada" : "Marcar";

    const revealed = isRevealed(question);
    const selected = state.answers[question.id];
    const options = $("#question-options");
    if (question.type === "short_answer") {
      options.innerHTML =
        '<div class="answer-field">' +
        '<label for="short-answer" class="sr-only">Tu respuesta</label>' +
        '<input id="short-answer" type="text" autocomplete="off" spellcheck="false" placeholder="Escribe la respuesta" value="' +
        escapeHtml(selected || "") +
        '"' +
        (revealed ? " disabled" : "") +
        ">" +
        (!state.exam.config.immediate_feedback || revealed
          ? ""
          : '<div><button class="btn btn-secondary btn-sm" type="button" id="check-answer">Comprobar respuesta</button></div>') +
        "</div>";
    } else {
      options.innerHTML = question.options
        .map((option, index) => {
          let className = "option";
          if (selected === index) className += " is-selected";
          if (revealed) {
            if (index === question.correct_index) className += " is-correct";
            else if (selected === index) className += " is-wrong";
          }
          return (
            '<button class="' +
            className +
            '" type="button" data-option="' +
            index +
            '"' +
            (revealed ? " disabled" : "") +
            ">" +
            '<span class="option-key">' +
            String.fromCharCode(65 + index) +
            "</span><span>" +
            escapeHtml(option) +
            "</span></button>"
          );
        })
        .join("");
    }

    const feedback = $("#question-feedback");
    if (revealed) {
      const correct = isCorrect(question, selected);
      feedback.hidden = false;
      feedback.innerHTML =
        '<div class="feedback-head ' +
        (correct ? "is-correct" : "is-wrong") +
        '">' +
        (correct ? "Respuesta correcta" : "Respuesta incorrecta") +
        "</div>" +
        (!correct
          ? "<div>La respuesta correcta es <strong>" + escapeHtml(displayCorrectAnswer(question)) + "</strong>.</div>"
          : "") +
        "<div>" +
        escapeHtml(question.explanation) +
        "</div>" +
        (question.source_quote ? "<blockquote>«" + escapeHtml(question.source_quote) + "»</blockquote>" : "");
    } else {
      feedback.hidden = true;
      feedback.innerHTML = "";
    }

    $("#prev-btn").disabled = state.current === 0;
    const next = $("#next-btn");
    next.textContent = state.current === total - 1 ? "Finalizar" : "Siguiente";
    $("#skip-btn").hidden = state.current === total - 1;
    renderQuestionGrid();
  }

  function renderQuestionGrid() {
    const questions = state.exam.questions;
    const answered = questions.filter(hasAnswer).length;
    $("#answered-count").textContent = answered + "/" + questions.length;
    $("#exam-progress-bar").style.width = questions.length ? (answered / questions.length) * 100 + "%" : "0%";
    $("#question-grid").innerHTML = questions
      .map((question, index) => {
        const classes = ["q-grid-btn"];
        if (hasAnswer(question)) classes.push("is-answered");
        if (state.flags.has(question.id)) classes.push("is-flagged");
        if (index === state.current) classes.push("is-current");
        return (
          '<button class="' +
          classes.join(" ") +
          '" type="button" data-question-index="' +
          index +
          '" aria-label="Ir a la pregunta ' +
          (index + 1) +
          '">' +
          (index + 1) +
          "</button>"
        );
      })
      .join("");
  }

  function setOptionAnswer(index) {
    const question = currentQuestion();
    if (!question || isRevealed(question)) return;
    state.answers[question.id] = index;
    renderQuestion();
  }

  function setShortAnswer(value) {
    const question = currentQuestion();
    if (!question) return;
    state.answers[question.id] = value;
    renderQuestionGrid();
  }

  function toggleFlag() {
    const question = currentQuestion();
    if (!question) return;
    if (state.flags.has(question.id)) state.flags.delete(question.id);
    else state.flags.add(question.id);
    renderQuestion();
  }

  function navigate(delta) {
    if (!state.exam) return;
    const next = state.current + delta;
    if (next < 0) return;
    if (next >= state.exam.questions.length) {
      submitExam(false);
      return;
    }
    state.current = next;
    renderQuestion();
    $("#question-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function normalizeAnswer(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9ñü\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isCorrect(question, answer) {
    if (!hasAnswer(question)) return false;
    if (question.type === "short_answer") {
      const given = normalizeAnswer(answer);
      const alternatives = String(question.correct_answer || "")
        .split(/[\/|]/)
        .map(normalizeAnswer)
        .filter(Boolean);
      if (alternatives.some((expected) => expected === given)) return true;
      if (alternatives.some((expected) => expected.length > 4 && (given.includes(expected) || expected.includes(given)))) {
        return true;
      }
      return alternatives.some((expected) => expected.length > 5 && editDistance(expected, given) <= 1);
    }
    return Number(answer) === Number(question.correct_index);
  }

  function editDistance(a, b) {
    if (a === b) return 0;
    const rows = a.length + 1;
    const cols = b.length + 1;
    const matrix = Array.from({ length: rows }, (_, index) => [index, ...Array(cols - 1).fill(0)]);
    for (let column = 1; column < cols; column += 1) matrix[0][column] = column;
    for (let row = 1; row < rows; row += 1) {
      for (let column = 1; column < cols; column += 1) {
        const cost = a[row - 1] === b[column - 1] ? 0 : 1;
        matrix[row][column] = Math.min(
          matrix[row - 1][column] + 1,
          matrix[row][column - 1] + 1,
          matrix[row - 1][column - 1] + cost
        );
      }
    }
    return matrix[rows - 1][cols - 1];
  }

  function displayCorrectAnswer(question) {
    if (question.type === "short_answer") return question.correct_answer;
    return question.options[question.correct_index] || question.correct_answer;
  }

  async function submitExam(auto) {
    if (!state.exam || state.submitted) return;
    const unanswered = state.exam.questions.filter((question) => !hasAnswer(question)).length;
    if (!auto) {
      const body =
        unanswered > 0
          ? "Quedan " + unanswered + " " + plural(unanswered, "pregunta sin responder", "preguntas sin responder") + ". Se contarán como fallos."
          : "Vas a corregir el examen. Después podrás revisar cada respuesta.";
      const confirmed = await confirmAction({
        title: "Finalizar examen",
        body,
        confirmLabel: "Corregir ahora",
      });
      if (!confirmed) return;
    }
    clearInterval(state.timerInterval);
    state.timerInterval = null;
    state.submitted = true;
    const details = state.exam.questions.map((question) => {
      const answer = state.answers[question.id];
      const answered = hasAnswer(question);
      return {
        question,
        answer,
        answered,
        correct: answered && isCorrect(question, answer),
      };
    });
    const correct = details.filter((item) => item.correct).length;
    const answeredCount = details.filter((item) => item.answered).length;
    state.results = {
      details,
      correct,
      wrong: answeredCount - correct,
      unanswered: details.length - answeredCount,
      score: details.length ? Math.round((correct / details.length) * 100) : 0,
    };
    $("#nav-results").disabled = false;
    $("#nav-exam-dot").hidden = true;
    renderResults();
    go("results");
    toast("Examen corregido: " + state.results.score + " %.", "success");
  }

  function renderResults() {
    if (!state.results || !state.exam) return;
    const result = state.results;
    $("#score-value").textContent = result.score + "%";
    $("#score-title").textContent = scoreTitle(result.score);
    $("#score-subtitle").textContent =
      state.exam.title +
      " · " +
      result.details.length +
      " preguntas · " +
      (state.exam.provider === "demo" ? "modo demostración" : state.exam.provider + " / " + state.exam.model);
    $("#stat-correct").textContent = String(result.correct);
    $("#stat-wrong").textContent = String(result.wrong);
    $("#stat-unanswered").textContent = String(result.unanswered);
    $("#stat-type").textContent = bestType(result.details);
    $("#new-exam").textContent = "Nuevo examen";

    const warnings = state.exam.warnings || [];
    $("#warnings-box").innerHTML = warnings.length
      ? '<div class="warning-box"><ul>' + warnings.map((warning) => "<li>" + escapeHtml(warning) + "</li>").join("") + "</ul></div>"
      : "";
    renderReview();
  }

  function scoreTitle(score) {
    if (score >= 90) return "Dominio sólido";
    if (score >= 75) return "Base bien asentada";
    if (score >= 60) return "Aprobado ajustado";
    if (score >= 40) return "Hay que repasar";
    return "Temario pendiente";
  }

  function bestType(details) {
    const groups = {};
    for (const detail of details) {
      const key = detail.question.type;
      if (!groups[key]) groups[key] = { total: 0, correct: 0 };
      groups[key].total += 1;
      if (detail.correct) groups[key].correct += 1;
    }
    const ranked = Object.entries(groups).sort(
      (a, b) => b[1].correct / b[1].total - a[1].correct / a[1].total
    );
    return ranked.length ? TYPE_SHORT[ranked[0][0]] || ranked[0][0] : "—";
  }

  function renderReview() {
    if (!state.results) return;
    const filter = state.reviewFilter;
    const details = state.results.details.filter((detail) => {
      if (filter === "wrong") return !detail.correct;
      if (filter === "flagged") return state.flags.has(detail.question.id);
      return true;
    });
    $$("#review-filter button").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.filter === filter);
    });
    const list = $("#review-list");
    if (!details.length) {
      list.innerHTML = '<li class="empty-state compact">No hay preguntas en este filtro.</li>';
      return;
    }
    list.innerHTML = details
      .map((detail) => {
        const question = detail.question;
        const number = state.exam.questions.findIndex((item) => item.id === question.id) + 1;
        const given = detail.answered
          ? question.type === "short_answer"
            ? detail.answer
            : question.options[detail.answer]
          : "Sin responder";
        return (
          '<li class="review-item">' +
          '<div class="review-top">' +
          '<span class="review-status ' +
          (detail.correct ? "is-correct" : "is-wrong") +
          '">' +
          (detail.correct ? "✓" : "×") +
          "</span>" +
          '<span class="review-ref">#' +
          number +
          " · " +
          escapeHtml(TYPE_LABELS[question.type] || question.type) +
          "</span>" +
          '<span class="review-topic">' +
          escapeHtml(question.topic || question.source_document || "") +
          "</span></div>" +
          '<p class="review-prompt">' +
          escapeHtml(question.prompt) +
          "</p>" +
          '<p class="review-answer">Tu respuesta: <strong class="' +
          (detail.correct ? "" : "is-wrong") +
          '">' +
          escapeHtml(given) +
          "</strong></p>" +
          (!detail.correct
            ? '<p class="review-answer">Correcta: <strong>' + escapeHtml(displayCorrectAnswer(question)) + "</strong></p>"
            : "") +
          '<p class="review-explanation">' +
          escapeHtml(question.explanation) +
          "</p>" +
          (question.source_quote
            ? '<blockquote class="review-quote">«' + escapeHtml(question.source_quote) + "»" + (question.source_document ? " · " + escapeHtml(question.source_document) : "") + "</blockquote>"
            : "") +
          "</li>"
        );
      })
      .join("");
  }

  async function exportExam(format, includeAnswers, button) {
    if (!state.exam) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Exportando…";
    try {
      const data = await apiCall("export_exam", {
        exam: state.exam,
        format,
        include_answers: includeAnswers,
      });
      toast("Guardado en " + data.path, "success", 7000);
    } catch (error) {
      toast(cleanError(error), "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function newExam() {
    clearInterval(state.timerInterval);
    state.timerInterval = null;
    state.exam = null;
    state.results = null;
    state.answers = {};
    state.flags = new Set();
    state.checked = new Set();
    state.submitted = false;
    state.current = 0;
    state.timerDeadline = null;
    $("#nav-exam").disabled = true;
    $("#nav-results").disabled = true;
    $("#nav-exam-dot").hidden = true;
    syncSetupForm();
    go("setup");
    refreshCoverage();
  }

  /* ------------------------------------------------------------------ */
  /* Settings                                                            */
  /* ------------------------------------------------------------------ */

  function hydrateSettings() {
    const settings = state.settings;
    if (!settings) return;
    $("#groq-model").value = settings.groq_model || "";
    $("#gemini-model").value = settings.gemini_model || "";
    $("#default-count").value = String(settings.default_question_count || 20);
    $("#default-timer").value = String(settings.default_timer_minutes || 30);
    $("#default-chunk").value = String(settings.chunk_chars || 4500);
    $("#exclusion-keywords").value = (settings.exclusion_keywords || []).join("\n");
    $("#about-version").textContent = "v" + (settings.version || "1.0.0");
    $("#about-path").textContent = settings.data_dir || "—";
    renderProvider();
    renderThemeButtons();
  }

  function renderSettings() {
    hydrateSettings();
    renderProvider();
  }

  async function saveSettings(patch, { silent = false } = {}) {
    try {
      const data = await apiCall("save_settings", patch);
      state.settings = data.settings;
      renderProvider();
      updateGenerateState();
      if (!silent) toast("Ajustes guardados.", "success");
      return true;
    } catch (error) {
      toast(cleanError(error), "error");
      return false;
    }
  }

  function applyTheme(theme) {
    const resolved =
      theme === "system"
        ? window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : theme;
    document.documentElement.dataset.theme = resolved;
    const labels = { light: "Tema claro", dark: "Tema oscuro", system: "Tema del sistema" };
    $("#theme-label").textContent = labels[theme] || labels.system;
    renderThemeButtons();
  }

  function renderThemeButtons() {
    const theme = state.settings ? state.settings.theme : "system";
    $$("[data-theme-choice]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.themeChoice === theme);
    });
  }

  async function cycleTheme() {
    if (!state.settings) return;
    const order = ["light", "dark", "system"];
    const next = order[(order.indexOf(state.settings.theme) + 1) % order.length];
    state.settings.theme = next;
    applyTheme(next);
    await saveSettings({ theme: next }, { silent: true });
  }

  async function testProvider(provider, button) {
    const keyInput = provider === "groq" ? $("#groq-key") : $("#gemini-key");
    const key = keyInput.value.trim();
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Probando…";
    try {
      const data = await apiCall("test_provider", { provider, api_key: key });
      toast(data.message || "Conexión correcta.", "success", 6000);
      if (key) {
        const saved = await saveSettings({ provider, api_keys: { [provider]: key } }, { silent: true });
        if (saved) keyInput.value = "";
      }
    } catch (error) {
      toast(cleanError(error), "error", 8000);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function clearKey(provider) {
    const confirmed = await confirmAction({
      title: "Borrar API key",
      body: "La clave de " + (provider === "groq" ? "Groq" : "Gemini") + " se eliminará de este equipo.",
      confirmLabel: "Borrar clave",
      danger: true,
    });
    if (!confirmed) return;
    const saved = await saveSettings({ clear_key: provider });
    if (saved) toast("Clave eliminada.", "success");
  }

  async function saveProviderConfig() {
    const payload = {
      groq_model: $("#groq-model").value.trim(),
      gemini_model: $("#gemini-model").value.trim(),
      api_keys: {},
    };
    const groqKey = $("#groq-key").value.trim();
    const geminiKey = $("#gemini-key").value.trim();
    if (groqKey) payload.api_keys.groq = groqKey;
    if (geminiKey) payload.api_keys.gemini = geminiKey;
    const saved = await saveSettings(payload);
    if (saved) {
      $("#groq-key").value = "";
      $("#gemini-key").value = "";
    }
  }

  async function saveExclusions() {
    const keywords = $("#exclusion-keywords")
      .value.split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!keywords.length) {
      toast("Añade al menos una frase de exclusión.", "error");
      return;
    }
    const saved = await saveSettings({ exclusion_keywords: keywords });
    if (saved) await refreshCoverage();
  }

  async function saveDefaults() {
    const payload = {
      default_question_count: clamp(Number($("#default-count").value) || 20, 1, 100),
      default_timer_minutes: clamp(Number($("#default-timer").value) || 30, 1, 240),
      chunk_chars: clamp(Number($("#default-chunk").value) || 4500, 1200, 12000),
    };
    const saved = await saveSettings(payload);
    if (saved) {
      state.config.count = payload.default_question_count;
      state.config.timerMinutes = payload.default_timer_minutes;
      state.config.chunkChars = payload.chunk_chars;
      syncSetupForm();
      refreshCoverage();
    }
  }

  async function clearSession() {
    const confirmed = await confirmAction({
      title: "Vaciar la sesión",
      body: "Se quitarán los documentos y el examen actual. Los archivos originales no se modifican.",
      confirmLabel: "Vaciar sesión",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await apiCall("clear_session");
    } catch (error) {
      // La sesión local se limpia igualmente si el puente ya no responde.
    }
    state.docs = [];
    state.selectedDocId = null;
    state.previewCache.clear();
    state.exam = null;
    state.results = null;
    state.submitted = false;
    state.coverage = null;
    $("#nav-exam").disabled = true;
    $("#nav-results").disabled = true;
    $("#nav-exam-dot").hidden = true;
    renderDocuments();
    renderCoverage();
    updateGenerateState();
    go("documents");
    toast("Sesión vacíada.");
  }

  function updateNavState() {
    $("#nav-doc-count").textContent = String(state.docs.length);
    $("#nav-exam").disabled = !state.exam;
    $("#nav-results").disabled = !state.submitted;
    $("#nav-exam-dot").hidden = !state.exam || state.submitted;
  }

  /* ------------------------------------------------------------------ */
  /* Events                                                              */
  /* ------------------------------------------------------------------ */

  function bindEvents() {
    document.addEventListener("click", async (event) => {
      const routeButton = event.target.closest("[data-route]");
      if (routeButton) {
        go(routeButton.dataset.route);
        return;
      }
      const action = event.target.closest("[data-action]");
      if (action) {
        const name = action.dataset.action;
        if (name === "add-docs") await pickDocuments();
        if (name === "go-documents") go("documents");
        if (name === "new-exam") newExam();
        return;
      }
      const remove = event.target.closest("[data-remove-doc]");
      if (remove) {
        event.stopPropagation();
        await removeDocument(remove.dataset.removeDoc);
        return;
      }
      const docItem = event.target.closest("[data-doc-id]");
      if (docItem) {
        await selectDocument(docItem.dataset.docId);
        return;
      }
      const step = event.target.closest("[data-step]");
      if (step) {
        const input = $("#question-count");
        input.value = String(clamp((Number(input.value) || 1) + Number(step.dataset.step), 1, 100));
        state.config.count = Number(input.value);
        updateWeightStatus();
        scheduleCoverage();
        return;
      }
      const preset = event.target.closest("[data-preset]");
      if (preset) {
        const presets = {
          test: { multiple_choice: 100, true_false: 0, short_answer: 0 },
          mixed: { multiple_choice: 60, true_false: 30, short_answer: 10 },
          tf: { multiple_choice: 0, true_false: 100, short_answer: 0 },
        };
        setWeights(presets[preset.dataset.preset]);
        state.config.weights = weightsFromForm();
        scheduleCoverage();
        updateGenerateState();
        return;
      }
      const difficulty = event.target.closest("[data-difficulty]");
      if (difficulty) {
        setDifficulty(difficulty.dataset.difficulty);
        return;
      }
      const option = event.target.closest("[data-option]");
      if (option) {
        setOptionAnswer(Number(option.dataset.option));
        return;
      }
      const gridButton = event.target.closest("[data-question-index]");
      if (gridButton) {
        state.current = Number(gridButton.dataset.questionIndex);
        renderQuestion();
        return;
      }
      const filter = event.target.closest("[data-filter]");
      if (filter) {
        state.reviewFilter = filter.dataset.filter;
        renderReview();
        return;
      }
      const themeChoice = event.target.closest("[data-theme-choice]");
      if (themeChoice) {
        state.settings.theme = themeChoice.dataset.themeChoice;
        applyTheme(state.settings.theme);
        await saveSettings({ theme: state.settings.theme }, { silent: true });
        return;
      }
      const testButton = event.target.closest("[data-test-provider]");
      if (testButton) {
        await testProvider(testButton.dataset.testProvider, testButton);
        return;
      }
      const clearButton = event.target.closest("[data-clear-key]");
      if (clearButton) {
        await clearKey(clearButton.dataset.clearKey);
        return;
      }
    });

    $("#browse-btn").addEventListener("click", (event) => {
      event.stopPropagation();
      pickDocuments();
    });

    const dropzone = $("#dropzone");
    dropzone.addEventListener("click", (event) => {
      if (!event.target.closest("button")) pickDocuments();
    });
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        pickDocuments();
      }
    });
    ["dragenter", "dragover"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-dragging");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.remove("is-dragging");
      });
    });
    document.addEventListener("dragover", (event) => event.preventDefault());
    document.addEventListener("drop", (event) => event.preventDefault());

    $("#preview-toggle").addEventListener("click", () => {
      $(".panel-preview").classList.toggle("is-collapsed");
    });

    $("#question-count").addEventListener("input", () => {
      state.config.count = clamp(Number($("#question-count").value) || 1, 1, 100);
      scheduleCoverage();
    });
    $("#timer-toggle").addEventListener("change", () => {
      $("#timer-minutes").disabled = !$("#timer-toggle").checked;
    });
    $("#timer-minutes").addEventListener("input", scheduleCoverage);
    $("#chunk-chars").addEventListener("input", () => {
      state.config.chunkChars = clamp(Number($("#chunk-chars").value) || 4500, 1200, 12000);
      scheduleCoverage();
    });
    $("#immediate-feedback").addEventListener("change", () => {
      state.config.immediateFeedback = $("#immediate-feedback").checked;
    });
    $$("[data-format]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const type = checkbox.dataset.format;
        const input = $('[data-weight="' + type + '"]');
        input.disabled = !checkbox.checked;
        if (!checkbox.checked) input.value = "0";
        const active = $$("[data-format]").filter((item) => item.checked);
        if (!active.length) {
          checkbox.checked = true;
          input.disabled = false;
        }
        if (checkbox.checked) normalizeWeights();
        state.config.weights = weightsFromForm();
        updateWeightStatus();
        updateGenerateState();
        scheduleCoverage();
      });
    });
    $$("[data-weight]").forEach((input) => {
      input.addEventListener("input", () => {
        state.config.weights = weightsFromForm();
        updateWeightStatus();
        updateGenerateState();
        scheduleCoverage();
      });
    });
    $("#generate-btn").addEventListener("click", generateExam);

    $("#prev-btn").addEventListener("click", () => navigate(-1));
    $("#next-btn").addEventListener("click", () => navigate(1));
    $("#skip-btn").addEventListener("click", () => navigate(1));
    $("#flag-btn").addEventListener("click", toggleFlag);
    $("#submit-exam").addEventListener("click", () => submitExam(false));
    $("#question-options").addEventListener("input", (event) => {
      if (event.target.id === "short-answer") setShortAnswer(event.target.value);
    });
    $("#question-options").addEventListener("click", (event) => {
      const check = event.target.closest("#check-answer");
      if (!check) return;
      const question = currentQuestion();
      if (question && String(state.answers[question.id] || "").trim()) {
        state.checked.add(question.id);
        renderQuestion();
      } else {
        toast("Escribe una respuesta antes de comprobarla.");
      }
    });

    $("#review-filter").addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      state.reviewFilter = button.dataset.filter;
      renderReview();
    });
    $("#export-md").addEventListener("click", (event) => exportExam("md", false, event.currentTarget));
    $("#export-pdf").addEventListener("click", (event) => exportExam("pdf", false, event.currentTarget));
    $("#export-md-solutions").addEventListener("click", (event) => exportExam("md", true, event.currentTarget));
    $("#export-pdf-solutions").addEventListener("click", (event) => exportExam("pdf", true, event.currentTarget));
    $("#new-exam").addEventListener("click", newExam);

    $("#provider-options").addEventListener("change", async (event) => {
      const input = event.target.closest("input[type='radio']");
      if (!input) return;
      await saveSettings({ provider: input.value }, { silent: true });
      updateGenerateState();
      toast("Proveedor activo: " + input.value + ".");
    });
    $("#save-provider-fields")?.addEventListener("click", saveProviderConfig);
    $("#save-exclusions").addEventListener("click", saveExclusions);
    $("#save-defaults").addEventListener("click", saveDefaults);
    $("#save-provider")?.addEventListener("click", saveProviderConfig);
    $("#theme-toggle").addEventListener("click", cycleTheme);
    $("#open-data-dir").addEventListener("click", async () => {
      try {
        await apiCall("open_data_dir");
      } catch (error) {
        toast(cleanError(error), "error");
      }
    });
    $("#clear-session").addEventListener("click", clearSession);

    document.addEventListener("keydown", (event) => {
      if (!$("#modal").open && event.key === "Escape" && state.route === "exam") return;
      if (state.route !== "exam" || state.submitted) return;
      if (event.target.matches("input, textarea")) return;
      if (event.key === "ArrowRight") {
        event.preventDefault();
        navigate(1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        navigate(-1);
      } else if (/^[1-4]$/.test(event.key)) {
        const question = currentQuestion();
        if (question && question.type !== "short_answer") {
          const index = Number(event.key) - 1;
          if (index < question.options.length) setOptionAnswer(index);
        }
      } else if (event.key.toLowerCase() === "f") {
        toggleFlag();
      }
    });

    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        if (state.settings && state.settings.theme === "system") applyTheme("system");
      });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Public hooks used by the Python bridge                              */
  /* ------------------------------------------------------------------ */

  window.ExamForge = window.ExamForge || {};

  window.ExamForge.onGenerationProgress = (current, total, message) => {
    const safeTotal = Number(total) || 0;
    const safeCurrent = Number(current) || 0;
    const percent = safeTotal ? Math.round((safeCurrent / safeTotal) * 100) : 0;
    $("#generation-bar").style.width = percent + "%";
    $("#generation-count").textContent = safeCurrent + " / " + safeTotal;
    if (message) $("#generation-message").textContent = message;
    if (safeCurrent > 0 && safeCurrent < safeTotal) $("#generation-title").textContent = "Redactando preguntas";
    if (safeTotal && safeCurrent >= safeTotal) $("#generation-title").textContent = "Validando el examen";
  };

  window.ExamForge.handleDroppedFiles = (paths) => {
    addDocuments(paths || []);
  };

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    if (window.pywebview && window.pywebview.api) {
      boot();
    } else {
      window.addEventListener("pywebviewready", boot, { once: true });
      setTimeout(() => {
        if (!state.booted && window.pywebview && window.pywebview.api) boot();
      }, 1200);
    }
  });
})();
