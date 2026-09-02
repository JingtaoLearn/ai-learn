# ADR-0012: Preserve authoritative A-share cash-dividend and tax evidence

- **Status:** Accepted
- **Date:** 2026-08-31
- **Issue:** [#201](https://github.com/JingtaoLearn/ai-learn/issues/201)
- **V1 implementation scope:** Bank of Communications (`601328.SS`) on XSHG, held by an ordinary mainland individual investor through a supported public/transfer-market path. Other issuers, markets, SZSE instruments, account classes, and acquisition paths fail closed as `UNSUPPORTED_SCOPE` until a separately reviewed source contract admits them.

## Context

A price-return replay cannot answer a total-shareholder-return question when cash dividends, deferred individual dividend tax, source revisions, or entitlement dates are missing. A normalized vendor row is not enough: it does not preserve the official bytes, publication/retrieval evidence, corrections, conflicts, or proof that an empty interval contains no action.

This decision defines the evidence contract consumed later by #196 and #197. It does not implement accounting, claim verified total return, access a broker account, or create an order path.

The evidence-first BOCOM Spike reached `PARTIAL`:

- the real 2025 interim event materially changed strategy-versus-control results;
- a separate integer-fen oracle matched known-event account totals;
- complete interval action coverage could not be proven;
- the existing broad candidate incorrectly upgraded partial evidence and used wrong tax timing.

Therefore evidence state and accounting claim state must remain separate.

## Decision

### 1. Source authority is role-specific

Use this precedence by fact type:

1. **Issuer action terms:** an official exchange-hosted implementation announcement or official issuer-hosted implementation announcement. Preserve each raw artifact independently; accept normalized terms only when every available co-primary official artifact agrees on the load-bearing terms. Byte identity permits deduplication but is not required. Any disagreement is `QUARANTINED_CONFLICT`.
2. **Correction/supersession:** an explicit official correction or replacement that identifies the affected announcement/distribution.
3. **Tax law:** effective Ministry of Finance, State Taxation Administration, and CSRC instruments.
4. **Registration/collection workflow:** current official ChinaClear and applicable branch operating notices.
5. **Discovery/cross-check only:** exchange query indexes and dividend datasets.
6. **Discovery only:** commercial vendors, aggregators, media, search results, and derived adjusted-price events.

A lower-ranked source cannot replace a higher-ranked term. Unresolved same-role official disagreement is `QUARANTINED_CONFLICT`; no consumer may select one by timestamp, row order, or convenience.

AdjustedClose may identify a date requiring investigation. It never proves a dividend amount, entitlement, payment, tax, or absence of other actions.

### 2. Public acquisition is secret-free and fail-closed

Acquisition must be unauthenticated public HTTPS where available. No cookie, token, session, account credential, client certificate, CAPTCHA bypass, PII, or proxy credential may enter a request record, log, hash input, fixture, or artifact.

Allowed source boundaries in the BOCOM/XSHG v1:

- SSE announcement discovery: `https://www.sse.com.cn/disclosure/listedinfo/announcement/` and `https://query.sse.com.cn/security/stock/queryCompanyBulletin.do`;
- SSE announcement artifacts: `https://www.sse.com.cn/disclosure/listedinfo/announcement/**` and `https://static.sse.com.cn/disclosure/listedinfo/announcement/**`;
- issuer artifacts: exact public Bank of Communications file URLs recorded by the accepted event;
- tax instruments: exact `https://fgk.chinatax.gov.cn/**` URLs;
- ChinaClear operating evidence: exact official `www.chinaclear.cn` document URL, including the preserved historical HTTP URL where HTTPS is unavailable.

SZSE and non-BOCOM issuer acquisition are intentionally absent. A request outside the listed scope is `UNSUPPORTED_SCOPE`; callers may not fall back to search results, vendor rows, or another issuer's contract.

Every redirect hop and final URL must be revalidated against the source-specific host/path allowlist. An unexpected host, path, authentication request, response media type, or redirect is terminal failure.

Retry only transport timeout/reset, HTTP 429, and HTTP 500/502/503/504. Permit at most three immutable attempts with deterministic delays of 1 and 4 seconds. Do not combine partial pages or responses. Exhaustion yields `UNKNOWN_MISSING`.

### 3. Preserve raw evidence before normalization

For every retrieval, preserve:

- canonical request method, URL, ordered query parameters, non-secret allowlisted headers, and request ID;
- start/completion timestamps and attempt number;
- every redirect status/location;
- final URL, status, allowlisted response metadata, and retrieval ID;
- exact transfer-decoded response body bytes;
- byte length, ordinary SHA-256, media type, and content-addressed artifact ID;
- collector version and source-contract version.

The body bytes are immutable content-addressed artifacts in the platform evidence store. A URL, claimed hash, extracted text, fixture, or repository metadata without the exact bytes cannot satisfy accepted source evidence.

Repository tests may use bounded synthetic fixtures. They do not replace production source bytes.

### 4. Keep publication, retrieval, and use role distinct

Record separately:

- official publisher date/time exactly as stated, including timezone and precision when available;
- exchange/issuer index observation and locator;
- first successful platform retrieval time;
- parser version and normalization time;
- evidence use role: `ACCOUNTING_OUTCOME` or `CAUSAL_FEATURE`.

Date-only publisher metadata remains date precision; it is never rewritten as a precise timestamp.

A later-retrieved official implementation announcement may establish an actual historical cash-flow outcome for retrospective accounting. It must not become a pre-event strategy feature. `CAUSAL_FEATURE` use requires evidence availability no later than the applicable decision cutoff.

Freshness is role-specific:

- accepted raw announcement artifacts and Event Revisions are immutable and do not expire; a later official correction creates a new revision and a new checked-as-of projection;
- a discovery/index response is usable only in the retrieval in which it was fetched and remains `UNKNOWN_MISSING` for absence; it is never cached into no-action evidence;
- every coverage projection records `checked_as_of` and includes all accepted revisions retrieved by that instant; a later retrieval never rewrites the earlier projection;
- a historical tax policy is selected by the event record date and its effective interval, not by a generic "current" flag;
- before admitting a new future/current record date, the tax-policy source contract must be checked on that UTC date against the official tax policy collection. An inaccessible, malformed, superseded, or unchecked response yields `UNKNOWN_MISSING`, not a stale policy fallback.

### 5. Normalize events without inferring absent facts

An accepted cash-dividend event revision contains only:

- schema version;
- instrument, market, currency, and event class `CASH_DIVIDEND`;
- official announcement/distribution identifier;
- official announcement date or explicit `UNKNOWN`;
- record date, ex-date, and pay date as `PRESENT`, `EXPLICITLY_ABSENT`, or `UNKNOWN` with evidence pointers;
- gross cash per share as an exact decimal string and published basis/original wording;
- entitlement population, cutoff, registration/settlement wording, and declared exclusions;
- source artifact/retrieval IDs and source URL;
- logical event ID, immutable revision ID, parser version, and normalization digest;
- explicit correction/supersession link where officially stated;
- acceptance state and bounded validation findings.

Required BOCOM-ready dates satisfy:

```text
record_date < ex_date <= pay_date
```

The amount must be issuer-declared gross CNY cash per A share. Net-only, ambiguous basis, missing currency, missing required date, or unknown entitlement is not accepted.

The first accepted implementation notice creates an immutable Event Series Key from instrument, event class, and its root notice identifier. Every source notice retains its own Notice ID. A later correction joins the same Event Series only when it explicitly identifies a notice already in that series; following explicit links reaches the root notice and therefore the same Event Series Key. A correction with no explicit affected-notice relationship is a separate candidate/conflict, never a similarity merge.

A logical event ID binds the Event Series Key. A revision ID additionally binds the contributing Notice IDs, normalized terms, source artifact identities, and explicit correction links. Similarity cannot manufacture a correction or merge two events.

### 6. Represent coverage honestly

Every requested interval has exactly one coverage state:

- `VERIFIED_EVENTS`: one or more accepted events are proven, but no claim is made about absent dates;
- `VERIFIED_NO_ACTION`: a complete authoritative enumeration contract proves zero relevant events for the entire bounded interval;
- `UNKNOWN_MISSING`: absence, completeness, conflict, freshness, or required terms cannot be proven.

Current SSE `commonQuery.do` dividend data and `queryCompanyBulletin.do` announcement index do **not** expose an immutable completeness, pagination/retention, revision, correction, and supersession guarantee. Empty query results therefore prove only that those requests returned no rows. They cannot produce `VERIFIED_NO_ACTION`.

Until a stronger official enumeration contract is admitted, a BOCOM interval containing accepted notices is `VERIFIED_EVENTS`, and all uncovered absence remains `UNKNOWN_MISSING`.

Consumers must never map `UNKNOWN_MISSING` to an empty action array or zero cash.

### 7. Preserve every correction and conflict

Raw artifacts and normalized revisions are append-only. An explicit official correction creates a new revision linked to the revision it corrects/supersedes. No mutable `latest` event is evidence authority.

A convenience projection may identify the currently accepted revision only after verifying the entire chain. A missing, cyclic, dangling, changed, or conflicting chain fails closed. Manual review may correct source classification or bind an explicit official correction; it cannot alter source bytes/terms, infer a correction, choose between genuine official conflicts, or upgrade incomplete coverage.

### 8. Freeze dividend-tax policy as separate evidence

For record dates after 2015-09-08, policy `CN-INDIVIDUAL-A-2015-101-v1` binds 财税〔2015〕101号, the retained operating rules of 财税〔2012〕85号, and the ChinaClear implementation notice.

Supported current brackets are:

- holding period up to and including one natural month: 100% taxable income at 20%, actual burden 20%;
- over one natural month through and including one natural year: 50% taxable income at 20%, actual burden 10%;
- over one natural year: temporarily exempt.

One month/year uses the official natural-month/year definition: from the acquisition date through the day before the corresponding date in the next month/year.

Holding period runs from acquisition through the day before **transfer settlement**, not an assumed order or trade date. FIFO applies per securities account. Quantities use post-settlement end-of-day holdings and net increases/decreases. Multiple custodians for one account are calculated separately under the ChinaClear notice.

For holdings no longer than one year, the issuer initially does not withhold at distribution. After transfer settlement, ChinaClear calculates the amount, sends collection details before the next trading day's open, and completes full collection through settlement reserves at that day's end after broker/custodian confirmation. Insufficient funds remain an outstanding tax obligation and cannot be represented as paid.

The official instruments do not establish:

- the trade-date-to-settlement-cycle mapping;
- a CNY rounding algorithm;
- an exact insufficient-funds deadline or partial-collection order;
- an intraday ordering rule for multiple same-day acquisitions/disposals;
- a general basis-allocation algorithm for unsupported corporate actions.

Those facts remain `UNKNOWN` unless separately evidenced. A research rounding assumption is versioned and labeled as an assumption, never an official rule.

### 9. Separate evidence state from accounting claim

An accepted event with incomplete interval coverage permits only:

```text
KNOWN_EVENT_CORRECTED_PARTIAL
```

It does not permit:

```text
AFTER_TAX_TOTAL_RETURN_VERIFIED
```

Only a trusted evidence/qualification layer may issue a total-return claim after verifying coverage, event revisions, tax policy applicability, settlement/account scope, exact per-account ledger reconciliation, and control-account parity. A replay engine cannot upgrade its caller's evidence state.

## Verified BOCOM examples

| Root notice | Record | Ex/pay | Gross/share | Issuer artifact SHA-256 |
|---|---|---|---:|---|
| `临2025-079` | 2025-12-24 | 2025-12-25 | CNY 0.1563 | `c2da69cd9ababa957c029dfd4a11fcca08efb66b73d0bac381024676ffd1f7a6` |
| `2026-026` | 2026-07-09 | 2026-07-10 | CNY 0.1684 | `e33ef7d991f3fcc4a8dba338db431c474d5ed50283b5c1ed241e3987ca4913b9` |

The first issuer PDF is 149,588 bytes; the second is 149,101 bytes. Both define entitlement as A-share holders registered with ChinaClear Shanghai after SSE close on the record date and state no differentiated distribution.

Direct attempts to retrieve both SSE PDF locators returned HTML challenge responses, not PDF artifacts. Those failed response bytes and hashes are preserved in the research evidence manifest and are not accepted event evidence. The official SSE locators remain provenance/discovery facts; the verified terms above come from the issuer-hosted official PDFs. These two events prove themselves. They do not prove that no other event exists in any larger interval.

## Canonical identities

For JSON identities, compute lower-case SHA-256 over `UTF8(domain_tag) || 0x00 || canonical_payload`. The domain tags are literal values frozen in [`../fixtures/corporate-actions/identity-v1.json`](../fixtures/corporate-actions/identity-v1.json). The ID is stored beside, never inside, its ID-free payload.

Canonical payload is UTF-8 JSON with object keys sorted by Unicode code point, no insignificant whitespace, exact array order, no duplicate keys, and `ensure_ascii=false`. Identity payloads permit only strings, integers, booleans, null, arrays, and objects; floating-point values are forbidden. Economic decimals are canonical strings; money posting uses integer fen after an explicitly versioned rounding policy.

The v1 normalized request payload is exactly `{schema_version, method, url, query, headers}`. `method` is `GET`; `url` is an absolute HTTPS URL without credentials, fragment, or query, except for the one exact allowlisted historical ChinaClear HTTP artifact; scheme/host are lower-case and default port is absent; path and percent escapes are the source-contract literal; `query` and `headers` are objects with unique keys and string values; header names are lower-case; only source-contract allowlisted headers are present. Duplicate query/header names, multi-value headers, implicit defaults, or alternate percent encoding are rejected instead of normalized. The canonical query is rendered for transport by sorted key, without changing the identity payload.

For raw artifacts, compute SHA-256 over `UTF8("quant-platform/source-artifact/v1") || 0x00 || exact_body_bytes`; preserve ordinary body SHA-256 separately.

- `artifact_id`: domain-separated digest of exact raw body bytes;
- `request_id`: digest of the closed normalized request;
- `retrieval_id`: digest of request ID, attempt, redirect/final response facts, timestamps, and artifact ID or terminal failure;
- `logical_event_id`: digest of instrument, event class, and root Event Series Key;
- `event_revision_id`: digest of logical event ID, contributing Notice IDs, exact normalized terms, source artifacts, parser version, and explicit correction links;
- `coverage_id`: digest of interval, relevant event revisions, query/retrieval evidence, coverage state, and limitations;
- `tax_policy_id`: digest of a separate human policy family/version plus policy scope, effective record-date interval, brackets, holding-period/FIFO/account/collection semantics, assumptions/unknowns, checked-at evidence, and exact source artifacts.

The identity fixture contains the literal ID-free payloads, canonical JSON strings, domain tags, and expected digests for one BOCOM request/artifact/retrieval, Event Series, Event Revision, Coverage State, and tax policy. Implementations must reproduce every expected digest before admission.

## Minimal fixtures

[`../fixtures/corporate-actions/decision-v1.json`](../fixtures/corporate-actions/decision-v1.json) records bounded cases for:

- unsupported no-action proof;
- the real BOCOM 2025 interim dividend;
- deterministic multiple dividends;
- explicit correction;
- same-rank conflict;
- missing date;
- tax bracket boundaries and unknown rounding.

[`../fixtures/corporate-actions/identity-v1.json`](../fixtures/corporate-actions/identity-v1.json) provides independently reproducible, non-circular identity vectors.

Only the BOCOM and tax source metadata/digests represent retrieved official facts. Synthetic cases use `fixture.invalid` and exist only to freeze expected decision states.

## Research audit package

The documentation-only #201 change retains two compact audit records:

- [`../evidence/bocom-total-return-2026-08-31/evidence-manifest.json`](../evidence/bocom-total-return-2026-08-31/evidence-manifest.json) binds every verified/rejected retrieval to its official URL, exact byte count/hash, retrieval time basis, media type, and preserved host locator;
- [`../evidence/bocom-total-return-2026-08-31/spike-result.json`](../evidence/bocom-total-return-2026-08-31/spike-result.json) binds the predeclared Spike record, code/Snapshot/config identities, executed A/B control, deterministic diagnostic, independent oracle, decision effect, and accepted `PARTIAL` constraint.

Those records make this research decision auditable on the development host. They are not a substitute for #196's production content-addressed evidence store and cannot be consumed as accepted Dataset evidence.

## Primary official evidence

1. BOCOM `临2025-079`: <https://www.bankcomm.com/BankCommSite/file/fileDownload.html?fileId=94697c067ebe4427a4165910712df44d>
2. SSE copy of `临2025-079`: <https://www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-12-19/601328_20251219_V61A.pdf>
3. BOCOM `2026-026`: <https://www.bankcomm.com/BankCommSite/file/fileDownload.html?fileId=0aba78fdf863447ab12a7068f7714ccf>
4. SSE copy of `2026-026`: <https://www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-07-06/601328_20260706_SZOE.pdf>
5. 财税〔2015〕101号: <https://fgk.chinatax.gov.cn/zcfgk/c102416/c5203902/content.html>
6. 财税〔2012〕85号: <https://fgk.chinatax.gov.cn/zcfgk/c102416/c5204415/content.html>
7. ChinaClear implementation notice: <http://www.chinaclear.cn/old_files/1354262382580.pdf>

Verified raw tax-source identities:

| Document | Bytes | SHA-256 |
|---|---:|---|
| 财税〔2015〕101号 | 38,314 | `9eadaaa4693282e4a9b65ca5f02b0b2ab4720e6b0cfe6da97be4c1f1faa9845b` |
| 财税〔2012〕85号 | 41,373 | `5cce33fece6d57c9de662086927a822092b66e0f92cfeec23e59e469e16d04e8` |
| ChinaClear notice | 116,151 | `6ed1598a2bf5d841d95a149b88543eaf8f8375f8acb53e8d8369fb329016b5ae` |

## Consequences

- Known events can be preserved and used for explicitly partial retrospective correction.
- Complete total return remains blocked until absence/coverage, settlement, rounding assumptions, per-account ledgers, and controls are honestly represented.
- Source bytes and evidence identities become part of Dataset/Result lineage rather than mutable vendor metadata.
- Fail-closed unknowns are expected outcomes, not ingestion failures to hide.
- No broker/account credentials or automatic trading capability is introduced.

## Rejected alternatives

- **AdjustedClose-derived cash:** rejected because adjusted prices do not prove an issuer cash flow.
- **Empty query means no action:** rejected because current official query contracts do not prove completeness.
- **One complete-looking vendor row:** rejected because it loses raw bytes, causal/publication evidence, revisions, and conflicts.
- **Replay emits Verified:** rejected because computation cannot upgrade evidence authority.
- **Trade-date tax:** rejected because official rules use transfer settlement and end-of-day settlement records.
- **One timeless tax rate/rounding rule:** rejected because applicability, holding period, collection timing, and rounding evidence are distinct.
