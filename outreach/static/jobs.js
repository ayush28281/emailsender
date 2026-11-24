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
  const dateFormatter = new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"});
  let dashboard = model;
  let filter = "active";
  let editingId = null;
  let displayKey;

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
      return false;
    }
  }

  function editJob(job = {}) {
    editingId = job.id ?? null;
    el("job-editor-title").textContent = editingId ? "Edit application" : "Add a job";
    for (const name of ["title", "company", "posting_url", "next_date", "next_action", "notes"]) {
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
    const progress = el("job-progress");
    progress.hidden = !job.stage_changed_at;
    progress.textContent = job.stage_changed_at ? "Stage updated " + stamp(job.stage_changed_at) : "";
    el("interview-prep").open = false;
    refreshEditor();
    dialog.showModal();
    fields.title.focus();
  }

  function refreshEditor() {
    fields.next_action.placeholder = examples[fields.stage.value];
    const link = el("job-editor-posting");
    link.hidden = !postingLink(link, fields.posting_url.value);
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
    const next = [...tasks.filter(task => task.due), ...outreach, ...tasks.filter(task => !task.due)].slice(0, 5);
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
    const currentDisplay = JSON.stringify([today, dashboard.jobs, (dashboard.contacts || []).map(contact => [
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
  el("add-job").addEventListener("click", () => editJob());
  closeButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", event => {
    if (saveButton.disabled) event.preventDefault();
  });
  fields.stage.addEventListener("change", refreshEditor);
  fields.contact_id.addEventListener("change", refreshEditor);
  for (const field of [fields.title, fields.company, fields.posting_url, fields.next_date, fields.next_action]) {
    field.addEventListener("input", refreshEditor);
  }
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
  window.addEventListener("dashboard:render", event => {
    dashboard = event.detail;
    render();
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
  render();
})();
