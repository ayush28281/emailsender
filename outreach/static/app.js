"use strict";

const el = id => document.getElementById(id);
const goals = {
  founder: "Join a startup", role: "Apply for a role", referral: "Ask for a referral",
  freelance: "Find freelance work", dm: "Draft a social DM"
};
const statusLabels = {
  ready: "Ready to send", needs_context: "Needs context", dm_draft: "DM draft",
  waiting: "Awaiting reply", completed: "Completed", replied: "Replied", stopped: "Stopped",
  suppressed: "Opted out", bounced: "Bounced", uncertain: "Check delivery"
};
const attention = new Set(["needs_context", "uncertain", "dm_draft"]);
const stoppable = new Set(["ready", "needs_context", "dm_draft", "waiting", "uncertain"]);
let model = {contacts: [], metrics: {}, profile: {}, mailbox: {}, worker: {}};
let activeFilter = "all";
let selected = null;
let profileDirty = false;
let refreshVersion = 0;
let noticeTimer;
let stopped = false;

function node(tag, text = "", cssClass = "") {
  const item = document.createElement(tag);
  if (text) item.textContent = text;
  if (cssClass) item.className = cssClass;
  return item;
}

function pretty(value) {
  return String(value || "").replaceAll("_", " ");
}

function stamp(value) {
  if (!value) return "";
  const parsed = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
  }).format(parsed);
}

function notice(text, error = false) {
  if (stopped) return;
  const bar = el("notice");
  clearTimeout(noticeTimer);
  bar.textContent = text;
  bar.classList.toggle("error", error);
  bar.hidden = !text;
  if (!error) noticeTimer = setTimeout(() => { bar.hidden = true; }, 6000);
}

async function api(path, data) {
  const options = {cache: "no-store", credentials: "same-origin"};
  if (data !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch("/api/" + path, options);
  } catch {
    throw new Error("Could not reach Emailsender. Check that the local app is running.");
  }
  const result = await response.json().catch(() => null);
  if (!response.ok || !result || result.error) {
    throw new Error(result?.error || "The server could not complete that action. Try again.");
  }
  return result;
}

function addOptions(select, options) {
  for (const [value, label] of Object.entries(options)) select.add(new Option(label, value));
}

const contactFields = [
    ["scenario", "Outreach goal", "select", ""], ["name", "Recipient name", "text", ""],
    ["company", "Company", "text", ""], ["role", "Target job title", "text", ""],
    ["context", "Reason for reaching out", "textarea", "What did you notice about the company or its work?"],
    ["source", "Context source", "url", "https://"], ["job_url", "Job listing", "url", "https://"],
    ["idea", "Freelance idea", "textarea", "What would you propose to this business?"]
];
const questions = {
  company: "Which company does this person work for?",
  context: "What did you notice about this company?",
  role: "What’s the exact title of the open role?",
  job_url: "What’s the link to the job opening?",
  idea: "What would you propose to this business?",
  source: "Where did you find the company information?",
  name: "What name should the greeting use?",
  scenario: "What do you want to contact this person about?"
};

function addFields(container, contact = {}, keys = contactFields.map(([key]) => key), guided = false) {
  for (const key of keys) {
    const [, title, type, placeholder] = contactFields.find(([name]) => name === key);
    const label = node("label", guided ? questions[key] : title, type === "textarea" || guided ? "wide" : "");
    let field;
    if (type === "select") {
      field = node("select");
      addOptions(field, {auto: "Choose automatically", ...goals});
    } else if (type === "textarea") {
      field = node("textarea");
      field.rows = 2;
    } else {
      field = node("input");
      field.type = type;
    }
    field.name = key;
    if (type !== "select") field.maxLength = 2000;
    if (placeholder) field.placeholder = placeholder;
    field.value = contact[key] || (key === "scenario" ? "auto" : "");
    if (key === "company" || key === "source") {
      field.addEventListener("input", () => {
        const check = field.form?.elements.reuse_context;
        if (check) check.checked = false;
      });
    }
    label.append(field);
    container.append(label);
    if (key === "context") {
      const share = node("label", "", "wide share-observation");
      const check = node("input");
      check.type = "checkbox";
      check.name = "reuse_context";
      check.checked = Boolean(contact.reuse_context);
      const syncShare = () => {
        check.disabled = !field.value.trim();
        if (check.disabled) check.checked = false;
        share.hidden = check.disabled;
      };
      field.addEventListener("input", () => { check.checked = false; syncShare(); });
      field.form?.addEventListener("reset", () => queueMicrotask(syncShare));
      share.append(check, node("span", "Save this observation for other contacts at this company"));
      container.append(share);
      syncShare();
    }
  }
}

