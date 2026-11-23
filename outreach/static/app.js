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

function addFields(container, contact = {}) {
  const definitions = [
    ["scenario", "Outreach goal", "select", ""], ["name", "Recipient name", "text", ""],
    ["company", "Company", "text", ""], ["role", "Target job title", "text", ""],
    ["context", "Reason for reaching out", "textarea", "What did you notice about the company or its work?"],
    ["source", "Context source", "url", "https://"], ["job_url", "Job listing", "url", "https://"],
    ["idea", "Freelance idea", "textarea", "What would you propose to this business?"]
  ];
  for (const [key, title, type, placeholder] of definitions) {
    const label = node("label", title, type === "textarea" ? "wide" : "");
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
    label.append(field);
    container.append(label);
  }
}

function formValues(form) {
  const values = {};
  for (const [key, value] of new FormData(form)) values[key] = value.trim();
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
  el("empty-help").textContent = model.contacts.length ? "Try another search or filter." : "Add a recipient’s email above to start your first outreach.";
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
    open.addEventListener("click", () => {
      selected = contact.id;
      renderDetail(contact);
      el("detail").showModal();
    });
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
  const setupParts = [];
  if (!profile.name || !profile.proof_url) setupParts.push("save your sender profile");
  if (!connected) setupParts.push("connect a mailbox");
  el("setup-note").hidden = !setupParts.length;
  const sentence = setupParts.join(" and ");
  el("setup-note-text").textContent = sentence ? sentence[0].toUpperCase() + sentence.slice(1) + " to enable automatic sending." : "";
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
      if (contact) renderDetail(contact);
      feedback(success);
    }
  } catch (error) {
    feedback(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderDetail(contact) {
  feedback("");
  el("detail-title").textContent = contact.name || contact.email;
  el("detail-company").textContent = [contact.name && contact.email, contact.company, goals[contact.scenario]].filter(Boolean).join(" · ");
  el("detail-status").replaceChildren(statusChip(contact.status));
  const content = el("detail-body");
  content.replaceChildren();
  if (contact.reason) section(content, "Status detail").append(node("p", contact.reason));
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
  if (["ready", "needs_context", "dm_draft"].includes(contact.status)) {
    const details = node("details", "", "detail-edit");
    details.open = contact.status === "needs_context";
    details.append(node("summary", "Edit outreach details"));
    const form = node("form");
    const fields = node("div", "", "form-grid");
    addFields(fields, contact);
    const save = node("button", "Save and prepare again", "button dark save");
    save.type = "submit";
    form.append(fields, save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      changeContact(contact.id, "update", formValues(form), save, "Outreach details updated.");
    });
    details.append(form);
    content.append(details);
  }
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
addOptions(el("profile-form").elements.goal, goals);
el("profile-form").addEventListener("input", () => { profileDirty = true; });
onSubmit("profile-form", "profile-error", async form => {
  const data = formValues(form);
  data.auto_respond = form.elements.auto_respond.checked;
  await api("profile", data);
  profileDirty = false;
  await refresh();
  notice("Sender profile saved.");
});
onSubmit("add-form", "add-error", async form => {
  const data = formValues(form);
  await api("contacts", data);
  form.reset();
  el("new-details").open = false;
  await refresh();
  notice(data.email + " added to your queue.");
});
onSubmit("mailbox-form", "mailbox-error", async form => {
  const data = formValues(form);
  try { await api("mailbox", data); }
  finally { form.elements.password.value = ""; }
  el("mailbox-connect").open = false;
  await refresh();
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
