"""Deterministic services behind the CLI.

Workspace and storage lifecycle, language packs and their authoring, learners and
tracks, onboarding, resources, curricula, assessment runs, and the learner model:
the knowledge graph, attempts and evidence, recurring errors, skill estimates, and
bounded context bundles.

A service owns *how* something is written; the rules it writes by live in the pure
modules beside them -- `evidence`, `mastery`, `error_model`, `placement`, `provenance`,
and `text` -- so a rule can be tested and versioned without a database.
"""
