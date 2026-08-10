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

function showTab(name) {
  const button = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!button) return;
  document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  button.classList.add("active");
  document.getElementById(`tab-${name}`).classList.add("active");
  return loadTab(name);
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => showTab(button.dataset.tab));
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
  return (loaders[name] || (() => Promise.resolve()))().catch((err) => showError(err.message));
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

// The article table is paged rather than capped: the 2-hourly job keeps adding
// rows, and "the latest 25" quietly hides the rest. `newsOffset` is the only
// state - the server owns the total and the page size.
const NEWS_PAGE_SIZE = 25;
let newsOffset = 0;
let newsSymbol = "";

async function loadArticles() {
  const params = new URLSearchParams({ limit: NEWS_PAGE_SIZE, offset: newsOffset });
  if (newsSymbol) params.set("symbol", newsSymbol);
  const page = await api(`/api/news?${params}`);

  // A page can come back empty because the filter matches nothing, or because
  // the offset ran off the end after a filter change - say which.
  if (!page.articles.length) {
    emptyRow(
      "news-rows",
      5,
      page.total
        ? "No articles on this page - go back."
        : newsSymbol
          ? `No articles stored for ${newsSymbol} yet.`
          : "No articles stored yet - run the ingestion job.",
    );
  } else {
    document.getElementById("news-rows").innerHTML = page.articles
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

  const last = page.offset + page.articles.length;
  document.getElementById("news-range").textContent = page.total
    ? `${page.offset + 1}\u2013${last} of ${page.total}${newsSymbol ? ` for ${newsSymbol}` : ""}`
    : "";
  document.getElementById("news-prev").disabled = page.offset === 0;
  document.getElementById("news-next").disabled = last >= page.total;
}

document.getElementById("news-prev").addEventListener("click", async () => {
  newsOffset = Math.max(0, newsOffset - NEWS_PAGE_SIZE);
  await loadArticles().catch((err) => showError(err.message));
});

document.getElementById("news-next").addEventListener("click", async () => {
  newsOffset += NEWS_PAGE_SIZE;
  await loadArticles().catch((err) => showError(err.message));
});

// Filtering restarts at page 1 - keeping the offset would land on a page that
// does not exist in the filtered set.
document.getElementById("news-filter").addEventListener("change", async (event) => {
  newsSymbol = event.target.value.trim().toUpperCase();
  event.target.value = newsSymbol;
  newsOffset = 0;
  await loadArticles().catch((err) => showError(err.message));
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
          : '<span class="muted">key issued &mdash; see the Chat tab</span>';
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
      "Accept this proposal? This mints a confirmation key and sends it to the agent " +
        "on the Chat tab, which will place the order with Alpaca.",
    )
  )
    return;

  event.target.disabled = true;
  const replyBox = document.getElementById("trade-reply");
  try {
    if (approve) {
      replyBox.innerHTML = '<p class="muted">Minting the confirmation key…</p>';
      const result = await api(`/api/trades/${approve}/approve`, { method: "POST" });
      replyBox.innerHTML = `<p class="muted">${escapeHtml(result.note)}</p>`;
      showError("");
      await loadTradeQueue();
      // Hand off to the Chat tab: the approval message is posted as an ordinary
      // turn so the user watches execute_trade run and reads the outcome in the
      // same conversation, rather than getting a summary of a hidden exchange.
      await sendToChat(result.chat_message);
      return;
    }

    await api(`/api/trades/${reject}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: "Rejected in the app." }),
    });
    replyBox.innerHTML = "";
    showError("");
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

const autoApprove = document.getElementById("chat-auto-approve");
autoApprove.addEventListener("change", () => {
  localStorage.setItem("capstone-auto-approve", autoApprove.checked ? "1" : "0");
});

async function loadChat() {
  const status = await api("/api/chat/status");
  const box = document.getElementById("chat-status");
  box.innerHTML = status.configured
    ? `<p class="muted">Connected to <code>${escapeHtml(status.endpoint)}</code>.</p>`
    : `<p class="muted">${escapeHtml(status.message)}</p>`;
  document.getElementById("chat-input").disabled = !status.configured;

  // The server supplies the starting position (CAPSTONE_CHAT_AUTO_APPROVE);
  // once the user has touched the box, their choice wins and is remembered
  // across reloads, so it does not have to be re-ticked every visit.
  const stored = localStorage.getItem("capstone-auto-approve");
  autoApprove.checked = stored === null ? Boolean(status.auto_approve) : stored === "1";

  if (status.configured) loadSuggestions();
  else suggestionBox.innerHTML = "";
}

// ------------------------------------------------------- agent suggestions
//
// The openers are not a list in this file. The agent writes them, so a
// portfolio holding NVDA is offered questions about NVDA and someone with no
// plan is offered the question that creates one. There is deliberately no
// canned fallback: if the agent cannot answer, the row says so and offers a
// retry rather than passing off a fixed list as its idea.
//
// Three things keep that off the critical path of opening the tab, because
// waiting on a model to be told what you could ask is a bad trade:
//
//   1. the server sends the portfolio facts in the prompt, so the agent needs
//      no tool calls - see `_suggestion_context` in app.py;
//   2. the page prefetches on load, so the turn is usually already spent by the
//      time the Chat tab is clicked;
//   3. what came back is kept in sessionStorage and rendered instantly on the
//      next open, then quietly revalidated.

const suggestionBox = document.getElementById("chat-suggestions");
const SUGGESTION_STORE = "capstone-suggestions";
let suggestionsPending = null;

// The sparkle and the violet tint are the whole tell: these came from the
// model, not from the app.
const AI_NOTE = (text, busy) =>
  `<p class="ai-note"><span class="spark" aria-hidden="true">&#10024;</span> ${text}
   <button type="button" class="ai-refresh" data-suggest-refresh="1"
           title="Ask the agent for new questions" ${busy ? "disabled" : ""}>&#8635;</button></p>`;

function storedSuggestions() {
  try {
    const held = JSON.parse(sessionStorage.getItem(SUGGESTION_STORE) || "null");
    return held && Array.isArray(held.questions) && held.questions.length ? held.questions : null;
  } catch {
    return null;
  }
}

function renderSuggestions(state, payload) {
  if (state === "loading") {
    // Ghost chips rather than a spinner: the row keeps its height, so the chat
    // log below does not jump when the real questions land.
    const ghosts = ["13rem", "17rem", "10rem"]
      .map((width) => `<span class="chip ai ghost" style="width:${width}"></span>`)
      .join("");
    suggestionBox.innerHTML = AI_NOTE("Your agent is thinking of some questions&hellip;", true) +
      `<div class="chips">${ghosts}</div>`;
    return;
  }

  if (state === "error") {
    suggestionBox.innerHTML = AI_NOTE(
      `Could not think of any questions right now &mdash; ${escapeHtml(payload)}`,
      false,
    );
    return;
  }

  suggestionBox.innerHTML =
    AI_NOTE("Suggested by your agent, from what you actually hold", false) +
    `<div class="chips">${payload
      .map(
        (text) =>
          `<button class="chip ai" type="button" data-suggest="${escapeHtml(text)}">${escapeHtml(text)}</button>`,
      )
      .join("")}</div>`;
}

// Fetches once even if the tab open and the page-load prefetch land together:
// the in-flight promise is shared rather than started twice.
function fetchSuggestions(refresh) {
  if (suggestionsPending) return suggestionsPending;
  suggestionsPending = api(`/api/chat/suggestions${refresh ? "?refresh=1" : ""}`)
    .then((body) => {
      const questions = body.suggestions || [];
      if (questions.length) {
        sessionStorage.setItem(SUGGESTION_STORE, JSON.stringify({ questions }));
      }
      return questions;
    })
    .finally(() => {
      suggestionsPending = null;
    });
  return suggestionsPending;
}

async function loadSuggestions(refresh = false) {
  // Anything already known goes up immediately; the refresh button is the one
  // case where the user asked for new ones and should see them being written.
  const held = refresh ? null : storedSuggestions();
  if (held) renderSuggestions("ok", held);
  else renderSuggestions("loading");

  try {
    const questions = await fetchSuggestions(refresh);
    if (questions.length) renderSuggestions("ok", questions);
    else if (!held) renderSuggestions("error", "the agent returned nothing");
  } catch (err) {
    // Kept inside the suggestion row on purpose. Openers are a nicety; failing
    // to write them should not raise the page-wide error banner as if the chat
    // itself were broken, because it is not. If chips are already on screen
    // they stay - stale questions beat an error message.
    if (!held) renderSuggestions("error", err.message);
  }
}

suggestionBox.addEventListener("click", (event) => {
  if (event.target.closest("[data-suggest-refresh]")) {
    loadSuggestions(true);
    return;
  }
  const text = event.target.dataset.suggest;
  if (!text) return;
  document.getElementById("chat-input").value = text;
  document.getElementById("chat-form").requestSubmit();
});

// Reads the SSE response from /api/chat/stream and hands each event to
// `onEvent` as it lands - text deltas and tool calls both. Not EventSource:
// that can only issue GETs, and the conversation history has to be POSTed.
async function streamChat(body, onEvent) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Events are separated by a blank line; the trailing fragment is kept for
    // the next read because a chunk can split an event in half.
    const events = buffer.split("\n\n");
    buffer = events.pop();

    for (const block of events) {
      const line = block.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }
      if (event.type === "error") throw new Error(event.message);
      else onEvent(event);
    }
  }
}

