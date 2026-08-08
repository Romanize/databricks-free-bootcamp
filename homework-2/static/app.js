/* Vanilla JS front end for the weather vector-search app. */

const $ = (id) => document.getElementById(id);

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 5000);
}

async function api(url, options = {}) {
  const resp = await fetch(url, options);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

async function postJSON(url, body) {
  return api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function busy(button, isBusy, label) {
  button.disabled = isBusy;
  if (isBusy) {
    button.dataset.label = button.textContent;
    button.textContent = label;
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
}

async function loadStats() {
  const s = await api("/weather/stats");
  const byType = s.by_source_type || {};
  const cards = [
    ["Documents", s.documents],
    ["Alerts", byType.alert || 0],
    ["Forecasts", byType.forecast || 0],
    ["Chunks", s.chunks],
    ["Pending", s.pending],
    ["Locations", s.locations],
  ];
  $("stats").innerHTML = cards
    .map(([label, value]) => `<div class="stat"><div class="value">${value}</div><div>${label}</div></div>`)
    .join("");
}

async function loadDocuments() {
  const docs = await api("/weather/documents?limit=25");
  if (!docs.length) {
    $("doc-list").innerHTML = '<li class="empty">Nothing synced yet.</li>';
    return;
  }
  $("doc-list").innerHTML = docs
    .map(
      (d) => `<li>
        <div class="doc-head">
          <span class="badge ${d.source_type}">${d.source_type}</span>
          <strong>${escapeHtml(d.headline || d.event || d.id)}</strong>
        </div>
        <div class="meta">${escapeHtml(d.location)} &middot; ${d.chunk_count} chunk(s)
          &middot; ${d.effective_at ? new Date(d.effective_at).toLocaleString() : "no date"}</div>
      </li>`
    )
    .join("");
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

async function refresh() {
  await Promise.all([loadStats(), loadDocuments()]);
}

$("sync-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const locations = $("locations").value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (!locations.length) return toast("Enter at least one location.", true);

  busy($("sync-btn"), true, "Syncing...");
  try {
    const result = await postJSON("/weather/sync", {
      locations,
      limit: Number($("sync-limit").value),
    });
    toast(`Synced ${result.synced} documents (${result.alerts} alerts, ${result.forecasts} forecasts).`);
    if (Object.keys(result.errors || {}).length) {
      toast(`Some locations failed: ${Object.keys(result.errors).join(", ")}`, true);
    }
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy($("sync-btn"), false);
  }
});

$("embed-btn").addEventListener("click", async () => {
  busy($("embed-btn"), true, "Embedding...");
  try {
    const result = await postJSON("/weather/embed", { limit: 500 });
    toast(`Embedded ${result.chunks} chunks from ${result.documents} documents.`);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy($("embed-btn"), false);
  }
});

$("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  busy($("search-btn"), true, "Searching...");
  try {
    const result = await postJSON("/weather/search", {
      query: $("query").value,
      top_k: Number($("top-k").value),
      source_type: $("source-type").value,
    });
    renderResults(result);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy($("search-btn"), false);
  }
});

function renderResults(result) {
  if (result.message) return ($("results").innerHTML = `<li class="empty">${result.message}</li>`);
  if (!result.results.length) return ($("results").innerHTML = '<li class="empty">No matches.</li>');

  $("results").innerHTML = result.results
    .map(
      (r) => `<li>
        <div class="doc-head">
          <span class="score">${r.similarity.toFixed(3)}</span>
          <span class="badge ${r.source_type}">${r.source_type}</span>
          <strong>${escapeHtml(r.headline || r.event)}</strong>
        </div>
        <div class="meta">${escapeHtml(r.location)}</div>
        <p class="chunk">${escapeHtml(r.chunk_text)}</p>
      </li>`
    )
    .join("");
}

refresh().catch((err) => toast(err.message, true));
