/* ==========================================================================
   SOP Builder - browser application

   No framework, no build step, no external requests: the whole interface is
   three files served by the same FastAPI process that generates documents.
   That keeps the air-gap promise literally true and means a reinstall can
   never break a dependency the office cannot download.
   ========================================================================== */

(function () {
  "use strict";

  // ------------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------------
  var state = {
    step: 1,
    people: [],          // {name, role, department}
    tools: [],           // {name, category, custom, description, source, version}
    ready: false,
    health: null,
    profile: null,
    selectedModel: null,
    generating: false,
    lastResult: null
  };

  var el = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  };

  // ------------------------------------------------------------------------
  // API helpers
  // ------------------------------------------------------------------------
  function api(path, options) {
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.detail || ("Request failed (" + response.status + ")"));
          error.status = response.status;
          error.body = body;
          throw error;
        }
        return body;
      });
    });
  }

  function postJSON(path, payload, headers) {
    var allHeaders = { "Content-Type": "application/json" };
    for (var key in (headers || {})) { allHeaders[key] = headers[key]; }
    return api(path, { method: "POST", headers: allHeaders, body: JSON.stringify(payload) });
  }

  // ------------------------------------------------------------------------
  // Health and readiness
  // ------------------------------------------------------------------------
  function setPill(kind, text, busy) {
    var pill = el("statusPill");
    pill.className = "pill pill--" + kind + (busy ? " pill--busy" : "");
    el("statusText").textContent = text;
  }

  function showNotice(kind, icon, title, bodyHtml) {
    var notice = el("setupNotice");
    notice.className = "notice notice--" + kind;
    el("setupIcon").textContent = icon;
    el("setupTitle").textContent = title;
    el("setupBody").innerHTML = bodyHtml;
  }

  function checkHealth() {
    return api("/api/v1/health").then(function (health) {
      state.health = health;
      state.ready = health.status === "ready";

      if (state.ready) {
        setPill("ok", "Ready", false);
        el("setupNotice").classList.add("hidden");
      } else if (!health.ollama_reachable) {
        setPill("danger", "Not ready", false);
        showNotice("danger", "⚠️", "The writing engine is not running",
          "This application needs a free program called <strong>Ollama</strong> running in the " +
          "background.<br><br>" +
          "1. Install it once from <strong>ollama.com/download</strong><br>" +
          "2. Then open a terminal and run <code>ollama serve</code><br><br>" +
          "This page checks again every few seconds — no need to reload.");
      } else if (!health.model_available) {
        setPill("warn", "Model missing", false);
        showNotice("warn", "📥", "The writing model has not been downloaded yet",
          "Open a terminal and run this once:<br><br>" +
          "<code>ollama pull " + esc(health.model_name) + "</code><br><br>" +
          "It is a few gigabytes and only needs doing once. " +
          "Open <strong>Settings</strong> to pick a smaller model if your computer is older.");
      }
      el("setupNotice").classList.toggle("hidden", state.ready);
      updateReadyWarning();
      return health;
    }).catch(function () {
      state.ready = false;
      setPill("danger", "No connection", false);
      showNotice("danger", "✕", "Lost contact with the application",
        "The program window may have been closed. Restart it and refresh this page.");
      el("setupNotice").classList.remove("hidden");
    });
  }

  function updateReadyWarning() {
    var warning = el("readyWarning");
    if (!warning) { return; }
    warning.classList.toggle("hidden", state.ready);
    el("generateBtn").disabled = !state.ready || state.generating;
    if (!state.ready && state.health) {
      el("readyWarningBody").innerHTML = state.health.ollama_reachable
        ? "The model <code>" + esc(state.health.model_name) + "</code> has not been downloaded. " +
          "Open <strong>Settings</strong> to choose one."
        : "The writing engine (Ollama) is not running. See the message at the top of the page.";
    }
  }

  // ------------------------------------------------------------------------
  // Combobox: a searchable dropdown backed by the catalog API
  // ------------------------------------------------------------------------
  function makeCombo(options) {
    var input = el(options.inputId);
    var menu = el(options.menuId);
    var timer = null;
    var activeIndex = -1;
    var current = [];

    function close() {
      menu.classList.remove("combo__menu--open");
      input.setAttribute("aria-expanded", "false");
      activeIndex = -1;
    }

    function render(results, query) {
      current = results;
      var html = "";
      results.forEach(function (entry, index) {
        var meta = entry.category || entry.tier || "";
        html += '<button type="button" class="combo__option' +
          (index === activeIndex ? " combo__option--active" : "") +
          '" role="option" data-index="' + index + '">' +
          '<span class="combo__option__name">' + esc(entry.name) + "</span>" +
          (meta ? '<span class="combo__option__meta">' + esc(meta) + "</span>" : "") +
          "</button>";
      });

      var typed = query.trim();
      var exact = results.some(function (entry) {
        return entry.name.toLowerCase() === typed.toLowerCase();
      });
      if (typed && !exact && options.allowCustom !== false) {
        html += '<button type="button" class="combo__option combo__option--add" data-add="1">' +
          "＋ " + esc(options.addLabel || "Add") + ' “' + esc(typed) + '”</button>';
      }
      if (!html) {
        html = '<div class="combo__empty">Nothing found. Keep typing to add it yourself.</div>';
      }

      menu.innerHTML = html;
      menu.classList.add("combo__menu--open");
      input.setAttribute("aria-expanded", "true");
    }

    // Responses can arrive out of order on a slow machine; only the newest
    // request is allowed to paint, otherwise the list shows results for a
    // query the user has already typed past.
    var sequence = 0;

    function search() {
      var query = input.value.trim();
      var ticket = ++sequence;
      api("/api/v1/catalog/" + options.kind + "?q=" + encodeURIComponent(query) + "&limit=12")
        .then(function (data) {
          if (ticket !== sequence) { return; }
          render(data.results || [], query);
        })
        .catch(function () { if (ticket === sequence) { close(); } });
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(search, 140);
    });
    input.addEventListener("focus", search);

    input.addEventListener("keydown", function (event) {
      var buttons = menu.querySelectorAll(".combo__option");
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!buttons.length) { return; }
        activeIndex += (event.key === "ArrowDown" ? 1 : -1);
        if (activeIndex < 0) { activeIndex = buttons.length - 1; }
        if (activeIndex >= buttons.length) { activeIndex = 0; }
        buttons.forEach(function (button, index) {
          button.classList.toggle("combo__option--active", index === activeIndex);
        });
        buttons[activeIndex].scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (activeIndex >= 0 && buttons[activeIndex]) {
          buttons[activeIndex].click();
        } else if (input.value.trim()) {
          options.onPick({ name: input.value.trim(), custom: true });
          close();
        }
      } else if (event.key === "Escape") {
        close();
      }
    });

    menu.addEventListener("click", function (event) {
      var button = event.target.closest(".combo__option");
      if (!button) { return; }
      if (button.dataset.add) {
        options.onPick({ name: input.value.trim(), custom: true });
      } else {
        options.onPick(current[Number(button.dataset.index)]);
      }
      close();
    });

    document.addEventListener("click", function (event) {
      if (!menu.contains(event.target) && event.target !== input) { close(); }
    });

    return { close: close, input: input };
  }

  // ------------------------------------------------------------------------
  // People
  // ------------------------------------------------------------------------
  function renderPeople() {
    var container = el("peopleChips");
    container.innerHTML = state.people.map(function (person, index) {
      return '<span class="chip"><span class="chip__text">' +
        '<span class="chip__name">' + esc(person.name) + "</span>" +
        '<span class="chip__meta">, ' + esc(person.role) +
        (person.department ? ", " + esc(person.department) : "") + "</span></span>" +
        '<button class="chip__remove" type="button" data-person="' + index +
        '" aria-label="Remove ' + esc(person.name) + '">×</button></span>';
    }).join("");
  }

  function addPerson() {
    var name = el("personName").value.trim();
    var role = el("personRole").value.trim();
    var department = el("personDept").value.trim();
    var error = el("personError");

    if (!name || !role) {
      error.classList.add("field__error--shown");
      error.textContent = !name ? "Please enter the person's name." : "Please enter a designation.";
      (!name ? el("personName") : el("personRole")).focus();
      return;
    }
    error.classList.remove("field__error--shown");

    state.people.push({ name: name, role: role, department: department });
    renderPeople();

    // Persist the person so their name (and role/department) autocompletes next
    // time — they should never have to be retyped from memory.
    postJSON("/api/v1/people/remember", { name: name, role: role, department: department })
      .catch(function () { /* saving is a convenience, never block adding */ });

    el("personName").value = "";
    el("personRole").value = "";
    el("personDept").value = "";
    el("personName").focus();
  }

  // ------------------------------------------------------------------------
  // Tools
  // ------------------------------------------------------------------------
  function renderTools() {
    var container = el("toolChips");
    container.innerHTML = state.tools.map(function (tool, index) {
      var custom = tool.custom;
      return '<span class="chip ' + (custom ? "chip--custom" : "chip--tool") + '">' +
        '<span class="chip__text"><span class="chip__name">' + esc(tool.name) + "</span></span>" +
        (custom ? '<span class="chip__badge">added</span>' : "") +
        '<button class="chip__remove" type="button" data-tool="' + index +
        '" aria-label="Remove ' + esc(tool.name) + '">×</button></span>';
    }).join("");
  }

  function addTool(tool) {
    var exists = state.tools.some(function (item) {
      return item.name.toLowerCase() === tool.name.toLowerCase();
    });
    if (!exists) { state.tools.push(tool); renderTools(); }
    el("toolInput").value = "";
  }

  // --- unknown-tool research loop -----------------------------------------
  var research = { name: "", attempt: 0, rejected: [], draft: null };

  function openToolModal(name) {
    research = { name: name, attempt: 0, rejected: [], draft: null };
    el("toolModalTitle").textContent = "Tell us about " + name;
    el("toolModalLede").innerHTML =
      "<strong>" + esc(name) + "</strong> is not in our list yet. Describe it in a sentence " +
      "or two — or let the offline model suggest a description for you to check.";
    el("toolRejected").innerHTML = "";
    el("toolOwnText").value = "";
    el("toolModal").classList.add("modal-backdrop--open");

    // Open straight into the box the user can type in. Asking the model is a
    // deliberate choice behind a button: on a CPU-only machine that call takes
    // the better part of a minute, and most officers can describe their own
    // departmental tool faster than the model can guess at it.
    showToolPane("own");
    el("toolOwnText").focus();
  }

  function showToolPane(which) {
    el("toolModalBusy").classList.toggle("hidden", which !== "busy");
    el("toolDraftBlock").classList.toggle("hidden", which !== "draft");
    el("toolHintBlock").classList.toggle("hidden", which !== "hint");
    el("toolOwnBlock").classList.toggle("hidden", which !== "own");
    el("toolModalClose").classList.toggle("hidden", which !== "busy");
  }

  function nextDraft(hint) {
    research.attempt += 1;
    showToolPane("busy");
    research.aborter = new AbortController();

    api("/api/v1/tools/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: research.aborter.signal,
      body: JSON.stringify({
        name: research.name,
        attempt: research.attempt,
        rejected: research.rejected,
        hint: hint || ""
      })
    }).then(function (draft) {
      research.draft = draft;

      if (draft.needs_user_input || !draft.description) {
        el("toolOwnText").value = "";
        el("toolModalLede").textContent = draft.message ||
          "We could not find a description. Please write one yourself.";
        showToolPane("own");
        return;
      }

      el("toolDraftText").textContent = draft.description;
      var source = draft.source === "catalog" ? "From the built-in catalogue"
        : draft.source === "web_lookup" ? "From a public reference site"
        : "Suggested by the offline model";
      el("toolDraftSource").textContent =
        source + " · attempt " + research.attempt + " · please verify before accepting";
      showToolPane("draft");
    }).catch(function (error) {
      if (error.name === "AbortError") { return; }  // the user cancelled
      el("toolModalLede").textContent =
        "The suggestion could not be produced (" + error.message +
        "). Please write a description yourself.";
      showToolPane("own");
      el("toolOwnText").focus();
    });
  }

  function acceptTool(description, source) {
    var name = research.draft && research.draft.name ? research.draft.name : research.name;
    var category = (research.draft && research.draft.category) || "";

    addTool({
      name: name,
      category: category,
      custom: true,
      description: description,
      source: source,
      version: (research.draft && research.draft.typical_version) || ""
    });

    // Queue for administrator approval; failure here must not block the user.
    postJSON("/api/v1/tools/accept", {
      name: name, description: description, category: category, source: source
    }).catch(function () { /* the tool is already usable in this document */ });

    closeToolModal();
  }

  function closeToolModal() {
    el("toolModal").classList.remove("modal-backdrop--open");
    el("toolInput").value = "";
  }

  // ------------------------------------------------------------------------
  // Steps
  // ------------------------------------------------------------------------
  function goStep(step) {
    if (state.generating) { return; }
    [1, 2, 3, 4].forEach(function (index) {
      el("step" + index).classList.toggle("hidden", index !== step);
    });
    el("working").classList.add("hidden");
    el("result").classList.add("hidden");
    el("errorCard").classList.add("hidden");
    el("stepper").classList.remove("hidden");

    state.step = step;
    document.querySelectorAll(".step").forEach(function (button) {
      var target = Number(button.dataset.goto);
      button.classList.toggle("step--active", target === step);
      button.classList.toggle("step--done", target < step);
    });

    if (step === 4) { renderReview(); }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function validateStep1() {
    var ok = true;
    [["projectName", "projectNameError"], ["departmentInput", "departmentError"],
     ["description", "descriptionError"]].forEach(function (pair) {
      var input = el(pair[0]);
      var bad = !input.value.trim();
      el(pair[1]).classList.toggle("field__error--shown", bad);
      input.setAttribute("aria-invalid", bad ? "true" : "false");
      if (bad && ok) { input.focus(); }
      if (bad) { ok = false; }
    });
    return ok;
  }

  function renderReview() {
    var rows = [
      ["Project", el("projectName").value.trim(), "No name entered"],
      ["Department", el("departmentInput").value.trim(), "No department chosen"],
      ["What it does", el("description").value.trim(), "No description entered"],
      ["Sensitivity", el("classification").value, ""],
      ["People", state.people.length
        ? state.people.map(function (person) {
            return person.name + " (" + person.role +
              (person.department ? ", " + person.department : "") + ")";
          }).join(" · ")
        : "", "None added — standard government roles will be used"],
      ["Tools and systems", state.tools.length
        ? state.tools.map(function (tool) { return tool.name; }).join(" · ")
        : "", "None added — general office tooling will be assumed"],
      ["Writing model", (state.selectedModel || (state.health && state.health.model_name) || "default"), ""]
    ];

    el("reviewList").innerHTML = rows.map(function (row) {
      var value = row[1];
      return '<div class="review__row"><div class="review__key">' + esc(row[0]) + "</div>" +
        '<div class="review__val' + (value ? "" : " review__val--empty") + '">' +
        esc(value || row[2]) + "</div></div>";
    }).join("");
  }

  // ------------------------------------------------------------------------
  // Generation
  // ------------------------------------------------------------------------
  var TASKS = [
    { label: "Working out who is accountable", weight: 24 },
    { label: "Working out the technical setup", weight: 30 },
    { label: "Writing the step-by-step instructions", weight: 32 },
    { label: "Checking the document is complete", weight: 9 },
    { label: "Preparing the Word file", weight: 5 }
  ];

  var progressState = { timer: null, ticker: null, started: 0, index: 0, percent: 0, aborter: null };

  function renderTasks() {
    el("taskList").innerHTML = TASKS.map(function (task, index) {
      var status = index < progressState.index ? "done" : (index === progressState.index ? "active" : "");
      return '<div class="tasklist__item' + (status ? " tasklist__item--" + status : "") + '">' +
        '<span class="tasklist__mark">' + (status === "done" ? "✓" : index + 1) + "</span>" +
        "<span>" + esc(task.label) + "</span></div>";
    }).join("");
  }

  function setPercent(value) {
    progressState.percent = Math.min(99, value);
    var circumference = 327;
    el("progressBar").style.strokeDashoffset =
      String(circumference - (circumference * progressState.percent) / 100);
    el("progressPercent").textContent = Math.round(progressState.percent) + "%";
  }

  function startProgress() {
    progressState.started = Date.now();
    progressState.index = 0;
    setPercent(0);
    renderTasks();
    el("progressDetail").textContent = TASKS[0].label + "…";

    // The server does not stream progress, so the bar is modelled on the
    // expected duration of each pass. It never reaches 100% until the real
    // response lands, so it cannot claim to be finished before it is.
    var elapsedInTask = 0;
    var estimate = state.health && /:(1|3)b/.test(state.health.model_name || "") ? 90 : 300;

    progressState.timer = setInterval(function () {
      elapsedInTask += 1;
      var task = TASKS[progressState.index];
      var taskSeconds = (estimate * task.weight) / 100;
      var base = TASKS.slice(0, progressState.index).reduce(function (sum, item) {
        return sum + item.weight;
      }, 0);
      var within = Math.min(1, elapsedInTask / taskSeconds);
      setPercent(base + task.weight * within);

      if (within >= 1 && progressState.index < TASKS.length - 1) {
        progressState.index += 1;
        elapsedInTask = 0;
        renderTasks();
        el("progressDetail").textContent = TASKS[progressState.index].label + "…";
      } else if (within >= 1) {
        // Last task, and the time estimate has been used up but the server has
        // not answered yet. The bar is capped at 99% on purpose, so replace the
        // silent wait with an honest "still working" note — a larger model
        // simply takes longer than the estimate, it is not stuck.
        el("progressDetail").textContent =
          "Still working — larger models take longer than expected. Finalising the document…";
      }
    }, 1000);

    progressState.ticker = setInterval(function () {
      var seconds = Math.floor((Date.now() - progressState.started) / 1000);
      var minutes = Math.floor(seconds / 60);
      el("progressTimer").textContent =
        minutes + ":" + String(seconds % 60).padStart(2, "0") + " elapsed";
    }, 1000);
  }

  function stopProgress() {
    clearInterval(progressState.timer);
    clearInterval(progressState.ticker);
  }

  function buildPayload() {
    var toolDetails = state.tools.filter(function (tool) {
      return tool.custom && tool.description;
    }).map(function (tool) {
      return {
        name: tool.name,
        description: tool.description,
        category: tool.category || "",
        source: tool.source || "user",
        typical_version: tool.version || ""
      };
    });

    var payload = {
      project_name: el("projectName").value.trim(),
      department: el("departmentInput").value.trim(),
      description: el("description").value.trim(),
      security_classification: el("classification").value,
      stakeholders: state.people.map(function (person) {
        return { name: person.name, role: person.role, department: person.department || null };
      }),
      tools: state.tools.map(function (tool) { return tool.name; }),
      tool_details: toolDetails
    };
    if (state.selectedModel) { payload.model_override = state.selectedModel; }
    return payload;
  }

  function generate() {
    if (!validateStep1()) { goStep(1); return; }

    state.generating = true;
    el("stepper").classList.add("hidden");
    [1, 2, 3, 4].forEach(function (index) { el("step" + index).classList.add("hidden"); });
    el("errorCard").classList.add("hidden");
    el("working").classList.remove("hidden");
    setPill("neutral", "Writing…", true);
    startProgress();

    progressState.aborter = new AbortController();

    api("/api/v1/sop/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
      signal: progressState.aborter.signal
    }).then(function (result) {
      stopProgress();
      state.generating = false;
      state.lastResult = result;
      showResult(result);
    }).catch(function (error) {
      stopProgress();
      state.generating = false;
      if (error.name === "AbortError") { goStep(4); setPill("ok", "Ready", false); return; }
      showError(error);
    });
  }

  // A bare `<a download>` click is a silent no-op in some browser/OS setups —
  // it neither downloads nor errors, so the user sees "nothing happens". Drive
  // the save explicitly instead: fetch the file, hand the browser a blob to
  // store, and fall back to a top-level navigation (the server sends
  // Content-Disposition: attachment, so it downloads without leaving the page).
  function downloadDocx(event) {
    if (event) { event.preventDefault(); }
    var link = el("downloadBtn");
    var url = link.getAttribute("href");
    var filename = link.getAttribute("download") || "sop-document.docx";
    if (!url || url === "#") { return; }

    var original = link.textContent;
    var restore = function (text) {
      link.textContent = text;
      setTimeout(function () { link.textContent = original; }, 4000);
    };
    link.textContent = "Preparing your download…";

    fetch(url).then(function (response) {
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      return response.blob();
    }).then(function (blob) {
      var objectUrl = URL.createObjectURL(blob);
      var temp = document.createElement("a");
      temp.href = objectUrl;
      temp.download = filename;
      document.body.appendChild(temp);
      temp.click();
      document.body.removeChild(temp);
      setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 4000);
      restore("✓ Saved to your Downloads folder");
    }).catch(function () {
      // Network path failed (or the blob click was blocked): navigate straight
      // to the attachment URL as a last resort.
      link.textContent = original;
      window.location.href = url;
    });
  }

  function showResult(result) {
    setPercent(100);
    el("progressPercent").textContent = "100%";
    el("working").classList.add("hidden");
    el("result").classList.remove("hidden");
    setPill("ok", "Ready", false);

    el("resultDocId").textContent = result.document_id;
    el("downloadBtn").href = "/api/v1/sop/download/" + encodeURIComponent(result.document_id);
    el("downloadBtn").setAttribute("download", result.document_id + ".docx");

    // Show where the Word file was saved so the user can open it in Explorer.
    var pathEl = document.getElementById("savedPath");
    if (pathEl) { pathEl.textContent = result.docx_path || ""; }

    el("previewBody").textContent = result.markdown_content;

    var validation = result.validation;
    if (validation) {
      var checks = [
        ["Every tool has its own section", (validation.tools_missing || []).length === 0],
        ["Every activity has an approver and a doer", !(validation.issues || []).some(function (issue) {
          return issue.gate === "raci";
        })],
        ["All four work phases are present", (validation.phases_found || []).length >= 4],
        ["Instructions include real commands", !(validation.issues || []).some(function (issue) {
          return issue.gate === "execution_depth";
        })]
      ];
      el("checksList").innerHTML = checks.map(function (check) {
        return '<div class="check check--' + (check[1] ? "pass" : "fail") + '">' +
          '<span class="check__mark">' + (check[1] ? "✓" : "✕") + "</span>" +
          "<span>" + esc(check[0]) + "</span></div>";
      }).join("");
      el("checksBlock").classList.remove("hidden");
    } else {
      el("checksBlock").classList.add("hidden");
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function showError(error) {
    el("working").classList.add("hidden");
    el("errorCard").classList.remove("hidden");
    setPill("danger", "Problem", false);

    var title = "Something went wrong";
    var body = esc(error.message || "Unknown error");

    if (error.status === 503) {
      title = "The writing engine is not running";
      body = "Start Ollama and try again.<br><br>" +
        (error.body && error.body.hint ? esc(error.body.hint) : "");
    } else if (error.status === 422 && error.body && error.body.validation) {
      title = "The document did not meet the required standard";
      body = "The automatic checks rejected it, so it was not saved. This usually means the " +
        "model is too small for the level of detail required.<br><br>" +
        "<strong>Try this:</strong> open Settings and choose a larger model, or add more detail " +
        "to the project description.";
    }
    el("errorTitle").textContent = title;
    el("errorBody").innerHTML = body;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ------------------------------------------------------------------------
  // Settings
  // ------------------------------------------------------------------------
  function openSettings() {
    el("settingsModal").classList.add("modal-backdrop--open");
    api("/api/v1/system/profile").then(function (profile) {
      state.profile = profile;
      renderSpecs(profile);
      renderModels(profile);
    });
    api("/api/v1/settings").then(renderWebLookup);
  }

  function renderWebLookup(prefs) {
    var on = !!prefs.web_lookup;
    el("webLookupToggle").checked = on;
    el("webLookupRow").classList.toggle("model--selected", on);

    var badge = el("webLookupState");
    badge.textContent = on ? "On" : "Off — fully offline";
    badge.className = "tag " + (on ? "tag--pend" : "tag--have");

    el("webLookupDesc").textContent = on
      ? "Unknown tool names are sent to Wikipedia and DuckDuckGo to find a description. " +
        "You still review and approve every description before it is used."
      : "Nothing leaves this computer. Unknown tools fall through to a box where you type " +
        "the description yourself.";
  }

  function renderSpecs(profile) {
    var machine = profile.machine;
    var specs = [
      ["Computer", machine.tier_label],
      ["Memory", machine.ram_gb ? machine.ram_gb + " GB" : "Unknown"],
      ["Processor", machine.cpu_cores + " cores"],
      ["Graphics", machine.gpu_present && machine.vram_gb
        ? machine.gpu_name + " (" + machine.vram_gb + " GB)"
        : (machine.gpu_present ? machine.gpu_name : "None detected")],
      ["Free disk space", machine.free_disk_gb + " GB"],
      ["System", machine.os_name + " " + machine.os_version]
    ];
    el("specsGrid").innerHTML = specs.map(function (spec) {
      return '<div class="spec"><div class="spec__label">' + esc(spec[0]) + "</div>" +
        '<div class="spec__value">' + esc(spec[1]) + "</div></div>";
    }).join("");
  }

  function renderModels(profile) {
    el("modelReason").textContent = profile.reason;
    var installed = profile.installed_models || [];
    var active = state.selectedModel || profile.active_model;

    el("modelList").innerHTML = profile.options.map(function (option) {
      var have = installed.some(function (name) {
        return name === option.model || name.split(":")[0] === option.model.split(":")[0];
      });
      var blocked = !option.runnable;
      var tags = (option.recommended ? '<span class="tag tag--rec">Recommended</span>' : "") +
        (have ? '<span class="tag tag--have">Downloaded</span>' : "");

      return '<label class="model' + (option.model === active ? " model--selected" : "") +
        (blocked ? " model--blocked" : "") + '">' +
        '<input type="radio" name="model" value="' + esc(option.model) + '"' +
        (option.model === active ? " checked" : "") + (blocked ? " disabled" : "") + ">" +
        '<span class="model__body">' +
        '<span class="model__name">' + esc(option.label) + " " + tags + "</span>" +
        '<span class="model__meta">' + esc(option.parameters) + " · " + option.download_gb +
        " GB download · " + esc(option.quality) + "</span>" +
        '<span class="model__desc">' + esc(option.description) + "</span>" +
        (blocked ? '<span class="model__blockers">Not suitable: ' +
          esc(option.blockers.join("; ")) + "</span>" : "") +
        "</span></label>";
    }).join("");

    el("modelList").querySelectorAll("input[name=model]").forEach(function (radio) {
      radio.addEventListener("change", function () {
        state.selectedModel = radio.value;
        renderModels(profile);
        var have = installed.some(function (name) {
          return name === radio.value || name.split(":")[0] === radio.value.split(":")[0];
        });
        el("modelInstallHint").innerHTML = have
          ? "This model is already on your computer and ready to use."
          : "This model is not downloaded yet. Run <code>ollama pull " + esc(radio.value) +
            "</code> in a terminal once, then it is available.";
      });
    });
  }

  function loadQueue() {
    var token = el("adminToken").value.trim();
    api("/api/v1/admin/pending", { headers: { "X-Admin-Token": token } })
      .then(function (data) {
        if (!data.pending.length) {
          el("queueList").innerHTML = '<p class="muted">Nothing is waiting for approval.</p>';
          return;
        }
        el("queueList").innerHTML = data.pending.map(function (item) {
          return '<div class="queue__item"><div class="queue__body">' +
            '<div class="queue__name">' + esc(item.name) +
            ' <span class="tag tag--pend">' + esc(item.kind) + "</span></div>" +
            '<div class="queue__desc">' + esc(item.description || "No description supplied.") + "</div>" +
            '<div class="queue__meta">Added ' + esc((item.submitted_at || "").slice(0, 10)) +
            " · source: " + esc(item.source) + "</div></div>" +
            '<div class="queue__actions">' +
            '<button class="btn btn--ghost btn--sm" data-approve="' + esc(item.id) + '">Approve</button>' +
            '<button class="btn btn--danger btn--sm" data-reject="' + esc(item.id) + '">Reject</button>' +
            "</div></div>";
        }).join("");
      })
      .catch(function (error) {
        el("queueList").innerHTML = '<p class="muted">' +
          (error.status === 401
            ? "That password is not correct."
            : esc(error.message)) + "</p>";
      });
  }

  // ------------------------------------------------------------------------
  // Wiring
  // ------------------------------------------------------------------------
  function init() {
    makeCombo({
      inputId: "departmentInput", menuId: "departmentMenu", kind: "department",
      addLabel: "Add this department",
      onPick: function (entry) {
        el("departmentInput").value = entry.name;
        el("departmentError").classList.remove("field__error--shown");
        if (entry.custom) {
          postJSON("/api/v1/catalog/submit", {
            kind: "department", name: entry.name, description: "Added by an officer during document creation."
          }).catch(function () {});
        }
      }
    });

    makeCombo({
      inputId: "personName", menuId: "personNameMenu", kind: "person",
      addLabel: "Use", allowCustom: false,
      onPick: function (entry) {
        el("personName").value = entry.name;
        // Auto-fill role and department if the saved record has them.
        if (entry.role && !el("personRole").value.trim()) {
          el("personRole").value = entry.role;
        }
        if (entry.department && !el("personDept").value.trim()) {
          el("personDept").value = entry.department;
        }
      }
    });

    makeCombo({
      inputId: "personRole", menuId: "roleMenu", kind: "role", addLabel: "Use",
      onPick: function (entry) { el("personRole").value = entry.name; }
    });

    makeCombo({
      inputId: "personDept", menuId: "personDeptMenu", kind: "department", addLabel: "Use",
      onPick: function (entry) { el("personDept").value = entry.name; }
    });

    makeCombo({
      inputId: "toolInput", menuId: "toolMenu", kind: "tool",
      addLabel: "Tell us about",
      onPick: function (entry) {
        if (entry.custom) { openToolModal(entry.name); }
        else { addTool({ name: entry.name, category: entry.category, custom: false }); }
      }
    });

    document.querySelectorAll("[data-next]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = Number(button.dataset.next);
        if (target > 1 && state.step === 1 && !validateStep1()) { return; }
        goStep(target);
      });
    });

    document.querySelectorAll(".step").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = Number(button.dataset.goto);
        if (target > 1 && !validateStep1()) { goStep(1); return; }
        goStep(target);
      });
    });

    el("addPersonBtn").addEventListener("click", addPerson);
    ["personName", "personRole", "personDept"].forEach(function (id) {
      el(id).addEventListener("keydown", function (event) {
        if (event.key === "Enter" && id === "personName") { event.preventDefault(); addPerson(); }
      });
    });

    el("peopleChips").addEventListener("click", function (event) {
      var button = event.target.closest("[data-person]");
      if (button) { state.people.splice(Number(button.dataset.person), 1); renderPeople(); }
    });

    el("toolChips").addEventListener("click", function (event) {
      var button = event.target.closest("[data-tool]");
      if (button) { state.tools.splice(Number(button.dataset.tool), 1); renderTools(); }
    });

    el("generateBtn").addEventListener("click", generate);
    el("retryBtn").addEventListener("click", generate);
    el("errorBackBtn").addEventListener("click", function () { goStep(4); });
    el("cancelBtn").addEventListener("click", function () {
      if (progressState.aborter) { progressState.aborter.abort(); }
    });
    el("restartBtn").addEventListener("click", function () {
      state.people = []; state.tools = []; state.lastResult = null;
      ["projectName", "description", "departmentInput"].forEach(function (id) { el(id).value = ""; });
      renderPeople(); renderTools(); goStep(1);
    });

    el("downloadBtn").addEventListener("click", downloadDocx);

    el("copyBtn").addEventListener("click", function () {
      var text = el("previewBody").textContent;
      navigator.clipboard.writeText(text).then(function () {
        el("copyBtn").textContent = "Copied";
        setTimeout(function () { el("copyBtn").textContent = "Copy the text"; }, 1800);
      }).catch(function () { el("copyBtn").textContent = "Could not copy"; });
    });

    // Tool research modal
    el("toolAcceptBtn").addEventListener("click", function () {
      acceptTool(research.draft.description, research.draft.source);
    });
    el("toolRejectBtn").addEventListener("click", function () {
      research.rejected.push(research.draft.description);
      el("toolRejected").innerHTML = research.rejected.map(function (text) {
        return '<div class="draft draft--rejected">Rejected: ' + esc(text) + "</div>";
      }).join("");
      el("toolHint").value = "";
      showToolPane("hint");
    });
    el("toolHintGoBtn").addEventListener("click", function () { nextDraft(el("toolHint").value.trim()); });
    el("toolHintCancelBtn").addEventListener("click", function () { showToolPane("draft"); });
    el("toolWriteOwnBtn").addEventListener("click", function () {
      el("toolOwnText").value = (research.draft && research.draft.description) || "";
      showToolPane("own");
    });
    el("toolOwnCancelBtn").addEventListener("click", function () {
      if (research.draft && research.draft.description) { showToolPane("draft"); }
      else { closeToolModal(); }
    });
    el("toolSuggestBtn").addEventListener("click", function () { nextDraft(""); });
    el("toolBusyCancelBtn").addEventListener("click", function () {
      if (research.aborter) { research.aborter.abort(); }
      showToolPane("own");
      el("toolOwnText").focus();
    });
    el("toolOwnSaveBtn").addEventListener("click", function () {
      var text = el("toolOwnText").value.trim();
      if (!text) { el("toolOwnText").focus(); return; }
      acceptTool(text, "user");
    });
    el("toolCancelBtn").addEventListener("click", closeToolModal);

    // Settings modal
    el("settingsBtn").addEventListener("click", openSettings);
    el("settingsCloseBtn").addEventListener("click", function () {
      el("settingsModal").classList.remove("modal-backdrop--open");
      checkHealth();
    });
    el("webLookupToggle").addEventListener("change", function (event) {
      var enabled = event.target.checked;
      postJSON("/api/v1/settings/web-lookup", { enabled: enabled })
        .then(renderWebLookup)
        .catch(function (error) {
          event.target.checked = !enabled;  // the server is the source of truth
          alert("Could not change that setting: " + error.message);
        });
    });

    el("loadQueueBtn").addEventListener("click", loadQueue);
    el("queueList").addEventListener("click", function (event) {
      var approve = event.target.closest("[data-approve]");
      var rejectButton = event.target.closest("[data-reject]");
      if (!approve && !rejectButton) { return; }
      var token = el("adminToken").value.trim();
      var path = approve ? "/api/v1/admin/approve" : "/api/v1/admin/reject";
      var id = approve ? approve.dataset.approve : rejectButton.dataset.reject;
      postJSON(path, { entry_id: id }, { "X-Admin-Token": token })
        .then(loadQueue)
        .catch(function (error) { alert(error.message); });
    });

    [el("toolModal"), el("settingsModal")].forEach(function (modal) {
      modal.addEventListener("click", function (event) {
        if (event.target === modal) { modal.classList.remove("modal-backdrop--open"); }
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        document.querySelectorAll(".modal-backdrop--open").forEach(function (modal) {
          modal.classList.remove("modal-backdrop--open");
        });
      }
    });

    checkHealth();
    setInterval(function () { if (!state.generating) { checkHealth(); } }, 6000);
    el("projectName").focus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
