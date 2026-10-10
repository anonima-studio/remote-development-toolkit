#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
helper=$repo_root/bin/remote-dev-local-agent-config.mjs
fixture=$(mktemp -d)
cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT

config=$fixture/codex/config.toml
state=$fixture/bridge/mcp-install.json
node_copy=$fixture/node
server_copy=$fixture/server.mjs
mkdir -p "$(dirname "$config")"
cp "$(command -v node)" "$node_copy"
cp "$helper" "$server_copy"
chmod 0755 "$node_copy" "$server_copy"

printf 'model = "synthetic"\n\n[mcp_servers.keep_me]\ncommand = "odd-spacing" # preserve\n' > "$config"
original=$fixture/original.toml
cp "$config" "$original"

"$helper" install --config "$config" --state "$state" \
  --node "$node_copy" --server "$server_copy"
first_hash=$(shasum -a 256 "$config" | awk '{print $1}')
"$helper" install --config "$config" --state "$state" \
  --node "$node_copy" --server "$server_copy"
second_hash=$(shasum -a 256 "$config" | awk '{print $1}')
[[ $first_hash == "$second_hash" ]]
[[ $("$helper" status --config "$config" --state "$state") == mcp=ready ]]
grep -Fq '[mcp_servers.remote_dev_local_agent]' "$config"
grep -Fq 'command = "odd-spacing" # preserve' "$config"

printf '\n[projects."/new/path"]\ntrust_level = "trusted"\n' >> "$config"
appended=$fixture/appended.toml
cp "$config" "$appended"
"$helper" uninstall --config "$config" --state "$state"
if grep -Fq '[mcp_servers.remote_dev_local_agent]' "$config"; then
  echo 'uninstall retained the owned MCP section' >&2
  exit 1
fi
grep -Fq '[projects."/new/path"]' "$config"
python3 - "$original" "$config" <<'PY'
from pathlib import Path
import sys

original = Path(sys.argv[1]).read_bytes()
current = Path(sys.argv[2]).read_bytes()
assert current.startswith(original)
assert current[len(original):] == b'\n[projects."/new/path"]\ntrust_level = "trusted"\n'
PY
[[ $("$helper" status --config "$config" --state "$state") == mcp=absent ]]

printf '\n[mcp_servers.remote_dev_local_agent]\ncommand = "foreign"\n' >> "$config"
set +e
conflict_output=$("$helper" install --config "$config" --state "$state" \
  --node "$node_copy" --server "$server_copy" 2>&1)
conflict_status=$?
set -e
if (( conflict_status != 1 )) || [[ $conflict_output != *MCP_ENTRY_CONFLICT* ]]; then
  printf 'foreign MCP entry was not refused: %s\n' "$conflict_output" >&2
  exit 1
fi

router_home=$fixture/router-home
router_codex=$fixture/router-codex
router_state=$fixture/router-state/local-agent
router_bin=$fixture/router-bin
mutagen_log=$fixture/mutagen.log
mkdir -p "$router_home" "$router_codex" "$router_bin"
cat > "$router_bin/mutagen" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$REMOTE_DEV_TEST_MUTAGEN_LOG"
if [[ ${1:-} == version ]]; then
  printf '0.18.1\n'
fi
EOF
chmod 0755 "$router_bin/mutagen"
install_output=$(env \
  HOME="$router_home" \
  CODEX_HOME="$router_codex" \
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$router_state" \
  REMOTE_DEV_TEST_MUTAGEN_LOG="$mutagen_log" \
  PATH="$router_bin:$PATH" \
  "$repo_root/bin/remote-dev" local-agent install)
[[ $install_output == *'READY    Local Agent integration is installed.'* ]]
grep -Fxq 'daemon start' "$mutagen_log"
grep -Fq '[mcp_servers.remote_dev_local_agent]' "$router_codex/config.toml"
[[ -f $router_state/mcp-install.json ]]

orphan_claim=$router_state/bindings/by-runner/profile/orphan.claim
mkdir -p "$(dirname "$orphan_claim")"
printf '%s\n' "$router_state/bindings/by-root/missing" > "$orphan_claim"
set +e
orphan_output=$(env \
  HOME="$router_home" \
  CODEX_HOME="$router_codex" \
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$router_state" \
  REMOTE_DEV_TEST_MUTAGEN_LOG="$mutagen_log" \
  PATH="$router_bin:$PATH" \
  "$repo_root/bin/remote-dev" local-agent uninstall 2>&1)
orphan_status=$?
set -e
if (( orphan_status != 1 )) || [[ $orphan_output != *'unowned reverse-claim state'* ]]; then
  printf 'router uninstall did not fail closed on an orphan claim: %s\n' "$orphan_output" >&2
  exit 1
fi
grep -Fq '[mcp_servers.remote_dev_local_agent]' "$router_codex/config.toml"
rm -f -- "$orphan_claim"
rmdir "$(dirname "$orphan_claim")" "$router_state/bindings/by-runner" \
  "$router_state/bindings" 2>/dev/null || true

uninstall_output=$(env \
  HOME="$router_home" \
  CODEX_HOME="$router_codex" \
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$router_state" \
  REMOTE_DEV_TEST_MUTAGEN_LOG="$mutagen_log" \
  PATH="$router_bin:$PATH" \
  "$repo_root/bin/remote-dev" local-agent uninstall)
[[ $uninstall_output == *'READY    Local Agent integration and verified inactive local bridge state were removed.'* ]]
if grep -Fq '[mcp_servers.remote_dev_local_agent]' "$router_codex/config.toml"; then
  echo 'router uninstall retained its MCP entry' >&2
  exit 1
fi

printf 'PASS     local-agent MCP install is idempotent, reversible, and namespaced\n'