/*
 * The chat log is built incrementally rather than re-rendered from one big
 * template string, because a turn is now a list of BLOCKS in the order the
 * stream produced them - text, a tool row, a chart - instead of a strip of tool
 * chips above a paragraph.
 *
 * Two reasons that ordering matters. A tool shown where it was actually called
 * reads as the agent working ("let me check" / tool / what it found) instead of
 * as a summary bolted on top of an answer that was already written. And it is
 * what puts a chart in front of the paragraph explaining it.
 *
 * Incremental is not a nicety here: a Chart.js canvas replaced by an innerHTML
 * rebuild is destroyed, so re-rendering the whole log on every text delta would
 * tear down and redraw every chart dozens of times a turn. Each message and
 * each block therefore keeps its own element, and only its contents change.
 */

let chatChartSeq = 0;

/** Everything the agent said this turn - the part that goes back as history. */
function messageText(message) {
  return (message.blocks || [])
    .filter((block) => block.kind === "text")
    .map((block) => block.text)
    .join("\n\n")
    .trim();
}

/** The text block currently being written into, opening one if there is none. */
function openTextBlock(message) {
  const last = message.blocks[message.blocks.length - 1];
  if (last && last.kind === "text" && last.open) return last;
  const block = { kind: "text", text: "", open: true };
  message.blocks.push(block);
  return block;
}

