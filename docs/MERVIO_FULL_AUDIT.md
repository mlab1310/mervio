# MERVIO — Full Technical, Product and Production-Readiness Audit

| | |
|---|---|
| Audit date | 16 September 2026 |
| Repository | `/Users/mlab/Projects/mervio` |
| Branch / HEAD | `master` @ `bbe8355` ("Merge Mission 004.2: Background Jobs and Audit") |
| Scope | Read-only audit. This file is the only change. No source, test, migration or configuration file was modified. |
| Method | Direct inspection of code, migrations, tests, benchmark result files and documentation, plus two full test runs. |

**Evidence labels used throughout**

- **[FACT]**: verified directly in code, tests, migrations, benchmark JSON or a test run during this audit.
- **[INFERENCE]**: a technical conclusion drawn from verified facts.
- **[UNKNOWN]**: cannot be determined from the repository.
- **[EXTERNAL]**: a statement about a third-party platform (Shopify, Stripe, LLM vendors) that comes from general
  platform knowledge, not from this repository. It **must be re-verified against current vendor documentation** at
  the start of the relevant mission.

---

## 1. Executive Summary

**What Mervio is today** [FACT]
- A well-tested, deterministic e-commerce analytics **engine**, driven from a CLI and CSV exports.
- A **multi-tenant persistence and background-job layer** (PostgreSQL 17, forced RLS, immutable snapshots,
  byte-identical persisted reports, durable job queue, append-only audit).
- A **guard-railed LLM interpretation layer**. It has never been connected to a real model.

It is **not yet a product**:
- no HTTP API;
- no authentication;
- no frontend;
- no Shopify API connector;
- no deployment artefact;
- no CI;
- no billing.

No merchant can use it without a developer.

**Strengths**
- **Numerical honesty** [FACT]:
  - an absent value is reported as absent, never as zero;
  - weak profitability data is excluded from the score;
  - root-cause output is explicitly labelled "no causality established".
- **Isolation engineering** [FACT]:
  - forced RLS on every tenant table;
  - composite foreign keys;
  - unique keys prefixed with the organisation;
  - isolation proven through the repositories and through raw SQL.
- **Reproducibility** [FACT]: the persisted report is byte-identical to the CLI report.
- **Test discipline** [FACT]: 1097 tests pass against PostgreSQL, with no skips.
- **Architectural restraint** [FACT]:
  - no premature infrastructure;
  - every decision is recorded in an ADR.

**Structural gaps**
1. **Real-data validation.**
   - No native Shopify CSV export and no real merchant data has ever gone through the engine.
   - The only real dataset is a 106-order sales register reconstructed from a PDF (OH5).
   - D-046 itself forbids presenting numbers before a pilot export passes the validation harness.
2. **Analytical coverage.**
   - 10 of the 17 synthetic scenarios contain at least one declared `KNOWN_GAP`.
   - The engine cannot see these causes:
     - organic traffic;
     - churn and retention;
     - stockouts;
     - shipping problems;
     - discount policy;
     - seasonality.
   - A normal seasonal decline raises a false alarm.
3. **No product surface.**
   - There is no API, auth, UI or onboarding.
   - The worker is a library with no process entrypoint.
4. **Privacy.**
   - Customer emails are stored in plain text inside immutable snapshots that the application role cannot delete.
   - A GDPR erasure request therefore cannot be fulfilled through the application.
5. **Continuous-sync readiness.**
   - Snapshots are full copies per import.
   - A job lease is never renewed.
   - Workers only serve an explicit list of tenants.
   - There are no schedules and no job chaining.
   - Every one of these must change before a daily Shopify sync can run for many stores.

**Recommendation**
- **Keep the architecture:**
  - Python modular monolith;
  - PostgreSQL;
  - PostgreSQL-backed queue;
  - Next.js.
- **Reorder the roadmap:**
  1. A short platform-baseline mission (CI and runnable processes).
  2. Move the Shopify **data** connector ahead of the API. It carries the largest data-semantics risk, and it
     shapes the store, connection and snapshot schemas the API will expose.
  3. The API and a read-only frontend, which give the first visible product.
  4. Shopify install and continuous sync, analytics hardening, the production LLM, and a production environment.
     Together these give the first real end-to-end product.
  5. The commercial layer, which gives a paying-customer-ready product.
- **Actions** (approval and execution) move to post-MVP.

---

## 2. Current Repository State

### 2.1 Git [FACT]

| Check | Result |
|---|---|
| Branch | `master` |
| HEAD | `bbe8355` Merge Mission 004.2 |
| Working tree | clean before the audit; only this file added after |
| `7241068` (004.2 final commit) ancestor of HEAD | yes |
| `1760205` (004.1 merge) ancestor of HEAD | yes |
| Unrelated uncommitted work | none |
| Git remote | **none configured** (`git remote -v` is empty) |
| CI configuration | **none** (no `.github/`, no other CI file) |
| Tracked files | 211 |
| Python source (`src/`) | 12,112 lines |
| Tests (`tests/`, goldens included) | 14,027 lines |

`bbe8355` merged 31 files (+6,159 / −16): the jobs queue, audit, worker, observability code, migrations
`0004`–`0005`, their tests, the benchmark and the documentation.

### 2.2 Test verification [FACT]

- **`pytest -q` exactly as written fails in this shell:** `command not found`. pytest is only installed in `.venv`.
- **Both runs below used `.venv/bin/pytest`.**
  - The repository's `addopts = "-q"` combined with `-q` hides the summary line.
  - A second run with `-o addopts=""` was used to get the exact counts.
- **Runtime:** Python 3.11.16 in `.venv`.

| Run | Command | Result |
|---|---|---|
| Without database | `.venv/bin/pytest -p no:cacheprovider -o addopts="" -q` | **796 passed, 301 skipped** in 44.91 s. The 301 skips are the PostgreSQL tests, skipped because `MERVIO_TEST_ADMIN_DATABASE_URL` is absent (by design, ADR-004.1-008). |
| With database | `MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres MERVIO_REQUIRE_DATABASE_TESTS=1 .venv/bin/pytest -p no:cacheprovider -o addopts="" -q` | **1097 passed, 0 skipped, 0 failed, 0 errors** in 99.56 s |
| PostgreSQL | `select version()` | **PostgreSQL 17.11** (Homebrew, aarch64-apple-darwin), local disposable cluster in `.pgdata/`, port 55432 |

- **Baseline confirmed:** the claimed figures (1097 with the database; 796 + 301 without) match exactly.
- **No configuration was changed.** The local cluster was already running. The test fixture creates and drops its
  own throwaway database and roles.

### 2.3 Dependencies [FACT]
- **Engine runtime:** zero dependencies (`dependencies = []`, D-004).
- **Optional `persistence` extra:** `psycopg[binary]>=3.2,<4` (LGPL-3.0) and `alembic>=1.13,<2`.
- **Development:** `pytest`.
- **Absent:** FastAPI, pydantic, any HTTP client, any LLM SDK and any Node project.

### 2.4 Documentation state [FACT]
Accurate, detailed documents:
- `README.md` and `docs/PROJECT_STATE.md`;
- the 004.0, 004.1 and 004.2 handoffs, decisions and risk registers;
- `docs/DECISIONS.md` (D-001 to D-048);
- `docs/LLM_CONTRACT.md` and `docs/ANALYTICS_ENGINE.md`.

**Documentation drift found:**

| Document | Drift |
|---|---|
| `docs/ROADMAP.md` | Still lists Mission 002 (LLM) as the next step, "Stage 4 — AI 🔴" and "Git 🔴". Superseded. |
| `docs/TODO.md` | P0 still lists "Couche LLM (mission 002)". Several items are done. |
| `docs/ARCHITECTURE.md` | Lists "base de production" among the things deliberately not built (PostgreSQL has existed since 004.1). Its tree predates `persistence/`, `workers/`, `observability/` and `synthetic/`. |
| `docs/DATA_MODEL.md` | Points to `src/mervio/models.py`. The real path is `src/mervio/domain/models.py`. |
| Prompt baseline | Mentions top-level `migrations/`, `frontend/` and `configuration/`. None exist. Migrations live under `src/mervio/persistence/migrations/versions/`. |

---

## 3. What Mervio Can Actually Do Today

### 3.1 Repository inventory

**IMPLEMENTED (code + tests)** [FACT]

| Capability | Location |
|---|---|
| Canonical domain model and data-quality report | `src/mervio/domain/` |
| CSV connectors: Shopify orders and products, Stripe, Google Ads | `src/mervio/ingestion/` |
| Delimiter and encoding detection, binary-format refusal, date-order rules | `ingestion/base.py` |
| File validation, source detection, external-schema inspector, sensitive-data detection | `application/imports.py`, `inspector.py`, `sensitive.py` |
| Analysis service (never raises) | `application/service.py` |
| Engine: KPIs, profitability, time series, anomalies, root cause, health score, insights, report | `src/mervio/analytics/` |
| Deliverables: `report.json`, `report.txt` (15 sections), `data_quality.json` | `src/mervio/reporting/` |
| CLI: `analyze`, `validate`, `inspect`, `demo` | `src/mervio/cli/main.py` |
| LLM context 1.0, prompt channels, provider abstraction, response validator, mock provider | `src/mervio/llm/` |
| Persistence: users, orgs, memberships, roles, stores, CSV connections, snapshots, canonical rows, analysis runs, reports, provenance | `src/mervio/persistence/`, migrations `0001`–`0003` |
| Job queue (state machine, lease, retries, cancel, purge), audit log | `persistence/jobs.py`, `audit.py`, migrations `0004`–`0005` |
| Worker library: `run_once`, `run_until_empty`, `run_forever`; import, analysis and purge handlers | `src/mervio/workers/` |
| JSON logging with correlation and redaction | `src/mervio/observability/logging.py` |
| Migration CLI | `python -m mervio.persistence upgrade\|downgrade\|current\|heads` |
| Synthetic generator: 17 scenarios with ground truth | `src/mervio/synthetic/`, `data/scenarios/` |
| Benchmarks: engine, persistence, jobs, scenario robustness | `benchmarks/` |
| Real-export validation harness | `scripts/validate_real_export.py` |
| Local PostgreSQL (Docker or disposable cluster) | `docker-compose.yml`, `scripts/dev_postgres.sh` |

**PARTIALLY IMPLEMENTED**

| Capability | What exists | What is missing |
|---|---|---|
| LLM explanation | context, validator, mock, `explain_report()` [FACT] | real provider; any runtime caller (only tests and the validation harness call it) [FACT]; persistence of explanations; budget |
| Worker as a service | library class [FACT] | process entrypoint; SIGTERM handling; env-based configuration; tenant discovery (dispatcher); service identity; lease heartbeat [FACT: none of these exist] |
| Observability | JSON formatter, correlation, redaction [FACT] | `configure_json_logging` is called only by `benchmarks/jobs_benchmark.py` [FACT]; no metrics, alerting or collector |
| Retention and purge | terminal jobs and expired audit events [FACT] | organisation, store and snapshot deletion; customer-level erasure |
| Provenance | SHA-256 of input files, report → run → snapshot → source file → record chain [FACT] | raw files are not retained (no object storage), so provenance cannot be replayed from the source |
| Multi-tenancy | persistence-level isolation, roles, membership check per transaction [FACT] | identity provider, API enforcement, owner transfer, member role changes (no function) [FACT] |
| Recommendations | deterministic template sentences in `analytics/insights.py` [FACT] | tracking, status, dismissal, link to actions |
| Real-data validation | harness; one reconstructed real register (OH5, 106 orders) [FACT] | any native merchant export |

**PLANNED (documented, not implemented)** [FACT]
- FastAPI API v1 with OIDC.
- Connection pool.
- `schedules` table and job chaining.
- Multi-tenant dispatcher.
- Object storage.
- Shopify Admin GraphQL connector.
- OAuth and encrypted token storage.
- `web/` Next.js frontend.
- Findings, recommendations, explanations, actions and `idempotency_keys` tables.
- Action approval workflow.
- OpenTelemetry and Prometheus.
- PDF report.
- CI.
- Store-timezone periods.

**MISSING (required for a SaaS, not substantively planned anywhere)** [FACT: absent from code and docs]
- Billing, subscriptions, trials and plan limits.
- Signup and onboarding flows.
- Email delivery (weekly brief, alerts).
- Account and organisation deletion.
- GDPR erasure and export paths.
- Deployment artefacts:
  - no application Dockerfile;
  - no infrastructure-as-code;
  - no environment matrix.
- Backup and restore procedure.
- Secrets manager integration.
- Legal documents (terms, privacy policy, DPA).
- Support and operator tooling.
- Shopify app-platform obligations (see §13).
- Inventory, sessions and traffic data sources.
- Multi-currency conversion.
- Localisation: every user-facing engine string is in French, written without accents (e.g. "inferieur").

**EXPERIMENTAL / TOOLING**
- `mervio.synthetic`.
- `research/`.
- `scripts/validate_real_export.py`.
- `scripts/generate_data.py`.

These are valuable tooling, not product.

**DEAD / UNUSED OR DISCONNECTED** [FACT]
- `AnalyticsConfig.refund_reconciliation_tolerance` (`config.py:73`) is never read.
  `pipeline._reconcile_refunds` hardcodes `0.01` (`pipeline.py:130`).
