# Feishu Product Group Setup

This reusable runbook turns an existing private Feishu group into the user-facing entry point for a product suite. `AgenticWorkflow-Assistant` owns setup and conformance; `ProductOwnerAgent-<Product>` owns that product's decisions. Apply the example to the chosen product; it is not a mandatory self-demo before real-product testing.

The checked-in configuration example is [`feishu-group.desired.yaml`](feishu-group.desired.yaml). Instantiate it outside the framework checkout. Concrete runtime data stays in the installed Agent/product environment, including non-secret run state; credentials remain in their protected stores. GitHub Issues and product code repositories remain usable.

## Configuration example (not live state)

- Name: `Agentic Workflow V2`
- Description: `Agentic Workflow V2 产品群：唯一原生 AI Owner 入口，用于目标、决策、里程碑、风险与结果验收。`
- Avatar: [`../assets/group-avatar-v2.png`](../assets/group-avatar-v2.png)
- Type: private group chat
- Members: one human owner plus the Hermes bot
- Bot: group manager
- Route target: `productowneragentagenticworkflow`
- Activation probe: `AW-SUITE-ACTIVATE-002`, sent by the human owner

## Provisioning order

### 1. Discover before creating

Search for an existing product group and read its exact configuration. Reuse it when ownership and membership match. Never create a second group merely because the old conversation is not visible in the recent-chat list.

Record the live `chat_id` only in protected Hermes runtime configuration, never in this public repository.

### 2. Prepare and upload the avatar

Use the checked-in 512×512 PNG. Upload it with Feishu `image_type=avatar` and retain the returned `image_key` only long enough to apply the group update.

### 3. Apply identity and description

Update the existing group in one bounded write:

- name;
- description;
- avatar image key.

Do not create a replacement group or change its owner.

### 4. Harden permissions

Set the owner-controlled defaults declared in `feishu-group.desired.yaml`: owner-only member addition, no share card, owner-only metadata editing and `@all`, approval-required membership, and owner-only join/leave visibility and urgent messages.

If Feishu ignores a field or the current application identity cannot change it, record the read-back value as a platform limitation instead of claiming success.

### 5. Verify membership and authority

Read back and prove:

- the human remains group owner;
- exactly one expected human and one expected bot are present;
- the bot is a manager;
- the group remains private and uses normal chat messages;
- name, description, avatar and supported permissions match desired state.

Do not remove or impersonate the human owner.

### 6. Bind the native Hermes route

The default Gateway retains the sole Feishu credential. Configure exactly one route from the product group ID to its Product Owner Profile (the example uses `productowneragentagenticworkflow`; another product needs its own Owner). The Assistant maintains suite form, not product decisions.

Verify both the route and the multiplexer allowlist by reading them back. Do not add a second Gateway, callback daemon, message bus or workflow engine.

### 7. Establish the canonical Owner Session

Use a real human message in the product group to verify ingress and Owner identity. `AW-SUITE-ACTIVATE-002` is the reference probe, not a universal password or product requirement. Reuse an already verified native Owner context; do not demand repeat activation. A bot-authored seed message proves outbound delivery only.

Read back the Product Owner Session list and verify:

- one group-derived canonical Owner Session;
- reply delivered to the same group;
- a repeated activation message does not duplicate business work;
- a Gateway restart preserves routing and Session continuity.

### 8. Validate one real Outcome

Follow [`../VALIDATION.md`](../VALIDATION.md): an actual product Owner first supplies a baseline report in its product group and discusses it with the human, then independently chooses and delivers a real Outcome. Follow [`../REPORTING.md`](../REPORTING.md) for report-Thread behavior and non-blocking updates. The Owner may work directly or delegate preparation and execution.

For delegated work, use native Kanban or `message_agent` according to the task's durability needs. Completion is the observed product effect and owning-system read-back, not task-state transition alone.

### 9. Record evidence

Keep concrete execution and verification data in the existing Agent/product runtime, not in this framework repository. Preserve failures and limitations. Commit only reusable framework improvements; use Issues/PRs normally for collaboration and product code changes.

## Deprovisioning or replacement

Before introducing a newer suite, remove old triggers and routes, archive evidence, and prove that no old Profile, process or board remains active. One product has one current Owner runtime. Historical evidence may remain, but two executable suites may not.
