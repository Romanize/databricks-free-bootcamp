/*
 * Net Worth Tracker - vanilla JS, no framework.
 *
 * Charts use Chart.js, vendored into static/chart.min.js rather than loaded
 * from a CDN: Databricks Apps serve behind a strict content policy and an
 * external script tag is the kind of thing that works locally and fails once
 * deployed.
 *
 * Every fetch goes through api(), which turns a non-2xx JSON body into a thrown
 * Error carrying the server's message. The Flask side always answers JSON, even
 * for 500s, so this never tries to parse an HTML error page.
 *
 * All values are USD; there is no currency handling anywhere.
 */

const charts = {};
let chatHistory = [];
let draftLines = [];

// Shown on the Chat tab as clickable openers. These are the same prompts the
// agent's system prompt tells it to offer, kept here so a new user sees what
// the assistant is actually for instead of an empty box.
const CHAT_SUGGESTIONS = [
  "Do I have an investment plan? Help me set one up.",
  "Review my investment plan - am I on track?",
  "What are people saying about my holdings?",
  "What is my net worth and how is it split up?",
  "Which of my holdings has the worst news sentiment?",
];

// ------------------------------------------------------------------ plumbing

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function showError(message) {
  const box = document.getElementById("error");
  box.textContent = message;
  box.hidden = !message;
  if (message) window.scrollTo({ top: 0, behavior: "smooth" });
}

function formFields(form) {
  const data = {};
  new FormData(form).forEach((value, key) => {
    data[key] = value;
  });
  form.querySelectorAll('input[type="checkbox"]').forEach((box) => {
    data[box.name] = box.checked;
  });
  return data;
}

const money = (value) =>
  value === null || value === undefined || value === ""
    ? "-"
    : Number(value).toLocaleString(undefined, { style: "currency", currency: "USD" });

const number = (value, digits = 4) =>
  value === null || value === undefined || value === ""
    ? "-"
    : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });

const date = (value) => (value ? String(value).slice(0, 10) : "-");

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/** Replace a chart in place - Chart.js leaks the canvas otherwise. */
function draw(id, config) {
  if (charts[id]) charts[id].destroy();
  const canvas = document.getElementById(id);
  if (!canvas) return;
  charts[id] = new Chart(canvas, config);
}

function emptyRow(tbody, columns, message) {
  document.getElementById(tbody).innerHTML =
    `<tr><td colspan="${columns}" class="muted">${escapeHtml(message)}</td></tr>`;
}

// -------------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    button.classList.add("active");
    document.getElementById(`tab-${button.dataset.tab}`).classList.add("active");
    loadTab(button.dataset.tab);
  });
});

function loadTab(name) {
  const loaders = {
    overview: loadOverview,
    report: loadReportTab,
    holdings: loadHoldings,
    news: loadNews,
    plan: loadPlan,
    trades: loadTrades,
    chat: loadChat,
  };
  (loaders[name] || (() => {}))().catch((err) => showError(err.message));
}

// ----------------------------------------------------------------- overview

async function loadStats() {
  const stats = await api("/api/stats");
  const tiles = [
    ["Holdings", stats.holdings],
    ["Watchlist", stats.watchlist],
    ["Articles", stats.articles],
    ["Chunks", stats.chunks],
    ["Reports", stats.reports],
    ["Pending trades", stats.pending_trades],
  ];
  const flags = [
    ["Massive", stats.massive_configured],
    ["Alpaca", stats.alpaca_configured],
    ["Agent", stats.agent_configured],
  ];
  document.getElementById("stats").innerHTML =
    tiles
      .map(([label, value]) => `<div class="tile"><span>${label}</span><strong>${value ?? 0}</strong></div>`)
      .join("") +
    flags
      .map(
        ([label, on]) =>
          `<div class="tile"><span>${label}</span><strong class="${on ? "ok" : "off"}">${on ? "on" : "off"}</strong></div>`,
      )
      .join("");
}