- `mervio.llm` has no production caller.
- `observability.configure_json_logging` has no production caller.
- Two logging stacks coexist:
  - `mervio.logging_config`, used by the engine, ingestion and CLI;
  - `mervio.observability.logging`, used by the worker.
- `data/uploads/` workspace (`application/workspace.py`) sits inside the code tree. This only suits the local CLI.

### 3.2 The "Real Mervio Today" map

```
DATA SOURCE ── CSV exports (Shopify orders/products, Stripe, Google Ads)      IMPLEMENTED (CSV)  / MISSING (API)
    ↓
INGESTION ── ingestion/*.py, application/imports.py                           IMPLEMENTED (CSV)
    ↓
NORMALIZATION ── domain/models.py (float, UTC-naive datetimes)                 IMPLEMENTED
    ↓
SNAPSHOT ── persistence/snapshots.py, migration 0002                           IMPLEMENTED (full copy per import)
    ↓
ANALYTICS (KPI, profitability, series) ── analytics/kpi.py, profitability.py   IMPLEMENTED
    ↓
ANOMALY DETECTION ── analytics/anomaly.py                                      IMPLEMENTED (v1, no seasonality)
    ↓
ROOT CAUSE ── analytics/root_cause.py                                          PARTIAL (revenue only, 3 factors)
    ↓
REPORT ── analytics/report.py, reporting/, persistence/analyses.py             IMPLEMENTED
    ↓
JOBS ── persistence/jobs.py, workers/                                          PARTIAL (library, no process/dispatch)
    ↓
API ── (none)                                                                  MISSING (planned 004.3)
    ↓
FRONTEND ── (none)                                                             MISSING (planned 004.5)
    ↓
LLM ── llm/ (mock provider only, no runtime caller)                            PARTIAL (guardrails only)
    ↓
RECOMMENDATION ── analytics/insights.py (templates)                            PARTIAL (static, untracked)
    ↓
ACTION ── (none)                                                               MISSING (planned 004.6)
```

**The only end-to-end path that runs today** [FACT]:
1. `python -m mervio.analytics analyze --shopify-orders … --out dir`
2. The engine writes `report.json`, `report.txt` and `data_quality.json`.

The persisted path (CSV → snapshot → job → report in PostgreSQL) works only when driven from Python code or tests.
No CLI command provisions a user or organisation, enqueues a job, or starts a worker.

---

## 4. Architecture Audit

**Structure** [FACT]

The monolith is modular, with dependency rules enforced by AST tests (`tests/test_persistence_boundaries.py`):

| Rule | Detail |
|---|---|
| The engine is isolated | `analytics`, `domain` and `ingestion` never import `persistence` or `synthetic` |
| All SQL lives in one place | no SQL outside `persistence` (plus the reviewed use case) |
| The worker has no database driver | `workers` imports no DB driver, no analytics module and no `llm` |
| Observability is independent | `observability` imports no DB and no engine |

**Assessment**
- **Layering is clean and testable** [FACT].
  - The CLI → application → analytics → domain flow is one-directional.
  - The SaaS path reuses the engine unchanged, through `analyze_loaded_dataset` (ADR-004.1-009).
- **The deterministic core is the single source of numerical truth** [FACT].
  - No KPI is computed in SQL, in the worker or in the LLM layer.
- **A synchronous, connection-per-`Database` design** [FACT] is appropriate for a worker. The API will need a pool.
  - ADR-004.1-006 notes that `set_config(..., true)` is compatible with a transaction-mode pooler [INFERENCE:
    correct, because the settings are transaction-local].
- **Weak spots** [FACT]:
  - `report.py` assembles an untyped nested dict (known debt, `docs/TODO.md`). The API will need a formal schema
    for it.
  - The engine holds the entire snapshot in memory:
    - 4.6 GB peak RSS at 1M orders on the persistence path;
    - 3.7 GB on the CSV path.
  - Thresholds are partly hardcoded outside `config.py`, despite the stated rule:
    - `insights.py:137` (0.20 margin) and `:152` (0.40 concentration);
    - `health.py` threshold tables and `MIN_PRODUCT_COVERAGE`;
    - `root_cause.py` confidence penalties;
    - `pipeline.py:130`.
  - The report language is fixed (French, unaccented) inside the engine.

---

## 5. Data Architecture

### 5.1 Lifecycle [FACT]

1. CSV file.
2. `read_csv`:
   - delimiter sniffed;
   - UTF-8, cp1252 or latin-1;
   - ZIP, XLSX, XLS, PDF, UTF-16 and NUL bytes refused.
3. Connector: per-row tolerance, `DataQualityReport` issues.
4. `Dataset` (float amounts, UTC-naive datetimes).
5. `write_snapshot`:
   - single transaction;
   - batched inserts;
   - `numeric(19,4)` with exact float round-trip, otherwise refused;
   - ordered by `position`.
6. Snapshot sealed:
   - `inputs_sha256` makes re-imports idempotent;
   - triggers enforce immutability.
7. `load_dataset` re-reads the snapshot and verifies its counts (`SnapshotIntegrityError`).
8. The engine runs.
9. The report is stored as exact bytes (`text`), with a `jsonb` projection and a SHA-256 checked in the database.

### 5.2 Canonical model [FACT]

| Entity | Notes |
|---|---|
| `Order` | `subtotal` is **after** discounts (D-041); `discount` is informational; `financial_status`; `customer_email`; `items` |
| `OrderItem` | `line_revenue = quantity × unit_price` (gross, order discount not allocated) |
| `Product` | keyed by **SKU**; `unit_cogs` Optional; **no variant entity** |
| `Payment` | Stripe; `fee` and `net` Optional |
| `Refund` | amount only; dated at **order creation** (D-045); Shopify and Stripe kept separately |
| `Campaign`, `DailyAdPerformance` | Google Ads only |
| `Customer` | **derived**, keyed by **email** (`customer_id = email or "guest:<order>"`, `ingestion/shopify.py:115`) |
| Discounts | a field on the order; no discount entity, no discount codes, no line-level allocation |

### 5.3 Revenue contract (Mission 003.3) — re-checked [FACT]

| Rule | What the code does |
|---|---|
| Revenue definition | "CA avant ajustements" = Σ `Order.subtotal` (`kpi.py`) |
| Structural guard | a test forbids any discount subtraction in analytics |
| Order scope (D-044) | every exported order counts: cancelled, unsettled, draft and zero-value orders included; each case is signalled (`cancelled_orders_counted`, etc.) |
| Refunds (D-045) | `Refunded Amount` taken as is, dated at order creation; `refund_rate = Σ refunded / Σ Total` |
| Partial profit (D-047) | uses the product share of each refund only when it is arithmetically determined |
| Subtotal-convention check (D-046) | **partial** |

**D-046 check detail**
- **Detected:** a contradiction, reported as `subtotal_convention_contradiction`, with revenue marked `incomplete`.
- **Flagged as unverifiable:** `subtotal_contract_unverified`.
- **Undetectable:** a discount absent from every column.
- **Evidence status:** the partial-discount semantics rest only on Shopify API documentation. Nothing empirical
  supports them (`PARTIAL-DISCOUNT EVIDENCE NOT AVAILABLE`).

**The contract is internally consistent and well documented.** Its weak point is empirical: it is proven on one
reconstructed register.

### 5.4 Can the system safely process…

| Input | Status | Basis |
|---|---|---|
| Real native merchant exports | **UNKNOWN** | never tested; D-046 blocks presenting numbers until the harness passes on a pilot export |
| Large exports | **PROVEN to 1M orders** (synthetic) | 1M = 45 s / 3.7 GB (CSV path); snapshot write 78 s, re-read 17 s, analysis 7.8 s, RSS 4.6 GB |
| 10M orders | **UNKNOWN** | [INFERENCE] roughly linear scaling implies ~40 GB+ RSS; not feasible in memory on typical workers |
| Malformed exports | **PROVEN** (synthetic cases) | corrupt rows rejected individually; binary formats refused; ambiguous dates rejected, never guessed; unknown schema refused (Kaggle) |
| Partial exports (missing sources) | **PROVEN** | missing sources lead to `unavailable` metrics and excluded score dimensions |
| Partial exports (truncated date range) | **ASSUMED** | the engine analyses the last closed period in the data; **no freshness check against "today"** [FACT: nothing in `analytics/` compares data recency to wall-clock time] |
| Duplicate records within a file | **PROVEN** | the Shopify connector de-duplicates identical line items; Google Ads dedup key is `campaign|day` |
| Repeated imports of the same bytes | **PROVEN** | `inputs_sha256` gives `reused` |
| Overlapping or updated exports | **ASSUMED** | each import is a new full snapshot; nothing reconciles entity versions across snapshots (upsert deferred, ADR-004.1-004) |
| Changed schemas (renamed columns) | **PARTIAL** | required columns are enforced; `first_present` handles a few aliases; there is no schema-version detection for vendor CSV changes |
| Negative values | **PROVEN** | negative revenue kept and flagged, never clamped; negative COGS rejected |
| Missing values | **PROVEN** | `None` is never treated as 0; coverage is tracked per field |
| Currencies | **PROVEN (single currency)** | mixed currencies raise an error, with no conversion; an absent currency stays `unknown`; a store-currency mismatch is refused at import |
| Timezones | **GAP** | periods are cut in UTC; slash dates are treated as UTC; `stores` has **no timezone column** [FACT] |

---

## 6. Analytics Engine

### 6.1 Inventory [FACT]

**KPIs** (`compute_kpis`)
- Revenue (before adjustments), orders, units, AOV.
- Refunds, refund rate.
- Ad spend, paid clicks, paid conversion rate.
- New customers, CAC, ROAS.
- Gross margin, COGS, payment fees.
- Repeat rate, top-5 customer concentration.

**Comparisons** (`timeseries.compare_all`)
- WoW or MoM, and YoY (YoY needs at least 12 months).
- Applied to 8 series metrics: revenue, orders, aov, ad_spend, paid_clicks, paid_conversion_rate, roas, refunds.

**Trends**
- 12-period series with a 4-period rolling average.
- No trend or regression detection.

**Anomaly detector** (one method)
- The last closed period is compared with the mean of the previous 8 periods.
- Rule: materiality ≥ 8 % **and** (|Δ| ≥ 15 % **or** |z| ≥ 2 with at least 4 baseline periods).
- Severity is `high` when |Δ| ≥ 30 % or |z| ≥ 3.
- Each anomaly carries a directional assessment.

**Root cause** (one rule)
- Revenue = paid clicks × (orders / paid clicks) × AOV, using log-ratio contributions.
- Product and campaign contributors are additive.
- It compares the **current period with the immediately previous period**.

**Product analysis**
- Per-SKU revenue, units, COGS and margin.
- Low-margin products (< 20 %) are flagged.

**Customer analysis**
- New customers, measured against the full-history first order.
- Intra-period repeat rate.
- Top-5 concentration.

**Marketing analysis**
- Google Ads spend, clicks, ROAS, CAC.
- Per-campaign performance and the best campaign by conversions delta.

**Profitability**
- Contribution profit: revenue − COGS − fees − advertising − product share of refunds − shipping cost.
- `data_available=false` when any component is missing.
- Shipping cost is always missing from Shopify.

**Health score**
- Six weighted dimensions, with exclusion and renormalisation.
- Profitability is excluded without COGS or a determinable refund share.
- Product health is excluded below 80 % cost coverage.

**Not implemented** [FACT]
- Retention and cohorts.
- Inventory.
- Traffic, other than paid clicks.
- Conversion, other than orders divided by paid clicks.
- Seasonality.
- Forecasting.
- Discount analysis.
- Shipping and fulfilment.
- Customer lifetime value.

### 6.2 Coverage against the product promise

| Question | Can Mervio answer it? |
|---|---|
| What happened? | **Yes** [FACT], for revenue, orders, AOV, refunds and paid-marketing metrics, with data-quality context |
| Why did it happen? | **Partially** [FACT]. Only for a revenue **decline**, and only through three factors. The other anomalies carry the generic hypothesis "cause not established by this engine" |
| What evidence supports it? | **Partially** [FACT]. Numbers, z-scores, factor contributions and product and campaign contributors, but as preformatted strings in `evidence[]` |
| What should the merchant do? | **Weakly** [FACT]. Three factor-specific templates plus generic "verify X in the source" messages |

### 6.3 Blind spots confirmed by the Mission 004.0 synthetic scenarios [FACT]

- **Headline results**
  - 170/170 runs "pass". `KNOWN_GAP` expectations count as passing when the engine does **not** find the truth.
  - 10 of 17 scenarios declare at least one gap.
  - The primary cause is correctly identified in revenue_drop, AOV_decline, discount_explosion (as AOV),
    marketing_campaign_failure (as conversion), traffic_drop, conversion_drop and mixed_root_causes.

