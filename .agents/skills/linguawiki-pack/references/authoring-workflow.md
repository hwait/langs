# A pack-authoring session, end to end

## 1. Establish where the content will live

Author against an initialized workspace: `linguawiki pack list --workspace <path>` shows what
is installed. Drafting attaches content to an installed pack, so install the pack you are
extending first.

## 2. Register the template before drafting

```bash
cat > template.json <<'JSON'
{"schema_name": "lingua.pack.template.v1", "schema_version": 1,
 "template_key": "<key>", "version": 1, "purpose": "generation",
 "intended_kinds": ["construction"],
 "body": "<the prompt text, verbatim>",
 "known_failure_modes": ["<what this template gets wrong>"]}
JSON
linguawiki pack template validate --workspace <path> --input template.json --format json
```

Record the failure modes you already know. They are the reviewer's checklist, and the reason
a template's history is per-version.

## 3. Open a bounded batch, then draft into it

```bash
linguawiki pack author generate-draft --workspace <path> \
  --template-key <key> --template-version 1 --count <n> \
  --provider <provider> --model <model> --format json
```

Read `sampling_policy` and `required_sample` from the result *before* generating anything.
Then produce at most `--count` items and record them:

```bash
linguawiki pack author import --workspace <path> --batch <batch-id> --input items.json
```

Each item declares `stable_key`, `kind`, `title`, `body`, `level`, `themes`, `risk_tier`,
and optionally `dependencies` (knowledge stable keys) and `source_references`. Items arrive
as `draft` with nothing claimed on any axis. Levels and themes must be ones the installed
pack declares.

## 4. Work the queue, worst first

```bash
linguawiki pack author review-queue --workspace <path> --limit 10 --format json
```

Each entry reports its risk tier, what it would take to reach `promotion_target`, and its
dependency centrality — how much other content leans on it. Work top-down: a wrong item that
twelve others depend on costs more than a wrong leaf.

## 5. Review one axis at a time, and say how

```bash
linguawiki pack author review --workspace <path> --content <id> \
  --axis linguistic --state reference-verified \
  --reviewer-kind human --reviewer "<who>" \
  --method "checked against <reference>" --evidence "<locator>" \
  --inspection accepted --format json
```

`--method` is required for any review that satisfies a requirement: a pass with no method is
a claim with no basis. Pass `--inspection` on the review that actually performed the batch
inspection, so the sampling duty and the review are the same act.

When something is wrong, say so and stop:

```bash
linguawiki pack author review --workspace <path> --content <id> --axis linguistic \
  --state unreviewed --reviewer-kind human --reviewer "<who>" \
  --method "manual inspection" --inspection defective --finding "<the actual error>"
```

The command exits `1` and reports the quarantine. Re-draft from a new template version; do
not review the quarantined items.

## 6. Promote, or reject

```bash
linguawiki pack author approve --workspace <path> --content <id> --lifecycle approved-personal
linguawiki pack author reject  --workspace <path> --content <id> --reason "<why>"
```

A rejection keeps its reviews, so the decision stays auditable.

## 7. Stabilize the template only when it has earned it

```bash
linguawiki pack template stabilize --workspace <path> --template-key <key> --template-version 1
```

Three fully inspected, defect-free runs. The command reports how far short it is otherwise.

## 8. Publish from files, not from the database

Pack releases come from the pack directory: edit the files, `pack stamp`, `pack validate`,
`pack coverage`, then `pack publish`. The workspace database is where drafting and review
happen; the directory is what ships.
