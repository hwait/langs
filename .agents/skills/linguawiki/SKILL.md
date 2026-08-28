---
name: linguawiki
description: Inspect and operate a LinguaWiki language-learning workspace through its typed CLI. Use for LinguaWiki status and, as later stages add them, route learning, assessment, pack, review, and maintenance requests to specialist skills.
---

# LinguaWiki

Use the Python CLI as the machine boundary. Do not edit a learner database or generated wiki as a substitute for a command.

## Current Stage 0 workflow

For status or installation checks, run:

```bash
linguawiki status --format json
```

Treat the JSON envelope as authoritative. Report the application version, contract schema version, stage, and persistence state. A nonzero exit or `ok: false` is a failure; preserve its error code and retryability in the explanation.

Stage 0 has no persistence commands and contains no learner state. Do not invent onboarding, lesson, evidence, or database operations before the corresponding specialist skills and CLI commands exist.
