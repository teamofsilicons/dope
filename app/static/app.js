"use strict";

const $ = (id) => document.getElementById(id);
const labels = { active: "Open", completed: "Done", review: "In review", saved: "Saved", archived: "Archived" };
let account, boards = [], board, items = [], viewerId;
let filter = "active", busy = false, loaded = false, boardVersion = 0, contextVersion = 0;
const drafts = new Map();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, className, action) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

async function api(path, { method = "GET", body, scope } = {}) {
  const headers = { Accept: "application/json" };
  if (scope) {
    headers["X-Dope-Board"] = scope;
    headers["X-Dope-Account"] = account.uuid;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: JSON.stringify(body),
    credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const json = await response.json().catch(() => ({}));
    const error = new Error(typeof json.detail === "string" ? json.detail : "The request could not be completed.");
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function notice(message, retry = false) {
  $("notice").hidden = !message;
  $("notice").firstElementChild.textContent = message || "";
  $("retry").hidden = !retry;
}

function signIn(message) {
  ++boardVersion;
  ++contextVersion;
  account = board = null;
  items = []; boards = []; drafts.clear(); loaded = false;
  $("description").value = "";
  $("dopes").replaceChildren();
  $("boards").replaceChildren();
  $("board-select").replaceChildren();
  $("board-title").textContent = "";
  $("detail").close();
  $("detail-body").replaceChildren();
  $("workspace").hidden = $("account-menu").hidden = $("loading").hidden = true;
  $("signin").hidden = false;
  $("signin-message").textContent = message || "";
  $("signin-message").hidden = !message;
}

function clearBoard() {
  items = []; viewerId = null; loaded = false;
  $("detail").close();
  $("detail-body").replaceChildren();
  $("dopes").replaceChildren();
  $("filters").replaceChildren();
  $("list-status").hidden = false;
  $("list-status").textContent = "Loading dopes…";
  $("add").disabled = true;
}

function storedBoard() {
  try { return sessionStorage.getItem("silicon-dopes.board." + account.uuid); } catch { return null; }
}

function renderBoards() {
  const rail = $("boards"), select = $("board-select");
  rail.replaceChildren(); select.replaceChildren();
  const groups = new Map();
  for (const choice of boards) {
    const groupId = choice.kind === "team" ? "organization:" + choice.id.slice(5).split("/")[0] : choice.id;
    if (!groups.has(groupId)) {
      const label = choice.organization || "Private";
      const section = element("div", "board-group");
      section.append(element("p", "group-name", label));
      rail.append(section);
      const options = element("optgroup"); options.label = label; select.append(options);
      groups.set(groupId, { section, options });
    }
    const name = choice.kind === "organization" ? "Organization board" : choice.name;
    const link = button("", "board-link", () => selectBoard(choice));
    const icon = element("span", "board-icon " + choice.kind);
    icon.setAttribute("aria-hidden", "true");
    link.append(icon, element("span", "", name));
    link.setAttribute("aria-current", String(choice.id === board?.id));
    const group = groups.get(groupId);
    group.section.append(link);
    const option = element("option", "", name);
    option.value = choice.id; group.options.append(option);
  }
  if (board) select.value = board.id;
}

async function loadContext() {
  const version = ++contextVersion;
  try {
    const result = await api("/api/boards");
    if (version !== contextVersion) return;
    const switched = account?.uuid !== result.account.uuid;
    if (switched) { drafts.clear(); board = null; }
    account = result.account; boards = result.boards;
    $("account-name").textContent = account.display_name;
    $("account-handle").textContent = account.handle;
    $("initial").textContent = Array.from(account.display_name)[0]?.toUpperCase() || "•";
    $("signin").hidden = $("loading").hidden = true;
    $("workspace").hidden = $("account-menu").hidden = false;
    const choice = boards.find((b) => b.id === (board?.id || storedBoard())) || boards[0];
    await selectBoard(choice);
  } catch (error) {
    if (version !== contextVersion) return;
    if (error.status === 401) {
      signIn(signinMessage);
    } else {
      ++boardVersion;
      clearBoard();
      $("list-status").textContent = "Your boards could not be loaded.";
      if (!account) $("loading").textContent = "Your boards could not be loaded.";
      notice(error.message || "Your boards could not be loaded.", true);
    }
  }
}

async function selectBoard(choice) {
  if (!choice) return;
  if (board?.id !== choice.id) {
    if (board) drafts.set(board.id, $("description").value);
    clearBoard();
    $("description").value = drafts.get(choice.id) || "";
    $("search").value = ""; filter = "active"; notice();
  }
  board = choice;
  try { sessionStorage.setItem("silicon-dopes.board." + account.uuid, choice.id); } catch {}
  renderBoards();
  $("board-title").textContent = choice.name;
  $("board-context").textContent = choice.kind === "team" ? choice.organization + " / Team board" :
    choice.kind === "organization" ? "Organization board" : "Private board";
  document.title = choice.name + " · Silicon Dopes";
  await refreshBoard();
}

async function refreshBoard() {
  const version = ++boardVersion;
  const id = board.id;
  try {
    const data = await api("/api/board", { scope: id });
    if (version !== boardVersion) return;
    board = data.board; items = data.dopes; viewerId = data.viewer_id; loaded = true;
    renderList(); updateComposer();
  } catch (error) {
    if (version !== boardVersion) return;
    clearBoard();
    $("list-status").textContent = "This board could not be loaded.";
    if (error.status === 401) signIn("Sign in again to open your boards.");
    else if (error.status === 403 || error.status === 409) {
      board = null;
      await loadContext();
    } else notice(error.message, true);
  }
}

function updateComposer() {
  const input = $("description");
  input.style.height = "auto";
  input.style.height = Math.min(320, Math.max(50, input.scrollHeight)) + "px";
  $("add").disabled = !loaded || busy || !input.value.trim();
}

function formatDate(iso) {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(iso));
}

function attribution(item) {
  if (item.completed_at) return "Completed by " + item.finisher + " · " + formatDate(item.completed_at);
  return "Added by " + item.creator + " · " + formatDate(item.created_at);
}

function renderList() {
  const counts = {};
  for (const item of items) counts[item.status] = (counts[item.status] || 0) + 1;
  if (!["active", "completed"].includes(filter) && !counts[filter]) filter = "active";
  $("filters").replaceChildren();
  for (const [key, label] of Object.entries(labels)) {
    if (key !== "active" && key !== "completed" && !counts[key]) continue;
    const tab = button(label, "filter", () => { filter = key; renderList(); });
    tab.setAttribute("aria-pressed", String(filter === key));
    tab.append(" ", element("span", "count", String(counts[key] || 0)));
    $("filters").append(tab);
  }
  const query = $("search").value.trim().toLowerCase();
  const visible = items.filter((d) => d.status === filter &&
    [d.title, d.description, d.assignee, d.creator, d.finisher, d.note].some((v) => v?.toLowerCase().includes(query)));
  $("list-status").hidden = visible.length > 0;
  $("list-status").textContent = query ? "No matching dopes." :
    filter === "active" ? "No open dopes. Add one above." : "No completed dopes yet.";
  $("dopes").replaceChildren(...visible.map(dopeRow));
}

function dopeRow(item) {
  const row = element("li", "dope-row");
  const done = item.status === "completed";
  const toggle = button(done ? "✓" : "", "toggle" + (done ? " done" : ""), () =>
    mutate(item, done ? "uncomplete" : "complete"));
  toggle.setAttribute("aria-label", (done ? "Reopen " : "Mark done: ") + item.title);
  toggle.title = done ? "Reopen dope" : "Mark done";
  toggle.disabled = busy || !(done || (item.status === "active" && !item.blocked));
  const main = element("div", "row-main");
  main.append(button(item.title, "dope-title", () => showDetail(item)),
    element("span", "row-meta", attribution(item)));
  row.append(toggle, main);
  if (item.status === "active") {
    if (item.blocked) main.append(element("span", "row-meta", "Waiting on a dependency"));
    if (item.assigned_to) {
      const owner = button(item.assigned_to === viewerId ? "Taken by you" : item.assignee,
        "row-action taken", () => showDetail(item));
      owner.setAttribute("aria-label", "Taken by " + item.assignee + ", view dope");
      row.append(owner);
    } else if (!item.blocked) {
      const take = button("Take", "row-action", () => mutate(item, "assign"));
      take.disabled = busy; row.append(take);
    }
  }
  return row;
}

function addDiscussion(container, item, scope) {
  const section = element("details", "secondary-panel");
  section.append(element("summary", "", "Comments"));
  const list = element("div", "comments"), form = element("form");
  const input = element("textarea"); input.rows = 2; input.maxLength = 10000; input.required = true;
  input.setAttribute("aria-label", "Comment"); input.placeholder = "Write a comment…";
  const send = element("button", "row-action", "Post comment"); send.type = "submit";
  form.append(input, send); section.append(list, form); container.append(section);
  const path = "/api/dopes/" + item.id + "/comments";
  async function read() {
    const comments = await api(path, { scope });
    if (!section.isConnected || !section.open) return;
    list.replaceChildren();
    for (const comment of comments) {
      const entry = element("div", "comment");
      entry.append(element("strong", "", comment.user.display_name), element("p", "full-text", comment.body));
      list.append(entry);
    }
    if (!comments.length) list.append(element("span", "row-meta", "No comments yet."));
    await api(path + "/read", { method: "POST", scope });
  }
  section.addEventListener("toggle", () => {
    if (section.open) read().catch((error) => notice(error.message));
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (send.disabled || !input.value.trim()) return;
    send.disabled = true;
    try {
      await api(path, { method: "POST", scope, body: { body: input.value.trim() } });
      if (!section.isConnected) return;
      input.value = "";
      await read();
    } catch (error) { notice(error.message); }
    finally { send.disabled = false; }
  });
}

async function loadReferences(container, item, scope) {
  // Recover existing links and images from legacy rich descriptions without inserting their HTML.
  const full = await api("/api/dopes/" + item.id, { scope });
  if (!container.isConnected) return;
  const template = document.createElement("template");
  template.innerHTML = full.description_html;
  const urls = new Map(full.commit_links.map((url) => [url, url]));
  for (const source of template.content.querySelectorAll("a[href]")) {
    const href = source.getAttribute("href");
    urls.set(href, source.textContent.trim() || href);
  }
  for (const [url, label] of urls) {
    if (!/^https?:\/\//i.test(url)) continue;
    const link = element("a", "reference", label);
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
    container.append(link);
  }
  for (const source of template.content.querySelectorAll("img[src]")) {
    const src = source.getAttribute("src");
    if (!/^(https?:\/\/|data:image\/(png|gif|jpe?g|webp);base64,)/i.test(src)) continue;
    const img = element("img", "attachment");
    img.alt = source.alt || "Dope attachment"; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
    img.src = src; container.append(img);
  }
}

function showDetail(item, editing = false) {
  const dialog = $("detail"), container = $("detail-body");
  container.replaceChildren(element("p", "field-title", board.name));
  const title = element("h2", "", editing ? "Edit dope" : item.title);
  title.id = "detail-title"; container.append(title);
  if (!editing && item.description && item.description !== item.title) container.append(element("p", "full-text", item.description));
  container.append(element("span", "row-meta", attribution(item)));
  if (item.assignee) container.append(element("span", "row-meta", "Taken by " + item.assignee));
  if (item.note && !editing) container.append(element("p", "full-text", item.note));
  const actions = element("div", "dialog-actions");
  function field(labelText, value) {
    const label = element("label", "", labelText); label.htmlFor = "detail-input";
    const input = element("textarea"); input.id = label.htmlFor; input.rows = 3;
    input.maxLength = 20000; input.value = value; container.append(label, input);
    return input;
  }
  if (editing) {
    const input = field("Description", item.description || item.title);
    actions.append(button("Save changes", "primary", () => {
      if (!input.value.trim()) { input.focus(); return; }
      const body = { description: input.value.trim() };
      if (item.title !== (item.description || "").split("\n")[0].slice(0, 180)) body.title = item.title;
      mutate(item, "edit", body);
    }));
  } else if (item.status === "active") {
    if (!item.blocked) {
      const input = field("Completion note (optional)", "");
      actions.append(button("Mark done", "primary", () => mutate(item, "complete", { description: input.value.trim() })));
      if (!item.assigned_to) actions.append(button("Take", "", () => mutate(item, "assign")));
    }
    if (item.assigned_to && (item.assigned_to === viewerId || board.can_manage)) {
      actions.append(button("Release", "", () => mutate(item, "unassign")));
    }
    actions.append(button("Edit", "", () => showDetail(item, true)));
  } else if (item.status === "completed" || item.status === "review") {
    if (item.status === "review" && board.can_manage) actions.append(button("Approve", "primary", () => mutate(item, "review/approve")));
    actions.append(button("Reopen", "", () => mutate(item, "uncomplete")));
  } else {
    const saved = item.status === "saved";
    actions.append(button(saved ? "Return to open" : "Restore", "primary", () => mutate(item, saved ? "move-to-active" : "restore")));
  }
  container.append(actions);
  if (!editing) {
    const references = element("div");
    container.append(references);
    loadReferences(references, item, board.id).catch((error) => {
      if (references.isConnected) notice(error.message);
    });
    addDiscussion(container, item, board.id);
    if (item.status === "active" || item.status === "completed") {
      const more = element("details", "secondary-panel");
      more.append(element("summary", "", "More actions"));
      const options = element("div", "dialog-actions");
      if (item.status === "active") options.append(button("Save for later", "", () => mutate(item, "save-for-later")));
      options.append(button("Archive", "", () => mutate(item, "archive")));
      more.append(options); container.append(more);
    }
  }
  if (!dialog.open) dialog.showModal();
}

async function write(path, method, body, onSuccess) {
  if (busy || !loaded || !board) return;
  const id = board.id, uuid = account.uuid;
  busy = true; updateComposer(); notice();
  try {
    await api(path, { scope: id, method, body });
    if (board?.id !== id || account?.uuid !== uuid) return;
    onSuccess();
    await refreshBoard();
  } catch (error) {
    if (board?.id !== id || account?.uuid !== uuid) return;
    if (error.status === 401) signIn("Sign in again to open your boards.");
    else {
      if ([403, 409].includes(error.status)) {
        $("detail").close();
        await loadContext();
      }
      notice(error.message || "The change could not be saved.");
    }
  } finally {
    busy = false;
    updateComposer();
    if (loaded) renderList();
  }
}

function mutate(item, action, body) {
  const base = "/api/dopes/" + item.id;
  return write(action === "edit" ? base : base + "/" + action, action === "edit" ? "PUT" : "POST", body, () => {
    $("detail").close();
    if (action === "complete") notice("Marked done.");
  });
}

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("description").value.trim();
  if (!value) return;
  write("/api/dopes", "POST", { description: value }, () => {
    if ($("description").value.trim() === value) $("description").value = "";
    drafts.delete(board.id); filter = "active"; $("search").value = ""; $("description").focus();
  });
});
$("description").addEventListener("input", updateComposer);
$("description").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault(); $("composer").requestSubmit();
  }
});
$("shortcut").textContent = (/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl") + " ↵";
$("board-select").addEventListener("change", (event) => selectBoard(boards.find((b) => b.id === event.target.value)));
$("search").addEventListener("input", renderList);
$("retry").addEventListener("click", () => { notice(); loadContext(); });
$("dismiss").addEventListener("click", () => notice());
$("logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); location.replace("/"); }
  catch (error) { notice(error.message); }
});
$("detail").addEventListener("click", (event) => { if (event.target === $("detail")) $("detail").close(); });
document.addEventListener("click", (event) => {
  if (!$("account-menu").contains(event.target)) $("account-menu").open = false;
});
function syncVisible() {
  if (document.visibilityState === "visible" && account && !busy && !$("detail").open) loadContext();
}
window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
document.addEventListener("visibilitychange", syncVisible);
setInterval(syncVisible, 45000);
const signinStatus = new URLSearchParams(location.search).get("signin");
const signinMessage = {
  cancelled: "Sign-in was cancelled.", expired: "This sign-in expired. Try again.",
  failed: "Silicon Accounts could not finish signing you in. Try again."
}[signinStatus];
if (signinStatus) history.replaceState(null, "", "/");
loadContext().then(() => { if (account && signinMessage) notice(signinMessage); });
