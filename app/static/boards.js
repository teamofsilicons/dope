"use strict";

const routes = ["active", "saved", "review", "completed", "archived"];
const boardSelectionKey = () => "silicon-dopes.board." + state.account.uuid;

function resetBoard() {
  ++state.generation;
  ++state.routeVersion;
  stopAutoRefresh();
  clearInterval(state.settingsTimer);
  clearInterval(state.diagTimer);
  document.querySelectorAll("dialog[open]").forEach(d => d.close());
  for (const id of ["modal-body", "workspace-body", "diagnostics-body", "active-assigned", "dope-list", "progress-chart", "api-key-value"]) $(id).replaceChildren();
  Object.assign(state, {board: null, user: null, booted: false, dopes: [], allDopes: [], categories: [], progress: [], diag: null,
    pending: new Map(), peopleCache: new Map(), sharedSenders: null, nextResetAt: null, lastBoardRefreshAt: 0, boardRefreshInFlight: false, categoriesFetchedAt: 0});
  $("board-shell").hidden = $("diagnostics-page").hidden = true;
  $("settings-open").hidden = $("review-nav").hidden = true;
  $("new-dope").disabled = true;
  $("search").value = "";
  $("toast").hidden = true;
  $("dope-celebration")?.remove();
}

function endSession(message = "") {
  resetBoard();
  state.account = null;
  state.boards = [];
  $("boards-list").replaceChildren();
  $("auth-notice").textContent = message;
  $("auth-notice").hidden = !message;
  showAuth();
}

async function init() {
  try {
    const result = await api("/api/boards");
    state.account = result.account;
    state.boards = result.boards;
    let saved;
    try { saved = sessionStorage.getItem(boardSelectionKey()); } catch {}
    await selectBoard(state.boards.find(b => b.id === saved) || state.boards[0]);
  } catch (err) {
    if (err.name !== "AbortError") endSession(err.status === 401 ? "" : err.message);
  }
}

function recoverBoard() {
  resetBoard();
  init();
}

async function selectBoard(choice) {
  if (!choice || (choice.id === state.board?.id && state.user)) return;
  resetBoard();
  state.board = choice;
  const epoch = state.generation;
  renderBoards();
  setRouteLoading(true);
  try {
    const hash = location.hash.slice(1);
    const route = routes.includes(hash) && (hash !== "review" || choice.can_manage) ? hash : "active";
    const seed = await api("/api/workspace?status=" + route + "&days=" + state.progressDays);
    state.board = seed.board;
    state.user = seed.viewer;
    state.categories = seed.categories;
    state.categoriesFetchedAt = Date.now();
    cacheWrite("categories", seed.categories);
    state.filters = {...DEFAULT_FILTERS, ...draftRead("filters", {})};
    try { sessionStorage.setItem(boardSelectionKey(), choice.id); } catch {}
    $("account-handle").textContent = state.account.handle;
    $("new-dope").disabled = false;
    showApp();
    renderBoards();
    await loadRoute(seed);
    if (epoch !== state.generation) return;
    state.booted = true;
    document.title = choice.name + " · Dopes";
    startAutoRefresh();
    refreshInboxCount();
  } catch (err) {
    if (epoch !== state.generation) return;
    endSession(err.message);
  } finally {
    if (epoch === state.generation) setRouteLoading(false);
  }
}

function renderBoards() {
  const nav = $("boards-list"), select = $("mobile-board");
  nav.replaceChildren(); select.replaceChildren();
  const groups = new Map();
  const roots = state.boards.filter(b => !b.scope_id && !b.shared_by);
  const ordered = roots.flatMap(root => [root, ...state.boards.filter(b => !b.shared_by && b.scope_id === root.id)]);
  ordered.push(...state.boards.filter(b => !ordered.includes(b)));
  for (const b of ordered) {
    const scopeId = b.scope_id || b.id;
    const groupId = b.shared_by ? "shared" : b.kind === "personal" ? "personal" : scopeId.split(":")[1].split("/")[0];
    if (!groups.has(groupId)) groups.set(groupId, {name:b.shared_by ? "Shared with me" : b.organization || "Personal", boards:[]});
    groups.get(groupId).boards.push(b);
  }
  for (const {name:group, boards} of groups.values()) {
    const label = document.createElement("p");
    label.className = "side-heading"; label.textContent = group; nav.append(label);
    const options = document.createElement("optgroup"); options.label = group; select.append(options);
    for (const b of boards) {
      const button = document.createElement("button");
      button.type = "button";
      const icon = b.shared_by ? "share-network" : b.kind === "organization" ? "buildings" : b.kind === "team" ? "users-three" : "lock-simple";
      button.innerHTML = `<i aria-hidden="true" class="ph ph-${icon}"></i><span class="board-name">${escapeHtml(b.name)}</span>`;
      const parent = b.scope_id && !b.shared_by && roots.find(root => root.id === b.scope_id);
      button.classList.toggle("child-board", !!parent);
      button.disabled = !!b.pending;
      const sender = state.sharedSenders?.get(b.id);
      if (sender) {
        button.querySelector("i").outerHTML = avatarHtml(sender);
        const byline = document.createElement("small");
        byline.className = "board-sender"; byline.textContent = sender.display_name;
        button.querySelector(".board-name").append(byline);
      }
      button.setAttribute("aria-label", b.name);
      button.setAttribute("aria-current", String(b.id === state.board?.id));
      button.onclick = () => selectBoard(b);
      nav.append(button);
      const option = new Option(parent ? parent.name + " / " + b.name : b.name, b.id);
      option.disabled = !!b.pending; options.append(option);
    }
  }
  select.value = state.board?.id || "";
  select.onchange = () => selectBoard(state.boards.find(b => b.id === select.value));
  $("board-create").hidden = !state.boards.some(b => b.can_manage && !b.scope_id);
  $("board-invite").hidden = !state.board?.can_manage;
}