async function loadOverdue() {
  const status = await api("/api/reports/status");
  const banner = document.getElementById("overdue");
  if (!status.overdue) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.innerHTML = status.has_report
    ? `It has been <strong>${status.days_since} days</strong> since your last report
       (${date(status.last_report_date)}). Time for a new reading &mdash;
       open the <strong>New report</strong> tab.`
    : `No net worth report yet. Add your holdings, then open the
       <strong>New report</strong> tab to record what they are worth.`;
}

async function loadOverview() {
  await Promise.all([
    loadStats(),
    loadOverdue(),
    drawNetworth(),
    drawDistribution(),
    loadReportLines(),
    loadHighlights(),
  ]);
}

function selectedGranularity() {
  const checked = document.querySelector('input[name="granularity"]:checked');
  return checked ? checked.value : "monthly";
}

async function drawNetworth() {
  const data = await api(`/api/charts/networth?granularity=${selectedGranularity()}`);
  draw("chart-networth", {
    type: "line",
    data: {
      labels: data.points.map((p) => date(p.date)),
      datasets: [
        { label: "Total", data: data.points.map((p) => p.total), tension: 0.2 },
        { label: "Invested", data: data.points.map((p) => p.invested), tension: 0.2 },
        { label: "Cash", data: data.points.map((p) => p.cash), tension: 0.2 },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        tooltip: {
          callbacks: {
            // Monthly points are a specific report's numbers, not an average -
            // say which report the point came from.
            afterTitle: (items) => {
              const point = data.points[items[0].dataIndex];
              return point.report_date ? `report ${date(point.report_date)}` : "";
            },
          },
        },
      },
    },
  });
}

document.querySelectorAll('input[name="granularity"]').forEach((radio) =>
  radio.addEventListener("change", () => drawNetworth().catch((e) => showError(e.message))),
);

async function drawDistribution() {
  const data = await api("/api/charts/distribution");
  draw("chart-distribution", {
    type: "doughnut",
    data: {
      labels: data.by_type.map((row) => row.holding_type),
      datasets: [{ data: data.by_type.map((row) => row.value) }],
    },
    options: { responsive: true },
  });

  const holdings = (data.by_holding || []).slice(0, 15);
  draw("chart-holdings", {
    type: "bar",
    data: {
      labels: holdings.map((row) => row.alias),
      datasets: [{ label: "Value", data: holdings.map((row) => row.value) }],
    },
    options: { responsive: true, plugins: { legend: { display: false } } },
  });
}

async function loadReportLines() {
  const data = await api("/api/reports/latest");
  const meta = document.getElementById("report-meta");
  if (!data.report) {
    meta.textContent = "No report yet.";
    emptyRow("report-lines", 7, "Open the New report tab to record your first one.");
    return;
  }
  meta.innerHTML = `Latest report <strong>${date(data.report.report_date)}</strong> &mdash;
                    ${money(data.report.total_value)} across ${data.report.holdings_count} holdings`;
  document.getElementById("report-lines").innerHTML = data.lines
    .map(
      (line) => `<tr>
        <td>${escapeHtml(line.alias)}</td>
        <td>${escapeHtml(line.holding_type)}</td>
        <td>${escapeHtml(line.symbol || "-")}</td>
        <td class="num">${number(line.quantity)}</td>
        <td class="num">${line.price === null ? "-" : money(line.price)}</td>
        <td class="num">${money(line.value)}</td>
        <td class="muted">${escapeHtml(line.price_source || "-")}</td>
      </tr>`,
    )
    .join("");
}

