const $ = (id) => document.getElementById(id);

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = !message;
}

async function getJSON(url) {
  const resp = await fetch(url);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

function timeAgo(iso) {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function renderStats(stats) {
  const tiles = [
    ["Total calls", stats.total_calls ?? 0],
    ["Last 24h", stats.last_24h ?? 0],
    ["Errors", stats.errors ?? 0],
    ["Locations", stats.locations ?? 0],
    ["Avg latency", stats.avg_duration_ms ? `${stats.avg_duration_ms} ms` : "-"],
  ];
  $("stats").innerHTML = tiles
    .map(([label, value]) => `<div class="tile"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");
}

function renderRows(calls) {
  if (!calls.length) {
    $("rows").innerHTML =
      '<tr><td colspan="6">No tool calls logged yet. Ask the agent a weather question.</td></tr>';
    return;
  }
  $("rows").innerHTML = calls
    .map((call) => {
      const detail = call.status === "error" ? call.error_message : call.summary;
      return `<tr>
        <td title="${call.called_at ?? ""}">${call.called_at ? timeAgo(call.called_at) : "-"}</td>
        <td><code>${call.tool_name}</code></td>
        <td>${call.location ?? "-"}</td>
        <td><span class="badge ${call.status}">${call.status}</span></td>
        <td>${detail ?? "-"}</td>
        <td>${call.duration_ms ?? "-"}</td>
      </tr>`;
    })
    .join("");
}

async function load() {
  const params = new URLSearchParams({ limit: $("limit").value });
  if ($("tool").value) params.set("tool", $("tool").value);
  try {
    const [stats, calls] = await Promise.all([
      getJSON("/api/stats"),
      getJSON(`/api/calls?${params}`),
    ]);
    renderStats(stats);
    renderRows(calls);
    $("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
    showError("");
  } catch (err) {
    showError(`Could not load the tool-call log: ${err.message}`);
  }
}

let timer = null;
$("refresh").addEventListener("click", load);
$("tool").addEventListener("change", load);
$("limit").addEventListener("change", load);
$("auto").addEventListener("change", (event) => {
  clearInterval(timer);
  timer = event.target.checked ? setInterval(load, 10000) : null;
});

load();