| Scenario | Ground truth | Engine behaviour |
|---|---|---|
| order_volume_drop | organic traffic −60 % | organic traffic is not observed; paid-only "conversion rate" moves instead (orders ÷ paid clicks) |
| discount_explosion | discount policy | recommends on AOV; discount rate is not tracked |
| margin_collapse | cost increase | the current cost snapshot only, so no margin trend |
| product_failure | a product demand collapse | the decomposition knows only traffic, conversion and AOV |
| customer_churn / retention_decline | churn | intra-period repeat rate only; no retention trend |
| marketing_campaign_failure | campaign issue | recommendation targets the checkout funnel; the campaign appears only in detail |
| seasonal_business | normal seasonal decline | **false alarm** (no seasonal model) |
| stockout | inventory | no inventory data; misread as AOV or conversion |
| shipping_problem | fulfilment delay | no shipping data |

### 6.4 Additional analytical defects found in code [INFERENCE from code reading; not covered by a test]

**A-1 — Baseline mismatch** (`insights.py` and `root_cause.py`)
- The revenue anomaly is measured against the **8-period baseline**.
- Its explanation decomposes the change against **the previous period only**.
- `estimated_impact` uses the baseline delta.
- **Consequence:** in a sustained decline, the explanation describes a small or even positive week-over-week move.
  The impact figure then describes another comparison.

**A-2 — Paid conversion rate conflates two populations**
- `paid_conversion_rate` divides **all** orders, organic included, by **paid** clicks.
- **Consequence:** any organic-traffic change is attributed to "conversion". This is structural; it is documented
  as a limitation, but the insight and recommendation still assert a conversion problem.

**A-3 — Root cause runs for declines only**
- Root cause runs only for revenue declines.
- Refund spikes, AOV rises and other anomalies get no causal analysis.

**A-4 — Weak email-based customer identity**
- Guest checkouts become one customer per order.
- **Consequence:** repeat-rate and new-customer counts are biased for stores with many guest orders [INFERENCE].

**A-5 — No data-freshness guard**
- A stale export produces a report about an old period.
- Nothing states "data ends N days ago".

---

## 7. Analytics Quality

| Insight | Data required | Logic / threshold | Evidence emitted | Confidence | Limitations |
|---|---|---|---|---|---|
| `revenue_decline` | Shopify orders; Google Ads for the factors | anomaly rule (§6.1), direction down | anomaly values, primary factor fact, contribution %, decomposition-metric anomalies | primary factor heuristic `0.35 + 0.5·|contribution| − penalty`, clamped to [0.1, 0.9]; 0.4 without a factor | baseline mismatch (A-1); paid-only traffic (A-2); UTC periods |
| Other metric anomalies | series metric | same rule | observed, expected, Δ %, z | fixed 0.45 | no cause; generic recommendation |
| `profitability_unavailable` | products, Stripe, Ads | any cost component missing | list of limitations | fixed 0.9 | shipping cost is always missing from Shopify, so this insight is **almost always present** [INFERENCE] |
| `low_margin_product` | products with COGS | margin < 20 % (hardcoded) | revenue, COGS, units | fixed 0.7 | current cost only; order discounts not allocated to lines, so line margin is overstated when discounts exist [INFERENCE] |
| `customer_concentration` | orders with email | top-5 share ≥ 40 % (hardcoded) | definition string | fixed 0.6 | email identity (A-4) |
| `campaign_opportunity` | Google Ads | best Δ conversions > 0 | spend and conversions | fixed 0.4 | platform-attributed conversions; no margin per campaign |
| Health score | all sources | piecewise-linear thresholds, renormalised | per-dimension score and exclusions | n/a | weights are product choices, not calibrated on outcomes |

**Correlation vs causation** [FACT]
- The engine never claims causation:
  - `causality_established = false` on every root cause;
  - a limitation sentence is always present;
  - the LLM validator rejects causal wording (`causalite_affirmee`).
- The **confidence values are uncalibrated heuristics** (D-012). The fixed constants (0.4 to 0.9) are not derived
  from data.

**Needed before the LLM layer amplifies these outputs**
1. **Stronger evidence.**
   - Structured evidence objects: `metric_key`, `value`, `baseline`, `period`.
   - Today's preformatted strings force the LLM and the UI to re-parse numbers.
2. **Confidence levels.**
   - A qualitative scale backed by explicit criteria: data coverage, baseline length, factor coverage.
   - Stop using per-insight constants.
3. **Alternative explanations.**
   - Return the ranked set of factors, not only the "primary" one.
   - Add explicit "not observed" factors (organic traffic, stock, shipping) so the UI can say what was **not**
     checked.
4. **Historical context.**
   - A YoY or seasonal guard before flagging a decline.
   - "Is this new?" (was the same anomaly present last period?).
5. **Attribution logic.**
   - Separate paid from organic orders. Shopify order attribution fields may help [EXTERNAL].
   - Allocate discounts to lines.

---

## 8. AI / LLM

| Question | Answer |
|---|---|
| Is the integration real? | **No** [FACT]. `LLMProvider` is abstract; only `MockLLMProvider` exists; no SDK and no network code anywhere in `src/` |
| Is it wired into the product? | **No** [FACT]. `analyze_dataset()` never calls it (`_meta.llm_used = false`); the worker has no explanation job type; only tests and `scripts/validate_real_export.py` call `explain_report` |
| Inputs | a structured, versioned, bounded (64 kB) context built from the report (contract 1.0): facts, findings, evidence, hypotheses, recommendations, root causes, limitations, unavailable metrics, data quality, untrusted-content markers [FACT] |
| Deterministic numbers? | **Yes** [FACT]. Every number comes from the engine; the health score is copied, never read from the model |
| Output validation | **Yes** [FACT]. Closed JSON schema (1.0); reference grounding; every number must match a numeric context field; no numbers for unavailable metrics; no causal claims; currency consistency; PII and secret detection; instruction-leak detection; the whole response is rejected on any violation |
| Hallucination protection | **Partial by design** [FACT]. Lexical and numeric grounding is defence in depth; the contract says so explicitly |
| Evidence passed? | yes, with stable IDs [FACT] |
| Historical context | only what is in one report: 12-period series and comparisons [FACT]; no memory across reports |
| Business context | designed as optional untrusted input (ADR-004-006), **not implemented** [FACT] |
| Recommendations generated by the LLM? | it may **rephrase** engine recommendations with refs; it cannot create unreferenced ones [FACT] |
| Actions / human approval | **none** [FACT] |
| Prompt injection | channel separation, envelope escaping, text sanitising, instruction flagging, adversarial test suite [FACT] |

**What is needed for production grade**
1. **A real provider behind `LLMProvider`.**
   - Timeouts on the network client.
   - Error mapping.
   - An explicit dependency decision (D-004).
   - Model choice [EXTERNAL: select a current model at mission time].
2. **An `explanation` job type in the worker.**
   - An `explanations` table (tenant-scoped, immutable), with provenance: context SHA-256, versions, model,
     validator issue codes.
3. **Measurement before launch.**
   - Validator rejection rate on the 7 goldens and the 17 scenarios.
   - Latency and cost per explanation.
4. **Per-organisation cost budget and rate limit.** No automatic calls without opt-in.
5. **Language.**
   - The engine text is French.
   - The LLM becomes the natural localisation layer, but the validator's lexical rules (causality words, etc.) must
     cover each target language.
6. **Data-processing review.**
   - Contexts contain aggregated business data; PII is masked.
   - Vendor data-retention terms need review [EXTERNAL / legal].
7. **Fallback.** If an explanation is unavailable, the deterministic text is shown. This behaviour already exists
   (D-037).

---

## 9. Security & Multi-Tenancy

### 9.1 What is proven [FACT]

**Enforced by the database**
- `organization_id NOT NULL`.
- `ENABLE` + `FORCE` RLS on every tenant table.
- `USING` + `WITH CHECK` policies on every tenant table.
- Composite foreign keys, so a reference to another tenant's row fails.
- Unique keys prefixed with `organization_id`, which closes the uniqueness oracle.

**Enforced on the application role**
- `mervio_app` is neither owner, superuser nor `BYPASSRLS`.
- Its `UPDATE` rights are granted column by column.
- It has no `DELETE` on history.
- The application **refuses** to connect as a superuser or `BYPASSRLS` role, and to a non-UTF-8 server.

**Membership and role checks**
- `TenantSession` re-reads membership and role in **every** transaction.
- A forged context raises `TenantAccessDenied`, which surfaces as `NotFound`.
- A malformed ID raises `NotFound` and never reaches SQL.

**Job and audit guarantees**
- Restrictive policies on the job queue and the audit log.
- The audit log cannot be updated: no grant, and a trigger blocks it.
- Deletion is floored at 30 days.

**Worker**
- The role is re-checked at execution: a purge enqueued by an owner fails when the worker runs as an analyst.

**Test coverage**
- Cross-tenant tests exist at the repository level, at the application barrier with RLS bypassed, and at the
  raw-SQL RLS level.

### 9.2 Conceptual attack matrix

| Scenario | Current behaviour | Status |
|---|---|---|
| Tenant A → Tenant A | allowed through `TenantSession` | OK [FACT] |
| Tenant A → Tenant B | `NotFound`, indistinguishable from a missing resource; RLS returns 0 rows | OK at persistence [FACT]; **untested at HTTP** (no API) |
| Worker A → Tenant B | out of query scope (explicit session list) | OK [FACT] |
| Analyst → privileged operation | `PermissionDenied` (purge, members, audit read) | OK [FACT] |
| Forged organization ID | membership re-check fails → `NotFound` | OK [FACT] |
| Forged store ID | `fetch_store` filters by organisation → `NotFound` | OK [FACT] |
| Forged job ID | `NotFound` | OK [FACT] |
| Forged report ID | `get_report` filters by organisation and store | OK [FACT] |
| ID enumeration | UUIDv4 resource IDs; sequential IDs are never exposed | OK [FACT] |
| Uniqueness leakage | prefixed unique keys; the residual risk on global UUID primary keys is accepted (ADR-004.1-006) | accepted |

### 9.3 Findings (no fixes applied)

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| SEC-01 | **Import job payloads accept arbitrary local filesystem paths.** Nothing confines them to a tenant-owned location. Once an API lets a user enqueue imports, this becomes a path traversal and cross-tenant file read. | **HIGH** (latent until an API exists) | [FACT] `workers/handlers.py` `import_handler`; `jobs.validate_payload` checks only key names and sizes |
| SEC-02 | **Customer emails are stored in plain text** in `orders.customer_email` and `payments.customer_email`. These rows are immutable, and the application role cannot delete them. A GDPR/CCPA erasure request cannot be executed by the application; backups hold copies too. | **HIGH** (compliance) | [FACT] migration `0002`; ADR-004.1-007 |
| SEC-03 | **Report pseudonymisation is weak.** `cust_` + first 12 hex characters of an **unsalted** SHA-256 of the email: anyone holding a candidate email list can re-identify customers. | MEDIUM | [FACT] `analytics/report.py:15-24` |
| SEC-04 | **RLS context is set by the application** (`set_config`). Any code holding a `Database` can set any organisation. RLS protects against *missing filters*, not against a compromised or buggy application. The membership `INSERT` / `UPDATE(role)` policy checks only the organisation, so **privilege-escalation defence is Python-only** (`MANAGE_MEMBERS`). | MEDIUM | [FACT] `database.py`, `0001_tenancy.py` |
| SEC-05 | **The `users` table has no RLS** and `SELECT` is granted to `mervio_app`, so every IdP subject is enumerable by application-role SQL. Depending on the IdP, subjects can embed emails. | LOW–MEDIUM | [FACT] `0001_tenancy.py:140` |
| SEC-06 | **The worker executes as a real human member** (`user_id`). There is no service identity, so audit rows attribute worker actions to a person's ID. | MEDIUM | [FACT] ADR-004.2-002; `handlers.JobContext.audit` |
| SEC-07 | **No credential storage model exists** (`connections.kind` only allows `csv_upload`). OAuth token encryption is undesigned beyond the ADR text. | HIGH (blocks Shopify) | [FACT] `0001_tenancy.py:100` |
| SEC-08 | **No lease heartbeat.** A job running longer than 300 s can be recovered and run **concurrently** by a second worker. Idempotency limits the damage, but duplicate heavy work and a constraint-violation failure (classified permanent) are possible. | MEDIUM (HIGH for long Shopify bulk syncs) | [FACT] `jobs.DEFAULT_LEASE_SECONDS = 300`; no renew or heartbeat function |
| SEC-09 | **No owner transfer or removal, and no role change function**, even though the `UPDATE(role)` grant exists. | LOW | [FACT] `tenancy.py` |
| SEC-10 | **No authentication, rate limiting, CSRF or security headers.** There is no HTTP surface yet. | n/a today; HIGH at API launch | [FACT] |
| SEC-11 | **No secret scanning or dependency and licence scanning in CI** (there is no CI). | MEDIUM | [FACT] |
| SEC-12 | **Error leakage is well controlled.** Logs carry the exception type only; `last_error` is normalised with absolute paths redacted; the redaction key list is tested. | OK | [FACT] |

---

## 10. Jobs & Workers (Mission 004.2 review)

