# Config-Driven Single-Stock Strategy Runner Implementation Plan

**Goal:** Add a research-only `research strategy run --config FILE` workflow in which one
strict JSON document binds an immutable daily dataset, a safe versioned operator graph,
financial assumptions, and an output root, then publishes a complete causal replay as one
content-addressed immutable artifact directory.

**Architecture:** Keep the existing Docker submission runner unchanged and add an in-process
strategy research runner with a closed built-in operator registry. Configuration validation
uses exact object schemas at every level and exact parameter schemas owned by one template or
operator. Dataset snapshots remain backward compatible: required-only OHLCV data keeps the
existing v1 identity and verifier, while snapshots containing supported optional research
columns use a v2 identity that binds the ordered column schema and every canonical float64
value. The replay computes signals from history ending before each execution session, performs
cash/share accounting against raw Open prices, mechanically reconciles all ledgers, and only
then atomically publishes read-only artifacts. A source digest over the effective runner modules
joins canonical configuration and dataset identity in the run ID.

**Non-goals and boundaries:**

- No broker, order-routing, paper-trading, live-order, network, deployment, parameter-search,
  or broad UI path.
- No arbitrary Python import paths or caller-provided executable code.
- No adjustment of executable Open prices. `AdjustedClose` is signal-only; raw `Close` is
  accepted for signals only when explicitly selected.
- No dividend or corporate-action cash-flow model. The account is explicitly price-return only
  and must not be described as total shareholder return.
- No external financial-term popover code in the generated report.
- No overwrite or repair of an existing run directory. Exact reruns verify and reuse it;
  conflicts and corruption fail closed.

## Frozen configuration contract

The top-level object has exactly:

- `schema_version`: integer `1`;
- `dataset`: exactly `root`, `instrument`, and `snapshot_id`;
- `output_root`: immutable run parent directory;
- `template`: `{name, version, parameters}`;
- `operators`: exactly the required slots `fit`, `smoothing`, `statistic`, `decision`,
  `sizing`, `cost`, and `report`, each `{name, version, parameters}`.

The first template is `single_stock_daily_causal` version `1`. Its parameters own only
experiment/account/report defaults: instrument display name, evaluation start/end, initial
capital, initial state, terminal handling, and the explicit cost-assumption label. Initial
state is `flat`; terminal handling is `mark_to_market` or `force_liquidate`.

The first registry entries are:

| Slot | Name | Version | Owned parameters |
|---|---|---:|---|
| fit | `prior_log_ols` | `1` | `window_sessions`, `price_column` |
| smoothing | `recursive_log_ema` | `1` | `span_sessions` |
| statistic | `adjacent_curve_pct_slope` | `1` | none |
| decision | `post_start_threshold_crossing_hysteresis` | `1` | `buy_threshold_pct_per_day`, `sell_threshold_abs_pct_per_day` |
| sizing | `all_in_all_out_a_share_lots` | `1` | `lot_size`, `target_fraction` |
| cost | `cms_china_a_share` | `1` | `commission_rate`, `minimum_commission_cny`, `transfer_fee_rate`, `sell_stamp_tax_rate`, `buy_slippage_bps`, `sell_slippage_bps` |
| report | `concise_chinese_causal_trade` | `1` | none |

Every field and parameter is typed without implicit coercion. Integer/number validators reject
booleans; numeric validators reject NaN and infinity. Unknown/missing fields, unknown
operator/version, wrong slot, unsupported signal columns, undeclared parameters, and parameter
names declared by more than one component fail before any run directory is created. Canonical
JSON is sorted, compact, UTF-8, and finite; its exact SHA-256 is persisted.

## Task 1: Optional research-column snapshot identity

**Files:**

- Modify `src/quant_platform/datasets.py`
- Modify `src/quant_platform/updates.py`
- Modify `src/quant_platform/reference_job.py`
- Modify `tests/test_platform_datasets.py`
- Modify `tests/test_platform_updates.py`
- Modify `tests/test_platform_reference_job.py`