async function loadHighlights() {
  const data = await api("/api/sentiment/highlights");
  const box = document.getElementById("highlights");
  if (!data.symbols || !data.symbols.length) {
    box.innerHTML = `<p class="muted">${escapeHtml(data.message || "No sentiment data yet - the news job has not run.")}</p>`;
    return;
  }
  box.innerHTML = data.symbols
    .map((row) => {
      const score = Number(row.score ?? 0);
      const tone = score > 0.2 ? "pos" : score < -0.2 ? "neg" : "neutral";
      // A score from one or two articles is noise; label it rather than hide it.
      const weak = row.articles < 3 ? ' <span class="muted">(thin)</span>' : "";
      return `<div class="highlight ${tone}">
        <strong>${escapeHtml(row.symbol)}</strong>
        <span class="score">${score.toFixed(2)}</span>
        <span class="muted">${row.articles} articles${weak}</span>
        <span class="muted">+${row.positive} / ~${row.neutral} / -${row.negative}</span>
      </div>`;
    })
    .join("");
}

document.getElementById("revalue").addEventListener("click", async (event) => {
  const box = document.getElementById("live-valuation");
  event.target.disabled = true;
  box.innerHTML = '<p class="muted">Pricing…</p>';
  try {
    const live = await api("/api/reports/live");
    box.innerHTML = `<p><strong>${money(live.total_value)}</strong> right now, using the
      quantities from the ${date(live.based_on_report)} report
      (${live.repriced_holdings} re-priced${
        live.not_repriced.length ? `, ${escapeHtml(live.not_repriced.join(", "))} not` : ""
      }).</p>
      <p class="muted">${escapeHtml(live.note)}</p>`;
    showError("");
  } catch (err) {
    box.innerHTML = "";
    showError(err.message);
  } finally {
    event.target.disabled = false;
  }
});

// -------------------------------------------------------------- new report

async function loadReportTab() {
  const input = document.getElementById("report-date");
  if (!input.value) input.value = new Date().toISOString().slice(0, 10);
  if (!draftLines.length) await loadDraft();
}

async function loadDraft() {
  const reportDate = document.getElementById("report-date").value;
  const meta = document.getElementById("draft-meta");
  meta.textContent = "Loading…";
  try {
    const draft = await api(`/api/reports/draft?report_date=${encodeURIComponent(reportDate)}`);
    draftLines = draft.lines;
    renderDraft(draft);
    showError("");
  } catch (err) {
    draftLines = [];
    emptyRow("draft-rows", 7, err.message);
    meta.textContent = "";
    document.getElementById("submit-report").disabled = true;
    throw err;
  }
}

function renderDraft(draft) {
  const meta = document.getElementById("draft-meta");
  meta.textContent = draft.editing_existing
    ? "Editing the report that already exists for this date."
    : "New report.";

  const warnings = document.getElementById("draft-warnings");
  warnings.hidden = !draft.warnings.length;
  warnings.innerHTML = draft.warnings.map((w) => `<div>${escapeHtml(w)}</div>`).join("");

  document.getElementById("draft-rows").innerHTML = draftLines
    .map((line, index) => {
      const priced = line.holding_type === "ticker" || line.holding_type === "crypto";
      return `<tr data-index="${index}">
        <td>${escapeHtml(line.alias)}<div class="muted">${escapeHtml(line.holding_type)}</div></td>
        <td>${escapeHtml(line.symbol || "-")}</td>
        <td class="num">${
          priced
            ? `<input type="number" step="any" class="cell" data-field="quantity" value="${line.quantity ?? ""}">`
            : "-"
        }</td>
        <td class="num">${
          priced
            ? `<input type="number" step="any" class="cell" data-field="price" value="${line.price ?? ""}">`
            : "-"
        }</td>
        <td class="num"><input type="number" step="any" class="cell" data-field="value" value="${line.value ?? ""}"></td>
        <td class="muted">${escapeHtml(line.price_source || "manual")}
            ${line.quantity_source && line.quantity_source !== "none"
              ? `<div>qty: ${escapeHtml(line.quantity_source)}</div>` : ""}</td>
        <td><input type="text" class="cell wide" data-field="notes" value="${escapeHtml(line.notes || "")}"></td>
      </tr>`;
    })
    .join("");

  document.getElementById("submit-report").disabled = draftLines.length === 0;
  updateDraftTotal();
}

