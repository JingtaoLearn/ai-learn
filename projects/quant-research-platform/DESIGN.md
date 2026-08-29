---
version: alpha
name: Proofline

description: Calm evidence workbench for immutable quantitative research, using graphite neutrals and a spectral-cyan interaction color.
colors:
  primary: "#00677A"
  primary-hover: "#005364"
  primary-active: "#00424F"
  background: "#F4F6F7"
  shell: "#10191F"
  surface: "#FFFFFF"
  layer: "#E9EEF1"
  text: "#11181C"
  text-secondary: "#46545D"
  text-helper: "#596871"
  border: "#CCD6DB"
  border-strong: "#71808A"
  focus: "#7127A8"
  success: "#146C43"
  warning: "#7A4D00"
  danger: "#A3212B"
  info: "#115EA3"
  inverse: "#FFFFFF"
typography:
  h1:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 2rem
    fontWeight: 680
    lineHeight: 1.19
    letterSpacing: "-0.025em"
  h2:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 1.5rem
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.015em"
  h3:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 1.125rem
    fontWeight: 650
    lineHeight: 1.33
    letterSpacing: "-0.01em"
  body:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 0.875rem
    fontWeight: 400
    lineHeight: 1.43
    letterSpacing: "0em"
  body-large:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 1rem
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "-0.005em"
  label:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 0.75rem
    fontWeight: 600
    lineHeight: 1.33
    letterSpacing: "0.02em"
  mono:
    fontFamily: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace
    fontSize: 0.8125rem
    fontWeight: 400
    lineHeight: 1.54
    letterSpacing: "0em"
rounded:
  none: 0px
  small: 2px
  control: 4px
  panel: 6px
  pill: 999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  2xl: 48px
  3xl: 64px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.inverse}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: 44px
    padding: 16px
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.inverse}"
  button-danger:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.inverse}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: 44px
    padding: 16px
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: 44px
    padding: 12px
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: 24px
  navigation:
    backgroundColor: "{colors.shell}"
    textColor: "{colors.inverse}"
    typography: "{typography.body}"
    rounded: "{rounded.none}"
    height: 52px
---

## Overview

Proofline is an internal evidence workbench, not a marketing dashboard. It should feel precise, calm, dense without being cramped, and trustworthy during prolonged use. It borrows Carbon's disciplined grid, flat layers, and enterprise clarity without copying IBM branding, typography, iconography, component geometry, or blue palette.

Research lineage comes before decoration. Each first viewport exposes the current state, human identity, immutable identity, provenance, and next legitimate action. Decision and operational facts appear before configuration, logs, event payloads, and raw JSON.

## Colors

### Light theme

- Canvas `#F4F6F7`; shell `#10191F`; layer 01 `#FFFFFF`; layer 02 `#E9EEF1`; layer 03 `#DDE5E9`.
- Primary text `#11181C`; secondary `#46545D`; helper `#596871`; subtle border `#CCD6DB`; strong border `#71808A`.
- Interactive `#00677A`; hover `#005364`; active `#00424F`; link `#005F73`; focus `#7127A8`.
- Success text/surface `#146C43/#E4F7ED`; warning `#7A4D00/#FFF2CC`; danger `#A3212B/#FCE7E8`; info `#115EA3/#E5F1FF`.

### Dark theme

- Canvas `#0B1114`; shell `#081014`; layer 01 `#111A1F`; layer 02 `#18242A`; layer 03 `#213038`.
- Primary text `#F1F5F6`; secondary `#B7C2C7`; helper `#9EADB4`; subtle border `#34434B`; strong border `#657681`.
- Interactive `#67D5EA`; hover `#8DE1F0`; active `#42B8D0`; on-interactive `#062027`; link `#7CDBEE`; focus `#D49CFF`.
- Success text/surface `#74D9A7/#113427`; warning `#FFD080/#3B2B10`; danger `#FF9DA3/#401B20`; info `#9CC8FF/#142E4A`.

Status color is semantic only: green completed/valid, amber paused/drift/attention, red failed/destructive, blue informational, gray queued/neutral. Text is mandatory; color is supplemental.

Verified WCAG pairs exceed AA: light primary 16.54:1, secondary 7.21:1, helper 5.32:1, interactive on white 6.52:1, focus on canvas 7.57:1; dark primary 17.32:1, secondary 10.46:1, helper 8.23:1, on-interactive 9.88:1, focus on canvas 9.05:1.

## Typography

- Use the CJK-safe system stack; make no external font request.
- Product pages use 32/38px H1 desktop and 28/34px mobile, 24/30px H2, 18/24px H3, and 14/20px body.
- Use monospace only for immutable IDs, digests, versions, code, parameters, and numeric evidence.
- Enable tabular numerals and slashed zero where supported. Never use ligatures that alter the appearance of hashes or code.
- Human labels lead; machine identity follows. Long IDs wrap in detail views and use a unique 12-character prefix plus copy affordance in lists.

