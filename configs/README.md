# Shared configs

Repository-wide tooling configuration lives under `configs/*` as pnpm
workspace packages.

- [`markdownlint`](markdownlint/README.md) owns Markdown validation for the
  whole repository.

There are no shared ESLint, TypeScript, Prettier or Stylelint packages: the
repository is written in Bash, Python, a little Node.js, Terraform, YAML and
Markdown.