function updateDraftTotal() {
  const total = draftLines.reduce((sum, line) => sum + (Number(line.value) || 0), 0);
  document.getElementById("draft-total").textContent = money(total);
}

// Editing quantity or price re-derives the value, so the two can never be
// inconsistent - but typing directly into value still wins, since that is how
// you record a number the app could not compute.
document.getElementById("draft-rows").addEventListener("input", (event) => {
  const input = event.target;
  if (!input.classList.contains("cell")) return;
  const row = input.closest("tr");
  const line = draftLines[Number(row.dataset.index)];
  const field = input.dataset.field;

  line[field] = input.value === "" ? null : field === "notes" ? input.value : Number(input.value);

  if (field === "quantity" || field === "price") {
    if (line.quantity !== null && line.price !== null) {
      line.value = Math.round(line.quantity * line.price * 100) / 100;
      const valueInput = row.querySelector('[data-field="value"]');
      if (valueInput) valueInput.value = line.value;
    }
    line.price_source = line.price_source?.startsWith("manual") ? line.price_source : "manual (edited)";
  }
  updateDraftTotal();
});

document.getElementById("load-draft").addEventListener("click", () =>
  loadDraft().catch((err) => showError(err.message)),
);
document.getElementById("report-date").addEventListener("change", () =>
  loadDraft().catch((err) => showError(err.message)),
);

document.getElementById("submit-report").addEventListener("click", async (event) => {
  event.target.disabled = true;
  event.target.textContent = "Saving…";
  try {
    const result = await api("/api/reports", {
      method: "POST",
      body: JSON.stringify({
        report_date: document.getElementById("report-date").value,
        lines: draftLines,
      }),
    });
    showError("");
    document.getElementById("draft-meta").textContent =
      `Saved ${result.lines} lines for ${date(result.report_date)} - total ${money(result.report.total_value)}.`;
    await loadOverview();
  } catch (err) {
    showError(err.message);
  } finally {
    event.target.disabled = false;
    event.target.textContent = "Submit report";
  }
});

// ----------------------------------------------------------------- holdings

// Only priced holdings need a symbol; the rest are named accounts.
function syncHoldingForm() {
  const priced = ["ticker", "crypto"].includes(document.getElementById("holding-type").value);
  document.getElementById("holding-symbol").hidden = !priced;
}
document.getElementById("holding-type").addEventListener("change", syncHoldingForm);
syncHoldingForm();

async function loadHoldings() {
  const [holdings, latest] = await Promise.all([api("/api/holdings"), api("/api/reports/latest")]);
  if (!holdings.length) {
    emptyRow("holdings-rows", 7, "No holdings yet. Add one above.");
    return;
  }
  // Holdings carry no values, so show the last reported one for orientation.
  const lastValue = {};
  (latest.lines || []).forEach((line) => {
    lastValue[line.holding_id] = line;
  });

  document.getElementById("holdings-rows").innerHTML = holdings
    .map((h) => {
      const line = lastValue[h.id];
      return `<tr>
        <td>${escapeHtml(h.alias)}</td>
        <td>${escapeHtml(h.holding_type)}</td>
        <td>${escapeHtml(h.symbol || "-")}</td>
        <td>${escapeHtml(h.institution || "-")}</td>
        <td class="num">${line ? money(line.value) : '<span class="muted">never reported</span>'}</td>
        <td class="muted">${line ? date(line.report_date) : "-"}</td>
        <td><button data-delete-holding="${h.id}" type="button">Remove</button></td>
      </tr>`;
    })
    .join("");
}

