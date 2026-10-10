# Browser automation on a runner

## Purpose

Run `agent-browser` and Playwright browsers inside the runner workspace, next
to the application under test, so browser work does not load the Mac and
reaches the app on the runner's own loopback without tunnels. The Mac stays the
orchestrator: it sends commands and reads results.

## Install (runner user space, once per workspace)

```sh
npm i -g --prefix ~/.local agent-browser@<pinned version>
~/.local/bin/agent-browser install --with-deps
```

`--with-deps` installs Chrome's system libraries through the workspace's
passwordless sudo. The browser lands in `~/.agent-browser/browsers/`, outside
any project projection, so it causes no drift. Pin the version and review
upgrades like any other dependency.

For a project's own Playwright suite, install the browsers its lockfile
expects (`pnpm exec playwright install chromium chromium-headless-shell`); a
cached older build does not satisfy a newer Playwright.

## Use

Run through the binding (`local-agent exec` or `start`). Plain SSH to the same
runner is a diagnostic exception, only for an already running app when the
binding cannot project local source (for example because the Mac disk is
full). Label such captures with their last verified source receipt; they
cannot establish acceptance of current local edits. Never use that path for
dependency installation, builds, tests or changes to the managed projection.

Keep every browser command headless and close the session in the same command
so no browser process outlives it:

```sh
export PATH=$HOME/.local/bin:$PATH
AB="agent-browser --session <task>"
$AB open http://127.0.0.1:<port>/ && $AB set viewport 1440 900
$AB snapshot -i                         # refs for interaction
$AB screenshot --full /tmp/<name>.png   # evidence stays on the runner
$AB close
```

Screenshots, reports and videos stay on the runner. Copy to the Mac only the
few files a person or agent must look at (for example `base64 -w0 <file>` over
the exec channel), and delete them after viewing.

## Traps

- Never write into the managed projection from a browser run (reports,
  screenshots, `eslint --fix`, generated types): it raises `REMOTE_DRIFT`. Use
  `$HOME/...` or `/tmp` for output.
- `local-agent start` records stay `stale` after the process ends and keep the
  binding's health `blocked`; stop finished handles explicitly.
- The runner has no desktop, so headed mode is meaningless there. For a person
  to watch, record a video (Playwright `recordVideo`) or open the app on the
  Mac through `remote-dev preview` and use a local browser.

## Without installing browsers: disposable containers

When you may not change the workspace (no `sudo`, no system packages), or the
runner's own Chromium fails on missing libraries, run browsers in containers on
the runner's nested Docker (`containers` capability) and keep only the clients
in user space.

agent-browser against a headless Chrome container:

```sh
docker run -d --name <task>-chrome --network host --shm-size=1g \
  chromedp/headless-shell:<pinned tag> \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9333 --no-sandbox
~/.local/bin/agent-browser --session <task> --cdp 9333 open http://localhost:<port>/
```

A project's own Playwright suite in Chromium and WebKit, with no browser
installed on the runner and no project change: run the official image's browser
server at the project's exact Playwright version and point the test runner at
it.

```sh
docker run -d --name <task>-pw --network host --init --ipc=host \
  mcr.microsoft.com/playwright:v<version>-noble \
  sh -c "npx -y playwright@<version> run-server --port 3333 --host 127.0.0.1"
PW_TEST_CONNECT_WS_ENDPOINT=ws://127.0.0.1:3333/ \
  PLAYWRIGHT_CHROMIUM_CHANNEL=chromium pnpm exec playwright test
```

`--network host` lets the browser reach the app on the runner's loopback.
Remove the containers with `docker rm -f` when the task ends.

## More traps

- **agent-browser sessions attached over one CDP endpoint share one cookie jar
  and storage**; `--session` only selects a tab. Two identities need two
  browser containers on different ports, or Playwright contexts, which are
  isolated. A shared jar can produce false authentication findings.
- **The runner runs one bridge command at a time** and refuses a concurrent one
  with `OPERATION_LOCK_BUSY`. Several agents sharing a runner need a wrapper
  that retries; a long browser suite belongs in `local-agent start` so it does
  not hold the runner.
- **Every workspace has a cgroup PID cap** (1024 for `core`, 2048 for
  `containers`, including nested containers). A development server plus a
  browser's process tree can approach it, and page loads then stall. Check
  `pids.current` against `pids.max` in the workspace cgroup, stop unused
  browser sessions, or choose `containers` for browser-heavy work.
- Development builds may not serve a PWA service worker; run offline and
  service-worker scenarios against a production build.
- A `tar` stream created on macOS writes AppleDouble `._*` files on the runner
  and raises `REMOTE_DRIFT`; set `COPYFILE_DISABLE=1` for any manual transfer.
