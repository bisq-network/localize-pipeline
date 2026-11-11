# Bisq 1 localization profile

This profile translates the original Bisq desktop application. Its source is
`displayStrings.properties` under `core/src/main/resources/i18n`; targets use
`displayStrings_<locale>.properties`. The manual runner supports any subset of
its 15 configured locales and advances accepted local state only after the
complete quality gate. It leaves a staged patch; it does not publish changes.

Semantic QA reports findings without automatically applying suggested
replacements. Error findings still block acceptance. Review the saved candidate
against English, apply only verified corrections, and rerun source-aware
semantic and quality checks before accepting it. This avoids allowing a mistaken
review suggestion to overwrite an otherwise complete translation.

## Terminology provenance

`glossary.json` is a curated subset of `../bisq/glossary.json`, the existing
Bisq 2 glossary, checked on 2026-10-20. The source file's SHA-256 is
`c37099f963f2e0a7e65d5514efc7b5e51010e7016b648bda465007d4a210013f`.
This records the inherited mappings; it does not claim that every locale has
received a new native-speaker review.

Shared financial and UI concepts include accounts, account age, balances,
bonded BSQ, confirmations, fees, payment accounts, mediation/arbitration,
offerbooks, sending/receiving, trades, transaction IDs and wallets. The selected
English terms occur in the Bisq 1 source at upstream commit
`2dab5f8f81d6937f8bc7f6308908894339fa0734`.

The matching helper treats glossary keys as literal, case-insensitive phrases
with word boundaries; it does not interpret property IDs. Selected underscore
phrases are therefore converted to English spaces, such as `payment_account`
to `payment account` and `transaction_id` to `transaction id`. Phrases without
an actual Bisq 1 English-value match are not imported merely because a similar
property key exists.

Locale aliases are explicit:

| Bisq 2 glossary | Bisq 1 profile |
| --- | --- |
| `pt_BR` | `pt-br` |
| `pt_PT` | `pt` |
| `zh-Hans` | `zh-hans` |
| `zh-Hant` | `zh-hant` |

The other shared locale codes are unchanged. Persian (`fa`) has no inherited
mappings and remains `{}`. Missing individual entries also remain missing:
Czech has 17 mappings; Spanish, Brazilian Portuguese and Russian have 19; the
other populated locales have 20. No fallback from another language is inserted.

## Preferred terminology and existing interface labels

The profile uses `translation_glossary_enforcement: prompt-only`. These mappings
are preferred base terms in draft/review prompts, not deterministic acceptance
rules. Literal enforcement would reject valid German compounds and inflections,
for example `account` / `Konto` in `Handelskonto`, or `fee` / `Gebühr` in
`Transaktionsgebühr`. Placeholders, control characters, source-fallback checks,
whole-file publication gates and semantic review remain active. Semantic and
human review must distinguish real terminology mistakes from natural grammar.

These mappings follow the existing Bisq 1 interface rather than Bisq 2:

| Locale | English | Bisq 1 preferred term | Bisq 2 mapping |
| --- | --- | --- | --- |
| German | balance | Guthaben | Kontostand |
| German | trade id | Handels-ID | Trade-ID |
| Spanish | trade id | ID de intercambio | ID de transacción |
| Italian | trade id | ID Scambio | ID della transazione |

The Spanish and Italian overrides keep a trade identifier distinct from a
blockchain transaction identifier, matching Bisq 1's `shared.tradeId` and
`shared.txId` labels. This matters in the warning that prohibits including
the trade ID in bank payment messages. Other locales may legitimately use
the same word for both concepts; this is not a global renaming rule.

`arbitrator` keeps the inherited `Schiedsrichter` for new explanatory text;
`mediator` is `Mediator`. Bisq 1 has older interface labels such as
`shared.arbitrator=Vermittler`. Quoted navigation, button and tab references must
keep the actual existing UI label; this profile does not authorize a broad
legacy-label rewrite. In particular, new dispute-resolution prose must keep
mediators and arbitrators distinct.

## Deliberate exclusions

The seven `tacWindow.legal.section1.body` through
`tacWindow.legal.section7.body` values stay in English. Bisq 1's locale files
explicitly defer legal translations until they can be reviewed by lawyers.
The profile's `ignore_key_patterns` rule copies the current English source
verbatim on each run and excludes these bodies from model translation and
semantic review. It also changes the checkpoint policy fingerprint, so cached
outputs from a policy that translated them are not reused. Legal headings,
buttons and risk warnings remain translatable. New legal-section keys need an
explicit policy review before adding them to this bounded rule.

Bisq 2 user/profile identity, reputation-score and market-channel terminology,
network-role labels and role-manager names are not imported. Some occur only in
explicit Bisq 2 integration or DAO role references; they must not become global
Bisq 1 vocabulary rules. Key-only mappings such as `network.roles` and
`network.transport.headline.CLEAR` are also excluded. Phrases absent from the
English values, such as `initialize wallet`, `backup seeds`, `seed phrase` and
`toggle details`, are excluded. The inherited `burned_bsq` mapping is not copied:
some locales leave it as English, and importing that as a preferred translation
would need separate review. Other ambiguous or incidental terms such as
`community`, `guide` and `overview` are outside this bounded subset.

## Brands and technical names

The brand list starts with the existing Bisq 1 `Bisq`, `Bitcoin`, `BSQ`, `BTC`,
`DAO` and `Tor`, then reuses Bisq 2 names actually present in Bisq 1 English
values. This includes explicitly referenced `Bisq Easy`, `Liquid` and payment
brands such as `Revolut` and `Zelle`; presence does not make their protocols part
of Bisq 1. Unused names such as `MuSig`, `I2P` and `Lightning` are not imported.

Generic payment descriptions remain translatable. The inherited phrases
`ACH Transfer`, `SWIFT International Wire Transfer`, `BSQ Swaps`, `Wire Transfer`,
`US Money Order` and `Japan Furikomi` are not frozen as complete brand names.
Their applicable technical names (`ACH`, `SWIFT`, `BSQ`) remain protected.
`Amazon eGift Card` is reduced to the attested `Amazon` brand so the description
can use the local wording, such as German `Amazon-Gutschein`.
`Faster Payments` is also excluded from global brand protection: it names the
UK service but also occurs inside the Russian `Faster Payments System (SBP)`
description, where natural localization is appropriate. Existing UK service
labels stay unchanged; a global rule cannot distinguish these contexts.

The exact source-identical acceptances for `payment.account.revtag=Revtag`
and `CLEAR_X_CHANGE=Zelle` apply to each configured locale: both are
intentionally shared payment names. Acceptance requires the exact locale,
key, current English value and target value; changes revoke it. The brand
list does not replace those per-key acceptance decisions.
