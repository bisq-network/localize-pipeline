---
name: Localize Pipeline
description: Bisq tooling for reviewable translation pull requests
colors:
  green: "#25b135"
  green-dark: "#12871d"
  paper: "#fbfbfa"
  white: "#fffffe"
  soft: "#f3f3f3"
  soft-green: "#eef8ef"
  ink: "#191919"
  muted: "#626a64"
  line: "#d9ddd8"
  code: "#111513"
  code-line: "#2a312d"
typography:
  display:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "76px"
    fontWeight: 500
    lineHeight: 0.96
  headline:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "44px"
    fontWeight: 500
    lineHeight: 1.06
  title:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "19px"
    fontWeight: 500
    lineHeight: 1.25
  body:
    fontFamily: "IBM Plex Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 300
    lineHeight: 1.55
  lead:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "22px"
    fontWeight: 300
    lineHeight: 1.48
  label:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "14px"
    fontWeight: 500
  code:
    fontFamily: "IBM Plex Mono, SFMono-Regular, Consolas, monospace"
rounded:
  button: "4px"
spacing:
  compact: "8px"
  inline: "12px"
  control: "18px"
  block: "24px"
  section: "82px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.white}"
    rounded: "{rounded.button}"
    padding: "0 18px"
  button-secondary:
    textColor: "{colors.ink}"
    rounded: "{rounded.button}"
    padding: "0 18px"
  navigation:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
  tag:
    backgroundColor: "{colors.soft-green}"
    textColor: "{colors.ink}"
    padding: "7px 10px"
  route-link:
    textColor: "{colors.ink}"
    padding: "24px 0"
  command-panel:
    backgroundColor: "{colors.code}"
    typography: "{typography.code}"
---

# Localize Pipeline design system

## Overview

**Creative North Star: "The Bisq developer guide"**

Preserve the current GitHub Pages style. A maintainer reading the site alongside
a repository should find it familiar, spacious, and easy to scan. This is a
light documentation surface within the Bisq family, not a new product identity.

This is a scan of `docs/assets/site.css` and the rendered project website on
11 September 2026. [Bisq](https://bisq.network/) supplies the wider identity;
its live page shares IBM Plex typography and the green logo but currently uses
a dark theme. The project's approved light theme remains the reference here.
Hex tokens preserve the existing CSS exactly; they are not a new palette.

**Key Characteristics:**

- Near-white pages, charcoal text, and a restrained Bisq green accent.
- Left-aligned headings, generous section spacing, and thin dividing rules.
- Practical examples and direct links, with technical depth one level away.

The content width is capped at 1180px, with 24px desktop and 16px mobile gutters.
The existing breakpoints are 1040px and 760px. At narrower widths, columns stack
and navigation wraps; no essential information depends on hover.

## Colors

### Primary

Bisq green identifies the logo and small accents. Deep green is the text-link
color. Pale green groups supporting information without turning sections into
large promotional banners.

### Neutral

Paper is the page background; white and soft gray separate examples from prose.
Ink carries headings and primary actions. Muted text supports, but must not hide,
important qualifications. Thin gray-green rules divide sections. Command panels
use a dark green-charcoal surface, reserved for actual commands or output.

**The Contrast Rule.** Do not assume brand green is readable on every surface.
Use deep green for new small text and check every new foreground/background
pair. Existing bright-green button hover and translucent focus styling are
recorded below, not certified as accessibility-compliant.

## Typography

Use IBM Plex Sans for prose, headings, and controls, and IBM Plex Mono only for
commands and identifiers. These families are already part of Bisq's identity;
do not replace them to follow a new design trend. The site loads
[Bisq's font stylesheet](https://bisq.network/css/fonts.css) with local system
fallbacks. The supplied Mono face is weight 200, so heavier code may be synthesized.

The frontmatter records desktop roles. Display headings step down to 60px below
1040px and 44px below 760px. Section headings become 32px on mobile; compact
reference bands use 28px. Lead paragraphs become 18px on mobile.

**The Reading Rule.** Keep new explanatory prose near 65–75 characters per line.
Use a short heading and a concrete example before a list of constraints. No
all-caps paragraphs, invented metrics, or repeated promotional kickers.

## Elevation

Most content is flat, separated by whitespace, thin rules, or a quiet background
change. The existing run example uses one ambient shadow
(`0 22px 60px rgb(25 25 25 / 10%)`). The sticky desktop header has a translucent
paper background and a 12px blur; preserve it, but do not copy that treatment
onto every new section. Do not add nested cards or decorative floating panels.

## Components

### Buttons and links

Buttons have gently rounded corners, a 1px border, a minimum height of 46px, and
medium-bold text. The primary action is charcoal on paper; secondary actions
are outlined. Existing primary hover turns green, secondary hover turns the
border green, and pressed controls move down by 1px. New small-text green
actions must use an independently contrast-checked pairing.

Links and controls have a visible focus outline with a 4px offset. Preserve
keyboard focus; never remove it to make a screenshot cleaner. Existing color
transitions take 160ms with ease-out. Reduced-motion mode suppresses transitions
and smooth scrolling.

### Navigation

Keep the Bisq logo, compact project name, and short text links. The desktop
header is sticky; the mobile header becomes static and stacks. New section links
must have matching anchors and descriptive labels.

### Reference rows and tags

Reference rows use thin horizontal rules, a title, and one short description.
Tags identify supported formats or tools; they are not status badges or buttons.
Neither pattern needs icons or a surrounding card.

### Command panels

Use dark panels for real, copyable commands. Long lines must wrap or scroll
inside the panel, not widen the page. Do not present illustrative output as a
live production status. Public examples use portable paths and contain no secrets.

### Feature sections

Introduce one job per section. A Guardian section should explain how review
feedback leads to a checked correction or a proposed pipeline fix, show who
remains in control, and link to setup. Use the existing split layout and section
rhythm. Keep operational limits and recovery internals in the operator guide.
No form or dialog system exists on this site; do not invent one in this spec.

## Do's and Don'ts

### Do

- Do preserve the approved light theme, Bisq logo, and IBM Plex typography.
- Do explain a feature with a concrete workflow and a clear next step.
- Do keep setup, limits, and evidence easy to find without repeating the manual.
- Do test new sections on narrow screens and with keyboard navigation.

### Don't

- Don't add dense AI-generated text blocks that bury the useful outcome.
- Don't use generic AI-service marketing or promise flawless automation.
- Don't redesign the current GitHub Pages style or import Bisq's dark theme.
- Don't imply automatic policy approval, automatic merging, or free hosted operation.