| Aspect | Assessment |
|---|---|
| Queue | Home-grown `jobs` table under full tenancy rules; 5 states enforced by trigger; identity columns immutable [FACT]. Choosing this over procrastinate (ADR-004.2-001) was sound given the RLS requirement. |
| Claiming | Single-statement CTE with `FOR UPDATE SKIP LOCKED` plus the transition to running; the plan is pinned by tests (index, no sort, no seq scan) [FACT]. |
| Retries / backoff | Max 3 by default (up to 20); deterministic 30 s × 2ⁿ capped at 1 h; validation errors never retried; unknown exceptions retried [FACT]. |
| Stale recovery | `recover_stale_jobs` requeues the **same row**; concurrent supervisors are safe; a late worker gets `JobLeaseLost` [FACT]. **Nothing calls it periodically**: it is a `Worker.recover_stale()` method, so the host process must schedule it [FACT/INFERENCE]. |
| Idempotency | Enqueue key unique per (organisation, type); execution idempotency comes from `inputs_sha256` and a single completed run per report [FACT]. |
| Crash behaviour | At-least-once; replay after a simulated crash proven for import and analysis [FACT]. No lease renewal (SEC-08). No SIGTERM handling, so a container stop means a lease wait [FACT]. |
| Audit | Written in the same transaction as each transition; 15 actions constrained by `CHECK` [FACT]. |
| Correlation / logs | `ContextVar` correlation read by the formatter; redaction [FACT]. Not configured in any production entrypoint [FACT]. |
| Purge | Terminal jobs, then expired audit events; owner-only; manually enqueued per organisation [FACT]. |
| Concurrency | 100 jobs × 10 real workers with no duplicates; two tenants in parallel [FACT]. |
| Throughput | ~1,460 jobs/s at 4 workers, measured **with a no-op handler on one tenant** (`jobs_benchmark.py:127`) [FACT]. The "48 MB worker memory" also applies to no-op jobs only [FACT]. |

**Fitness for future workloads**

| Workload | Supported today? | Missing |
|---|---|---|
| CSV imports | yes (local path) | object storage; path confinement (SEC-01) |
| Analysis | yes | chaining after import; memory sizing |
| Shopify webhooks | no | HTTP ingress, HMAC verification, a new job type; coalescing ("sync needed" markers rather than one job per webhook) [INFERENCE] |
| Scheduled syncs / analysis | no | `schedules` table and evaluator (R-29) |
| Periodic purges | no | schedules |
| Multi-tenant processing | only via an explicit session list | dispatcher, service identity (R-26) |
| LLM explanations | no | job type, budget |

**Where it breaks first** [INFERENCE, ordered by likelihood]
1. **Tenant discovery.** Every new organisation requires reconfiguring a worker. This is operationally impossible
   beyond a handful of tenants.
2. **Long jobs versus the fixed 300 s lease.**
   - Measured: a 1M-order snapshot write takes 78 s, and Shopify bulk syncs can take minutes [EXTERNAL].
   - Risk: duplicate concurrent execution.
3. **Memory per analysis.**
   - Measured: 4.6 GB RSS at 1M orders.
   - A worker with several concurrent large jobs would be killed by the OOM killer. This needs a per-worker
     concurrency limit and possibly a large-tenant pool.
4. **Single-transaction import at 1M orders** (78 s; WAL and lock retention, R-24 still open for the import itself).
5. **Audit growth.**
   - Measured: ~1.65 audit rows per job (16,500 audit events for 10,000 jobs, of which 8,400 had been executed).
   - With webhooks this grows quickly; the 365-day retention needs sizing.

PostgreSQL queue throughput is **not** the constraint: measured headroom is several orders of magnitude above the
expected job rate. **There is no evidence justifying Redis, Kafka or a broker.**

---

## 11. API

**State: no API exists** [FACT].
- There is no FastAPI dependency and no `api/` package.
- "FastAPI" appears only in comments.
- The target design is in `MISSION_004_0_ARCHITECTURE.md` §6. It is thorough, and it is compatible with the current
  persistence layer.

### 11.1 Minimum API surface for the MVP

| Area | Routes (minimum) | Notes |
|---|---|---|
| Health | `GET /healthz`, `GET /readyz` (DB and migration head) | no auth |
| Authentication | none hosted: bearer tokens validated from an external OIDC IdP; `GET /v1/me` | `users.idp_subject` exists |
| Organizations | `POST /v1/organizations` (signup), `GET /v1/organizations/current` | `create_organization` exists |
| Memberships | `GET`/`POST`/`DELETE /v1/organization/members` (owner) | MVP may defer invitations to post-MVP |
| Stores | `GET /v1/stores`, `GET`/`PATCH /v1/stores/{id}` (timezone, currency) | needs a timezone column |
| Connections | `POST /v1/stores/{id}/connections/shopify` (install URL), OAuth callback, `GET`, `DELETE` | depends on the Shopify mission |
| Imports | `POST /v1/stores/{id}/imports` (CSV to object storage, then job) | fallback source; fixes SEC-01 |
| Jobs / sync runs | `GET /v1/stores/{id}/jobs`, `GET …/jobs/{job_id}` | onboarding progress |
| Analysis runs / Reports | `POST …/analysis-runs`, `GET …/analysis-runs/{id}`, `GET …/reports/latest`, `GET …/analysis-runs/{id}/report` | projections of the immutable report |
| Insights / Evidence | `GET …/findings`, `GET …/findings/{id}` (evidence, factors, limitations), `GET …/data-quality` | require stable finding IDs (they exist in the LLM context, not in the report) |
| Explanations | `GET …/analysis-runs/{id}/explanation` | after the LLM mission |
| Recommendations | `GET …/recommendations`, `POST …/{id}/dismiss` | MVP status tracking only |
| Actions | **post-MVP** | |
| Audit | `GET /v1/organization/audit-events` (admin) | repository exists |
| Webhooks | `POST /webhooks/shopify/*` (HMAC) | Shopify mission |

### 11.2 Evaluation criteria for the API mission

| Criterion | Requirement |
|---|---|
| Authorization | Role matrix checked server-side on each route; a test per route for insufficient role. |
| Tenant isolation | Organisation derived from membership, never from client input; cross-tenant access returns 404, tested per route (R-01). |
| Errors | RFC 9457 `problem+json` with stable codes; no echo of raw input. |
| Pagination | Opaque cursors (repositories currently use `limit` only, with no cursor) [FACT]. |
| Filtering | Allow-list per resource. |
| Idempotency | `Idempotency-Key` mapped to the existing `jobs.idempotency_key`. |
| Rate limiting | Per user and per organisation; stricter on imports, syncs and LLM calls. No implementation exists; a PostgreSQL-based token bucket is sufficient at MVP scale [INFERENCE]. |
| Versioning | `/v1` plus an OpenAPI contract test. |
| Service identity | Required for the worker and dispatcher (SEC-06). |
| API → worker | Enqueue only (never execute inline); status by polling; `request_id` reused as `correlation_id` (infrastructure ready). |

---

## 12. Integrations

| Integration | Implemented? | Auth | Data available | Webhooks | Incremental | Historical | Rate limits | Normalisation | Failure handling |
|---|---|---|---|---|---|---|---|---|---|
| Shopify | CSV only [FACT] | n/a (file) | orders, lines, refunds (amount only), discounts (order level), products + cost | no | no | full export | n/a | yes (D-041…D-048) | per-row quality issues |
| Stripe | CSV only; **real format unverified** [FACT] | n/a | payments, fees, refunds | no | no | full export | n/a | yes | reconciliation warning vs Shopify refunds |
| Google Ads | CSV only; **real format unverified** [FACT] | n/a | daily campaign spend, clicks, conversions | no | no | full export | n/a | yes | dedup key `campaign|day` |
| Meta Ads | no | — | — | — | — | — | — | — | — (TODO P1) |
| GA4 / traffic | no | — | — | — | — | — | — | — | root cause blind to organic traffic |
| CRM | no | — | — | — | — | — | — | — | — |
| Accounting | no | — | — | — | — | — | — | — | — |
| Other platforms (WooCommerce…) | no | — | — | — | — | — | — | — | the domain model is platform-neutral [FACT] |

**Prioritisation**

| Horizon | Integrations |
|---|---|
| **MVP required** | Shopify API connector (orders, lines, refunds with dates, discounts, products and variants with cost, shop timezone and currency), with CSV import kept as a fallback. |
| **Post-MVP** | Google Ads API (or keep CSV); Meta Ads; Shopify sessions / traffic data if accessible [EXTERNAL: verify ShopifyQL / analytics API availability and scopes]; Stripe only for stores not using Shopify Payments. [INFERENCE: for Shopify-first merchants, fees may be obtainable from Shopify transactions, EXTERNAL to verify.] |
| **Future** | GA4, CRM, accounting, other commerce platforms, shipping carriers. |

Not every integration is needed for a first customer. **Shopify alone is sufficient for an MVP**, as long as the
product openly marks profitability and paid-marketing metrics as unavailable.

---

## 13. Shopify

**Current state** [FACT]
- CSV export only.
- `connections.kind ∈ {csv_upload}`.
- No OAuth, token storage, GraphQL client, webhooks, store timezone, variant entity or refund date.

Everything below that describes Shopify itself is **[EXTERNAL]** and must be verified against current Shopify
developer documentation.

| Area | Requirement for a real merchant | Current gap |
|---|---|---|
| OAuth / install | Authorization-code grant, or managed install with token exchange for embedded apps; offline access token per shop | none implemented |
| Distribution | **Custom distribution** (single merchant, no App Store review) is the fastest pilot path; **public App Store** listing requires review and extra obligations | decision open |
| Scopes | `read_orders`, `read_products`, likely `read_customers`, `read_inventory` (for stockouts); `read_all_orders` is **required for history older than ~60 days** and needs Shopify approval | undecided; this directly affects historical depth |
| Protected customer data | Access to customer fields (email, name) requires a protected-customer-data declaration and approval | **Mervio keys customers by email** (§5.2). Keying by customer GID would reduce the approval level and privacy exposure [INFERENCE] |
| GraphQL Admin API | Versioned quarterly; each version is supported for a limited period, so upgrades are recurring maintenance | no client |
| Rate limits | Cost-based (calculated query cost, leaky bucket); throttling responses must be retried | none |
| Historical sync | Bulk Operations (async JSONL result file) | none; needs object storage for the result file |
| Incremental sync | `updated_at` filters plus webhooks (`orders/create`, `orders/updated`, `refunds/create`, `products/update`…) | the snapshot model is full-copy (ADR required) |
| Mandatory webhooks | `app/uninstalled`; GDPR compliance webhooks (`customers/data_request`, `customers/redact`, `shop/redact`), mandatory for public apps | none; `customers/redact` is **impossible** with immutable email rows (SEC-02) |
| Webhook security | HMAC-SHA256 verification, idempotent processing, replay tolerance | none |
| Refunds | Refund objects carry their own dates, amounts and line breakdown | would resolve D-045 (cohort dating) and parts of D-047 |
| Discounts | Subtotal after discounts, discount allocations per line | would provide the partial-discount evidence D-046 lacks (a dev store is **not** merchant proof) |
| Products / variants | Cost lives on inventory items per variant | the domain has `Product` by SKU with no variant; SKUs can be empty or duplicated in real stores [INFERENCE] |
| Customers | GID, order count; email is protected | identity model change |
| Cancellations | `cancelledAt`, cancel reason | currently counted in revenue (D-044); the API makes a reversal-dated view possible |
| Fulfilment | fulfilment status and timestamps | would close the `shipping_problem` gap |
| Timestamps / timezone | shop IANA timezone | `stores` has no timezone; periods are cut in UTC |
| Currencies | shop currency plus presentment currency (multi-currency storefronts) | single currency per dataset; presentment vs shop money must be decided |
| Retry / idempotency | source IDs (`gid://…`) as `source_record_id` (planned in ADR-004.1-003) | ready conceptually |

**Minimum Shopify vertical slice**
1. **Access**
   - One development store, then one pilot store under custom distribution.
   - An offline token stored encrypted (envelope encryption, key outside the database).
2. **Historical backfill**
   - A Bulk Operation for orders (lines, discounts, refunds with dates, cancellation, customer GID) and products or
     variants (cost).
   - Results stored as raw JSONL in object storage (SHA-256).
   - Normalised into the existing canonical tables, which yields a snapshot.
3. **Store settings**: store timezone and currency written to `stores`.
4. **Chaining**: the analysis job is chained after the sync.
5. **Mandatory webhooks**: `app/uninstalled` and the compliance webhooks, with a documented erasure path.
6. **Scheduling**: a daily scheduled re-sync (full re-sync acceptable for small stores; incremental ADR before
   larger stores).
7. **Verification**: reconciliation test between the API snapshot and the CSV export of the same dev store (already
   an acceptance criterion in the 004.0 handoff).

---

## 14. Frontend / UX

**State: no frontend exists** [FACT].
- No `web/`, `frontend/` or `package.json`.
- The only "UI" is `report.txt` (French) and the CLI.

| Journey step | Status |
|---|---|
| Landing | MISSING |
| Signup | MISSING (no IdP) |
| Create organization | PARTIAL (repository function only) |
| Connect Shopify | MISSING |
| Initial sync | MISSING (CSV import job exists) |
| Processing (progress) | PARTIAL (job status in the database; no API or UI) |
| Business Brief | PARTIAL (`executive_summary` and `report.txt` content exist) |
| Problem | PARTIAL (insights in the report) |
| Evidence | PARTIAL (evidence strings, factors, contributors, series) |
| Root Cause | PARTIAL (engine output) |
| Recommendation | PARTIAL (template text) |
| Action | MISSING |

