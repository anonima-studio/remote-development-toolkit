#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
fixture_root=$(mktemp -d)
trap 'rm -rf "$fixture_root"' EXIT

install_root="$fixture_root/remote-dev install"
project_root="$fixture_root/project with spaces"
non_git_root="$fixture_root/not-a-worktree"
mkdir -p "$install_root/bin" "$project_root/nested cwd" "$non_git_root"
cp "$repo_root/bin/remote-dev-local-agent-mcp.mjs" "$install_root/bin/"
chmod +x "$install_root/bin/remote-dev-local-agent-mcp.mjs"

cat > "$install_root/bin/remote-dev" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

node -e '
  const fs = require("node:fs");
  fs.appendFileSync(process.argv[1], `${JSON.stringify(process.argv.slice(2))}\n`);
' "$REMOTE_DEV_MCP_TEST_LOG" "$@"

subcommand=${2:-}
case "$subcommand" in
  exec)
    command_name=
    after_separator=false
    for argument in "$@"; do
      if [[ "$after_separator" == true ]]; then
        command_name=$argument
        break
      fi
      if [[ "$argument" == -- ]]; then
        after_separator=true
      fi
    done
    receipt="$REMOTE_DEV_MCP_TEST_RECEIPT_DIR/${command_name:-unknown}.tsv"
    mkdir -p "$REMOTE_DEV_MCP_TEST_RECEIPT_DIR"
    exit_code=0
    [[ $command_name != exit-seven ]] || exit_code=7
    printf 'schema\t1\nkind\texec\nreceipt_id\treceipt-%s\nmanifest_id\tmanifest-test\nexit_code\t%s\nsignal\t-\ntimed_out\tfalse\nresult\tcompleted\n' \
      "$command_name" "$exit_code" > "$receipt"
    printf '%s\n' "$receipt" >&3
    case "$command_name" in
      exit-seven)
        printf 'remote command stdout\n'
        printf 'remote command stderr\n' >&2
        exit 7
        ;;
      large-output)
        node -e 'process.stdout.write("x".repeat(1024 * 1024 + 17))'
        ;;
      *)
        printf 'remote command stdout\n'
        printf 'remote command stderr\n' >&2
        ;;
    esac
    ;;
  status)
    printf 'workspace_role\trunner\nlease\tready\n'
    ;;
  resume)
    printf 'READY          Local Agent binding resumed\n'
    ;;
  start)
    printf 'STARTED  proc-20260821T120000-123-456\n'
    ;;
  logs)
    printf 'managed process log snapshot\n'
    ;;
  stop)
    printf 'STOPPED  proc-20260821T120000-123-456\n'
    ;;
  restart)
    printf 'RESTARTED proc-20260821T120000-123-456\n'
    ;;
  *)
    printf 'unexpected core subcommand: %s\n' "$subcommand" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$install_root/bin/remote-dev"

git -C "$project_root" init -q