/** Drop the "thinking" placeholder as soon as the turn has anything real. */
function clearStatus(message) {
  if (message.blocks.length === 1 && message.blocks[0].kind === "status") message.blocks.pop();
}

// ------------------------------------------------------ charts in the answer

/*
 * Charts are built from the TOOL RESULT, never from the agent's prose. The
 * server forwards the payload of a handful of chart-shaped tools alongside the
 * "done" frame (see CHART_TOOLS in agent_chat.py), so the line on the screen is
 * drawn from the same numbers the agent was reasoning over. Asking the model to
 * emit chart data in its reply would put a hallucinated series one plausible
 * paragraph away, which is exactly the failure this whole project is built to
 * avoid.
 *
 * A builder returns {title, caption, config} or null - null when the result is
 * too thin to plot, e.g. a one-point history.
 */

function projectionCaption(result) {
  const goal = result.goal_reached || {};
  const parts = [
    `After ${result.years}y: ${money(result.final_nominal)} nominal, ` +
      `${money(result.final_real)} in today's money.`,
  ];

  if (result.goal_amount && "required_monthly_contribution" in result) {
    // The scenario tool solved for it: this is the answer to "what would it take".
    const needed = result.required_monthly_contribution;
    const neededReal = result.required_monthly_contribution_real;
    if (needed === null || needed === undefined) {
      parts.push(`${money(result.goal_amount)} is out of reach at these assumptions.`);
    } else {
      parts.push(
        `Reaching ${money(result.goal_amount)} needs ${money(needed)}/mo` +
          (neededReal ? ` - ${money(neededReal)}/mo to get there in today's money.` : "."),
      );
    }
  } else if (goal.goal_set) {
    parts.push(
      goal.reached_nominal
        ? `Goal ${money(goal.goal_amount)} reached in ${goal.reached_nominal_in_years}y nominal.`
        : `Goal ${money(goal.goal_amount)} is not reached inside the horizon.`,
    );
  }

  if (result.assumptions && result.assumptions.length) {
    parts.push(`Assumes ${result.assumptions.join(", ")}.`);
  }
  return parts.join(" ");
}

