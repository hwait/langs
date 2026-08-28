# Contract schema versioning

Checked-in files under `schemas/` are compatibility contracts generated from the Pydantic models by `scripts/generate_schemas.py`. Tests fail when the models and snapshots differ. Each schema carries `x-linguawiki-semantic-model`; consumers must use `validate_json_contract` (or an equivalent implementation) so cross-record chronology, hash binding, and reference rules are checked after structural JSON Schema validation. Standard JSON Schema cannot express those dynamic graph comparisons by itself.

Once a contract version is released:

1. Do not silently change the meaning of existing fields or validation rules.
2. Add optional backward-compatible fields only when old producers and consumers remain valid.
3. For a breaking change, introduce a new schema name/version and retain the old model while it is supported.
4. Add an explicit adapter or migration and fixtures for both versions.
5. Update compatibility declarations and locks only after old-to-new tests pass.
6. Regenerate snapshots deliberately and review their structural diff.

Use:

```bash
uv run python scripts/generate_schemas.py
uv run python scripts/generate_schemas.py --check
```

The first command is an intentional contract update; the second is the normal verification command.