**UX aspects**
- Loading states, empty states, errors, onboarding, responsive design, dashboard, insight hierarchy, evidence
  visualisation, confidence display, explanations, action approval, settings and account management are all
  **MISSING**.
- Design intent exists: component system, navigation and screen example in `MISSION_004_0_ARCHITECTURE.md` §4–5,
  `research/ux_benchmark.md`.
- The report already carries what the UI needs to render a FACT → EVIDENCE → INFERENCE → RECOMMENDATION hierarchy
  honestly: `data_quality`, `unavailable` reasons and `limitations` [FACT].

**UX risks** [INFERENCE]
- **`profitability_unavailable` will appear for almost every Shopify-only store.** Shipping cost is never available,
  so the Brief must frame it as "connect costs to unlock", not as an alarm.
- **Engine strings are French, unaccented and preformatted.** An English UI cannot reuse them without the LLM or a
  structured-message refactor.

---

## 15. The First "WOW" Experience

**Target**: a merchant connects a real Shopify store, and within minutes sees one prioritised problem with numbers,
evidence, a plain-language explanation, a confidence level and a concrete next step.

| Step | Exists | Must be built |
|---|---|---|
| Real Shopify store | — | pilot merchant (business task); custom-distribution app |
| Connect | — | OAuth install, encrypted token, connection kind `shopify`, onboarding screen |
| Data sync | snapshot writer, canonical tables, job queue, audit [FACT] | GraphQL Bulk client, raw object storage, normaliser, store timezone and currency, lease heartbeat, sync job type |
| Deterministic analysis | engine, `analyze_snapshot`, analysis job [FACT] | chaining sync → analysis; store-timezone periods; baseline alignment (A-1) |
| Anomaly | detector [FACT] | seasonality/YoY guard to avoid a false alarm in the very first session |
| Root cause | 3-factor decomposition [FACT] | product-level cause, new-vs-returning split, discount-rate factor (data available from the Shopify API) |
| Evidence | report fields [FACT] | structured evidence objects, finding IDs, API projections |
| LLM explanation | context + validator + mock [FACT] | provider, explanation job, persistence, budget |
| Recommendation | templates [FACT] | categories aligned with the new factors; dismiss / done status |
| Dashboard | — | Next.js Brief → Problem → Evidence, confidence badge, freshness indicator |

**Why this matters:** the first session is the one that must not show a false alarm or a nonsensical cause. Given
§6.3–6.4, **analytics hardening on real Shopify data must happen before the pilot sees the product**, not after.

---

## 16. SaaS Product Readiness

| Capability | Status | Needed for |
|---|---|---|
| Account creation | MISSING | FIRST USER |
| Authentication (OIDC IdP) | MISSING | FIRST USER |
| Organization | PARTIAL (repository) | FIRST USER |
| Store connection (Shopify) | MISSING | FIRST USER |
| Onboarding | MISSING | FIRST USER |
| Data sync (initial + scheduled) | MISSING | FIRST USER |
| Analysis | IMPLEMENTED (engine + job) | FIRST USER |
| Dashboard | MISSING | FIRST USER |
| Reports (in-app; email/PDF) | PARTIAL (JSON/TXT) | in-app: FIRST USER; email: FIRST PAYING CUSTOMER; PDF: POST-MVP |
| Settings (store tz/currency, members) | MISSING | FIRST USER (store) / FIRST PAYING CUSTOMER (members) |
| Error states & support contact | MISSING | FIRST USER |
| Trial | MISSING | FIRST PAYING CUSTOMER (self-serve) |
| Subscription / plans | MISSING | FIRST PAYING CUSTOMER |
| Billing & invoices | MISSING | FIRST PAYING CUSTOMER |
| Usage limits (stores, sync frequency, LLM calls) | MISSING | FIRST PAYING CUSTOMER |
| Cancellation | MISSING | FIRST PAYING CUSTOMER |
| Upgrade / downgrade | MISSING | POST-MVP |
| Account deletion | MISSING | FIRST PAYING CUSTOMER (and FIRST USER if a public Shopify app, because of `shop/redact`) |
| Data deletion / export | MISSING | FIRST PAYING CUSTOMER |
| SSO/SAML, audit export, multi-org | MISSING | ENTERPRISE |

---

## 17. Billing

**State: MISSING** [FACT]. Nothing in code or docs beyond "billing (plus tard)".

**What a € / month product needs**
- **Plans and entitlements:** a plan table and per-organisation entitlements (number of stores, sync frequency, LLM
  explanations per month, history depth).
- **Trial:** trial start/end dates, trial-to-paid conversion, and a defined data policy for expired trials.
- **Usage metering:** counts of syncs, analyses and LLM calls. The audit log and `jobs` already contain the raw
  events [FACT]; a metering projection is needed.
- **Payment failure:** dunning state, grace period and read-only mode. The engine must keep existing reports
  readable.
- **Cancellation:** end-of-period access, retention period, then an organisation purge. **The purge does not exist
  (R-21).**
- **Invoices / VAT:** delegated to the billing provider. EU VAT handling needs professional review [legal / tax].