function projectionChatChart(result) {
  const series = result.series || [];
  if (series.length < 2) return null;

  const goal = result.goal_reached || {};
  const datasets = [
    { label: "Nominal", data: series.map((p) => p.nominal), tension: 0.2, pointRadius: 0 },
    { label: "Today's money", data: series.map((p) => p.real), tension: 0.2, pointRadius: 0 },
    {
      label: "Contributed",
      data: series.map((p) => p.contributed),
      tension: 0.2,
      pointRadius: 0,
      borderDash: [5, 5],
    },
  ];
  if (goal.goal_set) {
    datasets.push({
      label: "Goal",
      data: series.map(() => goal.goal_amount),
      borderDash: [2, 4],
      pointRadius: 0,
    });
  }

  return {
    title: `${result.plan_name || "Projection"} - ${money(result.starting_value)} over ${result.years} years`,
    caption: projectionCaption(result),
    config: {
      type: "line",
      data: { labels: series.map((p) => `Y${Math.round(p.year)}`), datasets },
      options: chatChartOptions({ interaction: { mode: "index", intersect: false } }),
    },
  };
}

function breakdownChatChart(result) {
  const groups = (result.groups || []).filter((group) => group.value);
  if (!groups.length) return null;
  return {
    title: `Allocation by ${result.group_by} - ${money(result.total_value)}`,
    caption: `From the net worth report of ${date(result.report_date)}.`,
    config: {
      type: "doughnut",
      data: {
        labels: groups.map((group) => group.group),
        datasets: [{ data: groups.map((group) => group.value) }],
      },
      options: chatChartOptions(),
    },
  };
}

function historyChatChart(result) {
  const points = result.points || [];
  if (points.length < 2) return null;
  return {
    title: `Net worth, ${result.granularity}`,
    caption: `${points.length} points, change ${money(result.change_over_window)} over the window.`,
    config: {
      type: "line",
      data: {
        labels: points.map((point) => date(point.date)),
        datasets: [
          { label: "Total", data: points.map((p) => p.total_value), tension: 0.2 },
          { label: "Invested", data: points.map((p) => p.invested_value), tension: 0.2 },
          { label: "Cash", data: points.map((p) => p.cash_value), tension: 0.2 },
        ],
      },
      options: chatChartOptions({ interaction: { mode: "index", intersect: false } }),
    },
  };
}

/** Chart options for a chart living in a fixed-height box inside the log. */
function chatChartOptions(extra = {}) {
  return {
    responsive: true,
    // The wrapper sets the height; without this Chart.js keeps its own aspect
    // ratio and overflows the box on a narrow window.
    maintainAspectRatio: false,
    plugins: { legend: { labels: { boxWidth: 12, font: { size: 10 } } } },
    ...extra,
  };
}

// Tool name -> builder. A tool missing from here simply gets its row and no
// chart, which is the correct outcome for the twelve tools that answer in words.
const CHAT_CHARTS = {
  project_scenario: projectionChatChart,
  get_investment_plan_projection: projectionChatChart,
  get_holdings_breakdown: breakdownChatChart,
  get_networth_history: historyChatChart,
};

function drawChatChart(host, block) {
  host.innerHTML = `
    <div class="chat-chart-title">${escapeHtml(block.spec.title)}</div>
    <div class="chat-chart-box"><canvas id="${block.canvasId}"></canvas></div>
    ${block.spec.caption ? `<p class="muted">${escapeHtml(block.spec.caption)}</p>` : ""}`;
  draw(block.canvasId, block.spec.config);
}

/** Forget the Chart instances belonging to the log - used when it is cleared. */
function destroyChatCharts() {
  Object.keys(charts)
    .filter((id) => id.startsWith("chat-chart-"))
    .forEach((id) => {
      charts[id].destroy();
      delete charts[id];
    });
}

// ------------------------------------------------------------------ rendering

// The agent asked permission to run a tool. Shown with the arguments visible,
// because "may I run get_holdings_breakdown?" is not a decision anyone can make
// without seeing what it was about to be called with.
function approvalCard(message) {
  if (!message.approval) return "";
  const lines = message.approval.requests
    .map(
      (request) =>
        `<div><code>${escapeHtml(request.name || "tool")}</code>
         <span class="muted">${escapeHtml(request.arguments || "")}</span></div>`,
    )
    .join("");
  if (message.approval.decided) {
    return `<div class="approval">${lines}<p class="muted">${escapeHtml(message.approval.decided)}</p></div>`;
  }
  return `<div class="approval"><p>The agent wants to run:</p>${lines}
    <div class="chips">
      <button type="button" class="chip" data-approve="yes">Accept</button>
      <button type="button" class="chip" data-approve="no">Reject</button>
    </div></div>`;
}