**RED:**

1. Add a snapshot test proving legacy `Adj Close` is normalized only at ingestion to canonical
   `AdjustedClose`, persisted in Parquet, and represented in the manifest column schema.
2. Prove changing only `AdjustedClose` changes snapshot identity.
3. Prove Parquet tampering, manifest column tampering, and optional-column removal fail
   verification.
4. Prove required-only snapshots retain schema version 1 and their existing IDs.
5. Prove update merge/revision detection preserves and compares `AdjustedClose`.
6. Prove the deterministic reference job can still consume verified v1 and v2 snapshots while
   ignoring supported optional columns it does not use.

**GREEN:**

- Normalize `Adj Close` to `AdjustedClose`; reject input containing both names.
- Permit only the canonical optional `AdjustedClose`, validate it as finite and positive, and
  preserve a deterministic column order.
- Use the unchanged v1 canonical byte format and manifest shape for required-only frames.
- Use snapshot schema v2 for optional columns. Bind the exact ordered column list plus all
  canonical data bytes into `canonical_sha256`, include `columns` in the manifest, and teach
  the verifier both schemas.
- Make update reconciliation derive compared columns from the normalized frames instead of
  silently dropping optional data.

**REFACTOR/GATE:** Centralize supported-column helpers, run focused dataset/update/reference
tests, and confirm existing hard-coded v1 expectations remain unchanged.

**Commit:** `feat(quant): preserve optional snapshot columns`

## Task 2: Closed typed configuration and operator registry

**Files:**

- Create `src/quant_platform/strategy_config.py`
- Create `src/quant_platform/strategy_operators.py`
- Create `tests/test_strategy_config.py`
- Create `tests/test_strategy_operators.py`

**RED:**

1. Cover exact top-level, dataset, template, operator, and parameter object ownership.
2. Cover missing and unknown fields, wrong scalar/container types, booleans as numbers,
   non-finite values, unsafe/empty paths, and invalid dates/ranges.
3. Cover unknown operators, unknown versions, slot incompatibility, undeclared parameters, and
   registry parameter-name collisions.
4. Cover explicit `AdjustedClose` and raw `Close` signal selection; reject every other column.
5. Cover deterministic canonical config bytes/hash.
6. Cover prior-only OLS, recursive log EMA, adjacent percent slope, and hysteresis edge cases:
   first finite statistic initializes only, first valid up-cross buys, and a down-cross while
   flat is ignored.
7. Cover CMS itemized arithmetic and all-in board-lot sizing under minimum commission,
   transfer fee, stamp tax, slippage, and insufficient cash.

**GREEN:**

- Implement small immutable parameter/operator/template descriptors and strict primitive
  validators.
- Resolve every configured slot through a literal registry; never import from configuration.
- Implement pure numerical operators with declared inputs and outputs.
- Model slippage as an itemized cash cost against raw Open notional, so event price remains the
  actual raw Open while executable cash accounting includes slippage exactly.
- Find the largest affordable board-lot buy after all buy-side costs; sell every held share.

**REFACTOR/GATE:** Keep operators pure and deterministic, expose no mutable registry, run
focused schema/operator tests, and inspect failure messages for the exact offending path.

**Commit:** `feat(quant): add typed strategy operator registry`

## Task 3: Causal replay and reconciled ledgers

**Files:**

- Create `src/quant_platform/strategy_replay.py`
- Create `tests/test_strategy_replay.py`
- Create `tests/fixtures/strategy/daily.csv`

**RED:**

1. Add a deterministic synthetic end-to-end path with known threshold crossings and prices.
2. Prove each decision session's `history_end` is strictly earlier than execution date.
3. Prove mutating future input cannot alter any earlier curve, smoothed curve, statistic,
   decision, event, holding, or equity value.
