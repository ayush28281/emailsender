(() => {
  "use strict";

  const stages = {saved: "Saved", applied: "Applied", interview: "Interview", offer: "Offer", closed: "Closed"};
  const filters = {active: "Active", due: "Due", ...stages, all: "All"};
  const examples = {
    saved: "e.g. Tailor my résumé for this role",
    applied: "e.g. Follow up about my application",
    interview: "e.g. Prepare two project examples",
    offer: "e.g. Review the offer before the deadline",
    closed: ""
  };
  const dialog = el("job-editor");
  const closeButton = el("close-job-editor");
  const form = el("job-form");
  const fields = form.elements;
  const saveButton = form.querySelector('button[type="submit"]');
  const importButton = el("job-import");
  const assessButton = el("job-assess");
  const jobFieldNames = ["title", "company", "posting_url", "next_date", "next_action", "notes",
    "description", "resume_url", "cover_letter_url"];
  const discovery = el("job-discovery");
  const discoveryForm = el("job-discovery-form");
  const searchButton = discoveryForm.querySelector('button[type="submit"]');
  const boardShortcut = el("board-shortcut");
  const boardStoreKey = "emailsender:saved-boards";
  const materialLabels = {resume: "Résumé", cover_letter: "Cover letter"};
  const updateLabels = {interview: "Interview invitation", offer: "Offer", rejection: "Rejection"};
  const dateFormatter = new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"});
  let dashboard = model;
  let filter = "active";
  let editingId = null;
  let displayKey;
  let editorVersion = 0;
  let postingVersion = 0;
  let fitVersion = 0;
  let searchVersion = 0;
  let searchedBoard = "";
  let savedBoards = loadBoards();

  function dateKey() {
    const date = new Date();
    return [date.getFullYear(), date.getMonth() + 1, date.getDate()]
      .map(number => String(number).padStart(2, "0")).join("-");
  }

  function dateLabel(value) {
    return dateFormatter.format(new Date(value + "T12:00:00"));
  }

  function postingLink(link, value) {
    try {
      const url = new URL(value);
      if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return false;
      link.href = url.href;
      return true;
    } catch {
      link.removeAttribute("href");
      return false;
    }
  }

  function editJob(job = {}) {
    editorVersion++;
    editingId = job.id ?? null;
    el("job-editor-title").textContent = editingId ? "Edit application" : "Add a job";
    for (const name of jobFieldNames) {
      fields[name].value = job[name] || "";
    }
    fields.stage.value = job.stage || "saved";
    fields.contact_id.replaceChildren(new Option("No contact linked", ""));
    for (const contact of dashboard.contacts || []) {
      const label = contact.name ? contact.name + " · " + contact.email : contact.email;
      fields.contact_id.add(new Option(label, String(contact.id)));
    }
    fields.contact_id.value = job.contact_id == null ? "" : String(job.contact_id);
    el("job-error").hidden = true;
    renderJobProgress(job);
    renderMaterials(job);
    el("interview-prep").open = false;
    el("job-materials").open = false;
    el("job-description-panel").open = Boolean(job.match);
    el("job-import-status").textContent = job.match ? "Review the imported details, then save the job." : "";
    el("job-import-error").hidden = true;
    el("job-assess-error").hidden = true;
    importButton.disabled = false;
    clearFit();
    if (job.match) renderFit(job.match);
    refreshEditor();
    renderSuggestions();
    dialog.showModal();
    fields.title.focus();
  }

  function renderJobProgress(job) {
    const progress = el("job-progress");
    progress.hidden = !job.stage_changed_at;
    progress.textContent = job.stage_changed_at ? "Stage updated " + stamp(job.stage_changed_at) : "";
  }

  function refreshEditor() {
    fields.next_action.placeholder = examples[fields.stage.value];
    const link = el("job-editor-posting");
    link.hidden = !postingLink(link, fields.posting_url.value);
    const resume = el("job-resume-link");
    resume.hidden = !postingLink(resume, fields.resume_url.value);
    const cover = el("job-cover-link");
    cover.hidden = !postingLink(cover, fields.cover_letter_url.value);
    el("clear-job-action").hidden = !(fields.next_date.value || fields.next_action.value);
    const interview = fields.stage.value === "interview";
    el("interview-prep").hidden = !interview;
    if (interview) {
      const company = fields.company.value.trim() || "the team";
      const title = fields.title.value.trim() || "the role";
      el("interview-priorities").textContent = "Pick two requirements from the " + title +
        " posting. For each, find one example you can talk through.";
      el("interview-question").textContent = "Ask " + company + " what success in this role would look like after three months.";
      const contact = (dashboard.contacts || []).find(item => String(item.id) === fields.contact_id.value);
      const proof = contact?.work_sample?.title;
      el("interview-star").textContent = (proof ? "Rehearse “" + proof + "”" : "Rehearse one relevant work example") +
        " using the situation, task, your actions and the result. Be ready to explain what you personally did.";
    }
  }

  function showError(id, message) {
    const target = el(id);
    target.textContent = message;
    target.hidden = !message;
  }

  function loadBoards() {
    try {
      const saved = JSON.parse(localStorage.getItem(boardStoreKey) || "[]");
      return Array.isArray(saved) ? saved.filter(url => typeof url === "string").slice(0, 8) : [];
    } catch {
      return [];
    }
  }

  function renderBoards() {
    const current = discoveryForm.elements.url.value.trim();
    boardShortcut.replaceChildren(new Option("Choose a board", ""));
    for (const url of savedBoards) {
      try {
        const board = new URL(url);
        const label = board.hostname + board.pathname.replace(/\/$/, "");
        boardShortcut.add(new Option(label, url));
      } catch { /* Skip a stale invalid shortcut. */ }
    }
    boardShortcut.value = savedBoards.includes(current) ? current : "";
    el("saved-boards").hidden = boardShortcut.options.length < 2;
    el("forget-board").hidden = !boardShortcut.value;
    el("remember-board").hidden = current !== searchedBoard || !current || savedBoards.includes(current);
  }

  function storeBoards(boards) {
    try {
      localStorage.setItem(boardStoreKey, JSON.stringify(boards));
      savedBoards = boards;
      renderBoards();
    } catch {
      showError("job-discovery-error", "This browser could not save the board shortcut.");
    }
  }

  function renderMaterials(job) {
    const list = el("job-pdfs");
    list.replaceChildren();
    el("job-pdf-status").textContent = "";
    showError("job-pdf-error", "");
    if (!job.id) {
      list.append(node("p", "Save the job to attach a local PDF.", "muted"));
      return;
    }
    for (const [kind, label] of Object.entries(materialLabels)) {
      const card = node("section", "", "job-document");
      card.append(node("h3", label + " PDF"));
      const row = node("div", "", "detail-actions");
      const file = node("input");
      file.type = "file";
      file.accept = ".pdf,application/pdf";
      file.setAttribute("aria-label", "Choose " + label.toLowerCase() + " PDF");
      const attached = job.materials?.[kind];
      const upload = node("button", attached ? "Replace PDF" : "Attach PDF", "button outline");
      upload.type = "button";
      upload.disabled = true;
      file.addEventListener("change", () => { upload.disabled = !file.files.length; });
      upload.addEventListener("click", () => {
        const selected = file.files[0];
        if (selected) editMaterial(job.id, kind, selected, upload);
      });
      row.append(file, upload);
      if (attached) {
        const link = node("a", attached.name, "job-posting");
        link.href = "/api/jobs/" + job.id + "/materials/" + kind;
        const remove = node("button", "Remove " + label.toLowerCase() + " PDF", "button plain");
        remove.type = "button";
        remove.addEventListener("click", () => editMaterial(job.id, kind, null, remove));
        row.append(link, remove);
      }
      card.append(row);
      list.append(card);
    }
  }

  function encodedFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1]));
      reader.addEventListener("error", () => reject(new Error("Could not read the PDF.")));
      reader.readAsDataURL(file);
    });
  }

  async function editMaterial(id, kind, file, button) {
    if (saveButton.disabled) return;
    const version = editorVersion;
    if (file && (file.size > 5 * 1024 * 1024 || !/\.pdf$/i.test(file.name))) {
      showError("job-pdf-error", "Choose a PDF up to 5 MB.");
      return;
    }
    showError("job-pdf-error", "");
    closeButton.disabled = saveButton.disabled = button.disabled = true;
    el("job-pdf-status").textContent = file ? "Saving the PDF…" : "Removing the PDF…";
    try {
      const payload = file ? {name: file.name, content: await encodedFile(file)} : {remove: true};
      await api("jobs/" + id + "/materials/" + kind, payload);
      await refresh();
      const updated = dashboard.jobs?.find(job => job.id === id);
      if (dialog.open && editorVersion === version && updated) {
        renderMaterials(updated);
        el("job-pdf-status").textContent = file ? "PDF attached." : "PDF removed.";
      }
    } catch (error) {
      if (dialog.open && editorVersion === version) {
        el("job-pdf-status").textContent = "";
        showError("job-pdf-error", error.message);
      }
    } finally {
      closeButton.disabled = saveButton.disabled = button.disabled = false;
    }
  }

  function matchLabel(match) {
    const counts = (match?.matched?.length || 0) + " of " + (match?.checked?.length || 0);
    if (match?.checked?.length) {
      return counts + " saved keywords found" + (match.target_role_match ? " · Target role matches" : "");
    }
    return match?.target_role_match ? "Target role matches" : "Add skills in Sender setup to check for keyword overlap.";
  }

  function renderFit(match, brief) {
    const result = el("job-fit");
    result.replaceChildren(node("h3", "Profile match"), node("p", matchLabel(match)));
    if (match?.matched?.length) result.append(node("p", match.matched.join(", "), "fit-keywords"));
    if (match?.matched_samples?.length) {
      const titles = match.matched_samples.map(sample => sample.title).filter(Boolean);
      if (titles.length) result.append(node("p", "Relevant work samples · " + titles.join(", ")));
    }
    result.append(node("p", "This check looks for your saved keywords in the role text.", "muted"));
    if (brief) {
      const briefBox = node("details", "", "fit-brief");
      const copy = node("button", "Copy brief", "button outline");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(brief);
          copy.textContent = "Brief copied";
        } catch {
          copy.textContent = "Select the brief text to copy";
        }
      });
      briefBox.append(node("summary", "Application brief"), node("p", brief, "message-copy"), copy);
      result.append(briefBox);
    }
    result.hidden = false;
  }

  function clearFit() {
    fitVersion++;
    assessButton.disabled = false;
    el("job-fit").hidden = true;
  }

  function renderPostings(postings) {
    const results = el("posting-results");
    results.replaceChildren();
    for (const posting of postings) {
      const row = node("li");
      const label = node("div");
      label.append(node("strong", posting.title), node("p", [posting.company, posting.location].filter(Boolean).join(" · ")));
      if (posting.match?.checked?.length || posting.match?.target_role_match) {
        label.append(node("small", matchLabel(posting.match), "muted"));
      }
      const pick = node("button", "Review opening", "button outline");
      pick.type = "button";
      pick.setAttribute("aria-label", "Review " + posting.title + " at " + posting.company);
      pick.addEventListener("click", () => {
        const saved = dashboard.jobs?.find(job => job.posting_url === posting.posting_url);
        editJob(saved || posting);
      });
      row.append(label, pick);
      results.append(row);
    }
    el("job-discovery-status").textContent = postings.length
      ? postings.length + (postings.length === 1 ? " opening" : " openings") + " shown. Open one to review and save it."
      : "No openings matched this search.";
  }

  async function useSuggestion(suggestion, action) {
    const id = editingId;
    const version = editorVersion;
    const job = dashboard.jobs?.find(item => item.id === id);
    closeButton.disabled = saveButton.disabled = true;
    renderSuggestions();
    showError("job-error", "");
    try {
      const {job: updated} = await api("jobs/" + id + "/suggestions/" + suggestion.id, {action});
      if (version !== editorVersion || !dialog.open) return;
      if (action === "apply" && updated) {
        fields.stage.value = updated.stage;
        for (const key of ["next_action", "next_date"]) {
          if (fields[key].value === job?.[key]) fields[key].value = updated[key] || "";
        }
        renderJobProgress(updated);
        refreshEditor();
      }
      dashboard = {...dashboard, job_suggestions: dashboard.job_suggestions.filter(item => item.id !== suggestion.id)};
      renderSuggestions();
      notice(action === "apply" ? "Application stage updated." : "Recruiter update dismissed.");
      await refresh().catch(error => notice(error.message, true));
    } catch (error) {
      if (version === editorVersion && dialog.open) showError("job-error", error.message);
    } finally {
      closeButton.disabled = saveButton.disabled = false;
      if (version === editorVersion && dialog.open) renderSuggestions();
    }
  }

  function renderSuggestions() {
    const panel = el("job-suggestions");
    const suggestions = (dashboard.job_suggestions || []).filter(item => item.job_id === editingId);
    panel.replaceChildren();
    panel.hidden = !suggestions.length;
    for (const suggestion of suggestions) {
      const card = node("article", "", "job-suggestion");
      const buttons = node("div", "", "detail-actions");
      card.append(node("h3", (updateLabels[suggestion.kind] || "Recruiter update") + " found"),
        node("p", suggestion.excerpt, "suggestion-excerpt"));
      for (const [action, label] of [["apply", "Move to " + stages[suggestion.stage]], ["dismiss", "Dismiss"]]) {
        const button = node("button", label, action === "apply" ? "button dark" : "button plain");
        button.type = "button";
        button.disabled = saveButton.disabled;
        button.addEventListener("click", () => useSuggestion(suggestion, action));
        buttons.append(button);
      }
      card.append(buttons);
      panel.append(card);
    }
  }

  function actionDate(job, today) {
    if (!job.next_date) return "";
    if (job.next_date === today) return "Due today";
    if (job.next_date < today) return "Overdue · " + dateLabel(job.next_date);
    return "Due " + dateLabel(job.next_date);
  }

  function isDue(job, today) {
    return job.stage !== "closed" && Boolean(job.next_date && job.next_date <= today);
  }

  function sortedJobs() {
    return [...(dashboard.jobs || [])].sort((a, b) => {
      if ((a.stage === "closed") !== (b.stage === "closed")) return a.stage === "closed" ? 1 : -1;
      const dateDifference = (a.next_date || "9999").localeCompare(b.next_date || "9999");
      return dateDifference || b.id - a.id;
    });
  }

  function viewRecipient(id) {
    const contact = dashboard.contacts?.find(item => item.id === id);
    if (contact) openContact(contact);
  }

  function renderNextSteps(jobs, today) {
    const list = el("job-actions");
    list.replaceChildren();
    const tasks = jobs
      .filter(job => job.stage !== "closed" && (job.next_action || job.next_date))
      .map(job => ({
        title: job.next_action || "Review deadline",
        detail: [job.company + " · " + job.title, actionDate(job, today)].filter(Boolean).join(" · "),
        action: "Open application",
        run: () => editJob(job),
        due: isDue(job, today)
      }));
    const pendingJobs = new Set();
    const recruiterUpdates = [];
    for (const update of dashboard.job_suggestions || []) {
      const job = jobs.find(item => item.id === update.job_id);
      if (!job || pendingJobs.has(job.id)) continue;
      pendingJobs.add(job.id);
      recruiterUpdates.push({
        title: "Review " + (updateLabels[update.kind] || "recruiter update").toLowerCase(),
        detail: job.company + " · " + job.title,
        action: "Open application",
        run: () => { editJob(job); el("job-suggestions").querySelector("button")?.focus(); }
      });
    }
    const outreach = [];
    for (const contact of dashboard.contacts || []) {
      let title;
      if (contact.status === "needs_context") title = "Add missing outreach details";
      if (contact.status === "pending_review") title = "Review outreach draft";
      if (contact.status === "replied" && !contact.outcome && !["sent", "pending"].includes(contact.auto_response_status)) {
        title = "Review outreach reply";
      }
      if (title) {
        outreach.push({
          title, detail: contact.name || contact.email, action: "Open recipient",
          run: () => viewRecipient(contact.id)
        });
      }
    }
    const next = [...tasks.filter(task => task.due), ...recruiterUpdates, ...outreach,
      ...tasks.filter(task => !task.due)].slice(0, 5);
    for (const task of next) {
      const row = node("li");
      const text = node("div");
      text.append(node("p", task.title), node("small", task.detail));
      const button = node("button", task.action, "open-row");
      button.type = "button";
      button.setAttribute("aria-label", task.action + " for " + task.detail);
      button.addEventListener("click", task.run);
      row.append(text, button);
      list.append(row);
    }
    const empty = el("job-actions-empty");
    empty.hidden = next.length > 0;
    empty.textContent = jobs.length
      ? "Add a next step to an application to see it here."
      : "Save a role you’re interested in, then give it one next step and a date.";
  }

  function renderApplications(jobs, today) {
    const query = el("job-search").value.trim().toLowerCase();
    const visible = jobs.filter(job => {
      const stageMatch = filter === "all" || filter === job.stage ||
        (filter === "active" && job.stage !== "closed") || (filter === "due" && isDue(job, today));
      return stageMatch && (job.title + " " + job.company).toLowerCase().includes(query);
    });
    const contacts = new Map((dashboard.contacts || []).map(contact => [contact.id, contact]));
    const empty = el("job-empty");
    empty.hidden = Boolean(visible.length);
    el("job-count").textContent = visible.length;
    el("job-table").hidden = !visible.length;
    empty.querySelector("h3").textContent = jobs.length ? "No matching applications" : "No applications yet";
    empty.querySelector("p").textContent = jobs.length
      ? "Try a different search or filter."
      : "Add a role with its posting link and choose what to do next.";
    const rows = el("job-rows");
    rows.replaceChildren();
    const emphasis = {applied: "status-replied", interview: "status-attention", offer: "status-active"};
    for (const job of visible) {
      const row = node("tr");
      const role = node("td", "", "job-title-cell");
      role.append(node("strong", job.title), node("span", job.company, "second-line"));
      const posting = node("a", "Job posting ↗", "job-posting second-line");
      if (postingLink(posting, job.posting_url)) {
        posting.target = "_blank";
        posting.rel = "noopener noreferrer";
        posting.setAttribute("aria-label", "Job posting for " + job.title + " at " + job.company);
        role.append(posting);
      }
      const stage = node("td");
      stage.append(node("span", stages[job.stage], "status " + (emphasis[job.stage] || "")));
      const next = node("td");
      if (job.stage !== "closed" && (job.next_action || job.next_date)) {
        next.append(node("span", job.next_action || "Review deadline"));
        if (job.next_date) {
          next.append(node("small", actionDate(job, today), "job-next-date" + (isDue(job, today) ? " overdue" : "")));
        }
      } else next.textContent = "—";
      const outreach = node("td");
      const contact = contacts.get(job.contact_id);
      if (contact) {
        const open = node("button", contact.name || contact.email, "open-row");
        open.type = "button";
        open.addEventListener("click", () => viewRecipient(contact.id));
        outreach.append(open);
      } else outreach.textContent = "—";
      const actions = node("td");
      const edit = node("button", "Edit", "open-row");
      edit.type = "button";
      edit.setAttribute("aria-label", "Edit " + job.title + " at " + job.company);
      edit.addEventListener("click", () => editJob(job));
      actions.append(edit);
      row.append(role, stage, next, outreach, actions);
      rows.append(row);
    }
  }

  function render(force = false) {
    if (!Array.isArray(dashboard.jobs)) return;
    const today = dateKey();
    const currentDisplay = JSON.stringify([today, dashboard.jobs, dashboard.job_suggestions, (dashboard.contacts || []).map(contact => [
      contact.id, contact.name, contact.email, contact.status, contact.outcome, contact.auto_response_status
    ])]);
    if (!force && currentDisplay === displayKey) return;
    displayKey = currentDisplay;
    const jobs = sortedJobs();
    const counts = dashboard.stage_counts || {};
    for (const button of el("job-filters").children) {
      const key = button.dataset.stage;
      button.setAttribute("aria-pressed", String(key === filter));
      const count = key === "due" ? jobs.filter(job => isDue(job, today)).length : counts[key];
      button.textContent = filters[key] + (count ? " " + count : "");
    }
    renderNextSteps(jobs, today);
    renderApplications(jobs, today);
  }

  addOptions(fields.stage, stages);
  for (const [key, label] of Object.entries(filters)) {
    const button = node("button", label);
    button.type = "button";
    button.dataset.stage = key;
    button.setAttribute("aria-pressed", String(key === filter));
    button.addEventListener("click", () => {
      filter = key;
      render(true);
    });
    el("job-filters").append(button);
  }

  el("job-search").addEventListener("input", () => {
    if (Array.isArray(dashboard.jobs)) renderApplications(sortedJobs(), dateKey());
  });
  el("find-jobs").addEventListener("click", event => {
    discovery.hidden = !discovery.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!discovery.hidden));
    if (!discovery.hidden) discoveryForm.elements.url.focus();
  });
  function resetBoardResults() {
    searchVersion++;
    searchButton.disabled = false;
    searchedBoard = "";
    showError("job-discovery-error", "");
    el("job-discovery-status").textContent = "";
    el("posting-results").replaceChildren();
    renderBoards();
  }
  for (const field of [discoveryForm.elements.url, discoveryForm.elements.query]) {
    field.addEventListener("input", resetBoardResults);
  }
  boardShortcut.addEventListener("change", () => {
    const selected = boardShortcut.value;
    if (!selected) return;
    discoveryForm.elements.url.value = selected;
    resetBoardResults();
    searchButton.focus();
  });
  el("remember-board").addEventListener("click", () => {
    if (searchedBoard) storeBoards([searchedBoard, ...savedBoards.filter(url => url !== searchedBoard)].slice(0, 8));
  });
  el("forget-board").addEventListener("click", () => {
    storeBoards(savedBoards.filter(url => url !== boardShortcut.value));
  });
  discoveryForm.addEventListener("submit", async event => {
    event.preventDefault();
    const input = formValues(discoveryForm);
    const version = ++searchVersion;
    searchButton.disabled = true;
    showError("job-discovery-error", "");
    el("posting-results").replaceChildren();
    el("job-discovery-status").textContent = "Searching the openings on this job board…";
    try {
      const {postings} = await api("postings/discover", input);
      if (version === searchVersion) {
        searchedBoard = input.url;
        renderBoards();
        renderPostings(postings);
      }
    } catch (error) {
      if (version !== searchVersion) return;
      showError("job-discovery-error", error.message);
      el("job-discovery-status").textContent = "";
    } finally {
      if (version === searchVersion) searchButton.disabled = false;
    }
  });

  el("add-job").addEventListener("click", () => editJob());
  closeButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", event => {
    if (saveButton.disabled) event.preventDefault();
  });
  fields.stage.addEventListener("change", refreshEditor);
  fields.contact_id.addEventListener("change", refreshEditor);
  for (const field of [fields.title, fields.company, fields.posting_url, fields.next_date, fields.next_action,
    fields.resume_url, fields.cover_letter_url]) {
    field.addEventListener("input", refreshEditor);
  }
  for (const field of [fields.title, fields.company, fields.description]) {
    field.addEventListener("input", () => {
      postingVersion++;
      importButton.disabled = false;
      el("job-import-status").textContent = "";
      showError("job-assess-error", "");
      clearFit();
    });
  }
  fields.posting_url.addEventListener("input", () => {
    postingVersion++;
    importButton.disabled = false;
    showError("job-import-error", "");
    el("job-import-status").textContent = "";
  });
  importButton.addEventListener("click", async () => {
    if (!fields.posting_url.reportValidity()) return;
    const url = fields.posting_url.value.trim();
    if (!url) {
      showError("job-import-error", "Paste the job posting link to import it.");
      fields.posting_url.focus();
      return;
    }
    const activeEditor = editorVersion;
    const version = ++postingVersion;
    const isCurrent = () => dialog.open && activeEditor === editorVersion && version === postingVersion;
    importButton.disabled = true;
    showError("job-import-error", "");
    el("job-import-status").textContent = "Reading the job posting…";
    try {
      const {posting} = await api("postings/import", {url});
      if (!isCurrent()) return;
      for (const key of ["title", "company", "posting_url", "description"]) {
        fields[key].value = posting[key] || "";
      }
      el("job-description-panel").open = true;
      el("job-import-status").textContent = "Review the imported details, then save the job.";
      showError("job-assess-error", "");
      clearFit();
      renderFit(posting.match);
      refreshEditor();
    } catch (error) {
      if (!isCurrent()) return;
      showError("job-import-error", error.message);
      el("job-import-status").textContent = "";
    } finally {
      if (isCurrent()) importButton.disabled = false;
    }
  });
  assessButton.addEventListener("click", async () => {
    const title = fields.title.value.trim();
    const description = fields.description.value.trim();
    if (!title || !description) {
      showError("job-assess-error", "Add a title and role description to check fit.");
      (!title ? fields.title : fields.description).focus();
      return;
    }
    const activeEditor = editorVersion;
    const version = ++fitVersion;
    const isCurrent = () => dialog.open && editorVersion === activeEditor && fitVersion === version;
    assessButton.disabled = true;
    showError("job-assess-error", "");
    try {
      const {match, brief} = await api("jobs/assess", {title, company: fields.company.value.trim(), description});
      if (isCurrent()) renderFit(match, brief);
    } catch (error) {
      if (isCurrent()) showError("job-assess-error", error.message);
    } finally {
      if (isCurrent()) assessButton.disabled = false;
    }
  });
  el("clear-job-action").addEventListener("click", () => {
    fields.next_action.value = "";
    fields.next_date.value = "";
    refreshEditor();
    fields.next_action.focus();
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (saveButton.disabled) return;
    const id = editingId;
    closeButton.disabled = saveButton.disabled = true;
    const input = formValues(form);
    input.contact_id = input.contact_id ? Number(input.contact_id) : null;
    const errorField = el("job-error");
    errorField.hidden = true;
    try {
      await api(id ? "jobs/" + id + "/update" : "jobs", input);
      if (!id) {
        filter = input.stage === "closed" ? "closed" : "active";
        el("job-search").value = "";
      }
      dialog.close();
      notice(id ? "Application updated." : "Application added.");
      await refresh().catch(error => notice(error.message, true));
    } catch (error) {
      errorField.textContent = error.message;
      errorField.hidden = false;
    } finally {
      closeButton.disabled = saveButton.disabled = false;
    }
  });
  form.addEventListener("invalid", event => {
    const group = event.target.closest("details");
    if (group) group.open = true;
  }, true);
  window.addEventListener("dashboard:render", event => {
    const previous = JSON.stringify(dashboard.job_suggestions);
    dashboard = event.detail;
    render();
    if (dialog.open && JSON.stringify(dashboard.job_suggestions) !== previous) renderSuggestions();
  });
  window.addEventListener("emailsender:view", event => {
    if (event.detail === "jobs") render();
  });
  window.addEventListener("emailsender:track-contact", event => {
    const contact = event.detail;
    const existing = dashboard.jobs?.find(job => job.contact_id === contact.id);
    editJob(existing || {
      title: contact.role, company: contact.company, posting_url: contact.job_url, contact_id: contact.id
    });
  });
  renderBoards();
  render();
})();