function formValues(form) {
  const values = {};
  for (const [key, value] of new FormData(form)) values[key] = value.trim();
  if (form.elements.reuse_context) values.reuse_context = form.elements.reuse_context.checked;
  return values;
}

function statusChip(status) {
  const chip = node("span", statusLabels[status] || pretty(status) || "Pending", "status");
  if (attention.has(status) || status === "bounced") chip.classList.add("status-attention");
  else if (["ready", "waiting"].includes(status)) chip.classList.add("status-active");
  else if (status === "replied") chip.classList.add("status-replied");
  return chip;
}

function nextAction(contact) {
  if (contact.status === "needs_context") return "Add details";
  if (contact.status === "dm_draft") return "Copy draft";
  if (contact.status === "uncertain") return "Check sent mail";
  if (contact.status === "replied") return "Review reply";
  if (contact.next_at) return stamp(contact.next_at);
  if (contact.status === "ready") return "Next cycle";
  return "—";
}

function renderContacts() {
  const search = el("search").value.trim().toLowerCase();
  const rows = model.contacts.filter(contact => {
    if (activeFilter === "attention" && !attention.has(contact.status)) return false;
    if (activeFilter === "replies" && !(contact.replied_at || contact.status === "replied")) return false;
    return [contact.email, contact.name, contact.company].some(value => String(value || "").toLowerCase().includes(search));
  });
  el("contact-count").textContent = model.contacts.length;
  el("contact-table").hidden = !rows.length;
  el("empty").hidden = !!rows.length;
  el("empty-title").textContent = model.contacts.length ? "No matching recipients" : "Your queue is empty";
  el("empty-help").textContent = model.contacts.length ? "Try another search or filter." : "Recipients and their next steps will appear here.";
  const list = el("contact-rows");
  list.replaceChildren();
  for (const contact of rows) {
    const row = node("tr");
    const person = node("td", contact.name || contact.email);
    const extra = contact.name ? contact.email : contact.company;
    if (extra) person.append(node("span", extra, "second-line"));
    const status = node("td");
    status.append(statusChip(contact.status));
    const actions = node("td");
    const open = node("button", "Details", "open-row");
    open.type = "button";
    open.setAttribute("aria-label", "Details for " + contact.email);
    open.addEventListener("click", () => openContact(contact));
    actions.append(open);
    row.append(person, node("td", goals[contact.scenario] || "Automatic"), status, node("td", nextAction(contact)), actions);
    list.append(row);
  }
}

function setMetric(id, rate, title, count, delivered) {
  const percent = Number(rate || 0);
  el(id).textContent = new Intl.NumberFormat(undefined, {style: "percent", maximumFractionDigits: 1}).format(percent);
  el(id + "-detail").textContent = (count || 0) + " " + title + " / " + delivered + " delivered";
}

function renderCapture() {
  const {profile, mailbox} = model;
  const form = el("add-form");
  const saved = Boolean(profile.name && profile.proof_url);
  el("capture-title").textContent = saved ? "New outreach" : "Set up your sender profile";
  el("setup-note").hidden = saved;
  el("setup-note-text").textContent = mailbox.configured
    ? "Your mailbox is connected. Add your name and a work sample to personalize your messages."
    : "Save your name and a work sample, then connect the mailbox you want to send from.";
  el("sender-summary").hidden = !saved;
  el("setup-queue").hidden = !saved;
  form.hidden = !saved;
  if (!saved) return;

  el("sender-identity").textContent = mailbox.configured
    ? "Sending as " + profile.name + " (" + mailbox.address + ")"
    : "Sender profile · " + profile.name;
  el("sender-goal").textContent = "Default goal · " + (goals[profile.goal] || goals.founder);

  const fields = form.elements;
  const selectedGoal = fields.scenario.value;
  const jobOrProposal = [fields.role, fields.job_url, fields.idea].some(field => field.value.trim());
  const draftDM = selectedGoal === "dm" || (selectedGoal === "auto" && profile.goal === "dm" && !jobOrProposal);
  if (draftDM) {
    el("capture-action").textContent = "Create DM draft";
    el("capture-help").textContent = "We'll prepare a message for you to copy into LinkedIn or X.";
  } else if (mailbox.configured) {
    el("capture-action").textContent = "Start outreach";
    el("capture-help").textContent = "We'll research the company and send automatically when there's enough context.";
  } else {
    el("capture-action").textContent = "Queue for sending";
    el("capture-help").textContent = "We'll prepare the email now. Ready emails send automatically after you connect your mailbox in Sender setup.";
  }
}