requests_file="$fixture_root/requests.jsonl"
responses_file="$fixture_root/responses.jsonl"
stderr_file="$fixture_root/server.stderr"
calls_file="$fixture_root/calls.jsonl"
cat > "$requests_file" <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"runner_exec","arguments":{"argv":["node","-e","console.log('a b')","","line\nbreak","$HOME","semi;colon","quote'\""],"timeout_seconds":37}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"runner_exec","arguments":{"argv":["exit-seven","arg with spaces"]}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"runner_exec","arguments":{"argv":["true"],"workspace":"arbitrary-runner"}}}
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"other_tool","arguments":{"argv":["true"]}}}
{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}
{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"runner_exec","arguments":{"argv":"echo unsafe"}}}
{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"runner_status","arguments":{}}}
{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"runner_start","arguments":{"argv":["node","server.js","arg with spaces","; touch SHOULD_NOT_EXIST","$(touch ALSO_NOT)","quote'\""]}}}
{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"runner_logs","arguments":{"handle":"proc-20260821T120000-123-456"}}}
{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"runner_stop","arguments":{"handle":"proc-20260821T120000-123-456"}}}
{"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"runner_restart","arguments":{"handle":"proc-20260821T120000-123-456"}}}
{"jsonrpc":"2.0","id":14,"method":"tools/call","params":{"name":"runner_status","arguments":{"path":"/arbitrary"}}}
{"jsonrpc":"2.0","id":15,"method":"tools/call","params":{"name":"runner_logs","arguments":{"handle":"../../foreign"}}}
{"jsonrpc":"2.0","id":16,"method":"tools/call","params":{"name":"runner_exec","arguments":{"argv":["large-output"]}}}
{"jsonrpc":"2.0","id":17,"method":"tools/call","params":{"name":"runner_resume","arguments":{}}}
EOF

(
  cd "$project_root/nested cwd"
  REMOTE_DEV_MCP_TEST_LOG="$calls_file" \
    REMOTE_DEV_MCP_TEST_RECEIPT_DIR="$fixture_root/receipts" \
    "$install_root/bin/remote-dev-local-agent-mcp.mjs" \
    < "$requests_file" > "$responses_file" 2> "$stderr_file"
)

node --input-type=module - "$responses_file" "$calls_file" "$project_root" "$project_root/nested cwd" <<'EOF'
import fs from "node:fs";
import path from "node:path";

const [responsesPath, callsPath, projectRootInput, cwdInput] = process.argv.slice(2);
const projectRoot = fs.realpathSync(projectRootInput);
const cwd = fs.realpathSync(cwdInput);
const responses = fs.readFileSync(responsesPath, "utf8").trim().split("\n").map(JSON.parse);
const byId = new Map(responses.map((response) => [response.id, response]));

if (responses.length !== 17) throw new Error(`expected 17 responses, got ${responses.length}`);

const initialize = byId.get(1).result;
if (initialize.protocolVersion !== "2025-06-18") throw new Error("protocol version was not negotiated");
if (initialize.serverInfo.name !== "remote-dev-local-agent") throw new Error("unexpected server name");
if (!initialize.capabilities.tools || initialize.capabilities.tools.listChanged !== false) {
  throw new Error("tools capability is missing");
}
if (!initialize.instructions.includes(`MCP cwd: ${cwd}`)) throw new Error("initialize diagnostics omit cwd");
if (!initialize.instructions.includes(`Trusted local context: ${cwd}`)) {
  throw new Error("initialize diagnostics omit trusted context");
}
for (const instruction of [
  "local tools own source reading and editing, Git, skills, MCP/configuration",
  "Dependency installation, build, lint, typecheck, test, run, debug, and Docker workloads must use",
  "Use runner_exec for terminating commands",
  "If it returns WORKSPACE_STOPPED, call runner_resume once",
  "runner_resume starts only the workspace selected by the trusted binding",
  "not streamed through MCP",
  "capped at 1 MiB",
]) {
  if (!initialize.instructions.includes(instruction)) {
    throw new Error(`initialize routing instructions omit: ${instruction}`);
  }
}

const tools = byId.get(2).result.tools;
const expectedToolNames = [
  "runner_exec",
  "runner_status",
  "runner_resume",
  "runner_start",
  "runner_logs",
  "runner_stop",
  "runner_restart",
];
if (JSON.stringify(tools.map((tool) => tool.name)) !== JSON.stringify(expectedToolNames)) {
  throw new Error(`unexpected tools/list result: ${JSON.stringify(tools.map((tool) => tool.name))}`);
}
const toolsByName = new Map(tools.map((tool) => [tool.name, tool]));
for (const tool of tools) {
  if (!tool.description.includes("does not stream") && !tool.description.includes("not a follow or streaming call")) {
    throw new Error(`${tool.name} overclaims streaming behavior`);
  }
  for (const forbidden of ["cwd", "path", "profile", "root", "workspace"]) {
    if (Object.hasOwn(tool.inputSchema.properties, forbidden)) {
      throw new Error(`${tool.name} schema exposes forbidden field ${forbidden}`);
    }
  }
}

const schema = toolsByName.get("runner_exec").inputSchema;
if (schema.additionalProperties !== false) throw new Error("runner_exec schema permits extra fields");
if (JSON.stringify(schema.required) !== JSON.stringify(["argv"])) throw new Error("argv is not the sole required field");
if (schema.properties.argv.items.type !== "string") throw new Error("argv items are not strings");
if (schema.properties.timeout_seconds.type !== "integer") throw new Error("timeout_seconds is not an integer");
const startSchema = toolsByName.get("runner_start").inputSchema;
if (JSON.stringify(Object.keys(startSchema.properties)) !== JSON.stringify(["argv"]) ||
    JSON.stringify(startSchema.required) !== JSON.stringify(["argv"])) {
  throw new Error("runner_start schema is not an exact argv contract");
}
for (const name of ["runner_status", "runner_resume"]) {
  const emptySchema = toolsByName.get(name).inputSchema;
  if (Object.keys(emptySchema.properties).length !== 0 || emptySchema.additionalProperties !== false) {
    throw new Error(`${name} accepts model-controlled routing fields`);
  }
}
for (const name of ["runner_logs", "runner_stop", "runner_restart"]) {
  const handleSchema = toolsByName.get(name).inputSchema;
  if (JSON.stringify(Object.keys(handleSchema.properties)) !== JSON.stringify(["handle"]) ||
      JSON.stringify(handleSchema.required) !== JSON.stringify(["handle"])) {
    throw new Error(`${name} schema is not an exact handle contract`);
  }
}
const expectedOutputFields = [
  "exitCode", "receipt", "signal", "stderr", "stderrTruncated",
  "stdout", "stdoutTruncated", "timedOut",
];
for (const tool of tools) {
  const outputSchema = tool.outputSchema;
  if (JSON.stringify(Object.keys(outputSchema.properties).sort()) !== JSON.stringify(expectedOutputFields)) {
    throw new Error(`${tool.name} output schema is not the stable bounded result`);
  }
  if (JSON.stringify([...outputSchema.required].sort()) !== JSON.stringify(expectedOutputFields)) {
    throw new Error(`${tool.name} output schema has optional result fields`);
  }
}

const success = byId.get(3).result;
if (success.isError !== false) throw new Error("successful remote command was marked as error");
const successResult = success.structuredContent;
if (successResult.stdout !== 'remote command stdout\n') {
  throw new Error(`stdout was not preserved: ${JSON.stringify(successResult.stdout)}`);
}
if (successResult.stderr !== "remote command stderr\n") throw new Error("stderr was not preserved");
if (successResult.exitCode !== 0 || successResult.signal !== null || successResult.timedOut !== false) {
  throw new Error("successful execution metadata is wrong");
}
if (successResult.stdoutTruncated || successResult.stderrTruncated) {
  throw new Error("small successful output was marked truncated");
}
if (successResult.receipt?.id !== "receipt-node") throw new Error("receipt FD metadata was not extracted");
if (successResult.receipt?.manifestId !== "manifest-test") throw new Error("receipt manifest was not extracted");
if (success.content[0].text !== JSON.stringify(successResult)) throw new Error("text fallback differs from structured content");

const failed = byId.get(4).result;
if (failed.isError !== true) throw new Error("non-zero command was not marked as error");
if (failed.structuredContent.exitCode !== 7) throw new Error("non-zero exit code was not preserved");
if (failed.structuredContent.receipt?.id !== "receipt-exit-seven") throw new Error("failed receipt was not extracted");
if (failed.structuredContent.stdout !== "remote command stdout\n") {
  throw new Error("failed-command stdout was not preserved");
}
if (failed.structuredContent.stderr !== "remote command stderr\n") {
  throw new Error("failed-command stderr was not preserved");
}

if (byId.get(5).error?.code !== -32602 || !byId.get(5).error.message.includes("workspace")) {
  throw new Error("arbitrary workspace input was not rejected");
}
if (byId.get(6).error?.code !== -32602) throw new Error("unknown tool was not rejected");
if (JSON.stringify(byId.get(7).result) !== "{}") throw new Error("ping failed");
if (byId.get(8).error?.code !== -32602 || !byId.get(8).error.message.includes("array")) {
  throw new Error("interpolated shell string was not rejected");
}

const status = byId.get(9).result;
if (status.isError || status.structuredContent.stdout !== "workspace_role\trunner\nlease\tready\n") {
  throw new Error("runner_status did not preserve core output");
}
const started = byId.get(10).result;
if (started.isError || !started.structuredContent.stdout.includes("proc-20260821T120000-123-456")) {
  throw new Error("runner_start did not return the managed process handle");
}
if (byId.get(11).result.structuredContent.stdout !== "managed process log snapshot\n") {
  throw new Error("runner_logs did not preserve the bounded log snapshot");
}
if (byId.get(12).result.structuredContent.stdout !== "STOPPED  proc-20260821T120000-123-456\n") {
  throw new Error("runner_stop did not preserve core output");
}
if (byId.get(13).result.structuredContent.stdout !== "RESTARTED proc-20260821T120000-123-456\n") {
  throw new Error("runner_restart did not preserve core output");
}
if (byId.get(14).error?.code !== -32602 || !byId.get(14).error.message.includes("path")) {
  throw new Error("runner_status accepted a model-controlled path");
}
if (byId.get(15).error?.code !== -32602 || !byId.get(15).error.message.includes("handle")) {
  throw new Error("unsafe process handle was not rejected before the core boundary");
}
const large = byId.get(16).result.structuredContent;
if (large.stdout.length !== 1024 * 1024 || large.stdoutTruncated !== true || large.stderrTruncated !== false) {
  throw new Error("large output was not truthfully capped");
}
const resumed = byId.get(17).result;
if (resumed.isError || !resumed.structuredContent.stdout.includes("Local Agent binding resumed")) {
  throw new Error("runner_resume did not preserve core recovery output");
}

const calls = fs.readFileSync(callsPath, "utf8").trim().split("\n").map(JSON.parse);
if (calls.length !== 9) throw new Error(`invalid calls reached core: ${calls.length}`);
const expectedArgv = [
  "local-agent", "exec", "--for-context", cwd,
  "--timeout-seconds", "37", "--receipt-fd", "3", "--",
  "node", "-e", "console.log('a b')", "", "line\nbreak", "$HOME", "semi;colon", "quote'\"",
];
if (JSON.stringify(calls[0]) !== JSON.stringify(expectedArgv)) {
  throw new Error(`argv changed across MCP boundary:\n${JSON.stringify(calls[0])}\n${JSON.stringify(expectedArgv)}`);
}
const expectedFailedArgv = [
  "local-agent", "exec", "--for-context", cwd, "--receipt-fd", "3", "--", "exit-seven", "arg with spaces",
];
if (JSON.stringify(calls[1]) !== JSON.stringify(expectedFailedArgv)) {
  throw new Error("non-zero command argv changed across MCP boundary");
}
const handle = "proc-20260821T120000-123-456";
const expectedLifecycleCalls = [
  ["local-agent", "status", "--for-context", cwd],
  [
    "local-agent", "start", "--for-context", cwd, "--",
    "node", "server.js", "arg with spaces", "; touch SHOULD_NOT_EXIST", "$(touch ALSO_NOT)", "quote'\"",
  ],
  ["local-agent", "logs", "--for-context", cwd, handle],
  ["local-agent", "stop", "--for-context", cwd, handle],
  ["local-agent", "restart", "--for-context", cwd, handle],
  ["local-agent", "exec", "--for-context", cwd, "--receipt-fd", "3", "--", "large-output"],
  ["local-agent", "resume", "--for-context", cwd],
];
for (let index = 0; index < expectedLifecycleCalls.length; index += 1) {
  if (JSON.stringify(calls[index + 2]) !== JSON.stringify(expectedLifecycleCalls[index])) {
    throw new Error(
      `lifecycle argv changed across MCP boundary at ${index}:\n` +
      `${JSON.stringify(calls[index + 2])}\n${JSON.stringify(expectedLifecycleCalls[index])}`,
    );
  }
}
EOF

[[ ! -s "$stderr_file" ]] || {
  printf 'unexpected MCP server stderr:\n' >&2
  cat "$stderr_file" >&2
  exit 1
}

unbound_responses="$fixture_root/unbound-responses.jsonl"
unbound_calls="$fixture_root/unbound-calls.jsonl"
(
  cd "$non_git_root"
  REMOTE_DEV_MCP_TEST_LOG="$unbound_calls" \
    REMOTE_DEV_MCP_TEST_RECEIPT_DIR="$fixture_root/unbound-receipts" \
    "$install_root/bin/remote-dev-local-agent-mcp.mjs" > "$unbound_responses"
) <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"runner_exec","arguments":{"argv":["true"]}}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"runner_status","arguments":{}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"runner_start","arguments":{"argv":["node","server.js","; unsafe"]}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"runner_resume","arguments":{}}}
EOF

