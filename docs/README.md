# Health Data Hub Docs

This directory separates committed project artifacts from local-only working material.

## Committed Docs

- `health_data_hub_full_autonomous_design.md` is the approved Health Data Hub v1 design source.
- `gstack/` contains promoted Keel compiler inputs and generated autoplan artifacts that are sanitized and safe to commit.
- `evidence/` contains sanitized evidence/closure notes that are safe to commit.
- `keel-walkthrough_v*.html` are committed walkthrough artifacts.
- `reviews/` is for sanitized autonomous review artifacts that are safe to commit and push.

## Local-Only Docs

- `local/` is ignored by git except for its README.
- Use `local/` for review inputs, scratch notes, private operator context, raw feedback packets, or anything that should not be pushed.
- Promote a local document into a committed docs folder only after checking that it contains no secrets, raw health data, provider payloads, or local-only context.
