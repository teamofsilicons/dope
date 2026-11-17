"use strict";

const rootDialog = $("workspace-dialog");
const work = $("workspace-body");
let panelVersion = 0;
function panel(title, markup) {
  ++panelVersion;
  $("workspace-title").textContent = title;
  work.innerHTML = markup;
  if (!rootDialog.open) rootDialog.showModal();
  return panelVersion;
}
function activePanel(version) { return rootDialog.open && version === panelVersion; }
$("workspace-close").onclick = () => rootDialog.close();
rootDialog.addEventListener("close", () => { ++panelVersion; });
const operation = () => ({"Idempotency-Key": crypto.randomUUID()});
const jsonOptions = (method, body, headers = {}) => ({method, headers, body: JSON.stringify(body)});

function photoURL(value) {
  if (!value) return "";
  if (/^\/api\/contacts\/photos\/[\da-f-]+$/i.test(value)) return value;
  try {
    const u = new URL(value);
    return u.protocol === "https:" || (u.protocol === "http:" && u.origin === location.origin) ? u.href : "";
  } catch { return ""; }
}
function avatarHtml(p) {
  const initials = (p.display_name || p.id || "?").trim().split(/\s+/).slice(0, 2).map(w => Array.from(w)[0]).join("").toUpperCase();
  const photo = photoURL(p.pfp_url), fallback = photoURL(p.fallback_pfp_url);
  return `<span class="avatar"><span>${escapeHtml(initials)}</span>${photo ? `<img alt="" referrerpolicy="no-referrer" src="${escapeHtml(photo)}" data-fallback="${escapeHtml(fallback)}">` : ""}</span>`;
}
document.addEventListener("error", e => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !img.closest(".avatar")) return;
  const next = img.dataset.fallback;
  delete img.dataset.fallback;
  if (next && img.getAttribute("src") !== next) img.src = next; else img.remove();
}, true);
function personHtml(p) {
  return `<span class="person-line">${avatarHtml(p)}<span class="person-text"><strong>${escapeHtml(p.display_name)}</strong><small>${escapeHtml(p.id)}${p.trust ? " · Trust " + p.trust : ""}</small></span></span>`;
}

async function peopleSearch(q, {membersOnly = false, board = true, historical = false, fileSenders = false} = {}) {
  const params = new URLSearchParams({q, limit: "20"});
  if (board && state.board) params.set("board_id", state.board.id);
  if (membersOnly) params.set("members_only", "true");
  if (historical) params.set("historical", "true");
  if (fileSenders) params.set("file_senders", "true");
  const cache = state.peopleCache;
  const key = params.toString(), entry = cache.get(key);
  if (entry && Date.now() - entry.at < 15000) return entry.items;
  const items = (await api("/api/people?" + params)).items;
  if (cache.size > 80) cache.clear();
  cache.set(key, {items, at:Date.now()});
  return items;
}

