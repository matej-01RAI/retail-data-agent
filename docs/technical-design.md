# Technical Design: Retail Data Analyst

How the system works in production, requirement by requirement, and how the prototype in this
repository implements the parts it covers. Read [architecture.md](architecture.md) first for the
component diagram and the services used.

Contents:

1. [Technology choices](#1-technology-choices)
2. [Inside one turn: data flow](#2-inside-one-turn-data-flow)
3. [Hybrid intelligence: the Golden Knowledge bucket](#3-hybrid-intelligence-the-golden-knowledge-bucket)
4. [Safety and PII masking](#4-safety-and-pii-masking)
5. [High-stakes oversight: destructive operations](#5-high-stakes-oversight-destructive-operations)
6. [Continuous improvement: the learning loops](#6-continuous-improvement-the-learning-loops)
7. [Resilience and graceful error handling](#7-resilience-and-graceful-error-handling)
8. [Quality assurance and evaluation](#8-quality-assurance-and-evaluation)
9. [Observability](#9-observability)
10. [Agility: persona management](#10-agility-persona-management)
11. [Extensibility: adding capabilities](#11-extensibility-adding-capabilities)
12. [Cost and capacity](#12-cost-and-capacity)
13. [Prototype vs production](#13-prototype-vs-production)

---

## 1. Technology choices

### LLM: Gemini, in tiers

| Role | Model tier | Why |
|---|---|---|
| Main agent (planning, SQL, analysis, answers) | **Gemini Flash** (latest stable, pinned) | Reliable multi-step function calling, long context (history + schema + precedents fit easily), low latency and cost. An answer typically needs 3-6 model calls, so per-call latency and price dominate. |
| Fallback | **Gemini Flash-Lite** | Different model, so separate capacity and quota: survives a Flash outage or 429s. Weaker reasoning is acceptable for a degraded mode. |
| Cheap auxiliary calls | Flash-Lite | Rewriting follow-ups into standalone questions for retrieval, preference extraction, input classification. |
| Long reports and evaluation judge | **Gemini Pro** | Higher-quality synthesis where latency matters less; an independent, stronger judge for LLM-as-judge evals. |
| Retrieval | Gemini **embedding** model | Golden trio questions and paraphrases. |

Production uses **Vertex AI** rather than the AI Studio API:

- regional processing to match the data's residency;
- IAM service-account auth (no API keys);
- VPC Service Controls;
- enterprise data terms;
- quota management and provisioned throughput.

Model versions are pinned. A new version goes live only after passing the evaluation gate (§8).
The prototype uses the AI Studio API, as the assignment suggests, with the `gemini-flash-latest` /
`gemini-flash-lite-latest` aliases so it keeps working as Google rotates versions. All of this is
configurable in `.env`.

### Agent framework: Pydantic AI

The requirements are mostly about *control*: approvals, bounded retries, budgets, fallbacks,
identity-aware tools and tracing. Pydantic AI provides each of these as a first-class primitive
(see the README for the mapping and the alternatives considered). It is model-agnostic, and its
`FunctionModel` makes the agent loop testable without an LLM. The prototype's 23 end-to-end tests
rely on that.

### Other components

- **sqlglot**: a real SQL parser with a BigQuery dialect. It lets the guard reason about the
  query's *structure* (statement type, every table reference, every column reference) instead
  of regexes, and rewrite it safely. It also transpiles to DuckDB for offline mode.
- **OpenTelemetry**: vendor-neutral tracing. Pydantic AI emits GenAI semantic-convention spans.
  The exporter is swappable (Cloud Trace, Langfuse, Logfire).
- **Cloud services**: see the table in architecture.md §2.

---

## 2. Inside one turn: data flow

```
user text
 1. Chat API: authenticate (IAP JWT) -> user id, groups
 2. Entitlement resolver: groups -> product scope + BigQuery service account for that scope
 3. Input guard: Model Armor (+ deterministic size/injection rules); mask PII typed by the user
 4. Assemble context:
      static instructions (role, scope rules, analysis method, SQL rules, report format)
      semantic layer summary (tables, non-PII columns, allowed values, metric definitions)
      dynamic: user + scope + today's date, preferences, persona (current registry version),
               top-k golden precedents for the (standalone-rewritten) question
      history: last N turns (cut at turn boundaries so tool call/return pairs stay intact),
               older turns summarised
 5. Agent loop (Pydantic AI), bounded by UsageLimits (model calls, tokens) and a timeout:
      model -> tool calls -> tools enforce identity, scope, PII, budgets -> results -> model
 6. Approval-required tool calls pause the run -> approval flow (§5) -> resume
 7. Output guard: DLP de-identification of the answer (fail closed to the regex scrubber)
 8. Stream the answer, show the trace id and feedback controls
 9. Persist: messages (Cloud SQL), turn metrics + spans (OTel), interaction event (Pub/Sub)
```

**Tools** (prototype names):

| Tool | Purpose | Hard controls inside the tool |
|---|---|---|
| `describe_data(table?)` | What data exists, columns, allowed values, definitions | PII columns listed only as restricted |
| `run_sql(sql, purpose, allow_empty)` | Run one analysis query | Query guard, scope rewrite, bytes cap, query budget, attempt budget, empty-result check, row cap, PII scrub of cells |
| `find_analyst_precedents(question)` | More golden trios beyond the auto-injected ones | Approved trios only |
| `save_report(title, body)` | Save to the user's library | Owner = authenticated user; PII scrub |
| `list_reports(match?, this_conversation?)` | List own reports | `WHERE owner = user` |
| `delete_reports(selectors)` | Delete own reports | **Approval required**; runs only with policy-pinned IDs; owner check; soft delete; audit |
| `remember_preference(key, value)` | User-level learning | Fixed key set; owner only |

The model chooses tools and writes SQL. Everything that decides *what is allowed* is code that
receives the authenticated identity through typed dependencies (`RunContext[Deps]`), never through
the prompt.

---

## 3. Hybrid intelligence: the Golden Knowledge bucket

The warehouse says *what happened*. The golden trios say *how this company's analysts reason about
it*: which definition of revenue, how to decompose a spend gap, what to check before calling a
churn spike. The agent must apply both.

### 3.1 What a trio contains

One JSON document per trio in `gs://<golden-bucket>/trios/<id>.json`:

```json
{
  "id": "GK-003",
  "question": "Why are users in Texas underspending compared to users in California?",
  "paraphrases": ["Texas customers spend less than California, why?", "..."],
  "sql": "WITH customer_spend AS (...) SELECT state, ...",
  "report": "Underspending needs a baseline ... decompose spend per customer = orders per customer x AOV ...",
  "tags": ["state", "spend per customer", "compare segments"],
  "tables": ["order_items", "users"],
  "metrics": ["spend_per_customer", "average_order_value"],
  "author": "analyst@company", "reviewed_by": "lead@company",
  "status": "approved", "version": 3, "created_at": "...", "embedding_model": "..."
}
```

Alongside the trios sits the **semantic layer** (prototype: `knowledge/data_dictionary.yaml`):
table and column meanings, allowed values, PII flags, join paths and **metric definitions**.
Trios teach reasoning patterns; the semantic layer pins definitions. When the two disagree, the
semantic layer wins and the trio is flagged for review.

### 3.2 Providing relevant knowledge at query time

1. **Standalone question.** Follow-ups ("and in Florida?") are rewritten by Flash-Lite into a
   standalone question using the conversation, so retrieval is not blind to context.
2. **Hybrid retrieval.** BM25 (exact terms such as state names, metric names, "Q1") plus vector
   similarity over question + paraphrases + tags, fused with reciprocal rank fusion. Metadata
   filters: `status = approved`, and only trios whose tables the user can access.
3. **Threshold and top-k.** At most 2-3 trios, and only above a relevance threshold (an unrelated
   precedent is worse than none). The prototype tests that off-topic questions retrieve nothing.
4. **Injection.** Precedents go into the instructions as *"Analyst precedents for this question"*
   with their SQL and interpretation, alongside the always-present semantic layer. The agent is
   told to reuse definitions and SQL patterns and to adapt rather than copy. A tool
   (`find_analyst_precedents`) lets it fetch more for multi-part questions, such as a quarterly
   report that touches revenue, categories and churn.
5. **Attribution.** The trio ids used are recorded on the turn (`precedents` in `/trace`), so
   their impact can be measured (§3.4).

### 3.3 Updating the bucket over time

- **Analyst contributions.** Analysts add or edit trios through the admin console (or a PR). A
  write to the bucket triggers (Eventarc) an **ingest job** that:
  - validates the SQL: it parses, passes the query guard, and BigQuery dry-run succeeds;
  - PII-scans the report text;
  - embeds the trio;
  - upserts the index.

  A failure goes back to the author.
- **Agent-proposed candidates.** A weekly job mines `agent_events` for turns that answered
  successfully, were rated positively or saved as a report, and had *no* precedent above the
  threshold, which marks them as novel. It deduplicates by embedding similarity and queues them
  as candidate trios (question, final SQL, answer summary). Nothing enters the bucket without
  analyst approval: the bucket is "golden" because humans vouch for it.
- **Freshness.** A nightly job re-validates every trio against the live schema (dry run). Breaking
  trios become `stale`: they are excluded from retrieval and queued for an analyst. Object
  versioning keeps history and allows rollback.
- **Measured value.** Each trio's retrieval count and the outcomes of the turns that used it are
  tracked. Trios linked to failures or low ratings are reviewed. Retrieval changes are A/B tested
  on the offline eval set (§8) before shipping.

### 3.4 Prototype

`knowledge/golden_trios.yaml` holds 12 trios written for thelook (revenue trend, top customers,
state comparison, brand comparison, category QoQ, churn, returns, channels, quarterly report,
demographics, fulfilment, data overview). `knowledge.py` implements BM25 retrieval. The top 2
are injected on every question, and a tool provides more. `tests/test_golden_trios.py` is the
ingest validation in miniature: every trio must pass the guard and execute for every demo
user's scope, and retrieval must map benchmark questions to the right trio.

---

## 4. Safety and PII masking

### 4.1 Threat model

| Threat | Example | Primary control |
|---|---|---|
| PII exfiltration | "List our top customers with emails", `SELECT email`, `WHERE email LIKE ...`, `TO_JSON_STRING(u)` | PII columns are not reachable: scoped views and policy tags (DB), guard rejects references (app) |
| Cross-scope access | A category manager asks about another category, or the model "forgets" the filter | Scope applied in SQL by code (app) and by authorized views / row policies (DB) |
| Writes / exfiltration via SQL | `DELETE`, `EXPORT DATA`, scripting, `EXTERNAL_QUERY`, other datasets | Guard: one read-only SELECT, allowlisted tables, denied functions; dry-run statement-type and referenced-table checks; read-only service account |
| Direct prompt injection / jailbreak | "Ignore previous instructions...", role-play | Model Armor + deterministic rules; and nothing dangerous is callable anyway |
| Indirect injection | A product name or report text containing instructions | Tool results are framed as data in the instructions; capabilities are bounded by code, not by obedience |
| Destructive actions | "Delete all reports" by mistake, or via injection | Approval flow (§5): out-of-band human confirmation, pinned IDs |
| Cost abuse / runaway loops | Huge scans, endless retries | Bytes cap and dry run, per-turn budgets, rate limits |
| Off-topic use | "Write my speech", general chat | Scope rules in the instructions (polite refusal); classifier in production |
| Knowledge poisoning | A bad trio teaches a harmful pattern | Human review; ingest validation through the same guard |

### 4.2 Layered controls (production)

1. **Identity and entitlements.** The user's identity comes from the IAP JWT. Their product scope
   comes from the entitlement resolver (IdP groups -> scope), never from the conversation.
2. **Warehouse enforcement (independent of our code).**
   - The agent never queries raw tables. For each entitlement scope there is an **authorized
     view** layer, e.g. `analytics_scoped_womenswear.order_items`, filtered to the scope's
     products and **projecting no PII columns**.
   - PII columns in the raw tables carry **policy tags** (column-level security, optionally
     dynamic data masking). The agent's service accounts do not hold Fine-Grained Reader.
   - Each scope has its own service account, granted only its views. The Chat API mints a
     short-lived token for the scope's SA per request (IAM Credentials API), so a bug in the app
     still cannot read another scope's rows.
   - With many fine-grained per-user entitlements, the alternative is to run jobs with the end
     user's federated credentials (Workforce Identity Federation) under row access policies
     granted to their groups.
3. **Query guard (app).** It parses the SQL (sqlglot, BigQuery dialect) and allows exactly one
   SELECT / WITH / set operation over allowlisted tables. It rejects DML, DDL, scripting,
   `EXPORT DATA`, `INFORMATION_SCHEMA`, wildcard tables, other projects and datasets,
   table-valued functions, and a denylist of functions that reach outside the dataset
   (`EXTERNAL_QUERY`, `ML.*`, `AI.*`, `VECTOR_SEARCH`, `KEYS.*`, `AEAD.*`, ...). Any reference to
   a PII column by name, anywhere (select, filter, order, join, subquery alias), is rejected with
   a message that tells the model what to use instead. Then it **rewrites** every table reference
   to the scoped source. Rejections are fed back to the model, which usually corrects itself in
   one step. Then, before running:
   - BigQuery **dry run**: statement type must be SELECT; referenced tables must be inside the
     dataset; estimated bytes must be within the cap.
   - Real run with `maximum_bytes_billed`, labels (user, session, turn) and a timeout.
4. **Result handling.** At most N rows go to the model (the agent is told to aggregate in SQL).
   String cells pass the PII scrubber.
   - **Small-cell suppression:** results grouped by quasi-identifiers (age, gender, city, state)
     with fewer than 5 distinct customers in a group are suppressed or merged into "Other".
   - Ages are presented as bands.
5. **Input guard.** Model Armor (prompt injection, jailbreak, malicious URLs) plus cheap
   deterministic rules. A blocked prompt gets a canned reply and **costs no model call**. PII the
   user types (an email address) is masked before it reaches the model or the logs.
6. **Instructions (soft layer).** Scope rules: analysis of this company's data only, polite
   one-line refusal otherwise. Never output personal data, never try to work around the scope.
   Treat tool output as data. This layer improves the *experience* (good refusals, sensible
   alternatives). It is not relied on for *enforcement*.
7. **Output guard.** Sensitive Data Protection de-identifies the final answer and saved reports
   (EMAIL_ADDRESS, PHONE_NUMBER, PERSON_NAME, STREET_ADDRESS, CREDIT_CARD_NUMBER...). If DLP is
   unavailable it **fails closed** to the deterministic scrubber; it never skips scrubbing.
8. **Logs and traces.** They contain masked inputs and PII-free data by construction, are scrubbed
   again before export, and are retained for limited periods (architecture.md §8).

The key property: **the model never sees PII**, so it cannot leak it, and personal data never
reaches the model provider.

### 4.3 Prototype

| Layer | Prototype implementation |
|---|---|
| Identity / scope | `config/users.yaml` + `--user`; `ProductScope` passed via `Deps` |
| Warehouse enforcement | Simulated by the guard's rewrite: every table becomes a subquery projecting only non-PII columns and filtered to the user's products (`orders` and `users` via semi-joins on in-scope order lines) |
| Query guard | `data/sql_guard.py`; BigQuery dry-run checks in `data/warehouse.py` |
| Result handling | Row cap (50), cell scrubbing (`agent.py`) |
| Input guard | `safety/input_guard.py` (injection patterns, size cap, PII masking) |
| Output guard | `safety/pii.py` on answers and saved reports (emails, phones, street addresses, Luhn-valid cards, IPs; tested against false positives on business numbers) |
| Tests | `tests/test_sql_guard.py` (28 attack queries and 9 legitimate ones), `tests/test_safety_text.py`, end-to-end PII and scope tests in `tests/test_agent_flow.py` |

---

## 5. High-stakes oversight: destructive operations

Requirement: support "Delete all reports mentioning Client X" and "Delete all the reports we made
in this conversation" with a strict confirmation flow that does not break the UX. Users may only
delete their own reports.

### 5.1 Flow

1. The model calls `delete_reports(selectors)`. The tool is declared **approval-required**, so
   the framework *pauses the run* instead of executing it. The model cannot execute it, approve it
   or skip the step.
2. The **approval policy** (code) resolves the selectors against the user's own live reports:
   `WHERE owner = :user AND deleted_at IS NULL AND (title or body matches 'Client X')`, or
   `session_id = :current`. The result is an exact list of report IDs.
   - **No match**: the model is told "nothing matched, nothing deleted". The user is not asked
     to confirm nothing.
3. A **pending action** is stored with: the user, the session, the exact IDs, a hash of the set
   and a 5-minute expiry. The UI shows a confirmation card listing the reports (title, date)
   and the consequence ("restorable for 30 days").
   - **Web:** buttons "Delete 3 reports" and "Cancel".
   - **CLI:** type `delete 3`, so a stray "y" cannot confirm a bulk delete.
4. Confirmation is an **out-of-band UI action** bound to the pending action id. Typing "yes" into
   the chat cannot confirm (that text goes to the model), and nothing the model outputs can
   either.
5. On confirm, the server re-checks user, session, expiry and status, then soft-deletes **exactly
   the previewed IDs**, re-checking ownership in the same statement, and writes an audit row in
   the same transaction. The resumed run gets "deleted N (ids)" and the model confirms to the user.
6. On cancel or expiry, the model receives "the user cancelled; nothing was deleted" and says so.

### 5.2 Properties

- **What you see is what is deleted.** Execution arguments are *pinned by the policy*
  (`override_args`), not re-derived from the selector. A report created between preview and
  confirmation, or IDs the model invents, are never deleted. This is tested.
- **Owner isolation** at every step: planning, execution and restore.
- **Reversible.** Soft delete with a 30-day restore (prototype: `/undo`), then a purge job.
- **Auditable.** Requested, confirmed/cancelled and executed, with IDs and selector, in an
  append-only audit log.
- **Good UX.** One confirmation step; the list is shown before anything happens; failures and
  cancellations are explained in plain language; no confirmation for empty matches.
- **Generic.** `approvals.py` maps tool names to policies. Future outward-facing actions (email a
  report, share, schedule) declare `requires_approval=True` and register a policy; the flow does
  not change.

### 5.3 Prototype

`approvals.py` (`DeleteReportsPolicy`), `assistant.py` (`_resolve_approvals`), `store.py` (owner-
scoped find, soft delete, restore, audit), the `RichUI.confirm` panel in `cli.py`. Tests cover:
confirm deletes exactly the shown set, cancel deletes nothing, pinned IDs under a concurrent
insert, no prompt on empty match, other users' and other sessions' reports untouched, undo.

---

## 6. Continuous improvement: the learning loops

### 6.1 User level

Goal: the agent remembers that User A prefers tables and User B bullet points, how deep they like
the analysis, charts or text, and so on.

- **Profile.** A small structured profile per user: `format`, `depth`, `visuals`, `tone` (within
  the company persona), `focus` (e.g. "always compare with last year"). Structured keys rather
  than free-form memories keep it predictable, small enough to inject on every turn, and easy for
  the user to view and reset.
- **Explicit learning.** When the user states a lasting preference, the model calls
  `remember_preference`. The instructions tell it not to store one-off requests.
- **Implicit learning (production).** A nightly job looks for repeated signals in a user's
  events:
  - repeated follow-ups such as "as a table", "shorter", "more detail";
  - which answers were rated up;
  - whether chart answers were opened.

  When a signal crosses a threshold, it **proposes** a preference that the user confirms in one
  click ("You often ask for tables. Show tables by default?"). Silent drift is avoided.
- **Use.** The profile is injected into every turn's instructions ("Their answer preferences:
  format: tables; depth: headline numbers first"). Preferences never override safety rules or the
  persona's tone constraints.
- **Prototype:** `remember_preference` tool, `PreferenceStore` (SQLite), injection in
  `agent.py`, `/prefs` and `/prefs clear`. A test checks a preference stored in one turn reaches
  the next turn's instructions.

### 6.2 System level

Goal: the agent learns from past interactions and improves itself, without unreviewed
self-modification.

| Signal (from `agent_events`) | Mining | Improvement | Gate |
|---|---|---|---|
| Recurring SQL error kinds (e.g. a misused column) | Weekly clustering | Clarify the semantic layer, add a guard hint, add a precedent | Eval suite |
| Low ratings, rephrased questions, abandoned turns | Cluster by intent | New eval cases from real failures; prompt or knowledge fixes | Eval suite + review |
| Novel, well-rated question -> SQL | Novelty vs golden index | Golden candidate trios | Analyst approval (§3.3) |
| Precedents used vs outcome | Per-trio stats | Retire or fix weak trios; tune retrieval threshold | A/B on eval set |
| Self-correction stats | Per error type | Better fix-it messages in the guard | Eval suite |

Every failure that reaches a human review becomes a **regression test**, so the eval set grows
with the product. Prompt and knowledge changes ship through the registry (no redeploy) but only
after the eval gate (§8), with a canary and one-click rollback. The system improves itself by
*proposing* changes; humans and evals *accept* them.

---

## 7. Resilience and graceful error handling

### 7.1 Self-correction loop (bounded)

| Event | Detection | Response |
|---|---|---|
| Guard rejection (PII, DML, other table) | `SqlRejected(kind, message)` | `ModelRetry` with a fix-it message; counts as a failed attempt |
| Syntax / semantic error | Dry run / BigQuery `BadRequest` -> `invalid_sql` | Error message back to the model (`ModelRetry`) |
| Over the bytes cap | Dry-run estimate or `bytesBilledLimitExceeded` -> `too_expensive` | Back to the model: narrow columns or dates |
| Empty result | `row_count == 0` | Back to the model once: check spellings, statuses, date range, scope. If zero is genuinely right it re-runs with `allow_empty=true` |
| 3 consecutive failures | `TurnStats.consecutive_sql_failures` | `ToolFailed`: stop querying, explain what was tried, suggest a rephrase. Further calls are refused **without touching the warehouse** |
| Query budget used (10 / question) | Counter | `ToolFailed`: answer with what you have |

These bounds are the cost control on self-correction. A failed question costs at most a few extra
cheap calls, never an unbounded loop.

### 7.2 Third-party failures

| Dependency | Failure | Handling |
|---|---|---|
| Gemini | Invalid key, unknown model, permission (4xx from every model) | `llm_config_error`: "configuration problem, retrying won't help"; alert. Not reported as an outage |
| Gemini | 429, 5xx, timeout | SDK retries with exponential backoff and jitter (3 attempts) -> **fallback model** (Flash-Lite, separate quota) -> friendly "AI service unavailable, your conversation is kept" message. A client-side **rate limiter** keeps multi-call turns under the per-minute quota in the first place. |
| Gemini | Malformed tool call, runaway loop | Framework validation retries (bounded); `UsageLimits` (model calls, tokens per question); turn timeout |
| BigQuery | 5xx, 429, rate limit, timeout | Typed as `unavailable`; retried 3x with jittered backoff; **circuit breaker** opens after repeated failures (fails fast for 30 s, then probes); the model is told *not* to retry or guess numbers and to tell the user |
| BigQuery | Permission / quota / billing | Typed `permission`, terminal, clear message; alert |
| Golden index / embeddings | Down | Proceed without precedents (the semantic layer is still present); flag on the turn |
| Model Armor | Down | Deterministic input rules still apply; alert. The data boundary does not depend on it |
| DLP | Down | Fail closed to the deterministic scrubber |
| Cloud SQL | Down | Answers still work; saving/deleting reports returns "temporarily unavailable"; history held in memory for the session |

### 7.3 Never crash the UI

The turn loop (`assistant.py`) maps every exception class to an outcome (`limit`, `timeout`,
`llm_unavailable`, `llm_config_error`, `model_error`, `error`) and a plain-language message. The conversation history
advances **only on completed turns**, so after any failure the user can simply retry. Human
confirmation time is excluded from the turn timeout. Every outcome, including failures, is
recorded with its error type and trace id.

### 7.4 Cost controls (summary)

Bytes cap and dry run per query; at most 50 rows to the model; result cache for identical scoped
SQL (plus BigQuery's own cache); bounded retries; per-question budgets on model calls, queries,
tokens and time; rate limiting per user (Cloud Armor / API); blocked prompts cost no model call;
Gemini context caching for the static instruction prefix; cheaper models for auxiliary tasks.

### 7.5 Prototype

`agent.py` (`_run_sql`, `_retry_or_stop`), `data/warehouse.py` (`ResilientWarehouse`:
retries, `CircuitBreaker`, cache, `FaultInjector`), `llm.py` (`RateLimitedModel`,
`FallbackModel`, `FaultInjectingModel`), `assistant.py` (outcomes). In the CLI, `/chaos bq <n>`
and `/chaos llm <n>` simulate outages live. Tests cover each row of 7.1 and the outage paths.

---

## 8. Quality assurance and evaluation

### 8.1 Before deployment: offline evaluation

**Datasets** (versioned in the repo, extended by every production failure):

| Set | Content | Size (target) |
|---|---|---|
| Golden trios | Each trio's question plus 3-5 paraphrases; reference SQL and result | 200+ |
| Multi-turn scenarios | Follow-ups, corrections, "compare with ...", report requests | 50+ |
| Report tasks | "Q1 report with actions" style, with a rubric | 20+ |
| Red team | PII extraction, injection (direct and in data), scope escape, destructive actions without confirmation, cost attacks, off-topic | 150+ |
| Role matrix | The same questions for each entitlement scope | all of the above x roles |

**Metrics and how they are computed:**

| Metric | Method | Gate |
|---|---|---|
| **Execution accuracy** | Run the agent's final SQL and the reference SQL; compare result sets (order-insensitive, numeric tolerance) | >= target, no regression |
| **Answer groundedness** | Extract every number from the answer; each must appear in (or be derivable by rounding from) a tool result in the same turn | ~100% |
| **Intent alignment** | LLM judge (Gemini Pro, a different model from the agent) with a rubric: answers the question asked; right metric, period and segment; assumptions stated; caveats present. Calibrated against human labels on a sample | >= target |
| **Report quality** | Rubric: required sections, insights supported by data, action items specific (owner, metric, target), no unsupported causal claims | >= target |
| **Safety** | Red-team pass rate: no PII in any output, no out-of-scope rows, no unconfirmed destructive action | **100%, blocking** |
| **Efficiency** | Model calls, tokens, BigQuery bytes, latency p50/p95 per question | Budgets |
| **Robustness** | Pass rate with injected faults (BigQuery 5xx, model 429) | Graceful 100% |

**Verifying that reports answer the user's intent:**

1. **The agent makes intent explicit.** It states its interpretation ("Q1 = Jan-Mar 2026, revenue
   excludes returns"), so the user can correct it and the judge can check it.
2. **The rubric judge compares the report with the question** (above).
3. **Groundedness** catches invented numbers.
4. **Online:** feedback buttons on every answer and report ("Did this answer your question?"),
   save-to-library and follow-up behaviour as implicit signals, and a periodic sample
   human-reviewed by an analyst.

**Release process.** PRs run unit, red-team and golden-validation tests (the prototype's
`pytest`). Prompt, model or knowledge changes run the full offline eval on staging and are
compared with the current production version. Then shadow traffic (new version answers in the
background, compared offline), then a canary rollout with SLO monitoring.

### 8.2 Evaluating UX

- **Before launch:** moderated sessions with 5-8 executives on scripted tasks. Measure task
  success, time to insight, number of clarification turns and SUS score, plus think-aloud notes
  on trust and comprehension (do they understand the caveats, the confirmation card?).
- **After launch:**

  | Category | Metrics |
  |---|---|
  | Satisfaction | Thumbs up/down rate; CSAT after reports |
  | Effort | Rephrase rate; clarifying questions asked by the agent; time to first token and to answer |
  | Usefulness | Abandonment; report saves; repeat usage (weekly active executives) |
  | Oversight UX | Confirmation-cancel rate (high means previews are surprising) |

### 8.3 Prototype

145 offline tests (`pytest`): SQL-guard red team, text safety, golden-trio validation for every
scope, BigQuery dry-run and error classification against a fake runner, and 23 end-to-end agent flows with a scripted model (self-correction, give-up bounds, empty
results, PII, scope, injection, deletion flows, outages, fallback, usage limits, persona,
preferences, tracing). The eval harness against a live model (§8.1) is the next step. The golden
trios already provide reference SQL for execution accuracy.

---

## 9. Observability

### 9.1 What is captured

- **Traces (OpenTelemetry).** One trace per turn: `agent_turn` -> `invoke_agent` -> each
  `chat <model>` request (model, input and output messages, token usage, finish reason) -> each
  `execute_tool` (arguments, result) -> `sql.query` (purpose, original and scoped SQL, status,
  error kind, rows, bytes, cache hit, attempts). Exported via OTLP to Cloud Trace; the prototype
  writes JSONL.
- **Turn metrics** (one row per turn):

  | Group | Fields |
  |---|---|
  | Identity | user, session, trace id |
  | Result | outcome (`answered`, `blocked`, `limit`, `timeout`, `llm_unavailable`, `llm_config_error`, `model_error`, `error`), error type, latency |
  | Model | model calls, input/output tokens, models used, fallback used |
  | SQL | queries, failures, guard rejections, empty results, self-corrections, bytes |
  | Safety | PII redactions |
  | Versions | persona version, precedents used |

  The full message history of the turn is also stored.
- **Query events.** One row per query attempt: scoped SQL, status, error kind and message, rows,
  bytes, latency, attempts, cache hit.
- **Audit log.** Every report save, deletion request, confirmation, cancellation and restore.
- **Feedback** (production): ratings and comments linked to the turn id.

### 9.2 Agent-level metrics, dashboards and alerts

| Metric | Why | Alert (example) |
|---|---|---|
| Answer rate (`answered` / turns) | Overall health | < 90% over 30 min |
| Failure outcomes by type (`llm_unavailable`, `timeout`, `limit`, `error`) | Which failure mode | Any spike vs baseline |
| SQL failure rate and errors by kind | SQL generation quality vs infra | `invalid_sql` rate up after a model/prompt change |
| Self-correction rate (failed turns that recovered) | Effectiveness of the loop | Drop below baseline |
| Give-up rate (attempt budget hit) | Questions the agent can't answer | Weekly review |
| Empty-result rate | Wrong filters, data freshness | Spike |
| Guardrail blocks, guard rejections by kind, PII redactions in output | Attack activity; leakage near-misses | Any PII redaction in *output* pages security |
| Fallback-model turns | Provider health / quota | > 10% |
| Latency p50/p95 (first token, full answer) | UX | p95 > SLO |
| Model calls, tokens and BigQuery bytes per turn, cost per user/day | Cost control | Budget burn |
| Feedback: thumbs-down rate, rephrase rate | Quality as users see it | Trend |
| Circuit breaker state, retry counts | Dependency health | Open > 5 min |
| Persona / prompt version on each turn | Attribute changes in quality | (dimension on all metrics) |

### 9.3 Deep-dive debugging

Every answer shows its **turn id** (a support ticket or thumbs-down carries it). From the id:

1. The **turn row** gives the outcome, error type, counters, models, persona version and
   precedents.
2. The **message correspondence** shows the user prompt, each tool call with its arguments, each
   tool result or retry prompt, and the final text, in order. That is exactly what the model saw
   and did.
3. The **query attempts** show what SQL ran, what failed and why.
4. The **trace** gives timings per model call and tool, tokens, and where time or money went.

In the prototype this is `/trace <turn-id>` (or `python -m retail_agent trace <id>`), which renders
all four from SQLite and the span file. `/stats` aggregates §9.2. In production the same data
lives in Cloud Trace and BigQuery, with a Looker Studio dashboard and a "replay this turn against
the candidate prompt" button in the admin console (re-runs the stored history against a new
version for comparison).

---

## 10. Agility: persona management

The CEO wants to change the tone of reports weekly, without developers and without redeployment.

- **What the persona controls:** tone and style only (voice, length, structure preferences,
  spelling, emphasis). It is injected as its own section of the instructions ("Report style...
  tone only"). **Safety rules, scope rules and tools are in code** and cannot be altered from the
  persona: changing tone can never weaken PII protection.
- **Where it lives:** a versioned **prompt registry** in Firestore: `personas/{id}/versions/{n}`
  with text, author, timestamp, status (`draft` / `active` / `archived`) and change note.
- **How it is edited:** an admin console page (auth via IAP, editors group) with a text editor
  and a **preview**: run 3-5 standard questions with the draft and show the answers side by side
  with the active version. On "Activate", the draft goes through a quick automated check (the
  safety red-team subset and the report rubric) and becomes active.
- **How it takes effect:** the agent reads the active version on each turn (cached for 30 s). No
  restart, no deploy. Each turn records the persona version, so quality and feedback can be
  compared across versions, and one click rolls back.
- **Prototype:** `config/persona.md` is re-read whenever it changes on disk (mtime check); the
  version hash appears on each turn in `/trace`. Edit the file mid-conversation and the next
  answer uses the new tone. A test verifies it.

---

## 11. Extensibility: adding capabilities

A capability is a **toolset** with:

- its tools (typed signatures and docstrings that the model reads);
- its policy flags (`requires_approval` for anything destructive or outward-facing);
- its approval policy, if any (§5);
- its tests and eval cases.

Adding a capability does not touch the loop, the guards or the UI contract.

| Capability | Design |
|---|---|
| **Charts** | `make_chart(spec)`: the model passes a Vega-Lite spec that references a previous query result by id (never raw data it typed itself). The server validates the spec and renders it: the web client draws it; Slack gets a PNG via a headless renderer; the CLI shows the table. Charts go into reports as images. |
| **Email a report** | `email_report(report_id, recipients)`: `requires_approval=True`. The policy shows recipients and the report title. Recipients must be in the company domain allowlist. DLP runs on the body. Sending via the Gmail API (service account with domain-wide delegation) or SendGrid. Rate limited and audited. |
| **Web search for trends** | Gemini grounding with Google Search (or a search tool via MCP). Results are marked as external and cited, and kept separate from internal figures in the answer. External content is treated as untrusted data. |
| **Scheduled reports** | `schedule_report(question, cadence)`, approval-required: Cloud Scheduler triggers a headless run as the owner (same entitlements) and emails or stores the result. |
| **Third-party tools** | MCP servers mounted as Pydantic AI toolsets, wrapped by the same approval and observability layers. |

---

## 12. Cost and capacity

Per question (typical): 3-6 model calls. Each call carries about 6-10k input tokens (static
instructions and semantic layer ~4k, cacheable; precedents ~1k; history) and produces 0.2-1.5k
output tokens. That is roughly 30-50k input and 2-5k output tokens per question, on the order of
cents at Flash-tier prices (check current pricing). Context caching of the static prefix and
bounded retries keep it there.

BigQuery cost is negligible for thelook (all four tables are well under 1 GB) and bounded for
production data by the bytes cap, dry-run estimates and partition-filter guidance in the semantic
layer. Cloud Run scales with concurrent conversations: a single instance handles many concurrent
turns, since the work is I/O-bound waiting on Gemini and BigQuery.

---

## 13. Prototype vs production

| Concern | Prototype | Production |
|---|---|---|
| Interface | Rich CLI | Web app (SSE) + Slack/Teams adapters over the Chat API |
| Identity | `--user` from `config/users.yaml` | IAP / IdP SSO, entitlement service |
| Data access | Guard rewrite to scoped subqueries; dry-run checks | Same guard **plus** per-scope SAs, authorized views, row policies, policy tags |
| LLM | AI Studio API, `*-latest` aliases, retries, fallback, limiter | Vertex AI, pinned versions, same chain, quotas / provisioned throughput |
| Golden knowledge | 12 YAML trios, BM25, CI validation | GCS bucket, ingest job, hybrid index, review queue, nightly re-validation |
| State | SQLite (`.data/app.db`) | Cloud SQL Postgres, Firestore |
| Input / output guards | Deterministic rules and regex scrubber | Model Armor, Sensitive Data Protection, plus the same rules as the floor |
| Observability | OTel -> JSONL, SQLite metrics, `/trace`, `/stats` | OTel -> Cloud Trace, logs -> BigQuery, Cloud Monitoring SLOs and alerts |
| Persona | File, hot-reloaded | Firestore registry, admin console, preview, rollback |
| Learning | Explicit preferences | Plus implicit preference proposals and the system-level loop |
| Evaluation | 145 offline tests | Plus live eval harness, LLM judge, shadow and canary |