function renderState() {
  const {metrics, profile, mailbox, worker} = model;
  const connected = Boolean(mailbox.configured);
  el("mailbox-state").textContent = connected ? "Mailbox configured" : "Mailbox disconnected";
  el("mailbox-dot").classList.toggle("on", connected);
  el("setup-mailbox-state").textContent = connected ? mailbox.address : "No sending mailbox connected";
  el("mailbox-connect-label").textContent = connected ? "Update credentials" : "Connect mailbox";
  const mailForm = el("mailbox-form");
  if (connected && !el("mailbox-connect").open) {
    mailForm.elements.address.value = mailbox.address;
    for (const key of ["smtp_host", "smtp_port", "imap_host", "imap_port"]) {
      if (mailbox[key]) mailForm.elements[key].value = mailbox[key];
    }
  }
  mailForm.elements.address.readOnly = connected;
  renderCapture();
  const last = stamp(worker.last_run);
  el("worker-state").textContent = (worker.running ? "Worker running" : "Worker idle") + (last ? " · Last cycle " + last : " · No completed cycle yet");
  el("worker-error").textContent = worker.last_error || "";
  el("worker-error").hidden = !worker.last_error;
  if (!profileDirty) {
    const form = el("profile-form");
    for (const [key, value] of Object.entries(profile)) {
      const field = form.elements.namedItem(key);
      if (!field) continue;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else field.value = value || (key === "goal" ? "founder" : "");
    }
  }
  const delivered = metrics.delivered || 0;
  el("m-delivered").textContent = delivered;
  setMetric("m-reply", metrics.reply_rate, "replies", metrics.replies, delivered);
  setMetric("m-positive", metrics.positive_rate, "positive", metrics.positive, delivered);
  setMetric("m-outcome", metrics.outcome_rate, "outcomes", metrics.outcomes, delivered);
  renderContacts();
}

async function refresh() {
  if (stopped) return;
  const version = ++refreshVersion;
  const latest = await api("dashboard");
  if (version !== refreshVersion || stopped) return;
  model = latest;
  renderState();
}

function setView(focus = false) {
  if (stopped) return;
  const view = location.hash === "#setup" ? "setup" : "queue";
  for (const name of ["setup", "queue"]) {
    el(name + "-view").hidden = name !== view;
    const nav = el("nav-" + name);
    if (name === view) nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  }
  document.title = (view === "setup" ? "Sender setup" : "Outreach queue") + " · Emailsender";
  if (focus) el("main").focus();
}

function section(parent, heading) {
  const part = node("section", "", "dialog-section");
  part.append(node("h3", heading));
  parent.append(part);
  return part;
}

function sourceLink(parent, value) {
  if (!value) return;
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return;
    const link = node("a", "View source at " + url.host + " ↗");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    parent.append(link);
  } catch { /* A source can be absent on a manually prepared record. */ }
}

function feedback(text, error = false) {
  const field = el("detail-feedback");
  field.textContent = text;
  field.classList.toggle("error", error);
  field.hidden = !text;
}

