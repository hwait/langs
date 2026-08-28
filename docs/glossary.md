# LinguaWiki glossary

- **Core** — the generic Python package, contracts, migrations, skills, templates, and synthetic fixtures in this repository.
- **Language pack** — an independently versioned, capability-driven bundle of reusable language content, frameworks, assessments, activity templates, and resource metadata.
- **Learner workspace** — one private, independent Git repository for a learner/program. It pins released core and packs; it is not a normal fork of core.
- **DuckDB authority** — the local database is the source of truth for mutable learner objects. Generated Markdown is never authoritative.
- **Wiki projection** — deterministic, sanitized Markdown rendered from authoritative database state and suitable for reviewed private Git history.
- **Skill** — Codex instructions that orchestrate a learning or maintenance workflow through typed CLI commands.
- **Session package** — a provider-independent `lingua.session.v1` interchange package containing transcript layers, events, and external artifact references.
- **Evidence** — an atomic, provenance-bearing observation of learner performance in a particular task, modality, assistance condition, and time context.
- **Pack maturity** — an explicit capability/coverage state such as `fixture`, `pilot`, `onboarding-ready`, or `placement-ready`; it is not a claim about learner mastery.
- **Framework** — a pack-declared proficiency system whose levels and prerequisites are interpreted by that pack rather than globally by core.