**Open decision** [EXTERNAL]
- A public app distributed through the Shopify App Store must generally charge through **Shopify's Billing API**
  (charges appear on the merchant's Shopify invoice).
- Stripe is the natural choice for direct (non-App-Store) sales.
- Custom distribution plus manual invoicing is viable for the very first paying pilot.
- **Verify the current Shopify policy before choosing.** The choice changes the billing mission substantially.

---

## 18. Production Infrastructure

| Item | Status | Notes |
|---|---|---|
| Frontend deployment | NOT READY | no frontend |
| API deployment | NOT READY | no API, no ASGI server, no Dockerfile |
| Workers | NOT READY | no process entrypoint, no SIGTERM handling, no env config, no dispatcher |
| PostgreSQL hosting | LOCAL READY | Docker Compose / disposable cluster; the migration CLI works; roles designed for least privilege [FACT] |
| Object storage | NOT READY | none (ADR-004-002 requires it) |
| Secrets | NOT READY | `.env.example` with empty values only; no secret manager; `Database.from_env` reads `MERVIO_DATABASE_URL` [FACT] |
| Environment configuration | PARTIAL | three DB URLs documented; no settings module for API, worker, LLM or storage |
| Migrations | LOCAL READY | Alembic, up/down tested; no production runbook; no lock-timeout policy for online migrations |
| Backups / restore | NOT READY | none; no PITR plan; no restore drill |
| TLS / domain | NOT READY | none |
| Monitoring / alerting | NOT READY | logs only, not configured |
| Health checks | NOT READY | none (`/healthz` planned) |
| CI/CD | NOT READY | none; R-25 (DB tests silently skipped without the env var) is unmitigated in CI because there is no CI |
| Connection pooling | NOT READY | one connection per `Database` [FACT] |
| Scaling | NOT READY | no horizontal worker story without a dispatcher |

**Readiness levels**
- **LOCAL READY:** engine, CLI, PostgreSQL persistence, job library.
- **STAGING READY:** nothing yet.
- **PRODUCTION READY:** nothing yet.

---

## 19. Observability

**What exists** [FACT]
- Structured JSON logs for the worker path, with correlation IDs, job, organisation and store IDs, durations and
  error codes.
- Aggressive redaction.
- `jobs.queue_statistics` (counts by state and type, age of the oldest queued job).
- A correlated audit trail per execution.

**Operator questions**

| Question | Answerable today? | Gap |
|---|---|---|
| Why did this job fail? | Partially: `last_error_code`, a normalised `last_error`, audit outcome | the full diagnosis exists only in the worker log (R-27) and there is **no log collection**; exception messages are never logged (by design), so a stack-trace-level error tracker is needed |
| Why is this merchant not receiving data? | No | no sync entity, no connection health, no schedule, no freshness indicator |
| Why is analysis slow? | Partially: `duration_ms` in logs and audit | no per-stage timing in production (the benchmark has it), no memory metrics |
| Which tenant is affected? | Yes, in logs and audit (`organization_id`) | needs a log backend to query |
| How many jobs are failing? | Yes, by SQL (`queue_statistics`) | no metrics, dashboards or alerts |

**Missing**
- A log shipping destination.
- An error tracker.
- Metrics:
  - queue depth and age;
  - failures by code;
  - analysis duration and memory;
  - LLM rejection rate;
  - HTTP p95 and 5xx.
- Alert rules:
  - `failed` jobs;
  - queue age;
  - repeated sync failures;
  - 5xx.
- Database monitoring:
  - slow queries;
  - bloat on `jobs` and `audit_events`;
  - autovacuum.
- Uptime checks.
- **The engine's own logging (`mervio.logging_config`) is not the JSON logger.** Unifying the two stacks is needed
  so the API, engine and worker produce one format.

---

## 20. Performance & Scale

### 20.1 Measured evidence [FACT]

All figures: single Apple Silicon machine (18 cores), local PostgreSQL 17.11, **synthetic** data, one run per
size.

| Measurement | 10k | 100k | 1M | 10M |
|---|---|---|---|---|
| Engine end-to-end from CSV | 0.31 s, 70 MB | 3.6 s, 515 MB | 45 s, 3.7 GB | **UNKNOWN** |
| Snapshot write (single transaction) | 0.67 s | 6.6 s | 78 s | UNKNOWN |
| Snapshot re-read | 0.09 s | 1.1 s | 17 s | UNKNOWN |
| Analysis of the loaded dataset | 0.08 s | 0.75 s | 7.8 s | UNKNOWN |
| Peak RSS, persistence path | 99 MB | 611 MB | 4.6 GB | [INFERENCE] ~40–50 GB |
| Canonical table size (orders + lines + payments + refunds) | ~10 MB | ~114 MB | ~1.37 GB | UNKNOWN |
| Report size | ~50 kB | ~49 kB | ~49 kB | [INFERENCE] constant |
| Queue claim p50 / p95 at depth | 0.33 / 0.44 ms | 0.33 / 0.43 ms | 0.47 / 0.68 ms | UNKNOWN |
| Job throughput (no-op handler, 1 tenant) | 1,444/s @ 4 workers | 1,476/s | 1,461/s | — |
| Real import + analysis **as a job** at 1M | **not measured** (the 004.1 handoff asked for it; the 004.2 documents do not claim it) | | | |

### 20.2 Tenant scale [INFERENCE unless stated]

| Tenants | Expected first bottleneck |
|---|---|
| 10 | none technical; the explicit worker session list is manageable manually |
| 100 | worker configuration (no dispatcher); snapshot storage growth if syncs are daily full copies (e.g. 100 stores × 10k orders × 30 snapshots/month ≈ 30 GB/month of canonical rows, extrapolated from the ~10 MB per 10k-order snapshot) |
| 1,000 | dispatcher mandatory; snapshot retention mandatory; connection pooling; audit growth; scheduled-job fan-out |
| 10,000 | canonical table size (partitioning by organisation or store, or incremental snapshots); worker pool sizing by tenant size; vacuum on `jobs`; LLM cost controls |

### 20.3 Order-volume scale

| Orders per store | Status |
|---|---|
| 10k | MEASURED, comfortable |
| 100k | MEASURED, comfortable |
| 1M | MEASURED; 4.6 GB RSS means dedicated worker sizing, and the 78 s import transaction exceeds nothing today but approaches the 300 s lease |
| 10M | UNKNOWN; [INFERENCE] infeasible with the in-memory `Dataset` and full-copy snapshots. ADR-004-002's DuckDB re-evaluation criterion would trigger |

### 20.4 What to benchmark next
1. Import and analysis **as real jobs** at 100k and 1M, with worker RSS and lease timing.
2. Repeated daily snapshots: storage growth and re-read cost with N snapshots per store.
3. Multi-tenant queue behaviour: 1,000 organisations with the dispatcher, fairness and starvation.
4. API read latency for a persisted report (target p95 < 300 ms, from 004.0).
5. Shopify Bulk sync duration and memory on a dev store with a realistic synthetic volume.
6. LLM latency and cost per explanation.

---

## 21. Real-Data Validation

| Data type | Validated with real data? | Nature of evidence |
|---|---|---|
| Real Shopify native CSV export | **NO** | none exists in any public source (D-046 Gate 0) |
| Real merchant data | **PARTIAL**: one register (OH5, 106 orders, 119 lines, USD, 2018–2020) **reconstructed from a PDF** filed in a public legal proceeding | not the original export format |
| Real refunds | 3 refunds (OH5), all full refunds | no partial refunds, no refund dates |
| Real discounts | 30 orders, all 100 % discounts | **no partial discount ever observed** |
| Real variants | NO | — |
| Real customers | NO (no emails in OH5) | — |
| Real orders | 106 (OH5) | one merchant |
| Real timezone behaviour | NO | periods cut in UTC |
| Real currencies | USD only (OH5) | no multi-currency |
| Real edge cases | OH5: cancellations, drafts, zero-value orders | no test orders, no gift cards verified |
| Real Stripe export | NO | format unverified |
| Real Google Ads export | NO | format unverified |
| External public dataset | Kaggle "Shopify" dataset (60k rows), **correctly refused** as unsupported | robustness only |
| UCI Online Retail II (CC BY 4.0) | downloaded, **not converted or tested** | — |
| Synthetic validation | 17 scenarios × 2 profiles × 5 seeds; 1M-order benchmarks | tests code behaviour against the generator's own ground truth, **not merchant semantics** (R-05) |

**Conclusion**: synthetic validation, public-dataset validation and real-merchant validation are not equivalent.
Mervio has substantial synthetic validation, one partial real register, and **no real-merchant validation**. This is
a **production-readiness blocker**.

- A dev-store API sync will provide semantic evidence (discount allocation, refund dates) **but it is not merchant
  evidence**.
- A pilot merchant's data remains required.

---

## 22. Testing / QA

| Test type | State [FACT] |
|---|---|
| Unit | strong (engine, domain, ingestion, LLM, jobs unit, logging) |
| Integration (DB) | strong: real PostgreSQL, real roles, migrations up and down |
| RLS | strong: repository, application-only and raw-SQL levels |
| Concurrency | 100 jobs × 10 workers, two tenants |
| Worker | lifecycle, fault injection, crash replay |
| Analytics | unit tests plus 17 scenario tests plus goldens |
| Import | edge cases, dates, binary formats, external schema refusal, currency absence |
| LLM | context, golden (7), adversarial, provider timeouts, response validation |
| Query plans | claim plan pinned |
| API | **none** |
| Frontend | **none** |
| E2E | **none** |
| Real-data | harness exists; the only real input is OH5 (outside the repository) |
| Performance | benchmark scripts; **not automated**; no regression gate |
| Failure injection | worker-level, good; no DB-failover or network-partition tests |
| CI | **none**: tests run only when someone runs them locally |

**Required before the first real user**
- CI with PostgreSQL and `MERVIO_REQUIRE_DATABASE_TESTS=1`.
- API route tests: success, validation, cross-tenant 404, insufficient role, idempotency.
- Shopify connector tests on recorded responses (dev store).
- API-snapshot ↔ CSV reconciliation on the same dev store.
- **Pilot export or sync through the validation harness.**
- Webhook HMAC tests.
- A minimal E2E test (sign in → connect → brief).

**Required before the first paying customer**
- LLM rejection-rate measurement on the scenarios and goldens.
- Billing-state tests.
- Organisation deletion and data-erasure tests.
- Backup restore drill.
- Security tests: headers, rate limiting, secret scan, dependency and licence scan.
- Accessibility checks (axe) on the core screens.

**Required for production scale**
- Nightly performance regression (100k).
- Multi-tenant dispatcher load test.
- Snapshot-growth and retention tests.
- Chaos: worker kill during sync; DB restart.
- Shopify API version-upgrade test suite.

---

## 23. Privacy / Security / Compliance

**Not legal advice.** Items marked ⚖️ require review by a qualified professional.

| Topic | Technical/product requirement | State |
|---|---|---|
| Customer data (emails) | Minimise: key customers by platform ID and do not persist email unless needed; if persisted, make it erasable (separate mutable PII table or crypto-shredding) | **violated today** (SEC-02) |
| Order data | Tenant-isolated, encrypted at rest (hosting), retention policy | isolation OK; retention undefined (R-21) |
| Pseudonymisation in deliverables | Salted/keyed hash per organisation | unsalted (SEC-03) |
| Store data | Tenant isolation | OK |
| Credentials / OAuth tokens | Envelope encryption, key outside the DB, never logged, revoked on uninstall | not designed in code |
| Deletion | Organisation purge (DB + objects + tokens) as a traced job; customer-level redaction | missing |
| Retention | Documented periods for raw files, snapshots, reports, jobs, audit | only operational defaults for jobs and audit; **004.2 explicitly flags these for GDPR review** |
| Backups | Encrypted; retention aligned with deletion promises ⚖️ | none |
| Privacy policy, Terms, DPA ⚖️ | Required before handling a merchant's customer data | none |
| GDPR ⚖️ | Controller/processor roles (Mervio is likely a processor for merchant customer data [INFERENCE]), records of processing, sub-processor list (hosting, LLM vendor), data-subject request handling | none |
| CCPA / US state privacy ⚖️ | Service-provider terms; deletion handling | none |
| Shopify platform obligations [EXTERNAL] ⚖️ | Protected customer data requirements, compliance webhooks, app privacy details | none |
| Data residency ⚖️ | EU hosting likely preferred (ADR mentions "UE probable") | undecided |
| LLM sub-processing ⚖️ | Vendor retention and training terms; only aggregated, PII-masked context sent (already the design) | design OK, vendor undecided |
| Security certifications | None claimed (correctly, per the 004.0 architecture) | — |

---

## 24. Technical Debt

| ID | Problem | Location | Severity | Impact | Recommendation | Timing |
|---|---|---|---|---|---|---|
| TD-01 | Customer emails stored in plain text in immutable, non-deletable canonical rows | `0002_snapshots` (`orders`, `payments`) | CRITICAL | GDPR erasure impossible; blocks Shopify `customers/redact` | Key customers by platform ID; move PII to an erasable table or remove it; ADR + new migration | before first real user |
| TD-02 | No real-merchant validation of the revenue contract | engine / D-046 | CRITICAL | numbers may be wrong for real stores | pilot export + dev-store API reconciliation | before first real user |
| TD-03 | Import payload accepts arbitrary filesystem paths | `workers/handlers.py` | HIGH | cross-tenant file read once the API exists | object-storage keys scoped by organisation; refuse raw paths | API mission |
| TD-04 | No lease heartbeat or renewal | `persistence/jobs.py`, `workers/worker.py` | HIGH | duplicate concurrent execution of long jobs | heartbeat extending `lease_expires_at` by the lock holder | before Shopify sync |
| TD-05 | Worker has no process entrypoint, SIGTERM handling, env config or periodic stale recovery | `workers/` | HIGH | cannot be deployed | `mervio worker` command | platform baseline |
| TD-06 | Explicit tenant list; no dispatcher or service identity | `workers/worker.py`, ADR-004.2-002 | HIGH | does not scale past a handful of tenants; audit attributes actions to a human | dispatcher + service identity | Shopify install mission |
| TD-07 | No schedules, no job chaining | R-29 | HIGH | no continuous product | `schedules` table + chaining | Shopify install mission |
| TD-08 | Full-copy snapshot per import | ADR-004.1-004 | HIGH (at scale) | storage growth with daily syncs | incremental snapshot ADR + retention | before >10 stores on daily sync |
| TD-09 | No CI; DB tests skip silently without the env var | repo | HIGH | regressions undetected (R-25) | CI with PG17 + required DB tests | platform baseline |
| TD-10 | Root-cause baseline mismatch with anomaly baseline (A-1) | `analytics/insights.py`, `root_cause.py` | HIGH | explanation may contradict the anomaly | align comparison bases; engine version bump; goldens | analytics hardening |
| TD-11 | Paid conversion = all orders / paid clicks (A-2) | `analytics/kpi.py`, `root_cause.py` | HIGH | misattribution of organic changes | separate paid/organic orders or relabel; show "not observed" factors | analytics hardening |
| TD-12 | No seasonality/YoY guard | `analytics/anomaly.py` | HIGH | false alarms on seasonal stores | YoY comparison guard when ≥ 12 months | analytics hardening |
| TD-13 | UTC period boundaries; no store timezone | `stores` schema, `analytics/periods.py` | HIGH | periods mismatch the merchant's view | `stores.timezone`; tz-aware windowing | Shopify data connector |
| TD-14 | Untyped report dict; evidence as preformatted strings | `analytics/report.py`, `insights.py` | MEDIUM | fragile API/UI/LLM consumption | typed schema (report contract version) + structured evidence | API mission |
| TD-15 | Engine strings in French, unaccented, hardcoded | `analytics/`, `reporting/` | MEDIUM | blocks non-French UI | message keys + parameters, or render in the UI/LLM | before frontend |
| TD-16 | Hardcoded thresholds outside `config.py`; unused `refund_reconciliation_tolerance` | `insights.py:137,152`, `health.py`, `root_cause.py`, `pipeline.py:130`, `config.py:73` | MEDIUM | violates the stated rule; not tenant-configurable | move into config | analytics hardening |
| TD-17 | Unsalted SHA-256 customer pseudonyms | `analytics/report.py:15` | MEDIUM | re-identifiable | keyed hash per organisation | before first real user |
| TD-18 | Two logging stacks; JSON logging not wired | `logging_config.py`, `observability/` | MEDIUM | inconsistent operational logs | single configuration at process start | platform baseline |
| TD-19 | Raw source files not retained | ADR-004.1-010 | MEDIUM | provenance not replayable | object storage | API / Shopify mission |
| TD-20 | Single-transaction import (78 s at 1M) | `persistence/snapshots.py` | MEDIUM | WAL/lock retention (R-24) | staged import with a sealed switch | before large stores |
| TD-21 | In-memory engine (4.6 GB at 1M) | `domain/models.py`, `analytics/` | MEDIUM | worker sizing; 10M infeasible | per-worker concurrency limits; DuckDB re-evaluation per ADR-004-002 | scale |
| TD-22 | `Dataset.window()` O(n) per period | `domain/models.py` | LOW | ~38 % of engine time at 1M | index by period when measured necessary | scale |
| TD-23 | No variant entity; products keyed by SKU | `domain/models.py` | MEDIUM | real stores have empty or duplicate SKUs | variant/inventory-item identity | Shopify data connector |
| TD-24 | Email-based customer identity; guests = one customer per order | `ingestion/shopify.py:115` | MEDIUM | biased repeat/new-customer KPIs | platform customer ID | Shopify data connector |
| TD-25 | No data-freshness guard | `analytics/pipeline.py` | MEDIUM | stale reports look current | freshness metadata + warning | analytics hardening |
| TD-26 | No owner transfer, no role change, no org deletion | `persistence/tenancy.py` | MEDIUM | account lifecycle incomplete | functions + audit actions (new migration for `CHECK`) | API / commercial mission |
| TD-27 | `users` table readable by the app role without RLS | `0001_tenancy.py` | LOW | subject enumeration | restrict via view or policy on own row | API mission |
| TD-28 | Stale docs (ROADMAP, TODO, ARCHITECTURE, DATA_MODEL path) | `docs/` | LOW | misleading planning (R-18) | update or archive | platform baseline |
| TD-29 | Audit action list is a `CHECK` constraint; every new action needs a migration | `0005_audit.py` | LOW | friction (intentional) | keep; batch additions per mission | ongoing |
| TD-30 | Stripe and Google Ads CSV formats never verified on real exports | `ingestion/stripe.py`, `google_ads.py` | MEDIUM | fallback imports may fail on real files | verify with pilot files, or deprioritise | post-MVP |
| TD-31 | Pagination by `limit` only | `persistence/*` list functions | LOW | API needs cursors | cursor helpers | API mission |
| TD-32 | Order discounts not allocated to lines | `domain/models.py` (`line_revenue` gross) | MEDIUM | product margin and contributors overstated when discounted | use Shopify discount allocations | Shopify data connector |

---

## 25. Blockers

### 25.1 Blockers for the first user
*(One real merchant using Mervio without a developer.)*
1. No authentication or identity provider.
2. No API.
3. No frontend: signup, onboarding, brief, evidence.
4. No Shopify connector: OAuth, token storage, sync.
5. No hosted environment. A merchant's data cannot live on a developer laptop.
6. No schedules or job chaining; no deployable worker process.
7. No real-merchant validation of the numbers (D-046 condition).
8. Customer PII model incompatible with erasure (TD-01), especially under Shopify compliance webhooks.
9. Analytics false-alarm and misattribution risks (TD-10, TD-11, TD-12) in the very first report.
10. No store timezone (TD-13).
11. No privacy policy or terms covering merchant customer data ⚖️.

### 25.2 Blockers for the first paying customer
*(Items beyond 25.1.)*
1. No billing mechanism, or no decision on one (Shopify Billing API vs Stripe vs manual invoice).
2. No plans, entitlements or usage limits.
3. No cancellation path or data-retention-after-cancellation policy.
4. No organisation or account deletion.
5. No DPA or sub-processor list ⚖️.
6. No production LLM explanation, if the paid promise includes AI explanations.
7. No support channel or operator tooling to diagnose a merchant's sync.
8. No backups with a tested restore.

### 25.3 Blockers for production
1. No CI (R-25).
2. No deployment artefacts, no secrets management, no TLS or domain.
3. No log collection, error tracking, metrics or alerting.
4. No health or readiness endpoints.
5. No lease heartbeat (TD-04) and no graceful shutdown.
6. No connection pooling.
7. Import path confinement (TD-03).
8. No backup/PITR or restore procedure.
9. No migration runbook for a live database.

### 25.4 Blockers for scale
1. Explicit worker tenant list and no dispatcher (TD-06).
2. Full-copy snapshots and no snapshot retention (TD-08).
3. In-memory analysis of 4.6 GB at 1M orders (TD-21); 10M untested.
4. Single-transaction imports (TD-20).
5. Audit and jobs growth, and vacuum, at high webhook volume.
6. No performance regression gate.
7. LLM cost controls per organisation.
8. Shopify API rate-limit handling across many stores.

---

## 26. Roadmap Reassessment

The previous plan was: 004.3 API v1 → 004.4 Shopify GraphQL → 004.5 Frontend → 004.6 Production LLM + Actions.

| Previous mission | Verdict | Reasoning |
|---|---|---|
| 004.3 — API v1 | **MODIFY + move after the Shopify data core** | The API exposes stores, connections, snapshots and sync runs. The Shopify data model changes all of them: store timezone, connection kind, credential reference, customer identity, incremental snapshot. Building the API first means reworking it. The dispatcher, schedules and service identity move to the Shopify install mission, where they are needed. |
| 004.4 — Shopify GraphQL | **SPLIT** | (a) *Data connector core*: dev store, admin token, Bulk backfill, normalisation, timezone, variants, refund dates, customer ID, reconciliation. No HTTP needed; resolves the largest data-semantics risk earliest. (b) *Install & continuous sync*: OAuth, token encryption, webhooks (including compliance), dispatcher, schedules, chaining, incremental strategy. Needs the API. |
| 004.5 — Frontend | **KEEP (refocused)** | Read-only Brief → Problem → Evidence, onboarding shell, data-quality/freshness. No action UI. Can run on dev-store or CSV snapshots, which gives the first visible product. |
| 004.6 — Production LLM + Actions | **SPLIT + DEFER Actions** | LLM explanation is part of the MVP "wow". Actions (approval → execution) have no data source to write to yet, are high-risk and are not needed to prove value; keep only dismiss / mark-done on recommendations. |
| *(new)* Platform baseline | **ADD, first** | Nothing is deployable and nothing is CI-verified. Small, unblocks everything. |
| *(new)* Analytics hardening | **ADD** | The scenario gaps and the A-1/A-2/seasonality defects would undermine the first real session. |
| *(new)* Production environment & data protection | **ADD** | Real merchant data requires hosting, backups, secrets, erasure and legal documents before the pilot. |
| *(new)* Commercial SaaS | **ADD** | Billing, plans, lifecycle. |

---

## 27. Revised Roadmap

Sizes are relative (S/M/L). No calendar estimates are given: team capacity is **[UNKNOWN]**.

> **Parallel business track (starts now, not a code mission):** recruit 1–3 pilot merchants; open a Shopify
> Partner account and a development store; obtain a native CSV export from a pilot for
> `scripts/validate_real_export.py`; decide on distribution (custom vs public app) and billing channel; engage
> legal review for terms, privacy policy and DPA.

### Mission 004.3 — Platform Baseline (S)
- **Objective:** make the existing system continuously verified and runnable as processes.
- **Why now:** there is no CI, and the worker cannot be started as a service. Every later mission depends on both.
- **Dependencies:** none.
- **Deliverables:**
  - CI pipeline:
    - PostgreSQL 17 service;
    - `MERVIO_REQUIRE_DATABASE_TESTS=1`;
    - secret scan;
    - dependency and licence check.
  - `mervio worker` process entrypoint:
    - env configuration;
    - JSON logging wired;
    - SIGTERM graceful stop;
    - periodic `recover_stale`.
  - **Lease heartbeat** (TD-04).
  - Application container image (worker + migrations).
  - Unified logging configuration.
  - Stale docs updated (TD-28).
  - `docker compose` running PostgreSQL + worker.
- **Acceptance criteria:**
  - CI green: 1097+ tests, 0 skipped.
  - The container worker processes a CSV import and an analysis end to end against compose PostgreSQL.
  - A killed worker's job is recovered by another without duplicate snapshots (existing tests plus a heartbeat
    test).
  - A job longer than the lease is not stolen.