async function changeContact(id, action, data, button, success) {
  button.disabled = true;
  feedback("");
  try {
    await api("contacts/" + encodeURIComponent(id) + "/" + action, data);
    await refresh();
    if (selected === id && el("detail").open) {
      const contact = model.contacts.find(item => item.id === id);
      if (contact) {
        renderDetail(contact);
        if (action === "update" && contact.status === "needs_context") focusQuestion();
      }
      feedback(success);
    }
  } catch (error) {
    if (selected === id && el("detail").open) feedback(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function focusQuestion() {
  el("detail-body").querySelector(".needed-fields input, .needed-fields select, .needed-fields textarea, .profile-fix")?.focus();
}

function openContact(contact) {
  selected = contact.id;
  renderDetail(contact);
  if (!el("detail").open) el("detail").showModal();
  if (contact.status === "needs_context") focusQuestion();
}

function offerCompanyDetails(contact, form, panel) {
  const company = form.elements.company;
  const observation = form.elements.context;
  const source = form.elements.source;
  let request = 0;
  let timer;
  let applied = null;

  async function load(useInput = false) {
    const version = ++request;
    const query = company.value.trim();
    try {
      const {suggestion} = await api("contacts/" + contact.id + "/company-suggestion", useInput ? {company: query} : {});
      if (!form.isConnected || version !== request || company.value.trim() !== query || !suggestion) return;
      if (suggestion.company === query && (!suggestion.context || suggestion.context === observation.value)) return;
      const heading = node("strong", "Saved details for " + suggestion.company);
      panel.replaceChildren(heading);
      if (suggestion.context) panel.append(node("p", suggestion.context));
      sourceLink(panel, suggestion.source);
      const use = node("button", "Use saved details", "button outline");
      use.type = "button";
      use.addEventListener("click", () => {
        company.value = suggestion.company;
        if (suggestion.context) {
          observation.value = suggestion.context;
          observation.dispatchEvent(new Event("input", {bubbles: true}));
          source.value = suggestion.source || "";
          applied = {context: observation.value, source: source.value};
        }
        heading.textContent = "Using saved details for " + suggestion.company;
        use.remove();
        feedback("Saved details filled in. Review them before saving.");
      });
      panel.append(use);
      panel.hidden = false;
    } catch {
      // Saved information is optional. The editable fields remain usable offline.
    }
  }
  company.addEventListener("input", () => {
    ++request;
    panel.hidden = true;
    clearTimeout(timer);
    if (applied) {
      if (observation.value === applied.context) {
        observation.value = "";
        observation.dispatchEvent(new Event("input", {bubbles: true}));
      }
      if (source.value === applied.source) source.value = "";
      applied = null;
    }
    timer = setTimeout(() => { if (form.isConnected) load(true); }, 300);
  });
  for (const field of [observation, source]) {
    field.addEventListener("input", () => { if (applied) panel.hidden = true; });
  }
  load();
}

function contactEditor(parent, contact) {
  const held = contact.status === "needs_context";
  const missing = contact.missing_fields || [];
  const focused = missing.filter(key => Object.hasOwn(questions, key));
  const form = node("form", "", "contact-editor");
  parent.append(form);
  let suggestionPanel;
  if (held) {
    const needed = section(form, "Details needed");
    if (contact.reason) needed.append(node("p", contact.reason, "muted"));
    const fields = node("div", "", "form-grid needed-fields");
    needed.append(fields);
    addFields(fields, contact, focused, true);
    if (missing.includes("profile")) {
      const link = node("a", "Edit sender profile", "profile-fix");
      link.href = "#setup";
      link.addEventListener("click", () => el("detail").close());
      needed.append(link, node("p", "After saving your profile, return to this recipient and choose Prepare again."));
    }
    if (missing.includes("email")) {
      const correct = node("button", "Add corrected address", "button outline");
      correct.type = "button";
      correct.addEventListener("click", () => {
        el("detail").close();
        el("new-email").value = contact.email;
        el("new-email").focus();
        el("new-email").select();
      });
      needed.append(correct);
    }
    suggestionPanel = node("aside", "", "company-suggestion");
    suggestionPanel.hidden = true;
    needed.append(suggestionPanel);
  }
  const details = node("details", "", "detail-edit");
  details.append(node("summary", held ? "Other outreach details" : "Edit outreach details"));
  const fields = node("div", "", "form-grid");
  addFields(fields, contact, contactFields.map(([key]) => key).filter(key => !focused.includes(key)));
  details.append(fields);
  form.append(details);
  const save = node("button", held && !focused.length ? "Prepare again" : held ? "Save and continue" : "Save and prepare again", "button dark save");
  save.type = "submit";
  if (held) {
    if (!missing.includes("email")) {
      const help = contact.scenario === "dm" ? "When ready, the DM will be available to copy." :
        model.mailbox.configured ? "When ready, this email will send automatically." : "Ready emails wait in your queue until you connect a mailbox.";
      form.append(node("p", help, "muted prepare-note"), save);
    }
  } else {
    details.append(save);
  }
  const initial = formValues(form);
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (save.disabled || missing.includes("email")) return;
    const values = formValues(form);
    const changes = Object.fromEntries(Object.entries(values).filter(([key, value]) => initial[key] !== value));
    if (["context", "source", "company"].some(key => key in changes)) changes.reuse_context = values.reuse_context;
    changeContact(contact.id, "update", changes, save, "Outreach details updated.");
  });
  form.addEventListener("invalid", event => {
    const disclosure = event.target.closest("details");
    if (disclosure) disclosure.open = true;
  }, true);
  if (held && focused.some(key => key === "company" || key === "context")) offerCompanyDetails(contact, form, suggestionPanel);
}

function renderDetail(contact) {
  feedback("");
  el("detail-title").textContent = contact.name || contact.email;
  el("detail-company").textContent = [contact.name && contact.email, contact.company, goals[contact.scenario]].filter(Boolean).join(" · ");
  el("detail-status").replaceChildren(statusChip(contact.status));
  const content = el("detail-body");
  content.replaceChildren();
  if (contact.status === "needs_context") contactEditor(content, contact);
  else if (contact.reason) section(content, "Status detail").append(node("p", contact.reason));
  if (contact.created_at || contact.sent_at || contact.replied_at) {
    const history = section(content, "Contact history");
    const events = [["Added", contact.created_at], ["Initial email sent", contact.sent_at], ["Reply received", contact.replied_at]];
    for (const [label, date] of events) {
      if (date) history.append(node("p", label + " · " + stamp(date)));
    }
    if (contact.followups) history.append(node("p", "Follow-ups sent · " + contact.followups));
  }
  if (contact.context || contact.source) {
    const part = section(content, "Research");
    if (contact.context) part.append(node("p", contact.context));
    sourceLink(part, contact.source);
  }
  if (contact.body) {
    const part = section(content, contact.status === "dm_draft" ? "Social message draft" : "Initial email");
    if (contact.subject) part.append(node("p", contact.subject, "subject"));
    part.append(node("p", contact.body, "message-copy"));
    if (contact.status === "dm_draft") {
      const actions = node("div", "", "detail-actions");
      const copy = node("button", "Copy DM", "button outline");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(contact.body); feedback("Draft copied."); }
        catch { feedback("Select the draft text to copy it.", true); }
      });
      actions.append(copy);
      part.append(actions);
    }
  }
  if (contact.reply_kind || contact.reply_suggestion) {
    const part = section(content, "Reply");
    if (contact.reply_kind) part.append(node("p", "Classification · " + pretty(contact.reply_kind), "muted"));
    if (contact.reply_text) part.append(node("p", contact.reply_text, "message-copy"));
    if (contact.reply_suggestion) {
      part.append(node("h3", "Suggested response"), node("p", contact.reply_suggestion, "message-copy"));
    }
  }
  if (contact.auto_response_status) section(content, "Automatic response").append(node("p", pretty(contact.auto_response_status)));
  if (contact.next_at) section(content, "Next scheduled action").append(node("p", stamp(contact.next_at)));
  if (["ready", "dm_draft"].includes(contact.status)) contactEditor(content, contact);
  const outcomePart = section(content, "Outcome");
  const form = node("form", "", "outcome");
  const label = node("label", "Conversation outcome");
  const select = node("select");
  select.name = "value";
  addOptions(select, {none: "No outcome recorded", call: "Call", interview: "Interview", other: "Other outcome"});
  select.value = contact.outcome || "none";
  label.append(select);
  const save = node("button", "Save outcome", "button outline");
  save.type = "submit";
  form.append(label, save);
  form.addEventListener("submit", event => {
    event.preventDefault();
    changeContact(contact.id, "outcome", {value: select.value}, save, "Outcome saved.");
  });
  outcomePart.append(form);
  if (stoppable.has(contact.status) || contact.auto_response_status === "pending") {
    const actions = node("div", "", "detail-actions");
    const stop = node("button", "Stop outreach", "button outline");
    stop.type = "button";
    stop.addEventListener("click", () => changeContact(contact.id, "stop", {}, stop, "Outreach stopped."));
    actions.append(stop);
    content.append(actions);
  }
}

