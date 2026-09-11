# Retained workspace `uv.lock`

A real `uv lock` result for a learner workspace named `Polish LinguaWiki`, produced by

```bash
uv lock --project <workspace> --find-links <directory containing the built core wheel>
```

It is retained verbatim except for one normalization: the local artifact directory that
`--find-links` pointed at is replaced with `<local-artifacts>`, so no machine-specific
temporary path is committed. Nothing the validator inspects — package names, versions,
or dependency edges — is touched.

`tests/workspaces/test_workspace_recovery.py` uses it as the positive case for
`workspace doctor`'s dependency-lock check. Hand-written locks are only ever used as
negative cases: a fabricated lock is exactly what that check exists to reject.

Regenerate it deliberately when the core's pinned dependencies change:

```bash
uv build --wheel --out-dir <dist>
uv run linguawiki workspace init <workspace> --backup-root <outside> --name "Polish LinguaWiki"
uv lock --project <workspace> --find-links <dist>
```
