# Product Group Registry Template

Use one local registry for discovery and duplicate prevention. It is not a workflow engine or source of product truth. Verify every row against live Feishu groups, Hermes Profiles, native profile routes, and Session state before acting.

| Product | Group | Avatar | Owner Profile | Canonical Owner Session | Status |
|---|---|---|---|---|---|
| `<Product>` | `<group name>` (`<chat_id kept only in private live copy>`) | `A-<ProductAbbrev>` | `<profile-id>` | `<session-id>` | `PROVISIONING / ACTIVE / PAUSED / RETIRED` |

Rules:

- One active group per product; identity is the exact private `chat_id`, never the display name.
- One canonical Owner Session per product; never create a replacement as a routing fallback.
- Preserve unrelated routes and records when changing one product.
- Resume partial provisioning from its recorded status instead of creating another group.
- Rename only the display name; never reuse a retired `chat_id` for another product.
- Group avatars use `A-<ProductAbbrev>`, for example `A-QR`: solid deep-blue background, white centered text, and no border, frame, gradient, icon, or decoration.
- Do not commit live chat IDs, user IDs, credentials, or private group links.
