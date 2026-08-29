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
  body:
    fontFamily: system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif
    fontSize: 0.875rem
    fontWeight: 400
    lineHeight: 1.43
    letterSpacing: "0em"
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
    rounded: "{rounded.control}"
    height: 44px
    padding: 16px
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
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
    rounded: "{rounded.none}"
    height: 52px
---

# Proofline design specification

Proofline is a Carbon-inspired discipline for this research workbench: flat evidence layers, strong grid, enterprise density, precise states, and no decorative dashboards. It uses an original graphite and spectral-cyan palette rather than IBM branding, and relies only on CJK-safe system typography.

The authenticated application uses a 52px masthead, a 240px task-grouped rail on desktop, and a fixed five-item mobile bottom navigation for Overview, Operators, New experiment, Studies, and History. Template access, theme selection, and POST+CSRF logout remain in a native disclosure that works without JavaScript.

Light, dark, and system themes are authored equally. Production CSS consumes the semantic tokens above for surfaces, type, actions, borders, focus, and status states. Gradients, glass, glow, fake charts, emoji icons, external fonts, and external runtime design dependencies are not part of this identity.