// All recipient fields use the same server-ranked list, keyboard behavior and verified selection.
function attachPicker(input, {membersOnly = false, board = true, historical = false, fileSenders = false, onPick, allowDirectory = !membersOnly} = {}) {
  const list = document.createElement("div"), preview = document.createElement("div");
  list.className = "person-options"; list.id = "people-" + crypto.randomUUID(); list.setAttribute("role", "listbox");
  preview.className = "person-preview"; preview.setAttribute("aria-live", "polite");
  input.setAttribute("role", "combobox"); input.setAttribute("aria-controls", list.id);
  input.setAttribute("aria-autocomplete", "list"); input.setAttribute("aria-expanded", "false");
  input.after(preview, list);
  let request = 0, timer, items = [], active = -1;
  let recent = [];
  const result = {person: null, reset() { ++request; result.person = null; close(); preview.replaceChildren(); }, choose};
  function close() { list.replaceChildren(); input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); active = -1; }
  function choose(person) {
    ++request; result.person = person;
    input.value = membersOnly ? person.display_name : person.id;
    preview.innerHTML = personHtml(person);
    close(); onPick?.(person);
  }
  function show(values) {
    items = values; list.replaceChildren(); active = -1;
    input.setAttribute("aria-expanded", String(values.length > 0));
    for (const [i,p] of values.entries()) {
      const b = document.createElement("button");
      b.type = "button"; b.setAttribute("role", "option"); b.id = list.id + "-" + i; b.tabIndex = -1;
      b.innerHTML = personHtml(p);
      b.onmousedown = e => e.preventDefault();
      b.onclick = () => choose(p); list.append(b);
    }
  }
  async function search(autoSelect) {
    const id = ++request, q = input.value.trim();
    if (result.person && input.value === (membersOnly ? result.person.display_name : result.person.id)) return;
    const exact = /^(?:(?:c|si):[\w.-]+|[a-f0-9-]{36})$/i.test(q);
    if (q && recent.length) {
      const index = new Fuse(recent, {keys:["display_name","id"], threshold:.45, ignoreLocation:true, shouldSort:false});
      show(index.search(q).map(hit => hit.item));
    }
    try {
      let values = await peopleSearch(q, {membersOnly, board, historical, fileSenders});
      if (id !== request || !input.isConnected) return;
      let found = values.find(p => p.id.toLowerCase() === q.toLowerCase() || p.uuid === q);
      if (!found && exact && allowDirectory && (!board || state.board.kind === "personal")) {
        try { found = await api("/api/people/resolve?" + new URLSearchParams({id: q})); }
        catch (err) { if (err.status !== 404 && err.status !== 422) throw err; }
        if (found && found.uuid !== state.account.uuid && !values.some(p => p.uuid === found.uuid)) values.push(found);
      }
      if (id !== request || !input.isConnected) return;
      if (!q || !recent.length) recent = values;
      if (autoSelect && exact && found && found.uuid !== state.account.uuid) choose(found);
      else show(values);
    } catch (err) {
      if (id === request && input.isConnected && err.name !== "AbortError") { close(); preview.textContent = err.message; }
    }
  }
  input.addEventListener("focus", () => search(false));
  input.addEventListener("input", () => {
    result.reset(); onPick?.(null); clearTimeout(timer);
    timer = setTimeout(() => search(true), 100);
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Escape" && list.childElementCount) { e.preventDefault(); close(); return; }
    if (!list.childElementCount) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      Array.from(list.children).forEach((b,i) => b.setAttribute("aria-selected", String(i === active)));
      input.setAttribute("aria-activedescendant", list.children[active].id); list.children[active].scrollIntoView({block: "nearest"});
    } else if (e.key === "Enter") { e.preventDefault(); choose(items[Math.max(0, active)]); }
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
  return result;
}

function renderError(target, error) {
  if (target?.isConnected && error.name !== "AbortError") target.textContent = error.message;
}

async function updateBoards() {
  const result = await api("/api/boards");
  if (result.account.uuid !== state.account?.uuid) { endSession("The signed-in account changed."); return; }
  state.boards = result.boards;
  if (!state.boards.some(b => b.id === state.board?.id)) { await selectBoard(state.boards[0]); return; }
  state.board = state.boards.find(b => b.id === state.board.id);
  if (state.user) showApp();
  renderBoards();
}

async function refreshInboxCount() {
  try {
    if (state.boards.some(b => b.shared_by) && !state.sharedSenders) {
      const data = await api("/api/invitations");
      state.sharedSenders = new Map(data.items.filter(i => i.state === "accepted").map(i => [i.board, i.sender]));
      $("inbox-count").textContent = data.items.filter(i => i.state === "pending").length || "";
      renderBoards();
    } else {
      $("inbox-count").textContent = (await api("/api/invitations/count")).pending || "";
    }
  } catch {}
}