document.getElementById("holding-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/holdings", { method: "POST", body: JSON.stringify(formFields(event.target)) });
    event.target.reset();
    syncHoldingForm();
    showError("");
    await loadHoldings();
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById("holdings-rows").addEventListener("click", async (event) => {
  const id = event.target.dataset.deleteHolding;
  if (!id) return;
  try {
    await api(`/api/holdings/${id}`, { method: "DELETE" });
    await loadHoldings();
  } catch (err) {
    showError(err.message);
  }
});

// --------------------------------------------------------------------- news

async function loadNews() {
  await Promise.all([loadWatchlist(), loadArticles()]);
}

async function loadWatchlist() {
  const data = await api("/api/watchlist");
  const box = document.getElementById("watchlist-rows");
  box.innerHTML = data.watchlist.length
    ? data.watchlist
        .map(
          (row) =>
            `<span class="chip" title="${escapeHtml(row.reason || "")}">${escapeHtml(row.symbol)}
             <button data-unwatch="${escapeHtml(row.symbol)}" type="button">&times;</button></span>`,
        )
        .join("")
    : '<span class="muted">Nothing on the watchlist.</span>';
  box.insertAdjacentHTML(
    "beforeend",
    `<span class="muted"> tracked: ${escapeHtml(data.tracked_symbols.join(", ") || "none")}</span>`,
  );
}

document.getElementById("watchlist-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/watchlist", { method: "POST", body: JSON.stringify(formFields(event.target)) });
    event.target.reset();
    showError("");
    await loadWatchlist();
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById("watchlist-rows").addEventListener("click", async (event) => {
  const symbol = event.target.dataset.unwatch;
  if (!symbol) return;
  try {
    await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
    await loadWatchlist();
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const box = document.getElementById("search-results");
  box.innerHTML = '<p class="muted">Searching…</p>';
  try {
    const data = await api("/api/news/search", {
      method: "POST",
      body: JSON.stringify(formFields(event.target)),
    });
    if (!data.results || !data.results.length) {
      box.innerHTML = `<p class="muted">${escapeHtml(data.message || "No matches.")}</p>`;
      return;
    }
    box.innerHTML = data.results
      .map(
        (row) => `<article class="result">
          <h4><a href="${escapeHtml(row.article_url || "#")}" target="_blank" rel="noopener">${escapeHtml(row.title)}</a></h4>
          <p class="muted">${escapeHtml(row.publisher || "")} &middot; ${date(row.published_utc)}
             &middot; similarity ${Number(row.similarity).toFixed(3)}
             ${row.sentiment ? `&middot; <span class="chip ${escapeHtml(row.sentiment)}">${escapeHtml(row.sentiment)}</span>` : ""}</p>
          <p>${escapeHtml(row.chunk_text)}</p>
        </article>`,
      )
      .join("");
  } catch (err) {
    box.innerHTML = "";
    showError(err.message);
  }
});

document.getElementById("sentiment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const symbol = formFields(event.target).symbol;
  try {
    const data = await api(`/api/charts/sentiment?symbol=${encodeURIComponent(symbol)}`);
    draw("chart-sentiment", {
      type: "line",
      data: {
        labels: data.points.map((p) => date(p.day)),
        datasets: [
          { label: `${data.symbol} sentiment`, data: data.points.map((p) => p.score), tension: 0.2 },
          { label: "articles", data: data.points.map((p) => p.articles), yAxisID: "y1", type: "bar" },
        ],
      },
      options: {
        responsive: true,
        scales: {
          y: { min: -1, max: 1, title: { display: true, text: "score" } },
          y1: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false } },
        },
      },
    });
    showError(data.points.length ? "" : `No sentiment stored for ${symbol}.`);
  } catch (err) {
    showError(err.message);
  }
});

