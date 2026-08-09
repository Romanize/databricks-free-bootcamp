# Capstone — Net Worth Tracker with an Agentic MCP Server

A personal net worth tracker: holdings, a news + sentiment pipeline, an
investment plan, and an Agent Bricks agent that can read all three and propose
trades a human approves. It pulls together every piece of the three homeworks —
Lakebase CRUD (day 1), embeddings and vector search (day 2), an MCP server with
a tracing dashboard (day 3) — into one system, in its own isolated space.

```
                 ┌──────────────────────────────────────────────────────────┐
                 │  App 1  app/          Flask UI                           │
   you  ────────►│  holdings · report builder · plan · charts · approvals    │
                 │  chat tab ──────────┐                                     │
                 └─────────────────────┼──────────────────────────────────────┘
                            Accept ──► │ mints a single-use confirmation key
                                       ▼ and forwards it
                          ┌────────────────────┐
                          │  Agent Bricks      │
                          │  agent + prompt    │
                          └─────────┬──────────┘
                                    │ MCP (streamable HTTP)
                                    ▼
                 ┌──────────────────────────────────────────────────────────┐
                 │  App 2  mcp_server/   16 tools                            │
                 │  execute_trade ──── needs the key ────► Alpaca (paper)    │
                 └────────┬──────────────────────────────┬──────────────────┘
                          │ SQL + pgvector               │ one row per call
                          ▼                              ▼
              ┌───────────────────────┐        ┌──────────────────────┐
              │ Lakebase `capstone`   │        │ Lakebase             │
              │ 8 tables + 4 views    │        │ `capstone_tracing`   │
              └───────▲───────────────┘        └──────────┬───────────┘
                      │                                   │ reads
        ┌─────────────┴─────────────┐         ┌────────────▼───────────┐
        │ Job A (2h) news+embeddings│         │ App 3  dashboard/      │
        │ Job B (daily) SCD → UC    │         │ agent tracing          │
        └───────────────────────────┘         └────────────────────────┘
                      │
                      ▼
        Unity Catalog Delta: holdings_scd, networth_history
```

## Live links

| What                         | URL                                                                     |
| ---------------------------- | ----------------------------------------------------------------------- |
| App — net worth tracker      | https://networth-tracker-app-7474651537600327.aws.databricksapps.com    |
| Agent tracing dashboard      | https://networth-agent-tracing-7474651537600327.aws.databricksapps.com  |
| MCP server (`/mcp` endpoint) | https://mcp-server-networth-app-7474651537600327.aws.databricksapps.com |

## The two rules the design is built around

Everything below follows from these, so they are worth stating first.

**1. Numbers come from SQL. Only news comes from embeddings.**

The obvious way to build "an agent that answers questions about your net worth"
is to embed everything and let the agent retrieve. That is the wrong shape here.
A vector search _always_ returns its nearest neighbours whether or not they are
relevant, so RAG over balances produces confident, well-formatted, wrong numbers
— and the user has no way to notice. So net worth, allocation, plan projections
and trades are answered by typed SQL through purpose-built tools, and the
embedding index contains only news articles, where "here are the five most
similar passages" is exactly the right semantics.

**2. The agent can execute a trade, but only with a key a human minted.**

`propose_trade` queues a row and mints nothing. `execute_trade` requires a
confirmation key that comes into existence **only** when a person clicks Accept
in the app. Four properties, all enforced in `schema.py` rather than in the
prompt, make that hold:

| Property                                                                                    | Stops                                                   |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| No key exists until Accept                                                                  | The agent reading a key at proposal time                |
| `SAFE_TRADE_COLUMNS` allowlist — no tool ever returns `confirmation_hash`                   | The agent fetching its own key back and self-confirming |
| Redemption is one atomic `UPDATE … WHERE status='approved' AND hash=…` that clears the hash | Replay from conversation history; double submission     |
| 15-minute TTL                                                                               | An abandoned approval staying armed                     |

Only the SHA-256 is stored, so reading the database yields nothing usable
either. A fully jailbroken agent can call `execute_trade` all it likes and gets
a single, uninformative rejection every time.

This is a genuine change from an earlier revision, which made the agent
_structurally_ incapable of trading by never importing the order code. That was
a stronger guarantee and a worse product — the whole point is that the agent can
carry a trade through once you have said yes.

## Layout

