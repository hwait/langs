# ADR 0007: Capability-driven language packs

Status: accepted for Stage 0

## Decision

Core treats language tags, scripts, proficiency-framework levels, segmentation, morphology, pronunciation representation, and activity support as pack-declared data/capabilities. Framework level labels are opaque to core except for pack-declared ordering and prerequisites. Packs are immutable, content-addressed versions with explicit maturity and coverage.

## Alternatives rejected

- A Polish-shaped vocabulary/grammar schema: would encode cases, Latin script, and whitespace assumptions into core.
- Universal CEFR mapping: Chinese frameworks cannot be converted honestly without explicit reviewed mappings.
- Free-form pack directories: compatibility, reproducibility, and safe updates cannot be checked.

## Consequences

Pack adapters may implement segmentation or script-specific presentation, but core services query capabilities and common contracts. Unsupported dimensions must be reported rather than inferred.

## Enforced invariants

- The inflected whitespace and tonal non-whitespace fixtures declare contrasting capabilities and validate through one content contract.
- Core contract types contain no Polish or Chinese language-code branches.
- Stage 2 implements manifests, maturity gates, coverage, install/update, and framework validation; Stage 9 runs the Chinese portability proof.
