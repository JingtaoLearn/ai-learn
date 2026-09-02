# ADR-0013: Bound XSHG daily execution and market-friction claims

- **Status:** Proposed
- **Date:** 2026-08-31
- **Issue:** [#202](https://github.com/JingtaoLearn/ai-learn/issues/202)
- **Scope:** ordinary mainland individual cash-account replay for BOCOM (`601328`, XSHG main board)
- **Research verdict:** `PARTIAL`

## Decision artifacts

- [Boundary fixtures](../fixtures/execution/xshg-daily-v1.json)
- [Official-source evidence manifest](../evidence/xshg-execution-2026-08-31/evidence-manifest.json)
- [Claim-level official clauses](../evidence/xshg-execution-2026-08-31/claims-v1.json)

## Context

The current BOCOM Dataset Snapshot contains only:

```text
Date, Open, High, Low, Close, Volume, AdjustedClose
```

Its provider is `yahoo-chart-api`. It has no official suspension/resumption state, rule version, order type, submitted price or quantity, exchange-host receipt time, auction matched/unmatched quantity, queue position, broker execution report, client commission schedule, or cash-availability record.

The concrete official suspension/resumption authority is the XSHG [stock suspension page](https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/) and its page-defined `https://query.sse.com.cn/commonSoaQuery.do` request with `sqlId=GW_PL_JYTS_TFPXX`. The evidence package preserves the page, query script, exact BOCOM Spike-interval request record, and raw response. That response returned zero rows for `601328` from `2025-08-15` through `2026-06-30`; the claim is bounded to that exact query and does not prove unbounded source completeness.

A daily replay must therefore not turn the existence of an `Open` value into a claim that a hypothetical account could buy or sell its requested quantity at that price.

## Decision

### 1. Accept a bounded research approximation, not an attainable-fill claim

For the current daily-only source, v1 may use a named and versioned approximation:

`XSHG_DAILY_OPEN_FULL_FILL_RESEARCH_APPROXIMATION_V1`

It means:

1. the replay has a genuine non-zero daily opening print;
2. the session and instrument are not known to be suspended;
3. the applicable rule version and ordinary-main-board price-limit regime are known;
4. required data are fresh and non-conflicting;
5. the conservative limit-queue rules below do not reject the order;
6. the full requested quantity is priced from the reported daily open under a separately versioned spread/slippage assumption.

This is deterministic and useful for bounded research. It is not official evidence of order attainability or full fill.

Every Experiment or Study containing any such approximate fill is:

- eligible for research comparison;
- required to disclose the approximation and affected orders;
- ineligible for deployment qualification.

Only imported broker/exchange execution evidence, or a future independently accepted execution-evidence contract with sufficient order-level data, may issue an `ATTAINABLE_EXECUTION_VERIFIED` claim.

### 2. Use the date-correct XSHG rule version

- The archived 2023 Trading Rules (`上证发〔2023〕32号`) govern the BOCOM Spike interval `2025-08-15` through `2026-06-30`.
- The 2026 Trading Rules (`上证发〔2026〕41号`) apply from `2026-07-06` and explicitly repeal the 2023 rules.
- No 2026 rule may be backcast into the Spike interval.
- A replay date without a preserved applicable rule version fails closed.

The relevant ordinary-stock rules are materially consistent across the two preserved versions, but their separate identities remain part of the replay evidence. Historical risk-warning and special-board regimes are not inferred from the current rules.

### 3. Treat the official open as an observed first trade, not a guaranteed auction fill

Under XSHG Trading Rules 4.1.1 and 4.1.2:

- the open is the first auction-trading execution of the day;
- it normally comes from the opening call auction;
- if the opening call does not produce an open, the first continuous-auction trade becomes the open.

Therefore daily `Open` does not prove a 09:25 execution. It does not carry the first-trade time, the order book, the matched quantity, or the hypothetical account's queue position.

XSHG Rules 3.5.1 and 3.5.2 establish price/time priority and one call-auction clearing price. They do not guarantee any quantity for a newly introduced hypothetical order.

XSHG Rule 3.3.6 limits market declarations to continuous trading unless otherwise provided. A replay that imagines participation in the opening call must represent a bounded hypothetical limit order, not an exchange market order.

### 4. Fail closed for absent, stale, suspended, or contradictory evidence

No fill is recorded when any required condition is unknown or false:

- missing, null, zero, or vendor-synthesized open;
- full-day suspension or date-specific trading eligibility not established;
- stale bar or incomplete session;
- conflicting provider and official status evidence;
- unknown rule version or special regime;
- impossible price under the applicable price-limit/tick rules;
- request for a same-day sale of an ordinary A-share lot bought that day.

The replay may advance an unfilled intent to the next verified eligible session only through an explicit `DELAYED_UNTIL_ELIGIBLE_SESSION` policy. A delayed fill is not renamed as the originally requested next-open fill.

### 5. Price-limit states are legal bounds, not liquidity evidence

For ordinary main-board BOCOM under both preserved rule versions:

- the default daily limit is 10%;
- the limit is computed from previous close;
- A-share tick size is CNY 0.01;
- the rule-prescribed rounding applies;
- IPO, relisting, delisting-consolidation, risk-warning, and exchange-designated exceptions require their own date-specific evidence.

A daily open at the upper limit proves that some first trade occurred there. It does not prove a hypothetical buy could fill. The equivalent applies to a lower-limit sell.

The daily-only conservative policy is therefore:

- buy request with open at upper limit: `NO_FILL_CONSERVATIVE_LIMIT_QUEUE`;
- sell request with open at lower limit: `NO_FILL_CONSERVATIVE_LIMIT_QUEUE`.

This may create false negatives. It is a fail-closed research policy, not an XSHG rule.

An interior open still does not prove full fill; it remains the named research approximation.

### 6. Preserve T+1 account constraints without inventing broker cash behavior

XSHG Rules 3.1.4 and 3.1.5 state that securities bought by an investor may not be sold before settlement unless the product is eligible for round-trip trading. Ordinary A-shares are not in the same-day round-trip list.

ChinaClear Settlement Rules provide, for multilateral net settlement:

- T-day clearing from executed trades;
- required securities delivery and securities receipt at T-day end;
- final participant funds settlement by T+1 16:00;
- T+1 settlement batches from 09:00 through 16:00.

The rules also say that receivable securities for securities-company brokerage business are not marked with the DVP `saleable settlement lock`, while the member remains responsible for sufficient funds.

Accordingly, the bounded ordinary-A-share account replay must:

- prohibit sale of a lot on its purchase session;
- make a successfully purchased lot saleable no earlier than the next eligible trading session;
- retain trade date and settlement state separately;
- never infer dividend-tax holding dates from a generic `T+1` label; #201/#197 own that mapping.

The preserved official sources do not establish one universal retail rule for same-day reuse or withdrawal of sale proceeds. Broker account terms and actual account records control that fact. In their absence, v1 uses the conservative research assumption `SALE_PROCEEDS_REUSABLE_NEXT_ELIGIBLE_SESSION`, disclosed and deployment-ineligible.

### 7. Separate mandatory charges from broker/model assumptions

For ordinary XSHG A-share trades in the preserved current sources:

| Component | Current fact | Replay treatment |
|---|---|---|
| ChinaClear transfer fee | 0.01‰ of transaction amount, both buyer and seller | mandatory, separately identified |
| Securities transaction stamp tax | seller only; statutory 1‰ rate, halved from 2023-08-28 | mandatory 0.5‰ seller charge for covered dates |
| SSE handling fee | 0.00341% both sides, charged to members | do not add separately to a retail account when the chosen commission schedule already includes exchange handling |
| CSRC regulatory fee | 0.002% both sides, collected through SSE from members | do not add separately when included in retail commission |
| Retail commission | capped historically at 3‰, not below collected regulatory/exchange fees; A-share trade minimum CNY 5 under `证监发〔2002〕21号` | exact client rate remains broker/account-specific and versioned |
| Spread/slippage | no universal official constant | versioned model assumption only |

The current research policy keeps the predeclared BOCOM assumption:

- commission rate: 0.03% each side;
- minimum commission: CNY 5 per trade;
- transfer fee: 0.01‰ each side;
- stamp tax: 0.05% on sells for dates from 2023-08-28;
- buy/sell slippage: 5 bps each side.

Only the transfer fee and stamp-tax direction/rate are accepted current mandatory-charge facts. The commission and slippage values are research assumptions. The commission is treated as inclusive of exchange handling and regulatory fees to prevent double counting.

The 5-bps value is one combined spread-plus-slippage cash-friction assumption, not 5 bps for each component. The reported daily open must already be a valid CNY 0.01 tick. For quantity `q` and reported open `p`:

```text
open_notional_fen = exact(p * q * 100)
friction_fen = round_half_up(open_notional_fen * 0.0005)
buy_execution_value_fen = open_notional_fen + friction_fen
sell_execution_value_fen = open_notional_fen - friction_fen
display_average_price = execution_value_cny / q
```

`display_average_price` is a synthetic average, may be sub-tick, and is never submitted or represented as one exchange order price. Commission, transfer fee, and stamp tax are computed from `execution_value_fen` and each component is rounded independently to integer fen. The numeric fixture freezes both an ordinary and a delayed buy vector.

All money is represented in integer fen. Per-component rounding is `ROUND_HALF_UP_TO_FEN_RESEARCH_ASSUMPTION` until an account-specific broker statement proves a different rule. This rounding assumption also prohibits deployment qualification.

### 8. Preserve the execution claim independently from accounting and evaluation

An execution result records at least:

- instrument and XSHG market identity;
- rule version and source artifacts;
- requested decision session and actual execution session;
- direction, quantity, hypothetical order type, and limit/protection price;
- source bar identity and evidence freshness;
- suspension/limit/special-regime state;
- fill status and the exact approximation policy version;
- price, charge-policy identity, and integer-fen charges when a research fill exists;
- settlement states for cash and securities;
- qualification effect and limitations.

A downstream accounting or evaluation layer may not upgrade a research approximation into a verified attainable execution.

## Field-availability decision for the current BOCOM source

| Required fact | Current Snapshot | Consequence |
|---|---|---|
| Date and daily OHLCV | available | observed price/volume facts only |
| Genuine first-trade timestamp | absent | open phase is unknown |
| Official suspension/resumption state | absent | must be joined from official evidence or fail closed |
| Applicable rule/special regime | absent | must be versioned externally |
| Order type, limit, quantity | absent from market data | supplied by the replay request, not observed |
| Host receipt time / queue position | absent | attainability cannot be verified |
| Auction matched/unmatched quantity | absent | full opening fill cannot be verified |
| Broker execution report | absent | only research approximation possible |
| Broker commission schedule | absent | versioned assumption required |
| Cash reuse/withdrawal evidence | absent | conservative next-session assumption required |

## Qualification states

- `ATTAINABLE_EXECUTION_VERIFIED`: exact imported execution evidence satisfies a separately accepted contract.
- `RESEARCH_APPROXIMATE_FILL`: bounded deterministic daily fill; research only.
- `NO_FILL_CONFIRMED_INELIGIBLE`: official evidence proves no eligible execution.
- `NO_FILL_CONSERVATIVE_LIMIT_QUEUE`: daily-only limit state rejected conservatively.
- `NO_FILL_MISSING_REQUIRED_EVIDENCE`: missing, stale, conflicting, or unsupported evidence.
- `DELAYED_RESEARCH_APPROXIMATE_FILL`: intent reaches a later eligible session under an explicit carry policy.
- `REJECTED_SETTLEMENT_RESTRICTION`: T+0 ordinary-A-share sale or unsupported cash reuse.

Only the first state can support deployment qualification.

## Evidence preservation

The research manifest preserves exact official response bytes, URLs, sizes, and SHA-256 values outside Git. Repository evidence metadata is audit material, not a production evidence store.

The preserved authorities include:

- XSHG 2023 and 2026 Trading Rules and their official lifecycle pages;
- ChinaClear Settlement Rules and Shanghai funds-settlement guide;
- current ChinaClear Shanghai fee table;
- current SSE charge and collected-regulatory-fee pages;
- stamp-tax law and 2023 half-rate notice;
- commission cap/minimum notice;
- supporting DVP, transfer-fee, and dividend FIFO materials.

The claim catalog binds each accepted claim to an exact document identifier (or explicitly records that the official source shows no promulgation number), effective interval, clause number, quotation, and preserved source artifact. The evidence manifest alone is not used as a substitute for clause-level support.

## Alternatives rejected

### Treat every daily open as fully attainable

Rejected. It fabricates order type, queue priority, opposing liquidity, quantity, and timing.

### Reject all daily replay

Rejected. A named approximation is reproducible and useful for research if its qualification boundary is enforced.

### Infer suspension from a missing row or zero volume

Rejected. Vendor encoding is not an official suspension state.

### Fill at a limit price because at least one trade occurred

Rejected. A legal price and an observed trade do not prove allocation to the hypothetical account.

### Add exchange handling and regulatory fees on top of commission

Rejected. The cited retail commission definition includes collected regulatory and exchange fees; separate addition would double count under that policy.

### Model broker order books or automatic trading

Rejected. The platform remains an account-replay research system with no broker credential, order, routing, or fill path.

## Consequences

### Positive

- Existing daily data remain usable for honest bounded research.
- Every approximate fill becomes visible and machine-gated.
- #197 gets a deterministic T+1/charge boundary without pretending broker behavior is universal.
- #140 knows which official state fields must be acquired separately from OHLCV.
- Production qualification cannot silently rely on an impossible next-open assumption.

### Negative

- No current Yahoo daily-only BOCOM replay can qualify for deployment.
- Conservative limit handling creates known false negatives.
- A future deployable signal requires imported actual executions or richer independently governed market/order evidence.

## Follow-up implementation order

1. Merge #201 and this decision.
2. #196 freezes corporate-action evidence and honest Coverage State.
3. #197 implements the smallest integer-fen account ledger, T+1 restrictions, and versioned cost policy using these fixtures.
4. #198 keeps execution qualification independent from total-return qualification.
5. #140 may add official suspension/session evidence, but no adapter may fabricate queue or fill evidence.

No production signal changes are authorized by this ADR.