function renderBlocks(message, host) {
  message.blocks.forEach((block, index) => {
    let el = host.children[index];
    if (!el) {
      el = document.createElement("div");
      host.appendChild(el);
    }
    // An element is only ever reused for the same KIND of block. Inserting a
    // chart shifts everything after it by one, and a canvas quietly reclassified
    // as a paragraph would leave a live Chart bound to a node nobody can see.
    if (el.dataset.kind !== block.kind) {
      el.dataset.kind = block.kind;
      el.innerHTML = "";
      delete el.dataset.drawn;
    }

    if (block.kind === "text") {
      el.className = "text";
      el.innerHTML =
        escapeHtml(block.text) +
        (block.open && message.streaming ? '<span class="cursor">▍</span>' : "");
    } else if (block.kind === "status") {
      el.className = "text muted";
      el.textContent = block.text;
    } else if (block.kind === "tool") {
      el.className = "tool-row";
      el.innerHTML = `<span class="tool ${block.status}">${
        block.status === "done" ? "✓" : "⚙"
      } ${escapeHtml(block.name)}</span>`;
    } else if (block.kind === "chart") {
      el.className = "chat-chart";
      // Drawn once. Re-running this on every delta would rebuild the canvas
      // mid-stream and throw away the chart that is already on screen.
      if (!el.dataset.drawn) {
        el.dataset.drawn = "1";
        drawChatChart(el, block);
      }
    }
  });

  while (host.children.length > message.blocks.length) host.lastChild.remove();
}

function renderChat() {
  const log = document.getElementById("chat-log");
  const wanted = new Set();

  chatHistory.forEach((message) => {
    if (!message.blocks) {
      // A user turn, or an old assistant turn restored from plain text.
      message.blocks = message.content ? [{ kind: "text", text: message.content }] : [];
    }
    if (!message.el || message.el.parentNode !== log) {
      message.el = document.createElement("div");
      message.el.className = `message ${message.role}`;
      message.el.innerHTML = `<span class="who">${escapeHtml(message.role)}</span>
        <div class="blocks"></div><div class="approval-slot"></div>`;
      log.appendChild(message.el);
    }
    wanted.add(message.el);
    renderBlocks(message, message.el.querySelector(".blocks"));
    message.el.querySelector(".approval-slot").innerHTML = approvalCard(message);
  });

  // A turn dropped from the history (a failed one) takes its element with it.
  Array.from(log.children).forEach((el) => {
    if (!wanted.has(el)) el.remove();
  });
  log.scrollTop = log.scrollHeight;
}

/**
 * Fold one tool event into the turn's blocks.
 *
 * A running tool closes the paragraph above it: whatever the agent says next is
 * about what the tool found, so it belongs in its own row underneath rather than
 * glued onto the sentence that introduced the call.
 */
function handleToolEvent(message, event) {
  clearStatus(message);
  message.blocks.forEach((block) => {
    if (block.kind === "text") block.open = false;
  });

  // Matched on the call id so the result frame ticks off the call that started
  // it instead of adding a second row.
  let row = message.blocks.find((block) => block.kind === "tool" && block.id === event.id);
  if (row) {
    row.status = event.status;
    if (event.name) row.name = event.name;
  } else {
    row = { kind: "tool", id: event.id, name: event.name, status: event.status };
    message.blocks.push(row);
  }

  if (!event.result) return;
  const builder = CHAT_CHARTS[event.name];
  const spec = builder && builder(event.result);
  if (!spec || message.blocks.some((block) => block.kind === "chart" && block.id === event.id)) return;

  // Directly under the row that produced it, which is also directly above the
  // paragraph the agent is about to write about it.
  message.blocks.splice(message.blocks.indexOf(row) + 1, 0, {
    kind: "chart",
    id: event.id,
    spec,
    canvasId: `chat-chart-${++chatChartSeq}`,
  });
}

