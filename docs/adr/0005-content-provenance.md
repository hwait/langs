# ADR 0005: AI-assisted content with risk-based provenance

Status: accepted for Stage 0

## Decision

AI may draft pack items, examples, explanations, assessments, and adaptations. Every persistent item records item-level origin, content hash, rights/privacy metadata, dependencies, lifecycle, risk tier, and independent linguistic, pedagogical, source-alignment, rights, and privacy review states as applicable. Generation runs and prompt-template versions are recorded when AI participates.

AI output is never silently equivalent to reviewed content. Higher-risk pronunciation, assessment, normative, or shareable claims require stronger review gates.

`content_hash` uses **LinguaWiki Canonical Content JSON v1**. Build an object containing, in this exact logical field set, `schema_name`, `schema_version`, `content_id`, `language`, `kind`, `title`, `body`, `risk_tier`, `provenance`, and `dependencies`; serialize it as UTF-8 JSON with keys sorted recursively, no insignificant whitespace (`separators=(",", ":")`), Unicode preserved rather than ASCII-escaped, and non-finite numbers forbidden; then take the lowercase SHA-256 hex digest. `lifecycle`, `reviews`, and `content_hash` are excluded so review workflow changes do not alter the reviewed revision. The public `canonical_content_hash` Python helper is authoritative and the content model rejects a supplied digest that does not match it.

For the v1 minimum risk gate, any item at risk tier 3 or 4 must have a passed, hash-bound `source-alignment` review before entering `approved-personal`, `verified`, or `publication-ready`. Publication-ready content continues to require every review axis. Stage 2 may add kind-specific policies, named reviewer qualifications, sampling rules, and stricter thresholds without weakening this v1 floor.

## Alternatives rejected

- Requiring cited authentic sources for every seed: too restrictive for original exercises and still does not establish pedagogical correctness.
- Treating model/provider metadata as sufficient provenance: it does not identify source alignment, rights, or human review.
- One global “reviewed” flag: obscures which claim was actually checked.

## Consequences

Pack authoring requires review queues, batch/template sampling, quarantine, and dependency invalidation. Personal approval and publication readiness remain distinct.

## Enforced invariants

- `ContentItem` requires origin, risk, lifecycle, content hash, provenance, and unique review axes.
- `ContentItem` recomputes Canonical Content JSON v1 and rejects a mismatched hash.
- Promoted risk-tier 3–4 content requires a passed source-alignment review bound to that hash.
- Contract tests validate both synthetic fixture packs through the same content model.
- Stage 2 implements generation-run tracking, additional kind-specific review gates, template sampling, quarantine, and invalidation.