function createBoardDialog() {
  const roots = state.boards.filter(b => b.can_manage && !b.scope_id && !b.pending);
  const version = panel("Create board", `<form class="work-form"><label>Board name<input name="name" required maxlength="100" autocomplete="off"></label>
    <label>Location<select name="scope">${roots.map(b => `<option value="${escapeHtml(b.id)}">${escapeHtml(b.kind === "team" ? b.organization + " / " + b.name : b.name)}</option>`).join("")}</select></label>
    <p class="work-status" role="alert"></p><button>Create board</button></form>`);
  const form = work.querySelector("form"), status = form.querySelector(".work-status");
  form.elements.scope.value = roots.find(b => b.id === (state.board.scope_id || state.board.id))?.id || roots[0].id;
  form.onsubmit = async e => {
    e.preventDefault();
    const scope = form.elements.scope.value, name = form.elements.name.value.trim();
    if (!name || form.querySelector("button").disabled) return;
    const temp = {...roots.find(b => b.id === scope), scope_id: scope, id: "pending:" + crypto.randomUUID(), name, pending: true};
    state.boards.push(temp); renderBoards();
    form.querySelector("button").disabled = true; status.textContent = "Creating board…";
    try {
      const data = {scope_id: scope, name};
      const board = await api("/api/boards", jsonOptions("POST", data, retryHeaders("board-create", data)));
      draftRemove("operation:board-create");
      state.boards = state.boards.filter(b => b !== temp); state.boards.push(board); renderBoards();
      if (activePanel(version)) { rootDialog.close(); await selectBoard(board); }
    } catch (err) {
      state.boards = state.boards.filter(b => b !== temp); renderBoards(); renderError(status, err);
      form.querySelector("button").disabled = false;
    }
  };
}

function manageInvitations() {
  const board = state.board;
  const version = panel("Invite to " + board.name, `<form class="work-form"><label>Carbon or Silicon<input name="peer" autocomplete="off" placeholder="Name, c:… or si:…"></label>
    <button disabled>Send invitation</button><p class="work-status" role="status"></p></form><div class="work-list"></div>`);
  const form = work.querySelector("form"), status = form.querySelector(".work-status"), list = work.querySelector(".work-list");
  const submit = form.querySelector("button");
  const picker = attachPicker(form.elements.peer, {onPick: p => { submit.disabled = !p; }});
  const path = "/api/boards/" + encodeURIComponent(board.id) + "/invitations";
  let sent = [], pending = null, rev = 0;
  function draw() {
    if (!activePanel(version)) return;
    list.innerHTML = (pending ? [pending, ...sent] : sent).map(i =>
      `<div class="work-row"><div class="grow">${personHtml(i.recipient)}</div><small>${escapeHtml(i.state)}</small>${i.id ? `<button type="button" class="secondary" data-revoke="${escapeHtml(i.id)}">Revoke</button>` : ""}</div>`).join("");
    for (const b of list.querySelectorAll("[data-revoke]")) b.onclick = async () => {
      b.disabled = true;
      try { await api("/api/invitations/" + b.dataset.revoke + "/revoke", {method:"POST"}); sent = sent.filter(i => i.id !== b.dataset.revoke); ++rev; draw(); }
      catch (err) { b.disabled = false; renderError(status, err); }
    };
  }
  const started = rev;
  api(path).then(data => { if (started === rev) sent = data.items; else sent.push(...data.items.filter(i => !sent.some(s => s.id === i.id))); draw(); }).catch(e => renderError(status, e));
  form.onsubmit = async e => {
    e.preventDefault();
    if (!picker.person || pending) return;
    const person = picker.person;
    pending = {recipient: person, state: "Sending…"}; draw(); submit.disabled = true;
    try {
      const invite = await api(path, jsonOptions("POST", {recipient: person.uuid}, operation()));
      state.peopleCache.clear();
      ++rev; sent = [invite, ...sent.filter(i => i.id !== invite.id)];
      status.textContent = "Invitation sent."; picker.reset(); form.elements.peer.value = "";
    } catch (err) { renderError(status, err); }
    finally { pending = null; submit.disabled = !picker.person; draw(); }
  };
}