```
capstone/
├── app/                        App 1 — the Flask UI
│   ├── app.py                  routes: CRUD, charts, reports, trades, chat proxy
│   ├── reports.py              the report draft builder and submission
│   ├── trading.py              ** mints the confirmation key on Accept **
│   ├── agent_chat.py           proxy to the Agent Bricks serving endpoint
│   ├── schema.py lakebase.py embeddings.py config.py
│   ├── massive_api.py alpaca_api.py pricing.py projections.py
│   ├── templates/index.html  static/{app.js,style.css,chart.min.js}
│   └── app.yaml requirements.txt .env.example
│
├── mcp_server/                 App 2 — the MCP server
│   ├── networth_mcp_server.py  16 @mcp.tool functions + the _run wrapper
│   ├── trading.py              ** redeems the key, submits the order **
│   ├── tracing.py              one trace row per tool call
│   ├── smoke_test.py           drives the real MCP surface in-process
│   └── (the same shared modules, duplicated — see below)
│
├── dashboard/                  App 3 — agent tracing
│   ├── app.py lakebase.py tracing.py
│   └── templates/ static/ app.yaml requirements.txt
│
├── notebooks/
│   ├── ingest_news_embeddings.py    every 2 hours
│   └── scd_holdings_networth.py     daily, Lakebase → Unity Catalog
│
├── agent/system_prompt.md      the Agent Bricks prompt + why it is shaped that way
├── sql/                        generated DDL + example queries
├── setup_secrets.py            stores the five secrets, grants each app READ
└── screenshots/
```

`lakebase.py`, `schema.py`, `embeddings.py`, `config.py`, `massive_api.py`,
`alpaca_api.py`, `pricing.py`, `projections.py`, `reports.py` and `trading.py`
are **duplicated** across `app/` and `mcp_server/`. Each Databricks App deploys from its own directory, so
they cannot import a shared module — homework 3 hit the same wall with
`lakebase.py`/`schema.py`. Keep the copies in sync when editing; a shared wheel
installed by both `requirements.txt` files is the grown-up fix.

## The database

Two Lakebase databases on one instance, two secrets.

### `capstone` — the app database

One dimension, one fact table, and views over them. Everything is **USD**;
there is no currency column anywhere, because multi-currency means FX rates, a
rate date on every line and a conversion policy for the aggregates, and none of
that earns its place here.

| Table                     | What it holds                                                                               | Why it is shaped that way                                                                                                                                                                                                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `holdings`                | **reference only**: alias, type, symbol, institution                                        | Carries no values at all. What a thing is worth is a fact about a date, so it belongs to a report. The only rules left are a CHECK that a ticker or crypto holding names a symbol, and a unique alias **among active holdings only** - holdings are soft-deleted, so a global constraint would burn a name forever the moment you deactivated it. |
| `networth_report`         | **the fact table**: one row per (date, holding) with quantity, price, `price_source`, value | `UNIQUE (report_date, holding_id)` _is_ the "at most one report per day" rule — re-submitting updates the same rows. `ON DELETE RESTRICT` on `holding_id` is what makes soft-delete mandatory rather than conventional.                                                                                                                           |
| `watchlist`               | tickers to collect news for                                                                 | Upsert on `symbol`; re-adding reactivates rather than duplicating.                                                                                                                                                                                                                                                                                |
| `ticker_news`             | one row per article                                                                         | `embed_text` (title + description) is **stored**, not recomputed, so the job and any later audit agree on exactly what was vectorized. `synced_at` moves only when `embed_text` changes — that is what makes re-embedding incremental.                                                                                                            |
| `tickers_news_embeddings` | one row per chunk, `VECTOR(384)` + HNSW                                                     | `tickers` is copied from the article and GIN-indexed so "news about AAPL" stays one indexed scan instead of a join that would defeat the HNSW index.                                                                                                                                                                                              |
| `ticker_sentiments`       | per-article, per-ticker sentiment                                                           | Stored **verbatim** from Massive's `insights` array. Nothing computes sentiment locally, so every score traces to a specific article rather than to a model's opinion of one.                                                                                                                                                                     |
| `investment_plan`         | goal, rate, years, inflation, contributions, `created_by`                                   | A partial unique index allows only one `is_active` row. New plans are created **inactive**, so the agent writing one can never silently replace what you are charting.                                                                                                                                                                            |
| `pending_trades`          | the approval queue + `confirmation_hash`                                                    | A six-state machine (`pending → approved → executing → executed/failed`, plus `rejected`) where every transition is an atomic `UPDATE … WHERE status = <expected>`. That is what makes a double-click or a replayed key a no-op instead of a second real order.                                                                                   |

