# Support

The Remote development toolkit is published by Anonima Studio as a portfolio
project and a usable product, **as is**. There is no service-level agreement.

## Where to go

| Need | Where |
| --- | --- |
| Install and first run | [docs/quickstart.md](docs/quickstart.md), including its troubleshooting table |
| A command fails | The command's own recovery line, then the guide for that feature in [docs/](docs) |
| Known gaps | [docs/limitations.md](docs/limitations.md) |
| A bug or a documentation error | A GitHub issue |
| A security problem | Private report, see [SECURITY.md](SECURITY.md) |

## Issues

Issues are welcome. A useful issue states the command you ran, the topology
and mode, the full output of the failing command (with secrets removed), and
the output of `bin/remote-dev --profile <name> doctor` when the server is
involved. Never paste credentials, tokens or private keys.

## Pull requests

Pull requests are reviewed at the maintainer's discretion and may be closed
without detailed review. Before opening one:

- keep the change small and focused on one problem;
- run `pnpm lint` and `pnpm test`;
- follow [AGENTS.md](AGENTS.md), which also applies to human contributors:
  user-visible outcomes, fail-closed checks, no secrets or real host identities.

By submitting a contribution you agree that it is licensed under the
[Apache License 2.0](LICENSE).