async function inboxDialog() {
  const version = panel("Invitations", '<p class="work-status">Loading invitations…</p><div class="work-list"></div>');
  const status = work.querySelector(".work-status"), list = work.querySelector(".work-list");
  try {
    const {items} = await api("/api/invitations");
    if (!activePanel(version)) return;
    status.textContent = items.length ? "" : "No board invitations.";
    for (const i of items) {
      const row = document.createElement("div"); row.className = "work-row";
      row.innerHTML = `<div class="grow"><strong>${escapeHtml(i.board_name)}</strong>${personHtml(i.sender)}</div>`;
      list.append(row);
      if (i.state === "accepted") { const b = rowButton(row, "Open board"); b.onclick = () => { rootDialog.close(); selectBoard(state.boards.find(s => s.id === i.board)); }; continue; }
      for (const action of ["accept","decline"]) {
        const b = rowButton(row, action === "accept" ? "Accept" : "Decline");
        b.onclick = async () => {
          row.hidden = true; status.textContent = action === "accept" ? "Accepting…" : "Declining…";
          try {
            await api("/api/invitations/" + i.id + "/" + action, {method:"POST"});
            await updateBoards(); refreshInboxCount();
            if (activePanel(version)) await inboxDialog();
          } catch (err) { row.hidden = false; renderError(status, err); }
        };
      }
    }
  } catch (err) { renderError(status, err); }
}

function rowButton(parent, label) {
  const b = document.createElement("button"); b.type = "button"; b.className = "secondary"; b.textContent = label; parent.append(b);
  return b;
}

async function contactsDialog() {
  const version = panel("Contacts", '<p class="work-status">Loading Silicon Contacts…</p>');
  let status = work.querySelector(".work-status");
  try {
    const data = await api("/api/contacts");
    if (!activePanel(version)) return;
    if (!data.connected) {
      status.textContent = "Connect Silicon Contacts to use your saved names, photos and trust, and to add or update contacts.";
      const b = rowButton(work, "Connect Silicon Contacts");
      b.onclick = async () => {
        b.disabled = true;
        try { await api("/api/contacts/connection", jsonOptions("PUT", {enabled:true})); if (activePanel(version)) contactsDialog(); }
        catch (e) { b.disabled = false; renderError(status, e); }
      };
      return;
    }
    work.innerHTML = `<form class="work-form"><label>Carbon or Silicon<input name="peer" autocomplete="off" placeholder="Name, c:… or si:…"></label>
      <label>Saved name<input name="alias" required maxlength="200"></label>
      <label>Trust (1–255)<input name="trust" type="number" min="1" max="255" required value="1"></label>
      <button disabled>Save contact</button></form><p class="work-status" role="status"></p>
      <div class="work-list"></div>`;
    status = work.querySelector(".work-status");
    const form = work.querySelector("form"), save = form.querySelector("button"), list = work.querySelector(".work-list");
    const picker = attachPicker(form.elements.peer, {board:false, onPick: p => {
      save.disabled = !p;
      form.elements.alias.value = p?.display_name || "";
      form.elements.trust.value = p?.trust || 1;
    }});
    for (const p of data.items) {
      const b = rowButton(list, ""); b.innerHTML = personHtml(p); b.onclick = () => picker.choose(p);
    }
    form.onsubmit = async e => {
      e.preventDefault(); if (!picker.person || save.disabled) return;
      save.disabled = true; status.textContent = "Saving contact…";
      try {
        await api("/api/contacts", jsonOptions("PUT",{id:picker.person.uuid, name:form.elements.alias.value, trust:Number(form.elements.trust.value)}));
        state.peopleCache.clear(); state.sharedSenders = null; refreshInboxCount();
        if (activePanel(version)) { await contactsDialog(); toast("Contact saved"); }
      } catch (err) { renderError(status, err); save.disabled = false; }
    };
    const disconnect = rowButton(work, "Disconnect Silicon Contacts");
    disconnect.onclick = async () => {
      disconnect.disabled = true;
      try {
        await api("/api/contacts/connection", jsonOptions("PUT",{enabled:false}));
        state.peopleCache.clear(); state.sharedSenders = null; refreshInboxCount();
        if (activePanel(version)) contactsDialog();
      }
      catch (err) { disconnect.disabled = false; renderError(status, err); }
    };
  } catch (err) { renderError(status, err); }
}