node --input-type=module - "$unbound_responses" "$non_git_root" <<'EOF'
import fs from "node:fs";

const [responsesPath, cwd] = process.argv.slice(2);
const canonicalCwd = fs.realpathSync(cwd);
const responses = fs.readFileSync(responsesPath, "utf8").trim().split("\n").map(JSON.parse);
const initialize = responses.find((response) => response.id === 1).result;
if (!initialize.instructions.includes(`MCP cwd: ${canonicalCwd}`)) {
  throw new Error("unbound diagnostics omit cwd");
}
if (!initialize.instructions.includes(`Trusted local context: ${canonicalCwd}`)) {
  throw new Error("non-Git aggregate context was not trusted");
}
for (const id of [2, 3, 4, 5]) {
  const call = responses.find((response) => response.id === id).result;
  if (call.isError !== false || call.structuredContent.exitCode !== 0) {
    throw new Error(`non-Git aggregate tool call ${id} was not routed to core`);
  }
}
EOF

node --input-type=module - "$unbound_calls" "$non_git_root" <<'EOF'
import fs from "node:fs";
const [callsPath, contextInput] = process.argv.slice(2);
const context = fs.realpathSync(contextInput);
const calls = fs.readFileSync(callsPath, "utf8").trim().split("\n").map(JSON.parse);
if (calls.length !== 4) throw new Error(`expected four aggregate-context calls, got ${calls.length}`);
for (const call of calls) {
  const index = call.indexOf("--for-context");
  if (index < 0 || call[index + 1] !== context || call.includes("--for-root")) {
    throw new Error(`aggregate context was not preserved: ${JSON.stringify(call)}`);
  }
}
EOF

echo 'remote-dev local-agent MCP smoke: ok'
