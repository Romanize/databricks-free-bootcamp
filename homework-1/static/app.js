// Vanilla front-end for the Lakebase support desk. Everything it shows comes
// from the Flask API in app.py, which reads/writes Lakebase.

let meta = { users: [], statuses: [], priorities: [], categories: [] };
let selectedTicketId = null;

const $ = (id) => document.getElementById(id);
const toast = $("toast");

let toastTimer = null;

function showToast(message, kind) {
  toast.textContent = message;
  toast.className = "toast " + kind;
  // Success messages fade out on their own; errors stay until the next action.
  clearTimeout(toastTimer);
  if (kind === "success") {
    toastTimer = setTimeout(clearToast, 4000);
  }
}

function clearToast() {
  toast.className = "toast hidden";
}

async function api(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({ error: resp.statusText }));
  if (!resp.ok) throw new Error(data.error || "Request failed");
  return data;
}

function fillSelect(select, values, labelFn, valueFn) {
  select.innerHTML = "";
  values.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = valueFn ? valueFn(v) : v;
    opt.textContent = labelFn ? labelFn(v) : v;
    select.appendChild(opt);
  });
}

function label(value) {
  return value.replace(/_/g, " ");
}

function formatDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

// ------------------------------------------------------------------ load

async function loadMeta() {
  meta = await api("/api/meta");

  const userLabel = (u) => u.username;
  const userValue = (u) => u.user_id;
  fillSelect($("new-user"), meta.users, userLabel, userValue);
  fillSelect($("message-author"), meta.users, userLabel, userValue);

  fillSelect($("new-priority"), meta.priorities, label);
  $("new-priority").value = "medium";
  fillSelect($("new-category"), meta.categories, label);
  fillSelect($("detail-status"), meta.statuses, label);

  fillSelect($("filter-status"), ["all"].concat(meta.statuses), label);
  fillSelect($("filter-priority"), ["all"].concat(meta.priorities), label);
}

async function loadStats() {
  const stats = await api("/api/stats");
  const cells = [["total", stats.total]].concat(
    meta.statuses.map((s) => [s, stats.by_status[s] || 0])
  );
  $("stats").innerHTML = cells
    .map(
      ([name, count]) =>
        `<div class="stat"><div class="value">${count}</div>` +
        `<div class="label">${label(name)}</div></div>`
    )
    .join("");
}

async function loadTickets() {
  const params = new URLSearchParams({
    status: $("filter-status").value,
    priority: $("filter-priority").value,
  });
  const tickets = await api("/api/tickets?" + params.toString());
  const list = $("ticket-list");

  if (!tickets.length) {
    list.innerHTML = '<li class="empty" style="border:none;cursor:default;">No tickets match this filter.</li>';
    return;
  }

  list.innerHTML = tickets
    .map(
      (t) => `
      <li data-id="${t.ticket_id}" class="${t.ticket_id === selectedTicketId ? "active" : ""}">
        <div class="ticket-title">#${t.ticket_id} ${escapeHtml(t.title)}</div>
        <div class="ticket-meta">
          <span class="badge ${t.status}">${label(t.status)}</span>
          <span class="badge ${t.priority}">${t.priority}</span>
          <span>${escapeHtml(t.category)}</span>
          <span>by ${escapeHtml(t.created_by_username)}</span>
          <span>${t.message_count} msg</span>
          <span>${formatDate(t.created_at)}</span>
        </div>
      </li>`
    )
    .join("");

  list.querySelectorAll("li[data-id]").forEach((li) => {
    li.addEventListener("click", () => selectTicket(Number(li.dataset.id)));
  });
}

async function selectTicket(ticketId) {
  selectedTicketId = ticketId;
  const tickets = await api("/api/tickets");
  const ticket = tickets.find((t) => t.ticket_id === ticketId);
  if (!ticket) {
    clearDetail();
    return;
  }

  $("detail-title").textContent = `#${ticket.ticket_id} ${ticket.title}`;
  $("detail-meta").innerHTML =
    `<span class="badge ${ticket.priority}">${ticket.priority}</span>` +
    `<span>${escapeHtml(ticket.category)}</span>` +
    `<span>opened by ${escapeHtml(ticket.created_by_username)}</span>` +
    `<span>${formatDate(ticket.created_at)}</span>`;
  $("detail-status").value = ticket.status;
  $("detail-body").classList.remove("hidden");
  $("detail-empty").classList.add("hidden");
  resetDeleteConfirm();

  await loadMessages(ticketId);
  await loadTickets();
}

