# Localize Pipeline

## Register

brand

## Users

Open-source maintainers and developers deciding whether to automate localization
in their own repositories. They arrive from GitHub with limited time and need to
understand the workflow, inspect its safeguards, and find a working setup path.
Returning operators need direct access to commands and troubleshooting.

## Product Purpose

Turn source-string changes into translation pull requests that a team can review.
The optional, self-hosted Guardian follows up on authorized review feedback and
can propose fixes to recurring pipeline defects. Neither replaces human judgment.

The homepage introduces those two jobs. The README gets a developer started.
Operator guides explain configuration, authority, recovery, and limitations.
Success means a reader can explain what runs, who runs it, what it may change,
and where to start without first reading the implementation manual.

## Brand Personality

Direct, practical, restrained. Part of the Bisq tooling family, not a separate
AI-service brand. Use the current GitHub Pages website as the visual reference;
its style was explicitly approved by the maintainer on 11 September 2026.

Bisq's typography, green identity, open-source ethos, and emphasis on operator
control are the wider reference. Do not copy its exchange-specific language or
assume its current dark theme should replace this site's approved light theme.

## Anti-references

- Dense AI-generated text blocks that bury a feature beneath implementation detail.
- Generic AI-service marketing: vague superlatives, autonomous-agent promises,
  decorative dashboards, invented metrics, or claims of flawless results.
- A redesign that discards the current GitHub Pages style.

## Design Principles

1. Preserve the approved site. Extend its typography, spacing, logo, and palette.
2. Lead with the useful outcome. Show one concrete workflow before its mechanics.
3. Use progressive disclosure. Put setup in the README and operational detail in
   linked guides, not in repeated walls of text across every page.
4. Make control explicit. Projects operate Guardian themselves and choose its
   trusted reviewers, permissions, and limits. Human review still decides merges.
5. Make evidence match the claim. Distinguish proposed, implemented, tested,
   released, installed, and verified behavior. Never imply that one implies all.

## Accessibility & Inclusion

Keep semantic headings, keyboard access, visible focus, the skip link, responsive
reflow, and reduced-motion support. Aim for WCAG AA contrast in new content;
green must not be the sole carrier of status. This is a design target, not a claim
that the existing site has passed a complete accessibility audit.

## References

- [Approved project website](https://bisq-network.github.io/localize-pipeline/)
- [Bisq](https://bisq.network/)
- [Visual system](DESIGN.md)
- Implementation sources: `docs/index.html`, `docs/assets/site.css`,
  `docs/assets/bisq-logo.svg`.