async function loadArticles() {
  const articles = await api("/api/news?limit=25");
  if (!articles.length) {
    emptyRow("news-rows", 5, "No articles stored yet - run the ingestion job.");
    return;
  }
  document.getElementById("news-rows").innerHTML = articles
    .map(
      (row) => `<tr>
        <td>${date(row.published_utc)}</td>
        <td><a href="${escapeHtml(row.article_url || "#")}" target="_blank" rel="noopener">${escapeHtml(row.title)}</a></td>
        <td>${escapeHtml((row.tickers || []).join(", "))}</td>
        <td>${escapeHtml(row.publisher || "-")}</td>
        <td class="num">${row.chunk_count}</td>
      </tr>`,
    )
    .join("");
}

document.getElementById("sync-news").addEventListener("click", async (event) => {
  const symbol = prompt("Which ticker? (one per request - the free tier is 5 calls/minute)");
  if (!symbol) return;
  event.target.disabled = true;
  event.target.textContent = "Fetching…";
  try {
    const result = await api("/api/news/sync", { method: "POST", body: JSON.stringify({ symbol }) });
    showError(result.note || "");
    await loadArticles();
  } catch (err) {
    showError(err.message);
  } finally {
    event.target.disabled = false;
    event.target.textContent = "Fetch news for one ticker now";
  }
});

document.getElementById("embed-news").addEventListener("click", async (event) => {
  event.target.disabled = true;
  event.target.textContent = "Embedding…";
  try {
    const result = await api("/api/news/embed", { method: "POST", body: "{}" });
    showError(`Embedded ${result.chunks} chunks across ${result.articles} articles.`);
    await Promise.all([loadArticles(), loadStats()]);
  } catch (err) {
    showError(err.message);
  } finally {
    event.target.disabled = false;
    event.target.textContent = "Embed pending articles";
  }
});

// --------------------------------------------------------------------- plan

async function loadPlan() {
  const plans = await api("/api/plans");
  document.getElementById("plan-rows").innerHTML = plans.length
    ? plans
        .map(
          (plan) => `<tr>
            <td>${escapeHtml(plan.name)}</td>
            <td class="num">${money(plan.goal_amount)}</td>
            <td class="num">${(Number(plan.expected_annual_rate) * 100).toFixed(1)}%</td>
            <td class="num">${plan.years}</td>
            <td class="num">${money(plan.monthly_contribution)}</td>
            <td class="muted">${escapeHtml(plan.created_by)}</td>
            <td>${plan.is_active ? "active" : ""}</td>
            <td>
              ${plan.is_active ? "" : `<button data-activate-plan="${plan.id}" type="button">Activate</button>`}
              <button data-delete-plan="${plan.id}" type="button">Delete</button>
            </td>
          </tr>`,
        )
        .join("")
    : '<tr><td colspan="8" class="muted">No plans yet. Add one, or ask the agent to.</td></tr>';

  await drawProjection();
}

async function drawProjection() {
  const data = await api("/api/charts/projection");
  const summary = document.getElementById("projection-summary");
  if (!data.plan_name) {
    summary.innerHTML = `<p class="muted">${escapeHtml(data.message || "No active plan.")}</p>`;
    if (charts["chart-projection"]) charts["chart-projection"].destroy();
    return;
  }

  const goal = data.goal_reached || {};
  summary.innerHTML = `
    <p><strong>${escapeHtml(data.plan_name)}</strong> from ${money(data.starting_value)}
       (${escapeHtml(data.starting_value_source)}).</p>
    <p>After ${data.years} years: <strong>${money(data.final_nominal)}</strong> nominal,
       <strong>${money(data.final_real)}</strong> in today's money.
       Contributions ${money(data.total_contributed)}, growth ${money(data.growth)}.</p>
    ${
      goal.goal_set
        ? `<p>Goal ${money(goal.goal_amount)}:
             ${goal.reached_nominal ? `reached in <strong>${goal.reached_nominal_in_years}y</strong> nominal` : "<strong>not reached</strong> nominal"},
             ${goal.reached_real ? `${goal.reached_real_in_years}y in today's money` : "not reached in today's money"}.</p>`
        : ""
    }
    <p class="muted">${escapeHtml(data.basis)}</p>`;

  const datasets = [
    { label: "Nominal", data: data.series.map((p) => p.nominal), tension: 0.2 },
    { label: "Today's money", data: data.series.map((p) => p.real), tension: 0.2 },
    { label: "Contributed", data: data.series.map((p) => p.contributed), tension: 0.2, borderDash: [5, 5] },
  ];
  if (goal.goal_set) {
    datasets.push({
      label: "Goal",
      data: data.series.map(() => goal.goal_amount),
      borderDash: [2, 4],
      pointRadius: 0,
    });
  }

  draw("chart-projection", {
    type: "line",
    data: { labels: data.series.map((p) => `Y${Math.round(p.year)}`), datasets },
    options: { responsive: true, interaction: { mode: "index", intersect: false } },
  });
}

