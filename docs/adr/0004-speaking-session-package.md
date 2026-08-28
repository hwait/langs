# ADR 0004: Provider-independent speaking packages

Status: accepted for Stage 0

## Decision

Speaking systems exchange `lingua.session.v1` packages with LinguaWiki. The contract preserves a timestamped immutable raw transcript, optional normalized and reviewed-hearing layers, structured events, artifact hashes/references, target language, time bounds, and learning targets. Manual package creation is the mandatory baseline; voice-provider adapters are optional producers.

Transcript evidence can support lexical, grammatical, pragmatic, or general production observations. Confirmed pronunciation/acoustic evidence must link to retained or reviewable audio at the time of judgment.

## Alternatives rejected

- Depending on native voice in the repository agent: couples the learning model to one interface.
- Treating STT output as ground truth: normalization can hide or invent learner errors.
- Embedding audio in the database/package: creates privacy and size problems.

## Consequences

Raw, normalized, and reviewed transcript layers cannot overwrite one another. Media remains a referenced filesystem artifact with explicit retention state.

## Enforced invariants

- `SessionPackage` rejects missing/duplicate raw layers, unsafe artifact paths, invalid chronology, and confirmed pronunciation without linked audio.
- The synthetic package fixture and contract tests exercise both transcript layers and the audio gate.
- Stage 5 adds retention purge, tombstones, and real provider adapters without changing the core boundary.