**Views** — all four are computed, never stored:

| View                     | Purpose                                                                                                                                              |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `networth_report_lines`  | report ⋈ holdings, so callers get alias/type/symbol in one query                                                                                     |
| `networth_daily`         | per-date total / invested / cash. There is deliberately **no header table**: storing an aggregate beside the rows it sums is how the two drift apart |
| `networth_monthly`       | one row per month via `DISTINCT ON`, taking the **last** report of that month — a mid-month reading never becomes the month's number                 |
| `ticker_sentiment_daily` | per-day sentiment rollup for the per-ticker chart                                                                                                    |

## How a net worth report works

This is the least obvious part of the system.

A report is a **snapshot you fill in**, not something the app generates. Since
`holdings` carries no values, the app cannot know what anything is worth until
you tell it. `GET /api/reports/draft` builds one prefilled, editable line per
active holding:

| Field                    | Prefilled from                                                   |
| ------------------------ | ---------------------------------------------------------------- |
| quantity (ticker/crypto) | Alpaca's open positions → last report → blank                    |
| price (ticker/crypto)    | live Alpaca quote → Massive previous close → last report's price |
| value (cash/bank/wallet) | last report's value, **carried forward for you to correct**      |

Alpaca wins on quantity because it is the only source that knows about trades
the app never saw. Bank balances are carried forward and flagged as such,
because the honest alternative — showing zero — would be worse, and silently
reusing the old number without saying so would be worse still. Every line
reports its own `quantity_source` and `price_source`, and both are written into
the row.

Submitting upserts on `(report_date, holding_id)` and deletes any line for that
date you dropped. Re-submitting the same date edits that report; there is at
most one per day, enforced by the unique constraint rather than by application
logic.

**Trades do not touch reports.** Executing a trade places an order and nothing
else — no position is adjusted, no cash is moved, no report is rewritten. The
app tracks snapshots of what accounts were worth at a point in time; it does not
model cash flow or cost basis. After a trade, your next report picks up the new
reality from Alpaca.

## Data sources