document.getElementById("plan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/plans", { method: "POST", body: JSON.stringify(formFields(event.target)) });
    event.target.reset();
    showError("");
    await loadPlan();
  } catch (err) {
    showError(err.message);
  }
});

document.getElementById("plan-rows").addEventListener("click", async (event) => {
  const activate = event.target.dataset.activatePlan;
  const remove = event.target.dataset.deletePlan;
  try {
    if (activate) await api(`/api/plans/${activate}/activate`, { method: "POST" });
    else if (remove) await api(`/api/plans/${remove}`, { method: "DELETE" });
    else return;
    await loadPlan();
  } catch (err) {
    showError(err.message);
  }
});

// ------------------------------------------------------------------- trades

async function loadTrades() {
  await Promise.all([loadBroker(), loadTradeQueue()]);
}

async function loadBroker() {
  const box = document.getElementById("broker");
  const data = await api("/api/broker");
  if (!data.configured) {
    box.innerHTML = `<p class="muted">${escapeHtml(data.message)}</p>`;
    return;
  }
  const account = data.account;
  box.innerHTML = `
    <p>${data.paper ? '<span class="badge">paper</span>' : '<span class="badge danger">LIVE</span>'}
       Alpaca equity <strong>${money(account.equity)}</strong>,
       cash ${money(account.cash)}, buying power ${money(account.buying_power)}.</p>
    <p class="muted">${data.positions.length} open positions:
       ${escapeHtml(data.positions.map((p) => `${p.symbol} ${p.quantity}`).join(", ") || "none")}</p>`;
}

async function loadTradeQueue() {
  const trades = await api("/api/trades?limit=50");
  if (!trades.length) {
    emptyRow("trade-rows", 6, "No proposals yet.");
    return;
  }
  document.getElementById("trade-rows").innerHTML = trades
    .map((trade) => {
      const summary = `${trade.side} ${number(trade.quantity)} ${trade.symbol} (${trade.order_type}${
        trade.limit_price ? ` @ ${money(trade.limit_price)}` : ""
      })`;
      let actions = "";
      if (trade.status === "pending") {
        actions = `<button data-approve="${trade.id}" type="button" class="primary">Accept</button>
                   <button data-reject="${trade.id}" type="button">Reject</button>`;
      } else if (trade.status === "approved") {
        actions = trade.key_expired
          ? '<span class="muted">key expired</span>'
          : '<span class="muted">key issued, waiting for the agent</span>';
      } else if (trade.filled_price) {
        actions = `<span class="muted">filled ${money(trade.filled_price)}</span>`;
      }
      return `<tr>
        <td>${date(trade.created_at)}</td>
        <td>${escapeHtml(summary)}</td>
        <td>${escapeHtml(trade.proposed_by)}</td>
        <td>${escapeHtml(trade.rationale || "-")}</td>
        <td><span class="badge ${escapeHtml(trade.status)}">${escapeHtml(trade.status)}</span>
            ${trade.error_message ? `<div class="muted">${escapeHtml(trade.error_message)}</div>` : ""}</td>
        <td>${actions}</td>
      </tr>`;
    })
    .join("");
}