// One agent turn, whether it starts from a question or resumes a paused one.
async function runChat(body) {
  const turn = { role: "assistant", content: "", blocks: [{ kind: "status", text: "…" }] };
  chatHistory.push(turn);
  renderChat();

  try {
    await streamChat(body, (event) => {
      if (event.type === "approval") {
        // The turn stops here until the user decides. `state` is the agent's
        // paused conversation, kept client-side and handed straight back.
        turn.approval = { requests: event.requests, state: event.state };
        clearStatus(turn);
      } else if (event.type === "tool") {
        handleToolEvent(turn, event);
      } else if (event.type === "delta" && event.text) {
        clearStatus(turn);
        openTextBlock(turn).text += event.text;
        turn.streaming = true;
        turn.content = messageText(turn);
      } else {
        return;
      }
      renderChat();
    });

    delete turn.streaming;
    // Pieces arrive with their own spacing; the last one usually ends in one.
    turn.blocks.forEach((block) => {
      if (block.kind !== "text") return;
      block.text = block.text.trim();
      block.open = false;
    });
    turn.blocks = turn.blocks.filter((block) => block.kind !== "text" || block.text);
    turn.content = messageText(turn);

    // A turn that only asked permission, or only drew a chart, has no answer in
    // it yet - and saying "empty reply" over an approval card would be nonsense.
    if (!turn.content && !turn.approval && !turn.blocks.length) {
      turn.blocks.push({ kind: "text", text: "(the agent returned an empty reply)" });
    }
  } catch (err) {
    // Drop the placeholder rather than leaving a fake turn in the history that
    // the next request would forward back to the agent.
    chatHistory = chatHistory.filter((m) => m !== turn);
    showError(err.message);
  }
  renderChat();
  // The agent may have queued a proposal or written a plan during that turn.
  Promise.all([loadTradeQueue(), loadStats()]).catch(() => {});
}


function askAgent(text) {
  chatHistory.push({ role: "user", content: text });
  return runChat({
    // Only what was said, never the rendering state: a turn also carries its
    // blocks and its DOM element, and neither is any business of the agent's.
    messages: chatHistory
      .filter((message) => message.content)
      .map((message) => ({ role: message.role, content: message.content })),
    auto_approve: autoApprove.checked,
  });
}

document.getElementById("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  input.value = "";
  askAgent(text);
});

/**
 * Move to the Chat tab and send `text` as the user's turn.
 *
 * Used by the Accept button on the Trades tab: the message it sends carries the
 * confirmation key, so the tab has to be visible and the turn has to be a normal
 * one - the point is that the user sees exactly what was handed over and watches
 * the trade execute, instead of trusting a summary.
 */
async function sendToChat(text) {
  if (!text) return;
  await showTab("chat");
  await askAgent(text);
}

// Accept / Reject on an approval card. Delegated, because renderChat() rebuilds
// the whole log and any listener bound to a button would be thrown away with it.
document.getElementById("chat-log").addEventListener("click", (event) => {
  const choice = event.target.dataset.approve;
  if (!choice) return;
  const message = chatHistory.find((m) => m.approval && !m.approval.decided);
  if (!message) return;

  const approve = choice === "yes";
  message.approval.decided = approve ? "Approved." : "Rejected - the agent was told no.";
  renderChat();
  // Rejection is sent too: the agent needs to hear the answer to continue and
  // say it could not look that up, rather than being left hanging.
  runChat({
    resume: {
      items: message.approval.state.items,
      approvals: message.approval.requests.map((request) => ({ id: request.id, approve })),
    },
    auto_approve: autoApprove.checked,
  });
});

document.getElementById("chat-clear").addEventListener("click", () => {
  // Chart.js keeps its instances alive by canvas id, so dropping the history
  // without this leaks one chart per answer that ever drew one.
  destroyChatCharts();
  chatHistory = [];
  renderChat();
});

// -------------------------------------------------------------------- boot

loadTab("overview");

// Warm the chat openers while the user is reading the overview. Nothing is
// rendered here - the answer lands in sessionStorage and the server cache, so
// opening the Chat tab later shows chips immediately instead of ghosts. It is
// fire-and-forget on purpose: a failure is the chat tab's problem to report
// when it opens, not the overview's.
api("/api/chat/status")
  .then((status) => {
    if (status.configured) return fetchSuggestions(false);
  })
  .catch(() => {});