function filesDialog() {
  const version = panel("Files", `<div class="work-form"><label>Add a file to ${escapeHtml(state.board.name)}<input id="upload-file" type="file"></label>
    <label>Show<select id="file-scope"><option value="board">This board</option><option value="shared">Shared with me</option></select></label>
    <div class="work-controls"><label>Contacts<select id="file-contacts"><option value="all">Anyone</option><option value="saved">Saved contacts</option><option value="unsaved">Unsaved people</option></select></label>
    <label>Minimum trust<input type="number" id="file-trust" value="0" min="0" max="255"></label></div>
    <label>Shared by<input id="file-sender" placeholder="Search people" autocomplete="off"></label></div>
    <p class="work-status" role="status"></p><div class="work-list"></div>`);
  const status = work.querySelector(".work-status"), list = work.querySelector(".work-list");
  let epoch = 0, offset = 0, sender = null;
  attachPicker($("file-sender"), {board:false, fileSenders:true, onPick:p => { sender = p; refresh(); }});
  async function refresh(append = false) {
    const mine = ++epoch;
    if (!append) { offset = 0; list.replaceChildren(); }
    const params = new URLSearchParams({offset, min_trust: $("file-trust").value || "0", contacts:$("file-contacts").value});
    if ($("file-scope").value === "board") params.set("board_id", state.board.id); else params.set("shared_with_me","true");
    if (sender) params.set("sender", sender.uuid);
    try {
      const data = await api("/api/files?" + params);
      if (mine !== epoch || !activePanel(version)) return;
      status.textContent = data.total ? "" : "No matching files.";
      for (const f of data.items) {
        const row = document.createElement("div"); row.className = "work-row";
        row.innerHTML = `<div class="grow"><a class="file-link" href="/api/files/${encodeURIComponent(f.id)}" download>${escapeHtml(f.name)}</a><small> · ${escapeHtml(f.board_name)}</small>${personHtml(f.uploader)}</div>`;
        list.append(row);
      }
      offset += data.items.length;
      if (offset < data.total) { const more = rowButton(list, "Load more"); more.onclick = () => {more.remove(); refresh(true);}; }
    } catch (err) { if (mine === epoch) renderError(status, err); }
  }
  for (const id of ["file-scope","file-contacts","file-trust"]) $(id).onchange = () => refresh();
  $("upload-file").onchange = async e => {
    const input = e.target, file = input.files[0]; if (!file) return;
    if (file.size > 10 * 1024 * 1024) { status.textContent = "Files can be up to 10 MiB."; input.value = ""; return; }
    status.textContent = "Uploading " + file.name + "…"; input.disabled = true;
    try {
      await api("/api/files?" + new URLSearchParams({name: file.name}), {method:"POST",headers:{"Content-Type":"application/octet-stream",...operation()},body:file});
      if (activePanel(version)) await refresh();
    } catch (err) { renderError(status, err); }
    finally { input.disabled = false; input.value = ""; }
  };
  refresh();
}

$("board-create").onclick = createBoardDialog;
$("board-invite").onclick = manageInvitations;
$("board-inbox").onclick = inboxDialog;
$("board-contacts").onclick = contactsDialog;
$("board-files").onclick = filesDialog;
window.addEventListener("focus", () => { if (state.user) { updateBoards().catch(() => {}); refreshInboxCount(); } });
init();