document.getElementById("trade-rows").addEventListener("click", async (event) => {
  const approve = event.target.dataset.approve;
  const reject = event.target.dataset.reject;
  if (!approve && !reject) return;

  if (
    approve &&
    !confirm(
      "Accept this proposal? This mints a confirmation key and sends it to the agent, " +
        "which will place the order with Alpaca.",
    )
  )
    return;

  event.target.disabled = true;
  const replyBox = document.getElementById("trade-reply");
  try {
    if (approve) {
      replyBox.innerHTML = '<p class="muted">Handing the key to the agent…</p>';
      const result = await api(`/api/trades/${approve}/approve`, { method: "POST" });
      replyBox.innerHTML = `<p class="muted">${escapeHtml(result.note)}</p>
        ${result.agent_reply ? `<p><strong>Agent:</strong> ${escapeHtml(result.agent_reply)}</p>` : ""}`;
      showError("");
    } else {
      await api(`/api/trades/${reject}/reject`, {
        method: "POST",
        body: JSON.stringify({ reason: "Rejected in the app." }),
      });
      replyBox.innerHTML = "";
      showError("");
    }
    await loadTrades();
  } catch (err) {
    replyBox.innerHTML = "";
    showError(err.message);
    event.target.disabled = false;
  }
});

document.getElementById("trade-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/trades", { method: "POST", body: JSON.stringify(formFields(event.target)) });
    event.target.reset();
    showError("");
    await loadTradeQueue();
  } catch (err) {
    showError(err.message);
  }
});

// --------------------------------------------------------------------- chat

async function loadChat() {
  const status = await api("/api/chat/status");
  const box = document.getElementById("chat-status");
  box.innerHTML = status.configured
    ? `<p class="muted">Connected to <code>${escapeHtml(status.endpoint)}</code>.</p>`
    : `<p class="muted">${escapeHtml(status.message)}</p>`;
  document.getElementById("chat-input").disabled = !status.configured;

  document.getElementById("chat-suggestions").innerHTML = status.configured
    ? CHAT_SUGGESTIONS.map(
        (text) => `<button class="chip" type="button" data-suggest="${escapeHtml(text)}">${escapeHtml(text)}</button>`,
      ).join("")
    : "";
}

document.getElementById("chat-suggestions").addEventListener("click", (event) => {
  const text = event.target.dataset.suggest;
  if (!text) return;
  document.getElementById("chat-input").value = text;
  document.getElementById("chat-form").requestSubmit();
});

function renderChat() {
  const log = document.getElementById("chat-log");
  log.innerHTML = chatHistory
    .map(
      (message) =>
        `<div class="message ${message.role}"><span class="who">${message.role}</span>
         <div>${escapeHtml(message.content)}</div></div>`,
    )
    .join("");
  log.scrollTop = log.scrollHeight;
}

document.getElementById("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  chatHistory.push({ role: "user", content: text });
  input.value = "";
  const thinking = { role: "assistant", content: "…" };
  chatHistory.push(thinking);
  renderChat();

  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ messages: chatHistory.filter((m) => m !== thinking) }),
    });
    thinking.content = result.reply || "(the agent returned an empty reply)";
  } catch (err) {
    // Drop the placeholder rather than leaving a fake turn in the history that
    // the next request would forward back to the agent.
    chatHistory = chatHistory.filter((m) => m !== thinking);
    showError(err.message);
  }
  renderChat();
  // The agent may have queued a proposal or written a plan during that turn.
  Promise.all([loadTradeQueue(), loadStats()]).catch(() => {});
});

document.getElementById("chat-clear").addEventListener("click", () => {
  chatHistory = [];
  renderChat();
});

// -------------------------------------------------------------------- boot

loadTab("overview");