4. Cover first-bar-above-threshold waiting, first valid crossing, and sell-while-flat behavior.
5. Cover exact lot floor, minimum fee, stamp/transfer/slippage arithmetic, insufficient cash,
   residual cash, all-share sale, open terminal trade, and forced liquidation.
6. Cover all required daily fields and event/trade fields.
7. Cover zero-cost strategy and same-period raw-price buy-and-hold accounts under the same
   capital/lot/endpoint conventions.
8. Cover event/trade/daily/cost reconciliation, closed-trade win-rate exclusion of open trades,
   and `final_equity - initial_capital == net_profit`.

**GREEN:**

- Load and verify the bound immutable snapshot before evaluation.
- Compute one-step OLS curves from exactly the preceding `window_sessions`; compute smoothing
  and slopes causally; initialize decision state only inside the evaluation interval.
- Execute transitions at raw Open. Record history bounds, signal stack, previous statistic,
  decision/reason, position before/after, raw price, quantity, account balances, gross/net P&L,
  and every fee component.
- Mark holdings to each raw Close and terminal raw Close. Do not invent a terminal sale under
  `mark_to_market`; execute a real final-Open sale only under `force_liquidate`.
- Build round trips from actual events, retaining an explicit open trade without including it in
  closed-trade win rate.
- Run zero-cost and buy-and-hold comparisons through the same accounting primitives.
- Reject any replay that fails mechanical reconciliation or produces a non-finite artifact
  value.

**REFACTOR/GATE:** Separate signal generation, account transitions, ledger derivation, metrics,
and invariant checks. Run focused replay tests after each behavioral slice.

**Commits:**

- `feat(quant): implement causal daily strategy replay`
- `test(quant): cover strategy financial reconciliation`

## Task 4: Chinese self-contained causal report

**Files:**

- Create `src/quant_platform/strategy_report.py`
- Create `tests/test_strategy_report.py`

**RED:**

1. Fail closed when the verified CJK font file is unavailable or cannot be registered.
2. Prove user-controlled instrument and assumption labels are HTML escaped.
3. Prove the literal in-chart and HTML rules come from validated template/operator values:
   buy threshold, sell threshold, prior-close timing, next-open fill, initial-flat waiting mode,
   and all-in board-lot position size.
4. Prove BUY/SELL markers use event dates/raw Open prices and holding spans use actual event
   intervals.
5. Prove the report contains the required price/trend, slope/threshold, and cumulative-equity
   panels, summary fields, event/trade tables, provenance, price-return limitation, and no
   external component or network resource.

**GREEN:**

- Register `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc` and render one embedded PNG with
  three mobile-readable panels.
- Plot raw Close, fitted/smoothed trends, actual Open execution markers, and holding spans;
  plot slope plus configured thresholds; plot strategy, zero-cost, and buy-and-hold equity.
- Generate the chart rule box and concise Chinese prose from the canonical frozen config.
- Produce self-contained responsive HTML with base64 chart data and escaped tables/provenance.

**REFACTOR/GATE:** Keep chart/table construction deterministic, keep user data out of raw HTML
templates, and run focused report tests.

**Commit:** `feat(quant): render causal Chinese strategy report`

## Task 5: Atomic immutable run publication and CLI

**Files:**

- Create `src/quant_platform/strategy_runner.py`
- Modify `src/quant_platform/cli.py`
- Modify `src/quant_platform/__init__.py`
- Create `tests/test_strategy_runner.py`
- Modify `tests/test_platform_cli.py`

**RED:**

1. Cover `research strategy run --config FILE` one-line JSON success and one-line JSON failure.
2. Cover config JSON duplicate keys, NaN/infinity constants, and non-object roots.
3. Cover the complete required artifact set:
   `config.json`, `run_manifest.json`, `daily_replay.csv`, `events.csv`, `trades.csv`,
   `metrics.json`, `cost_breakdown.json`, and `report.html`.
4. Prove the run ID changes with canonical config, dataset identity, or effective source
   identity.