## Layout

- Use a 4px base grid with primary spacing steps 8, 16, 24, 32, 48, and 64px.
- Desktop at 1024px+: 52px sticky top bar, 240px persistent left rail, content width up to 1600px, 32px gutters (40px at 1440px+).
- Group desktop navigation by task: Overview; Create; Research; Catalog; Evidence. The active item uses `aria-current="page"` and a 3px cyan rail.
- Mobile below 768px: 52px top bar, a high-contrast 44px account/utility control, a fixed five-item bottom primary navigation, 16px content gutters, single-column forms, stacked definition lists, and safe-area-aware sticky actions.
- Mobile primary navigation contains Overview, Operators, New experiment, Studies, and History as ordinary links with `aria-current="page"`; each target is at least 56px high. Template, theme, and POST logout remain available in a native/no-JavaScript-capable utility disclosure from the top bar.
- Core navigation and submission must remain available without JavaScript. Fixed navigation reserves `env(safe-area-inset-bottom)` and matching page padding so no content or focused control is covered.
- Page-level horizontal overflow is forbidden at 320px and 200% zoom. Only named data tables, charts, or code regions may scroll in two dimensions.
- Simple lists become labelled record cards on mobile. Comparison/ranking matrices retain all columns in a labelled local-scroll region or become complete candidate cards.
- Long forms use visible sections and one H2 per step. Mobile sticky actions must reserve page padding and cannot cover fields, errors, or focused controls.

## Elevation & Depth

- Hierarchy is canvas → layer 01 → layer 02, using background and one-pixel rules.
- Panels have 6px radius and no shadow. Controls use 4px radius; status tags use 2px.
- Shadows are reserved for genuine overlays and drawers only.

## Shapes

- Avoid giant rounded cards, floating dashboard tiles, pill buttons, gradient surfaces, glass, glow, and ornamental charts.
- Pills are reserved for compact statuses or categorical tags.

## Components

- App shell: product identity and utilities in the top bar; task navigation in the rail/drawer; theme and POST logout in the account utilities.
- Page header: 12px label, one H1, one-sentence purpose, status/metadata, and at most one high-emphasis action.
- Buttons: primary, secondary, ghost, danger only. One primary per action group. Targets are at least 44×44px.
- Fields: persistent label, optional helper, native control semantics, visible invalid state, linked error text, 44px mobile target.
- Panels: use only for a meaningful unit that can move, expand, select, or update independently. Do not put every section in a card.
- Metric strip: 2–6 operational metrics, tabular values, no decorative color. Two columns on medium mobile and one below 480px.
- Tables: semantic markup, numeric alignment, sticky header for long sets, accessible sort direction, row action at the end, explicit local-scroll cue.
- Status tags: literal domain text plus semantic class. Never infer state only from color.
- Notices: 3px semantic rail, tinted surface, short heading and recovery action. Static guidance has no live role; async outcomes use polite status; blocking submit failures use alert.
- Decision summary: champion parameters and caveats precede ranking evidence. Tie, stability, significance, and holdout state remain visible without hover.
- Technical disclosure: provenance, identities, JSON, logs, leases, and events use native details/summary with 44px summary target. Required actions and blocking errors stay outside closed disclosures.
- Empty state: what is absent, why, and one legitimate next action. No illustration.
- Error state: preserve entered data, identify the operation, give a recovery step, and provide a copyable correlation ID where available. Never show raw tracebacks or secrets.
- Report workspace: parent Experiment/Study link, Attempt or Study identity, verification/sandbox status, back action, full-screen action, and unchanged opaque report sandbox.
- Motion: hover/focus 90ms, disclosure 160ms, drawer 240ms; opacity plus at most 8px translation. Reduced-motion makes transitions instant.

## Do's and Don'ts

### Do

- Put the current decision, operational state, and next action before audit detail.
- Keep planned windows, observed outer-OOS evidence, and terminal holdout visibly distinct.
- Preserve exact domain language where it affects interpretation.
- Test 390px and 1280/1440px, light/dark/system, JavaScript on/off, keyboard-only, forced colors, reduced motion, long IDs, and Chinese/English labels.
- Preserve auth-before-work, report sandbox, immutable identities, deduplication, rerun, Study, and holdout semantics.

### Don't

- Do not add decorative metrics, fake charts, generic icon cards, gradients, glassmorphism, neon glow, or emoji icons.
- Do not shrink text below 12px or controls below 44px on touch layouts.
- Do not make mobile a clipped desktop canvas or depend on hover/swipe/long-press.
- Do not hide columns merely to make a table fit.
- Do not add ad-hoc colors, spacing, radii, shadows, or z-index outside semantic tokens.
- Do not show raw 64-character IDs as the primary title when a human label exists.