- **Unlocks:** safe iteration, and staging deployments later.
- **Remaining afterwards:** API, Shopify, UI, LLM, production environment.

### Mission 004.4 — Shopify Data Connector Core (M/L)
- **Objective:** produce canonical snapshots from the Shopify Admin GraphQL API of a development store, with
  correct semantics.
- **Why now:** this is the largest correctness risk (D-045, D-046, D-047, timezone, identity). It changes the
  schemas the API will expose, and it needs no HTTP surface.
- **Dependencies:** 004.3.
- **Deliverables:**
  - **Decision and schema**
    - ADR on customer identity: platform ID; email not persisted or erasable (**fixes TD-01**).
    - ADR on variants and cost.
    - ADR on presentment vs shop currency.
    - ADR on refund dating (processing date alongside cohort).
    - Migration: `stores.timezone`, `stores.platform_domain`, connection kind `shopify`, credential reference
      (encrypted-secret abstraction, dev token only).
  - **Connector**
    - GraphQL client: cost-aware throttling, API version pinning.
    - Bulk Operation backfill.
    - Raw JSONL in an object-storage abstraction (local filesystem driver + S3-compatible interface).
    - Normaliser to `domain.models`.
    - `sync` job type.
  - **Analytics support:** timezone-aware period windowing in the engine (engine version bump, goldens regenerated
    with written justification).
  - **Verification**
    - Recorded-response tests.
    - Reconciliation test API snapshot ↔ CSV export of the same dev store.
- **Acceptance criteria:**
  - Dev-store API snapshot and CSV export reconcile to the cent on "CA avant ajustements", orders, units and
    refunds (with a documented treatment of known differences).
  - Refund processing dates available.
  - A partial discount observed on the dev store (documented as **not** merchant proof).
  - No token and no email in logs, audit or snapshots.
  - Periods follow the store timezone.
  - All existing contracts preserved or explicitly versioned.
- **Unlocks:** real-data semantics; schemas stable enough for the API.
- **Remaining afterwards:** install flow, continuous sync, API, UI.

### Mission 004.5 — API v1 (L)
- **Objective:** an authenticated, tenant-safe HTTP API over persisted data and the job queue.
- **Why now:** both the frontend and the Shopify install flow need it.
- **Dependencies:** 004.3, 004.4.
- **Deliverables:**
  - **Platform**
    - FastAPI.
    - OIDC token validation (IdP chosen by ADR).
    - Connection pool (transaction-mode compatible).
    - `problem+json`, cursor pagination, `Idempotency-Key`.
    - Rate limiting (PostgreSQL-based).
    - `/healthz` and `/readyz`.
    - `request_id` → `correlation_id`.
  - **Resources:** `/v1/me`, organisation signup, stores (timezone and currency), CSV imports via object storage
    (**fixes TD-03**), jobs, analysis runs, reports, findings/evidence/data-quality projections (typed report
    schema, TD-14), audit read.
  - **Contract:** OpenAPI contract test.
  - **Hardening:** the `users` visibility fix (TD-27).
- **Acceptance criteria:**
  - Every route is tested for success, validation, cross-tenant 404, insufficient role and idempotency.
  - No route computes a KPI (AST test).
  - Persisted-report read p95 < 300 ms.
  - The OpenAPI contract is frozen.
- **Unlocks:** frontend; install flow.
- **Remaining afterwards:** UI, OAuth install, schedules, LLM.

### Mission 004.6 — Frontend v1: Brief → Problem → Evidence (M/L)
- **Objective:** a read-only web product over persisted reports.
- **Why now:** the first visible product; validates the information architecture with pilots early.
- **Dependencies:** 004.5.
- **Deliverables:**
  - Next.js app: auth; organisation creation; store selector.
  - Screens:
    - **Business Brief:** top findings, KPI cards with quality and source, freshness indicator.
    - **Problem detail:** fact, evidence, factors, limitations, confidence badge, recommendation, dismiss.
    - **Data & quality** page.
    - **Job/import progress.**
    - **CSV upload fallback.**
  - Empty, loading and error states.
  - Types generated from OpenAPI.
  - Localisation approach decided (TD-15).
- **Acceptance criteria:**
  - Every displayed number maps to an API field (test).
  - FACT / INFERENCE / RECOMMENDATION are visually distinct.
  - Synthetic data is always badged.
  - axe: no critical violation.
  - Keyboard navigation works.
  - E2E: sign in → brief → problem.
- **Unlocks:** **M1 — FIRST VISIBLE PRODUCT** (staging, dev-store or CSV data).
- **Remaining afterwards:** real store connection, continuous data, explanation, production environment.

### Mission 004.7 — Shopify Install & Continuous Sync (L)
- **Objective:** a merchant connects their own store and data stays fresh without intervention.
- **Why now:** it turns the visible product into a usable one.
- **Dependencies:** 004.4, 004.5, 004.6.
- **Deliverables:**
  - **Install:** OAuth install (custom distribution first); encrypted offline tokens (envelope encryption);
    scopes decision, including `read_all_orders` [EXTERNAL].
  - **Webhooks:** `app/uninstalled`; compliance webhooks with an implemented erasure path; HMAC verification;
    webhook → coalesced sync marker.
  - **Orchestration:**
    - service identity;
    - multi-tenant dispatcher (**TD-06**);
    - `schedules` (**TD-07**);
    - chaining sync → analysis;
    - incremental-snapshot ADR and implementation, plus snapshot retention (**TD-08**).
  - **UI:** "Connect Shopify" onboarding with initial sync progress.
- **Acceptance criteria:**
  - A dev-store install through the UI reaches a first report without developer action.
  - Daily re-sync runs from schedules.
  - An uninstall revokes the token and stops jobs.
  - A `customers/redact` test proves erasure.
  - The dispatcher serves 1,000 synthetic organisations fairly (benchmark).
- **Unlocks:** continuous data for pilots.
- **Remaining afterwards:** analytics quality, explanation, production hosting.

### Mission 004.8 — Analytics Hardening on Shopify Data (M)
- **Objective:** remove known false alarms and misattributions before a merchant sees them.
- **Why now:** the first session must be credible.
- **Dependencies:** 004.4 (richer data); may run in parallel with 004.7.
- **Deliverables:**
  - **Fixes:** align the root-cause base with the anomaly baseline (TD-10); paid vs organic separation or explicit
    relabelling (TD-11); YoY/seasonality guard (TD-12); freshness metadata (TD-25).
  - **New signals:**
    - discount-rate metric and factor;
    - new-vs-returning revenue decomposition;
    - product-level cause (top-contributor concentration);
    - stockout signal if inventory scope is granted;
    - explicit "not observed" factors.
  - **Evidence and configuration:** structured evidence objects; thresholds moved into config (TD-16); line-level
    discount allocation (TD-32).
  - **Scenarios:** scenario expectations promoted from `KNOWN_GAP` to `MATCH` where the new data allows it.
- **Acceptance criteria:**
  - `seasonal_business` produces no false alarm.
  - At least 3 current `KNOWN_GAP` expectations become `MATCH` (discount, product failure, churn or stockout,
    depending on data).
  - 170/170 still pass.
  - Every engine contract change is versioned, with goldens regenerated and justified.
- **Unlocks:** trustworthy first reports.
- **Remaining afterwards:** explanation, production.

### Mission 004.9 — Production LLM Explanation (M)
- **Objective:** a validated plain-language explanation for each report.
- **Why now:** the explanation completes the "wow" and localises the output.
- **Dependencies:** 004.5, 004.8 (stable evidence); UI from 004.6.
- **Deliverables:**
  - **Provider and job:** a real provider behind `LLMProvider`; an `explanation` job; an `explanations` table
    with provenance.
  - **Controls:** per-organisation budget; opt-in.
  - **Measurement:** validator rejection-rate dashboard.
  - **Product:** UI explanation panel with deterministic fallback; language support in the validator.
- **Acceptance criteria:**
  - The existing LLM suite passes with the real provider behind a recorder.
  - Rejection rate is measured and published on the 17 scenarios.
  - An unavailable explanation never affects the report.
  - No prompt or response is logged.
  - Cost per explanation is measured.
- **Unlocks:** the complete wow experience.
- **Remaining afterwards:** production hosting, commercial layer.

### Mission 004.10 — Production Environment & Data Protection (M)
- **Objective:** a place where real merchant data may live.
- **Why now:** this is a precondition for any pilot on real data.
- **Dependencies:** 004.3; in practice alongside 004.7–004.9.
- **Deliverables:**
  - **Hosting:** managed PostgreSQL with PITR; object storage; secret manager/KMS; TLS/domain.
  - **Operations:**
    - staging and production environments with a migration runbook;
    - log shipping, error tracking and core metrics;
    - alerts (failed jobs, queue age, sync failures, 5xx);
    - backup restore drill.
  - **Data protection:** organisation and store purge job (R-21); keyed pseudonymisation (TD-17); retention
    policy document.
  - **Legal:** privacy policy, terms and DPA drafts ⚖️.
- **Acceptance criteria:**
  - A restore drill is documented.
  - An organisation purge removes DB rows, objects and tokens, and is audited.
  - Alerts fire in staging tests.
  - No secret is present in images or the repository.
  - Legal documents are reviewed ⚖️.
- **Unlocks:** **M2 — FIRST REAL END-TO-END PRODUCT** (with 004.7–004.9 and a pilot merchant passing validation).
- **Remaining afterwards:** commercial layer.