function clearDetail() {
  selectedTicketId = null;
  $("detail-title").textContent = "Select a ticket";
  $("detail-body").classList.add("hidden");
  $("detail-empty").classList.remove("hidden");
}

async function loadMessages(ticketId) {
  const messages = await api(`/api/tickets/${ticketId}/messages`);
  $("message-list").innerHTML = messages.length
    ? messages
        .map(
          (m) => `
        <li>
          <span class="author">${escapeHtml(m.author_username)}</span>
          <span class="when">${formatDate(m.created_at)}</span>
          <div class="text">${escapeHtml(m.message_text)}</div>
        </li>`
        )
        .join("")
    : '<li class="empty">No messages yet.</li>';
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// ---------------------------------------------------------------- events

$("filter-status").addEventListener("change", () => loadTickets().catch(fail));
$("filter-priority").addEventListener("change", () => loadTickets().catch(fail));

$("new-ticket-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearToast();
  const title = $("new-title").value.trim();
  if (!title) {
    showToast("Please enter a title for the ticket.", "error");
    return;
  }
  try {
    const ticket = await api("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title,
        created_by: $("new-user").value,
        priority: $("new-priority").value,
        category: $("new-category").value,
        message_text: $("new-message").value,
      }),
    });
    $("new-title").value = "";
    $("new-message").value = "";
    showToast(`Ticket #${ticket.ticket_id} created.`, "success");
    await refresh();
    await selectTicket(ticket.ticket_id);
  } catch (err) {
    fail(err);
  }
});

$("detail-status").addEventListener("change", async () => {
  if (!selectedTicketId) return;
  clearToast();
  try {
    await api(`/api/tickets/${selectedTicketId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: $("detail-status").value }),
    });
    showToast("Status updated.", "success");
    await refresh();
  } catch (err) {
    fail(err);
  }
});

$("new-message-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearToast();
  const text = $("message-text").value.trim();
  if (!text) {
    showToast("Please type a message before submitting.", "error");
    return;
  }
  try {
    await api(`/api/tickets/${selectedTicketId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_text: text, author: $("message-author").value }),
    });
    $("message-text").value = "";
    showToast("Message added.", "success");
    await loadMessages(selectedTicketId);
    await refresh();
  } catch (err) {
    fail(err);
  }
});

// Deleting takes two clicks: "Delete ticket" reveals an explicit confirm step.
$("delete-btn").addEventListener("click", () => {
  if (!selectedTicketId) return;
  showToast(
    `Deleting ticket #${selectedTicketId} also deletes all of its messages. This cannot be undone.`,
    "error"
  );
  $("delete-btn").classList.add("hidden");
  $("delete-confirm").classList.remove("hidden");
});

$("delete-no").addEventListener("click", () => {
  resetDeleteConfirm();
  clearToast();
});

$("delete-yes").addEventListener("click", async () => {
  if (!selectedTicketId) return;
  const deletedId = selectedTicketId;
  clearToast();
  try {
    await api(`/api/tickets/${deletedId}`, { method: "DELETE" });
    showToast(`Ticket #${deletedId} deleted.`, "success");
    clearDetail();
    await refresh();
  } catch (err) {
    fail(err);
  }
});

function resetDeleteConfirm() {
  $("delete-btn").classList.remove("hidden");
  $("delete-confirm").classList.add("hidden");
}

function fail(err) {
  showToast(err.message || String(err), "error");
}

async function refresh() {
  await loadStats();
  await loadTickets();
}

(async function start() {
  try {
    await loadMeta();
    await refresh();
  } catch (err) {
    fail("Could not load data from Lakebase: " + (err.message || err));
  }
})();
