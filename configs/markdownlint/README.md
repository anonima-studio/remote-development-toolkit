# Markdownlint config

This workspace package owns the repository-wide `markdownlint-cli2` dependency
and configuration (`.markdownlint-cli2.cjs`).

Run it from the repository root:

```bash
pnpm lint:md
pnpm lint:md:fix
```

The configuration checks all Markdown in the repository and excludes
dependency and generated-output directories. Line length (MD013) and ordered
list numbering style (MD029) are not enforced.