**Massive** (https://massive.com) for news and sentiment. `GET /v2/reference/news`
returns articles with an `insights` array already carrying a per-ticker
`sentiment` and `sentiment_reasoning`, which is why sentiment is stored rather
than computed.

The free "Stocks Basic" tier allows **5 requests per minute** and serves
**end-of-day** data. Both facts are load-bearing:

- `massive_api.py` has a module-level rate limiter that sleeps 12.5s between
  calls (4.8/min). Twenty tickers therefore takes about four minutes — fine for
  a background job, well past an HTTP request timeout. **That is why news
  ingestion is a job and not a button**, and why the app's manual sync handles
  exactly one ticker per request.
- Massive prices are a previous close, so they are the _fallback_ price source.

**Alpaca** (paper) for prices and trading. Its free IEX feed is real-time, prices
a whole portfolio in one batched request, and comes with the paper account the
trading half needs anyway. `pricing.py` tries Alpaca first and falls back to
Massive per symbol; every quote carries `source` and `as_of`, and both land in
`networth_report` — a line priced from yesterday's close, one priced live and
one carried forward from last month are three different claims, and the app, the
report form and the agent all have to be able to say which they are looking at.

Neither is required. Without Massive the news pipeline is disabled; without
Alpaca the trade queue is disabled and prices fall back to end-of-day. The rest
of the app works either way.

## Embeddings

`sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions, chunked at 800/100 —
the same model and settings as homework 2, so the two are directly comparable.
`MODEL_DIMENSIONS` maps model → dimension and the DDL builds `VECTOR(n)` from it,
so swapping models is one env var plus recreating the table.

News chunks harder than homework 2's weather text did: an article's title +
description runs 300–1500 characters, so 800/100 genuinely splits the longer ones
into 2–3 overlapping windows. Titles go first in `embed_text` because a headline
is the densest sentence in a news item and survives every chunk boundary.

## The MCP tools

Sixteen, in four groups.

| Tool                             | What it does                                                                                        |
| -------------------------------- | --------------------------------------------------------------------------------------------------- |
| `get_networth_summary`           | Latest report, its age, top lines. `refresh_prices` re-values share quantities live without saving. |
| `get_holdings_breakdown`         | Allocation by type or by holding, with percentages.                                                 |
| `get_networth_history`           | Net worth over time, monthly or per report.                                                         |
| `get_investment_plan`            | The active plan and any alternatives.                                                               |
| `create_investment_plan`         | Writes a plan — **inactive** unless explicitly activated.                                           |
| `update_investment_plan`         | Patches one; validates the _merged_ result, not just the patch.                                     |
| `activate_investment_plan`       | Switches which plan is charted.                                                                     |
| `get_investment_plan_projection` | Nominal **and** inflation-adjusted, with goal analysis.                                             |
| `search_ticker_news`             | Semantic search. The only embedding-backed tool.                                                    |
| `get_ticker_sentiment`           | Stored sentiment, with article counts.                                                              |
| `get_watchlist`                  | What is tracked — and therefore what news can exist.                                                |
| `add_to_watchlist`               | Starts collection on the next run. Reversible.                                                      |
| `get_alpaca_account`             | Read-only brokerage view.                                                                           |
| `propose_trade`                  | Queues a proposal. Mints no key, sends nothing.                                                     |
| `execute_trade`                  | Places the order — **requires the human-minted key**.                                               |
| `list_pending_trades`            | Proposals and outcomes. Never returns keys.                                                         |

Every tool returns `status` of `success`, `error` or **`no_data`**, and never
raises across the MCP boundary. `no_data` being a distinct status is a guardrail,
not tidiness: "the news index is empty" and "the news search failed" lead to very
different sentences, and collapsing them into one is exactly what produces an
agent that quietly answers from memory. `no_data` responses also carry a
`guidance` field spelling out that estimating is not acceptable.

## Guardrails, in one place

| Layer                                                  | What it prevents                                         |
| ------------------------------------------------------ | -------------------------------------------------------- |
| No confirmation key exists until a human clicks Accept | The agent executing its own proposal                     |
| `SAFE_TRADE_COLUMNS` allowlist on every trade query    | The agent reading a key back out of a tool result        |
| Key stored only as SHA-256                             | A database read yielding a usable key                    |
| Atomic redeem that clears the hash                     | Replay of a key left in conversation history             |
| 15-minute key TTL                                      | An abandoned approval staying armed indefinitely         |
| `WHERE status = <expected>` on every transition        | A double-click submitting the same order twice           |
| Plans created inactive                                 | An agent-written plan silently replacing the charted one |
| Key omitted from the trace row's arguments             | A live credential persisting in the tracing dashboard    |
| `no_data` as a first-class status                      | Empty results being read as licence to guess             |
| `as_of` + `price_source` on every price                | A previous close being quoted as if it were live         |
| `staleness_warning` on old reports                     | A two-month-old total quoted as current                  |
| Prices missing → holding excluded + warning            | A missing quote looking like a wipeout                   |
| Article counts on every sentiment score                | A +1.0 from two articles reading as a signal             |
| Real _and_ nominal on every projection                 | Inflation quietly doubling the apparent outcome          |
| Tracing on every call, best-effort                     | A dead trace database breaking working answers           |

## Running it

### 1. Lakebase

Create one Lakebase instance and two databases on it, `capstone` and
`capstone_tracing`, then enable pgvector on the first:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The apps create their own tables at startup (`init_db()`), so there is nothing
else to run. `sql/01_app_schema.sql` and `sql/02_tracing_schema.sql` contain the
same DDL if you would rather create it up front — they are generated from the
code by `python sql/generate.py`, so they cannot drift.

### 2. Secrets

```
python setup_secrets.py
```

Stores five values in scope `capstone`: the two Lakebase URLs (required), the
Massive key and the two Alpaca paper keys (all optional). Re-run with
`--grant-only` after deploying each app to grant its service principal READ, or
`--only massive-api-key` to rotate one value.

### 3. Deploy the three apps

Compute → Apps → Create app → Custom, once per folder:

| Folder        | Notes                                                                                                                    |
| ------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `app/`        | The UI. This is the link you share.                                                                                      |
| `mcp_server/` | **The MCP endpoint is the app URL with `/mcp` appended.** The bare URL returns 404 — expected, there is no web UI on it. |
| `dashboard/`  | Tracing. Needs only the tracing secret.                                                                                  |

Note each app's service principal UUID and re-run `setup_secrets.py --grant-only`.

### 4. Register the MCP server and build the agent

Full steps are in [`agent/system_prompt.md`](agent/system_prompt.md), along with
the prompt itself and the reasoning behind each rule in it. In short: register
the `/mcp` URL as an external MCP server, create an Agent Bricks Custom LLM
agent with it as a tool source, paste the prompt, deploy.

Then put the agent's **serving endpoint name** into `app/app.yaml` as
`CAPSTONE_AGENT_ENDPOINT` and redeploy the app — that is what turns on the Chat
tab. Until then the tab renders and explains itself rather than erroring.

### 5. Schedule the two jobs

Workflows → Create job → Notebook task:

| Notebook                              | Schedule      | Cron            |
| ------------------------------------- | ------------- | --------------- |
| `notebooks/ingest_news_embeddings.py` | every 2 hours | `0 0 */2 * * ?` |
| `notebooks/scd_holdings_networth.py`  | daily, 03:30  | `0 30 3 * * ?`  |

The news job needs READ on the scope for `lakebase-url` and `massive-api-key`.
The SCD job needs `lakebase-url` plus `CREATE TABLE` on the target Unity Catalog
schema (default `main.capstone_analytics`, a widget).

### 6. First run

1. Add holdings on the Holdings tab. These are reference only — no values.
2. Open **New report**, check the prefilled figures, correct your bank balances,
   and submit. That is your first net worth reading.
3. Add tickers to the watchlist.
4. Create an investment plan — or just ask the agent on the Chat tab to set one
   up with you.
5. Run the news job once by hand, or use "Fetch news for one ticker now".

## Challenges

- Configuring and deploying the agent, giving permissions to the app service principal
  and later debugging a couple of hours which is actually the expected input/output and
  streaming vs block to make it work in the app client.

  Three traps, each hiding the next. `serving_endpoints.query()` deserializes into a
  dataclass with no `output` field, so a ResponsesAgent's whole answer is dropped in
  transit and you are left holding a response id and three empty lists — it looks
  exactly like an endpoint returning nothing. The endpoint then answers a _stream_, so
  parsing the body as one JSON document fails at character 0, which reads like a
  network error rather than a format mismatch. And the endpoint labels an SSE **error**
  stream `application/json`, so the agent's own explanation of what was wrong was being
  reported as "the body was not JSON".

  `agent_chat.py` now posts to `/serving-endpoints/<name>/invocations` directly, sends
  only `input`, detects a stream by its shape rather than its Content-Type, and
  surfaces an agent-reported failure verbatim instead of retrying it.

- **Tools were being requested and never run.** The hardest one to see, because
  nothing failed: the chat tab answered "I'll check your current holdings for you."
  and stopped, the tracing table stayed empty, and the same question worked in the
  Playground.

  A tool reached through a registered MCP server is not run on the agent's say-so. The
  turn ends with an `mcp_approval_request` item — _may I run `get_holdings_breakdown`
  with these arguments?_ — and nothing happens until the caller answers. The Playground
  answers it for you. This app was reading that turn, finding text in it, and treating
  it as the finished reply.

  So `stream()` is a loop, not a request: it emits an `approval` event carrying the
  tool, its arguments and the paused conversation; the chat tab shows Accept / Reject;
  the decision goes back and the same turn continues. The paused state travels through
  the browser rather than sitting in server memory, because a Databricks App can restart
  or run more than one worker.

  An **Auto-approve tools** checkbox sits next to the chat box. Ticked, the approval is
  answered server-side and never reaches the browser — no card, no click, the agent goes
  straight through to the MCP server — and the answer arrives in one turn. The choice
  rides on each request rather than being read from the environment, so it takes effect
  immediately; `CAPSTONE_CHAT_AUTO_APPROVE` in `app.yaml` only sets where the box starts.
  Auto-approving is defensible here because `execute_trade` is still gated by a
  single-use key the user alone can mint on the Trades tab — the human stays in the loop
  by that key, not by this checkbox.

## Known limitations / what I'd do next

- **News fetching is O(tickers) requests.** At 12.5s apart, a 50-ticker watchlist
  would take ten minutes and exceed the free tier's daily practicality. A paid
  tier removes the pacing; there is no batch news endpoint to fall back on.
- **The chat tab keeps history in the browser only.** Refreshing loses the
  conversation, and the app cannot show you what the agent did last week even
  though the tracing dashboard can. Persisting conversations keyed by the same
  `session_id` the traces use would join those two halves.
- **Alpaca fills are polled for two seconds.** Outside market hours an order is
  accepted but unfilled, so the report is updated with the last known price and a
  warning. A webhook, or a job that reconciles open orders, would close that gap.
