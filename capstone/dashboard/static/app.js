/*
 * Tracing dashboard - vanilla JS, no framework, no build step.
 *
 * Same shape as homework 3's dashboard, plus the session table that the
 * session_id column made possible.
 */

let timer = null;

async function api(path) {
  const response = await fetch(path);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function showError(message) {
  const box = document.getElementById("error");
  box.textContent = message;
  box.hidden = !message;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

const when = (value) => (value ? String(value).replace("T", " ").slice(0, 19) : "-");
const num = (value) => (value === null || value === undefined ? "-" : Number(value).toLocaleString());

/** Short, stable label for a session id - the full value is in the title. */
const shortSession = (id) => (id ? `${String(id).slice(0, 8)}…` : "(none)");

function duration(startedAt, endedAt) {
  if (!startedAt || !endedAt) return "-";
  const seconds = (new Date(endedAt) - new Date(startedAt)) / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${(seconds / 60).toFixed(1)}m`;
}

async function loadStats() {
  const stats = await api("/api/stats");
  document.getElementById("stats").innerHTML = [
    ["Total calls", num(stats.total_calls)],
    ["Last 24h", num(stats.last_24h)],
    ["Conversations", num(stats.sessions)],
    ["Errors", num(stats.errors)],
    ["No data", num(stats.no_data)],
    ["Avg ms", num(stats.avg_duration_ms)],
  ]
    .map(([label, value]) => `<div class="tile"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");

  document.getElementById("by-tool").innerHTML = (stats.by_tool || []).length
    ? stats.by_tool
        .map(
          (row) => `<tr>
            <td><code>${escapeHtml(row.tool_name)}</code></td>
            <td class="num">${num(row.calls)}</td>
            <td class="num">${num(row.errors)}</td>
            <td class="num">${num(row.no_data)}</td>
            <td class="num">${num(row.avg_duration_ms)}</td>
          </tr>`,
        )
        .join("")
    : '<tr><td colspan="5" class="muted">No calls yet.</td></tr>';

  document.getElementById("by-session").innerHTML = (stats.by_session || []).length
    ? stats.by_session
        .map(
          (row) => `<tr>
            <td><code title="${escapeHtml(row.session_id)}">${escapeHtml(shortSession(row.session_id))}</code></td>
            <td class="num">${num(row.calls)}</td>
            <td class="num">${num(row.distinct_tools)}</td>
            <td class="num">${num(row.errors)}</td>
            <td>${when(row.started_at)}</td>
            <td class="num">${duration(row.started_at, row.ended_at)}</td>
          </tr>`,
        )
        .join("")
    : '<tr><td colspan="6" class="muted">No sessions recorded. FastMCP supplies the id; direct calls have none.</td></tr>';
}

async function loadCalls() {
  const params = new URLSearchParams({
    limit: document.getElementById("limit").value,
  });
  const tool = document.getElementById("tool").value;
  const status = document.getElementById("status").value;
  if (tool) params.set("tool", tool);
  if (status) params.set("status", status);

  const rows = await api(`/api/calls?${params}`);
  document.getElementById("rows").innerHTML = rows.length
    ? rows
        .map(
          (row) => `<tr>
            <td>${when(row.called_at)}</td>
            <td><code>${escapeHtml(row.tool_name)}</code>
                <div class="muted" title="${escapeHtml(JSON.stringify(row.arguments))}">${escapeHtml(
                  JSON.stringify(row.arguments).slice(0, 60),
                )}</div></td>
            <td>${escapeHtml(row.symbol || "-")}</td>
            <td><span class="badge ${escapeHtml(row.status)}">${escapeHtml(row.status)}</span></td>
            <td>${escapeHtml(row.summary || row.error_message || "-")}</td>
            <td class="num">${num(row.duration_ms)}</td>
          </tr>`,
        )
        .join("")
    : '<tr><td colspan="6" class="muted">No calls match this filter.</td></tr>';
}

async function refresh() {
  try {
    await Promise.all([loadStats(), loadCalls()]);
    showError("");
    document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    showError(err.message);
  }
}

document.getElementById("refresh").addEventListener("click", refresh);
["tool", "status", "limit"].forEach((id) =>
  document.getElementById(id).addEventListener("change", refresh),
);
document.getElementById("auto").addEventListener("change", (event) => {
  clearInterval(timer);
  if (event.target.checked) timer = setInterval(refresh, 10000);
});

refresh();