### Mission 005.0 — Commercial SaaS (M)
- **Objective:** charge merchants and manage their lifecycle.
- **Why now:** it turns pilots into customers.
- **Dependencies:** M2; the billing-channel decision.
- **Deliverables:**
  - **Billing:** Shopify Billing API or Stripe (per decision); plans and entitlements; trial; usage limits
    (syncs, LLM); dunning and read-only mode; cancellation with retention-then-purge.
  - **Account:** account deletion; member invitations and role management; owner transfer (TD-26).
  - **Communication:** weekly email brief; support contact and operator console (per-organisation sync and job
    diagnostics).
- **Acceptance criteria:**
  - Trial → paid → cancel → purge is tested end to end.
  - Limits are enforced server-side.
  - A failed payment degrades access without data loss.
  - An operator can answer the five §19 questions for any tenant.
- **Unlocks:** **M3 — FIRST PAYING-CUSTOMER-READY PRODUCT.**
- **Remaining afterwards:** post-MVP scope.

### Post-MVP (deferred)
- Action layer (draft → approval → execution, low-risk actions first).
- Google Ads / Meta Ads API connectors.
- PDF reports.
- Multi-store organisations.
- Upgrade/downgrade flows.
- Cohorts and LTV.
- Forecasting.
- Public App Store listing (if custom distribution was used first).
- SSO (enterprise).

---

## 28. Vertical Slice Milestones

| Milestone | Definition | Reached after | Evidence of completion |
|---|---|---|---|
| **M1 — FIRST VISIBLE PRODUCT** | A signed-in user on **staging** sees Business Brief → Problem → Evidence rendered from a persisted, job-produced report (dev-store API data or CSV upload) | 004.3 → 004.4 → 004.5 → 004.6 | E2E test sign in → brief → problem; every number traceable to a report field |
| **M2 — FIRST REAL END-TO-END PRODUCT** | One **pilot merchant** installs the app on their own store, the initial and daily syncs run, reports and validated explanations appear, with no developer action, **in production**, with numbers validated against the pilot's own data | M1 + 004.7 + 004.8 + 004.9 + 004.10 + pilot validation | pilot export/sync passes `validate_real_export.py` (D-046 condition); a week of unattended scheduled operation |
| **M3 — FIRST PAYING-CUSTOMER-READY PRODUCT** | A merchant can start a trial, pay, use, cancel and have their data deleted, all self-serve, under reviewed legal terms | M2 + 005.0 | trial→paid→cancel→purge E2E; legal review complete ⚖️ |

**Note:** a *manually invoiced* concierge pilot can be charged between M2 and M3 if legal terms are in place. That
is a business decision, not a product milestone.

---

## 29. Mervio MVP Definition

| Question | MVP answer |
|---|---|
| **Who is the user?** | The owner or operator of **one Shopify store** (small to mid-size, non-technical), with at most a few teammates. |
| **What data do they connect?** | Their Shopify store: orders, line items, discounts, refunds (with dates), products and variants (with cost when entered), shop timezone and currency. CSV upload for Google Ads is kept as an optional extra. |
| **What does Mervio calculate?** | The existing deterministic KPI set on closed weekly and monthly periods in the store's timezone: revenue before adjustments, orders, AOV, units, refunds and refund rate, discount rate, new vs returning customers, product performance and margin (where cost exists), and the health score with explicit exclusions. |
| **What problem does it detect?** | Material revenue, order, AOV and refund anomalies (seasonality-guarded); discount surges; low-margin products; customer concentration; missing-data situations that block profitability. |
| **How does it explain the problem?** | A deterministic decomposition (traffic where observed, conversion, AOV, discount, new vs returning, product contributors) labelled as **inference**, plus a validated LLM explanation in plain language that cannot add numbers or causal claims. |
| **What evidence does it show?** | The numbers behind each finding (observed vs expected, change, z-score), the contributing products, the series chart, data quality and freshness, the factors that were **not observed**, and the stated limitations. |
| **What recommendation does it provide?** | One deterministic recommendation per finding, tied to its category (acquisition, checkout, merchandising, discount policy, product, retention), with impact only when it is computable. |
| **What can the user do next?** | Mark a recommendation done or dismiss it with a reason; open the relevant object in Shopify admin; receive the weekly brief by email (at the M3 boundary). |

**Explicitly out of the MVP**
- Executing actions.
- Multi-platform connectors.
- Profitability beyond what the data supports.
- PDF reports.
- Multi-store portfolios.
- Forecasting.

---

## 30. Production-Ready Definition

Mervio is production-ready when **all** of the following hold.

**Correctness**
1. The revenue contract is validated on at least one real merchant's data through the harness. Any deviation is
   documented, and a regression test is added for each one.
2. The engine contract is versioned. Goldens and scenario suites pass in CI, with no unexplained `KNOWN_GAP` in
   the MVP's detected-problem list.
3. Periods follow the store timezone; data freshness is displayed.

**Security & privacy**
1. Every API route has a cross-tenant 404 test and an insufficient-role test, passing in CI.
2. No customer PII persists outside an erasable store; the compliance-webhook erasure path is tested.
3. OAuth tokens are encrypted with a key outside the database, never logged, and revoked on uninstall (tested).
4. The application connects as a non-superuser, non-BYPASSRLS role (existing check), and the worker runs under a
   service identity.
5. CI runs a secret scan and dependency/licence scans; security headers and rate limits are active.
6. Terms, privacy policy, DPA and sub-processor list are published after legal review ⚖️.

**Reliability**
1. CI runs all tests, DB tests included, and a skipped DB test fails the build.
2. Jobs have lease heartbeats, graceful shutdown, a dispatcher and schedules. A killed worker causes no duplicate
   snapshot or report (tested).
3. Managed PostgreSQL has PITR and a documented, rehearsed restore. The organisation purge is tested.
4. A migration runbook exists for a live database, including lock timeouts.

**Operability**
1. Logs are centralised in one JSON format with correlation IDs, and an error tracker is configured.
2. Metrics and alerts exist for queue age, failed jobs, sync failures, 5xx, analysis duration and memory, and the
   LLM rejection rate.
3. An operator can answer the five §19 questions for any tenant in minutes.
4. Health and readiness endpoints are wired to the platform.

**Performance**
1. Persisted-report API p95 < 300 ms, measured on staging.
2. Import and analysis as jobs are benchmarked at 100k and 1M orders; worker memory limits are set from those
   measurements.
3. The dispatcher is load-tested at the target tenant count.

**Product**
1. A merchant can sign up, connect Shopify, see a first brief, and cancel with data deletion, all without developer
   intervention.

---

## 31. Residual Risks

| Risk | Status after this audit |
|---|---|
| R-01 tenant leakage | mitigated at persistence; **open at HTTP** |
| R-05 synthetic mistaken for merchant validation | **open, and material**: 170/170 includes `KNOWN_GAP` passes |
| R-12 LLM out of contract in production | open (mock only) |
| R-13 LLM cost | open |
| R-14 store timezone ignored | open (no column) |
| R-15 partial discount / refund date unproven | open; the dev store helps, the pilot is required |
| R-21 retention & deletion | open; **aggravated** by the plaintext email finding (SEC-02) |
| R-24 long import transaction | open for the import itself |
| R-25 DB tests skipped in CI | open (no CI) |
| R-26 dispatcher | open |
| R-27 diagnostics only in worker logs | open (no log collection) |
| R-28 fsync saturation | accepted; not a near-term constraint |
| R-29 no scheduling | open |
| NEW: lease without heartbeat | open (SEC-08, TD-04) |
| NEW: path-based import payload | latent (SEC-01, TD-03) |
| NEW: Shopify platform approvals (`read_all_orders`, protected customer data) [EXTERNAL] | unknown timeline; can limit historical depth and customer analytics |
| NEW: French-only engine text | open (TD-15) |
| NEW: bus factor and team capacity | **UNKNOWN** from the repository |
| NEW: market fit and pricing | **UNKNOWN**: no user research in the repository (the UX benchmark states it has none) |

---

## 32. Open Decisions

1. **Distribution:** custom-distribution Shopify app for pilots, public App Store later, or App Store first
   [EXTERNAL implications].
2. **Billing channel:** Shopify Billing API vs Stripe vs manual invoicing for pilots.
3. **Identity provider** (OIDC) for the API and the frontend.
4. **Customer identity & PII:** platform customer ID as the key; whether email is ever stored; erasure mechanism.
5. **Scopes:** `read_all_orders`, `read_customers`, `read_inventory` (affects features and approvals).
6. **Snapshot strategy for continuous sync:** full copy with retention vs incremental/differential (ADR needed
   before scaling).
7. **Hosting provider and region** (EU data residency) ⚖️.
8. **LLM vendor and model**, data-retention terms ⚖️.
9. **Product language(s)** and the localisation mechanism (engine message keys vs LLM rendering).
10. **Retention periods** for raw files, snapshots, reports, jobs and audit ⚖️.
11. **Presentment-currency handling** for multi-currency storefronts.
12. **Whether Stripe and Google Ads CSV support** remains in the MVP surface (formats unverified).
13. **Order-scope definition (D-044)** once refund and cancellation dates are available from the API: keep "before
    adjustments" only, or add an "after adjustments" view.

---

## 33. Final Recommendation

### 33.1 If a real merchant received Mervio tomorrow, how far could they go without a developer?

**Nowhere** [FACT].
- There is no signup, no hosted service, no UI and no API. The merchant cannot even create an account.
- The only usable path is **operator-assisted**:
  1. the merchant sends Shopify, Stripe and Google Ads CSV exports;
  2. a technical operator runs `python -m mervio.analytics validate` and then `analyze` locally;
  3. the operator returns `report.txt` (French) and `report.json`.
- Even that path presents numbers that have **never been validated on a native merchant export**. D-046 requires
  `scripts/validate_real_export.py` to pass on the pilot's export before any number is presented.
- The persisted multi-tenant path (snapshots, jobs, reports) can only be driven from Python code. No command
  provisions a user or organisation, enqueues a job or starts a worker.

### 33.2 What exactly prevents charging that merchant today?
1. **No product to sell:** no self-serve access, no hosted environment, no continuous data (§25.1).
2. **Unvalidated numbers:** no native merchant export has passed the harness (D-046, R-15).
3. **Unsafe data handling for a paid relationship.**
   - Customer emails cannot be erased (SEC-02).
   - There is no organisation deletion, no backups, and no terms, privacy policy or DPA ⚖️.
4. **No billing mechanism or decision** (§17).
5. **Analytical credibility risks.** A seasonal store receives a false alarm, and organic-traffic changes are
   reported as conversion problems (§6.3–6.4).

### 33.3 The shortest technically sound path
1. **Now, in parallel (business track):**
   - obtain a pilot's native CSV export and run the harness;
   - open a Shopify Partner account and a dev store;
   - start the legal review.
   - This alone would permit a **manually delivered, manually invoiced diagnostic** once the harness passes and
     terms exist.
2. **004.3 Platform Baseline:** CI, worker process, lease heartbeat.
3. **004.4 Shopify Data Connector Core:** fixes the PII model, adds timezone and refund dates, dev-store
   reconciliation.
4. **004.5 API v1 → 004.6 Frontend v1:** M1, the first visible product.
5. **004.7 Install & Continuous Sync**, with **004.8 Analytics Hardening** in parallel, then **004.9 Production
   LLM**, alongside **004.10 Production Environment:** M2, the first real end-to-end product with a pilot.
6. **005.0 Commercial SaaS:** M3, paying-customer-ready.

### 33.4 Is the current architecture right for the next 12–24 months?

**Yes.** Nothing measured justifies microservices, a message broker or a separate analytics database.

| Component | Verdict | Condition or change |
|---|---|---|
| Python modular monolith | **Keep** | Boundaries are enforced by AST tests. The API and worker are two processes from one codebase. |
| Next.js | **Keep** (ADR-004-010) | The second stack is acceptable if types are generated from OpenAPI and no business logic lives in TypeScript. |
| PostgreSQL | **Keep** | Required: pooling, PITR, and partitioning or incremental snapshots before ~1,000 active stores on daily sync [INFERENCE]. |
| PostgreSQL job queue | **Keep** | Measured headroom far exceeds need. Required: heartbeat, dispatcher, schedules, webhook coalescing. Reconsider only on measured sustained contention or a webhook ingress that PostgreSQL cannot absorb, neither of which is in evidence. |
| Object storage | **Add** (already decided, ADR-004-002) | Needed for Shopify Bulk results, CSV uploads (fixes SEC-01) and replayable provenance. |
| LLM worker | **Keep the design** | A job type in the same worker, with concurrency and budget limits. No agents, no vector database. |

**What must change, and when**
1. **Before the pilot:**
   - the snapshot/PII model (erasable customer data);
   - lease heartbeat;
   - dispatcher and schedules;
   - store timezone.
2. **Before ~100 stores:**
   - incremental snapshots and snapshot retention;
   - per-worker memory and concurrency limits sized from real-job benchmarks.
3. **Only if a real tenant exceeds the measured envelope** (analysis > 5 min or beyond the worker memory budget,
   ADR-004-002): evaluate in-process DuckDB for the heaviest aggregations. Never move KPI computation into SQL or the
   frontend.

**Next mission:** **Mission 004.3 — Platform Baseline** (CI with required DB tests, worker process entrypoint,
lease heartbeat, container, unified logging, documentation sync). Meanwhile, the business track should start
getting a real pilot export and a Shopify development store.