function editorData(prefix) {
  const description = sanitizeHtml($(prefix + "-description").innerHTML);
  return {title: $(prefix + "-title").value.trim(), description_html: description, time_text: $(prefix + "-time").value.trim(),
    dependency_ids: selectedDependencyIds(), category_id: selectedCategoryId()};
}

async function hydrateDependencies(savedIds, excludeId = null) {
  const panel = $("dependency-panel");
  try {
    await ensureAllDopes(true);
    if (!panel?.isConnected) return;
    const template = document.createElement("template");
    const ids = new Set([...savedIds, ...selectedDependencyIds()]);
    for (const input of panel.querySelectorAll("[data-dependency-option]:not(:checked)")) ids.delete(Number(input.value));
    template.innerHTML = dependencyPickerHtml([...ids], excludeId);
    panel.querySelector(".dependency-list").replaceChildren(...template.content.querySelector(".dependency-list").childNodes);
    $("dependency-search").dispatchEvent(new Event("input"));
  } catch (err) { if (panel?.isConnected && err.name !== "AbortError") toast(err.message); }
}

function belongsToRoute(item, route) {
  return item.status === route || (route === "completed" && item.status === "review");
}

function visibleDopes(rows, route) {
  const byId = new Map(rows.map(d => [d.id, d]));
  for (const [id, item] of state.pending) {
    if (belongsToRoute(item, route)) byId.set(id, item); else byId.delete(id);
  }
  return [...byId.values()].filter(d => belongsToRoute(d, route));
}

function storeUpdatedDope(updated) {
  function apply(rows, route) {
    const found = rows.some(d => d.id === updated.id);
    if (route !== "all" && !belongsToRoute(updated, route)) return rows.filter(d => d.id !== updated.id);
    return found ? rows.map(d => d.id === updated.id ? updated : d) : [updated, ...rows];
  }
  for (const route of [...routes, "all"]) {
    const cached = cacheRead("dopes:" + route);
    if (cached) cacheWrite("dopes:" + route, apply(cached, route));
  }
  state.dopes = apply(state.dopes, state.route);
  if (state.allDopes.length) state.allDopes = apply(state.allDopes, "all");
}

function retryHeaders(key, data) {
  const fingerprint = JSON.stringify(data);
  const previous = draftRead("operation:" + key);
  if (previous.fingerprint === fingerprint) return {"Idempotency-Key": previous.id};
  const id = crypto.randomUUID();
  draftWrite("operation:" + key, {fingerprint, id});
  return {"Idempotency-Key": id};
}

let temporaryId = 0;
async function changeDope(original, action, data, {draftKey, message, undo} = {}) {
  const at = state.generation, now = new Date().toISOString();
  const item = original ? {...original} : {id: --temporaryId, status: "active", created_at: now, created_by: state.user,
    dependencies: [], versions: [], assignment_history: [], time_minutes: 0};
  if (state.pending.has(item.id)) return;
  if (action === "edit" || action === "create") {
    const plain = document.createElement("template");
    plain.innerHTML = data.description_html;
    const description = plain.content.textContent.trim();
    if (!description && !(data.title && plain.content.querySelector("img"))) return toast("Write a description for the dope.");
    Object.assign(item, {title: data.title || description.slice(0, 180), description_html: data.description_html,
      category: categoryById(data.category_id), time_minutes: minutesFromText(data.time_text) || item.time_minutes});
  }
  const status = {archive: "archived", "save-for-later": "saved", "move-to-active": "active", restore: "active", uncomplete: "active",
    review: "review", "review/approve": "completed", "review/reject": "active", complete: "completed"};
  if (status[action]) item.status = status[action];
  if (action === "assign") item.assigned_to = state.user;
  if (["unassign", "complete", "review", "uncomplete", "save-for-later"].includes(action)) item.assigned_to = null;
  if (action === "complete" || action === "review") Object.assign(item, {completed_by: state.user, completed_at: now});
  if (action === "save-for-later") Object.assign(item, {saved_by: state.user, saved_at: now});
  if (action === "archive") item.archived_at = now;
  const scope = state.pending;
  scope.set(item.id, item);
  const submittedDraft = draftKey && JSON.stringify(draftRead(draftKey));
  $("dope-dialog").close();
  render();
  const path = "/api/dopes" + (original ? "/" + item.id + (action === "edit" ? "" : "/" + action) : "");
  try {
    const headers = action === "create" ? retryHeaders("new-dope", data) : {};
    const actual = await api(path, {method: action === "edit" ? "PUT" : "POST", headers, body: JSON.stringify(data)});
    if (action === "create") draftRemove("operation:new-dope");
    scope.delete(item.id);
    storeUpdatedDope(actual);
    if (draftKey && JSON.stringify(draftRead(draftKey)) === submittedDraft) draftRemove(draftKey);
    render();
    if (action === "complete") celebrateDope();
    toast(message || "Dope updated", undo ? () => changeDope(actual, undo, {}, {message: "Change undone"}) : null);
    loadRoute().catch(err => { if (at === state.generation) toast(err.message); });
  } catch (err) {
    scope.delete(item.id);
    if (at !== state.generation) return;
    render();
    toast(err.message);
  }
}