5. Prove publication uses a sibling staging directory, only exposes the final directory after
   all reconciliations/checksums succeed, and seals all files/directories read-only.
6. Prove an exact rerun verifies every checksum and returns `NO_CHANGE`.
7. Prove any missing, extra, mutable, corrupted, or identity-conflicting existing artifact
   fails closed without rewriting it.
8. Prove every emitted JSON file is strict finite JSON and canonical config/hash are persisted.

**GREEN:**

- Parse JSON with duplicate-key and non-finite hooks before validation.
- Hash the effective strategy source module bytes and bind that digest with config hash and
  verified dataset ID into the run ID.
- Write deterministic CSV/JSON/report artifacts to a private sibling staging directory.
- Build `run_manifest.json` last with identities, semantics, artifact hashes/sizes, and
  reconciliation results; verify staging; atomically rename; chmod files `0444` and directories
  `0555`.
- On an existing run, verify directory name, identities, exact artifact set, modes, sizes,
  checksums, canonical config, dataset integrity, and manifest before returning it unchanged.
- Limit CLI output to `{ok,status,run_id,path,config_sha256,dataset_snapshot_id}` or one strict
  JSON error object.

**REFACTOR/GATE:** Reuse canonical JSON and snapshot verification helpers without broadening
the CLI exception boundary. Run focused runner/CLI tests, including corruption paths.

**Commit:** `feat(quant): publish immutable config-driven runs`

## Task 6: Example, documentation, and acceptance verification

**Files:**

- Create `examples/bocom-causal-slope.json`
- Modify `README.md`
- Modify tests where packaging exports need coverage

**RED:**

1. Validate the committed example through the production config validator.
2. Assert its frozen values: 20-session OLS, EMA5, +/-0.20 percent/day, initial flat,
   all-in/all-out 100-share lots, CNY 100,000, start 2025-01-02, data-end evaluation,
   raw Open execution, and `AdjustedClose` signal.
3. Assert the CMS parameters and report label explicitly say they are conservative research
   assumptions and do not claim an account-specific commission.

**GREEN:**

- Add a portable example using relative `state/platform` and `runs/strategy` roots plus a
  clearly replaceable snapshot ID placeholder.
- Document snapshot ingestion, exact config ownership, operator catalog, assumptions,
  artifacts, idempotency/corruption behavior, CLI JSON contract, causal timing, price-return
  limitation, and the absence of any broker/live path.

**Verification sequence:**

1. Run each focused pytest file during its RED/GREEN task.
2. Run the complete Python test suite serially under the project Python 3.12 virtualenv.
3. Run Ruff.
4. Run `node --test tests/test_strategy_lab.js`.
5. Run Gitleaks against the worktree.
6. Validate the committed example and execute one deterministic synthetic CLI smoke.
7. Re-run the identical smoke and verify `NO_CHANGE`; corrupt a disposable copied run and
   verify closed failure.
8. Confirm all generated JSON rejects NaN/infinity, all committed files are English except the
   required Chinese report literals, and `git status --short` is empty after the final commit.

**Commits:**

- `docs(quant): document config-driven strategy runs`
- `test(quant): verify config-driven strategy runner`

## Acceptance criteria

- One JSON file drives a complete single-stock daily experiment and the CLI emits JSON only.
- Configuration ownership is exact, typed, versioned, collision-free, and closed to unknowns.
- Supported optional snapshot columns are preserved and identity-bound without changing v1 IDs.
- Every evaluation decision uses only history strictly before its raw Open execution session.
- Actual event costs, board lots, residual cash, terminal positions, trades, daily equity,
  benchmarks, and metrics reconcile exactly.
- Report markers are actual Open executions and all rule prose is generated from frozen config.
- Every required artifact is present under one verified immutable run directory.
- Exact reruns return the same verified run; corruption and conflicts never self-heal.
- Existing and new Python tests, Ruff, Node tests, and Gitleaks pass.
- The final branch contains only local commits and has a clean worktree.