function onSubmit(id, errorId, handler) {
  const form = el(id);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const errorField = el(errorId);
    errorField.hidden = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { await handler(form); }
    catch (error) {
      errorField.textContent = error.message;
      errorField.hidden = false;
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });
}

addFields(el("new-fields"));
el("new-fields").addEventListener("input", renderCapture);
el("add-form").addEventListener("reset", () => queueMicrotask(renderCapture));
addOptions(el("profile-form").elements.goal, goals);
el("profile-form").addEventListener("input", () => { profileDirty = true; });
onSubmit("profile-form", "profile-error", async form => {
  const data = formValues(form);
  const finishesSetup = !model.profile.name && model.mailbox.configured;
  data.auto_respond = form.elements.auto_respond.checked;
  await api("profile", data);
  profileDirty = false;
  await refresh();
  if (finishesSetup) location.hash = "queue";
  notice("Sender profile saved.");
});
onSubmit("add-form", "add-error", async form => {
  const data = formValues(form);
  const {contact} = await api("contacts", data);
  form.reset();
  el("new-details").open = false;
  await refresh();
  const latest = model.contacts.find(item => item.id === contact.id) || contact;
  if (latest.status === "needs_context") {
    openContact(latest);
  } else {
    notice(latest.status === "dm_draft" ? "DM ready to copy in recipient details." : data.email + " added to your queue.");
  }
});
onSubmit("mailbox-form", "mailbox-error", async form => {
  const data = formValues(form);
  const finishesSetup = !model.mailbox.configured && model.profile.name;
  try { await api("mailbox", data); }
  finally { form.elements.password.value = ""; }
  el("mailbox-connect").open = false;
  await refresh();
  if (finishesSetup && !profileDirty) location.hash = "queue";
  notice("Mailbox connected.");
});
el("mailbox-connect").addEventListener("toggle", () => {
  if (!el("mailbox-connect").open) el("mailbox-form").elements.password.value = "";
});
for (const filter of document.querySelectorAll("[data-filter]")) {
  filter.addEventListener("click", () => {
    activeFilter = filter.dataset.filter;
    for (const option of document.querySelectorAll("[data-filter]")) option.setAttribute("aria-pressed", String(option === filter));
    renderContacts();
  });
}
el("search").addEventListener("input", renderContacts);
el("refresh").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await refresh(); notice("Queue refreshed."); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
el("run").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const cycle = await api("run", {});
    await refresh();
    const message = cycle.busy ? "A cycle is already running." : "Cycle complete. Sent " + (cycle.sent || 0) + " · Replies " + (cycle.replies || 0) + ".";
    notice(model.worker.last_error || message, Boolean(model.worker.last_error));
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
el("close-detail").addEventListener("click", () => el("detail").close());
el("quit").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api("quit", {});
    stopped = true;
    clearInterval(refreshTimer);
    clearTimeout(noticeTimer);
    el("notice").hidden = true;
    el("mailbox-form").elements.password.value = "";
    el("setup-view").hidden = true;
    el("queue-view").hidden = true;
    el("stopped-view").hidden = false;
    document.querySelector("nav").hidden = true;
    document.querySelector(".mailbox-state").hidden = true;
    document.querySelector(".brand").removeAttribute("href");
    document.title = "Local app stopped · Emailsender";
    el("main").focus();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});
el("detail").addEventListener("close", () => { selected = null; });
window.addEventListener("hashchange", () => setView(true));
setView();
refresh().catch(error => {
  notice(error.message, true);
  el("worker-state").textContent = "Workspace unavailable";
  el("empty-title").textContent = "Could not load recipients";
  el("empty-help").textContent = "Use Refresh to reconnect.";
});
const refreshTimer = setInterval(() => {
  if (!document.hidden && !el("detail").open) refresh().catch(error => notice(error.message, true));
}, 30000);
