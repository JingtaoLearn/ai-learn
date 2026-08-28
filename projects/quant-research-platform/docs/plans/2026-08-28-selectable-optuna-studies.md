# Selectable Optuna Parameter Studies

- Owner issue: `#169`
- Status: implementation
- Production remains on the last reviewed release until every gate passes.

## Objective

A researcher selects published operator parameters, defines typed domains, and runs a version-frozen Optuna TPE ask/tell loop that searches for the best observed configuration under the existing nested walk-forward and governed-holdout protocol.

## Frozen request contract

Each search-space entry is exactly one of:

```json
{"kind":"categorical","choices":[10,20,30]}
{"kind":"int","low":10,"high":60,"step":5,"log":false}
{"kind":"float","low":0.1,"high":0.9,"step":0.05,"log":false}
```

For logarithmic integer/float domains, `step` is omitted and `log` is true. Existing `{"values":[...]}` remains valid for legacy Grid/Seeded Random Studies and is normalized to categorical for new UI submissions.

The search block freezes:

- suggester and adapter version;
- Optuna package version;
- sampler name and settings;
- seed;
- unique Trial and raw suggestion budgets;
- typed distributions;
- objective role and direction.

## Adaptive state machine per Search Round

1. Rebuild the Suggester from frozen identity and the platform Suggestion Journal.
2. If an asked unique candidate lacks an inner evaluation:
   - ensure its Trial exists;
   - dispatch/observe only that candidate's inner-fold bindings;
   - independently evaluate complete canonical evidence;
   - append one success/failure tell event.
3. Otherwise ask exactly one next candidate and append the proposal before dispatch.
4. Repeat until exhausted by the frozen unique/raw budget.
5. Select the round champion from independently persisted evaluations.
6. Outer rounds audit their selected candidate; the final round freezes one champion before holdout access.

No outer/holdout evidence is accepted by the Suggester.

## Persistence

Add an append-only per-Study/per-round Suggestion Journal containing canonical proposal and inner-evaluation events. Optuna state is reconstructed, never trusted as primary storage. A crash at any boundary must replay to the same next proposal or fail closed.

## UI

- Explicit checkbox for every eligible selected-version operator parameter.
- Type-aware domain editor based on JSON Schema.
- Fixed baseline remains visible.
- Accessible field-level and summary errors.
- Preview explains adaptive binding ranges rather than pretending an exact count.
- Study detail/report show selected domains, ordered proposals, objective values, and why the champion won.

## Verification gates

- RED/GREEN focused tests for distribution validation and true ask/tell ordering.
- Deterministic replay/resume and duplicate/failure tests.
- Adversarial outer/holdout leakage tests.
- Existing Grid/Random compatibility suite.
- Complete project suite, Ruff, lock verification, Gitleaks, container build.
- Real bounded Optuna Study on Feng with restart continuation and one governed holdout.
- Desktop/390px/keyboard/JavaScript/no-JavaScript browser acceptance.
- Fixed Git archive release, rollback, health/auth/readback.
