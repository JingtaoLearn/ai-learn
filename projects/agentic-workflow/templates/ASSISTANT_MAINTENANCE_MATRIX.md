# Assistant maintenance acceptance matrix

Use one runtime copy per maintained installation. Keep concrete Profile, Session,
task, run, route and schedule identities in that runtime copy, never in this
framework template.

| Responsibility | Status | Observed gap | Applied solution | Evidence | Remaining blocker |
|---|---|---|---|---|---|
| Provision usable teams | `VERIFIED` / `IMPLEMENTED_NOT_VERIFIED` / `BLOCKED` / `NOT_APPLICABLE` | | | | |
| Connect collaboration lifecycle | `VERIFIED` / `IMPLEMENTED_NOT_VERIFIED` / `BLOCKED` / `NOT_APPLICABLE` | | | | |
| Maintain operational awareness | `VERIFIED` / `IMPLEMENTED_NOT_VERIFIED` / `BLOCKED` / `NOT_APPLICABLE` | | | | |
| Recover ordinary faults | `VERIFIED` / `IMPLEMENTED_NOT_VERIFIED` / `BLOCKED` / `NOT_APPLICABLE` | | | | |
| Preserve continuity | `VERIFIED` / `IMPLEMENTED_NOT_VERIFIED` / `BLOCKED` / `NOT_APPLICABLE` | | | | |
| Improve the framework | `VERIFIED` / `IMPLEMENTED_NOT_VERIFIED` / `BLOCKED` / `NOT_APPLICABLE` | | | | |

## Evidence rules

- `VERIFIED` requires a live effect and owning-system read-back, not a file,
  configured task, delivery receipt, timer counter or Agent completion claim.
- `IMPLEMENTED_NOT_VERIFIED` means the mechanism exists but its required real
  tracer has not completed.
- `BLOCKED` names the exact protected, external or product-decision boundary and
  the durable owner of the next safe step.
- `NOT_APPLICABLE` explains why the responsibility does not apply to this scope.
- Provisioning evidence includes a real bounded Role turn in the intended
  workspace and capability boundary.
- Collaboration evidence includes a terminal event and a later responsible
  Owner turn that inspects persisted evidence.
- Operational-awareness evidence includes a scheduled invocation under the
  served Assistant Profile and the retained finite cadence.
- Recovery evidence preserves the original task/history and proves a new claim
  or successful bounded effect after the repair.
- Continuity evidence distinguishes Thread context from approved gateway restart
  and later organic-message continuity.
- Framework evidence names the reviewed immutable source revision and exact
  remote-main read-back while omitting runtime IDs and private data.
