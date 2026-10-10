#!/usr/bin/env bash
# Reuse the established fake Coder/Mutagen lifecycle, not a second transport.
if [[ ${REMOTE_DEV_TEST_SLICE:-} != additive-policy ]]; then
  exec env REMOTE_DEV_TEST_SLICE=additive-policy bash "$(dirname "${BASH_SOURCE[0]}")/remote-dev-local-agent-smoke.sh"
fi

: > "$fixture/mutagen-ghost-create.once"
mv "$fake_bin/mutagen" "$fake_bin/mutagen-fixture"
cat > "$fake_bin/mutagen" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ -n ${REMOTE_DEV_TEST_POLICY_FLUSH_GATE:-} && ${2:-} == flush ]]; then
  printf '%s\n' "$$" > "$REMOTE_DEV_TEST_POLICY_FLUSH_GATE"
  while :; do sleep 0.05; done
fi
if [[ ${REMOTE_DEV_TEST_POLICY_PARTIAL_FLUSH:-false} == true && ${2:-} == flush ]]; then
  "$REMOTE_DEV_TEST_FAKE_BIN/mutagen-fixture" "$@"
  exit 1
fi
exec "$REMOTE_DEV_TEST_FAKE_BIN/mutagen-fixture" "$@"
EOF
chmod +x "$fake_bin/mutagen"
mkdir -p "$project/legacy"
printf 'legacy directory grant\n' > "$project/legacy/initial.ts"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src/generated.txt --allow-untracked legacy \
  --remote-writable data --exclude-tracked argv.sh >/dev/null
root_hash=$(printf '%s' "$project" | shasum -a 256 | awk '{print $1}')
binding_dir=$state_root/bindings/by-root/$root_hash
remote_project=$remote_home/projects/demo
mkdir -p "$remote_project/data"
printf 'remote only\n' > "$remote_project/data/preserved"
printf 'runner secret\n' > "$remote_project/.env"
printf 'new source\n' > "$project/src/new.ts"
printf 'not reviewed\n' > "$project/src/other.ts"
cp "$binding_dir/binding.tsv" "$fixture/binding.before"
cp "$binding_dir/policy.tsv" "$fixture/policy.before"

add_source() {
  env "${common_env[@]}" "$repo_root/bin/remote-dev" local-agent allow-untracked \
    --for-context "$project" --path "$1"
}
expect_refusal() {
  local expected=$1 path=$2 output
  if output=$(add_source "$path" 2>&1); then
    printf 'unexpected policy acceptance: %s\n' "$path" >&2; exit 1
  fi
  [[ $output == *"$expected"* ]] || { printf '%s\n' "$output" >&2; exit 1; }
  cmp "$fixture/policy.before" "$binding_dir/policy.tsv"
}

mkdir -p "$project/.hidden" "$project/data" "$project/src/build"
printf 'hidden\n' > "$project/.hidden/source.ts"
printf 'secret\n' > "$project/src/credentials.json"
printf 'remote-owned\n' > "$project/data/file.ts"
printf 'runtime\n' > "$project/src/build/file.ts"
ln -s new.ts "$project/src/link.ts"
for path in src .env .hidden/source.ts src/credentials.json src/link.ts src/build/file.ts 'src/*.ts' tracked.txt; do
  expect_refusal POLICY_UPDATE_PATH_UNSAFE "$path"
done
expect_refusal PROJECTION_POLICY_OVERLAP data/file.ts

stopped=$fixture/workspace-stopped
: > "$stopped"
if output=$(env "${common_env[@]}" REMOTE_DEV_TEST_WORKSPACE_STATE_FILE="$stopped" \
    "$tool" allow-untracked --for-context "$project" --path src/new.ts 2>&1); then exit 1; fi
[[ $output == *WORKSPACE_STOPPED* && -e $stopped ]]
cmp "$fixture/policy.before" "$binding_dir/policy.tsv"
rm "$stopped"

printf 'remote drift\n' > "$remote_project/tracked.txt"
expect_refusal REMOTE_DRIFT src/new.ts
[[ $(<"$remote_project/tracked.txt") == 'remote drift' && ! -e $remote_project/src/new.ts ]]
cp "$project/tracked.txt" "$remote_project/tracked.txt"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --recover-drift >/dev/null

for name in managed_manifest.tsv excluded_manifest.tsv current-manifest.id watcher-baseline.token; do
  cp "$binding_dir/$name" "$fixture/$name.before"
done
if output=$(env "${common_env[@]}" REMOTE_DEV_TEST_MUTAGEN_FAIL_FLUSH=1 \
    "$tool" allow-untracked --for-context "$project" --path src/new.ts 2>&1); then exit 1; fi
[[ $output == *POLICY_UPDATE_ROLLED_BACK* && -f $binding_dir/SYNC_FAILED ]]
cmp "$fixture/policy.before" "$binding_dir/policy.tsv"
for name in managed_manifest.tsv excluded_manifest.tsv current-manifest.id watcher-baseline.token; do
  cmp "$fixture/$name.before" "$binding_dir/$name"
done
[[ ! -e $binding_dir/projection/src/new.ts && ! -e $remote_project/src/new.ts ]]
expect_refusal SYNC_FAILED src/new.ts
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" >/dev/null

# A reported transfer failure may follow real remote writes. Preserve those
# bytes and block recovery until the operator restores the accepted remote tree.
if output=$(env "${common_env[@]}" REMOTE_DEV_TEST_POLICY_PARTIAL_FLUSH=true \
    "$tool" allow-untracked --for-context "$project" --path src/new.ts 2>&1); then exit 1; fi
[[ $output == *POLICY_UPDATE_ROLLED_BACK* && -f $binding_dir/REMOTE_DRIFT ]]
cmp "$fixture/policy.before" "$binding_dir/policy.tsv"
cmp "$fixture/managed_manifest.tsv.before" "$binding_dir/managed_manifest.tsv"
cmp "$project/src/new.ts" "$remote_project/src/new.ts"
expect_refusal SYNC_FAILED src/new.ts
rm "$remote_project/src/new.ts"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --recover-drift >/dev/null

# The workspace lease remains authoritative for this policy operation.
lease=$remote_home/.local/share/remote-dev/local-agent-workspace/lease.tsv
cp "$lease" "$fixture/lease.before"
awk 'BEGIN {FS=OFS="\t"} $1 == "owner_id" {$2="foreign-owner"} {print}' \
  "$fixture/lease.before" > "$lease"
expect_refusal LEASE_CONFLICT src/new.ts
cp "$fixture/lease.before" "$lease"

output=$(add_source src/new.ts)
[[ $output == *READY* ]]
cmp "$project/src/new.ts" "$remote_project/src/new.ts"
[[ ! -e $remote_project/src/other.ts && $(<"$remote_project/.env") == 'runner secret' \
   && $(<"$remote_project/data/preserved") == 'remote only' ]]
cmp "$fixture/binding.before" "$binding_dir/binding.tsv"
awk '$1 != "allowed_untracked" && $1 != "allowed_untracked_file"' "$fixture/policy.before" > "$fixture/protected.before"
awk '$1 != "allowed_untracked" && $1 != "allowed_untracked_file"' "$binding_dir/policy.tsv" > "$fixture/protected.after"
cmp "$fixture/protected.before" "$fixture/protected.after"
cp "$binding_dir/policy.tsv" "$fixture/accepted-policy"
add_source src/new.ts >/dev/null
cmp "$fixture/accepted-policy" "$binding_dir/policy.tsv"
run_tool exec --for-context "$project" -- /bin/cat src/new.ts > "$fixture/executed"
cmp "$project/src/new.ts" "$fixture/executed"

expect_exact_file_refusal() {
  local output
  if output=$(run_tool exec --for-context "$project" -- /usr/bin/true 2>&1); then
    echo 'exec accepted replacement of an exact-file grant' >&2; exit 1
  fi
  [[ $output == *PROJECTION_EXACT_FILE_INVALID* ]]
  if output=$(python3 "$repo_root/bin/remote-dev-local-agent-working-set.py" \
      watch-token --root "$project" --policy "$binding_dir/policy.tsv" 2>&1); then exit 1; fi
  [[ $output == *PROJECTION_EXACT_FILE_INVALID* ]]
  # The materialization entrypoint shares the same durable file constraint.
  if output=$(python3 "$repo_root/bin/remote-dev-local-agent-working-set.py" \
      exact-untracked --root "$project" --policy "$binding_dir/policy.tsv" 2>&1); then exit 1; fi
  [[ $output == *PROJECTION_EXACT_FILE_INVALID* ]]
}
rm "$project/src/new.ts"
mkdir "$project/src/new.ts"
printf 'must never transfer\n' > "$project/src/new.ts/credentials.json"
expect_exact_file_refusal
[[ -f $remote_project/src/new.ts && -f $binding_dir/projection/src/new.ts ]]
# Recovery calls materialize_projection before the watcher-token barrier.
printf 'synthetic recovery marker\n' > "$binding_dir/REMOTE_DRIFT"
if output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
    up demo-runner --project-id demo --local-project "$project" --recover-drift 2>&1); then exit 1; fi
[[ $output == *PROJECTION_EXACT_FILE_INVALID* && -f $binding_dir/REMOTE_DRIFT ]]
rm "$binding_dir/REMOTE_DRIFT" "$project/src/new.ts/credentials.json"
rmdir "$project/src/new.ts"
run_tool exec --for-context "$project" -- /usr/bin/true
[[ ! -e $remote_project/src/new.ts ]]
mkdir "$project/src/new.ts"
printf 'still unreviewed\n' > "$project/src/new.ts/credentials.json"
expect_exact_file_refusal
[[ ! -e $remote_project/src/new.ts ]]
rm "$project/src/new.ts/credentials.json"
rmdir "$project/src/new.ts"
ln -s other.ts "$project/src/new.ts"
expect_exact_file_refusal
rm "$project/src/new.ts"
printf 'recreated source\n' > "$project/src/new.ts"
run_tool exec --for-context "$project" -- /usr/bin/true
cmp "$project/src/new.ts" "$remote_project/src/new.ts"
git -C "$project" add src/new.ts
git -C "$project" commit -qm 'fixture: accepted source becomes tracked'
run_tool exec --for-context "$project" -- /usr/bin/true
cmp "$project/src/new.ts" "$remote_project/src/new.ts"
printf 'legacy descendant\n' > "$project/legacy/later.ts"
run_tool exec --for-context "$project" -- /usr/bin/true
cmp "$project/legacy/later.ts" "$remote_project/legacy/later.ts"

# A hard interruption cannot run shell rollback. Its stale operation lock
# must block the next public command while preserving the accepted snapshot.
printf 'second\n' > "$project/src/second.ts"
printf 'third\n' > "$project/src/third.ts"
env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  "$repo_root/bin/remote-dev" local-agent allow-untracked \
  --for-context "$project" --path src/second.ts --path src/third.ts >/dev/null
cmp "$project/src/second.ts" "$remote_project/src/second.ts"
cmp "$project/src/third.ts" "$remote_project/src/third.ts"
[[ -f $binding_dir/watcher.pid ]]
kill -0 "$(<"$binding_dir/watcher.pid")"
printf 'watcher update\n' > "$project/src/second.ts"
for attempt in $(seq 1 600); do
  cmp -s "$project/src/second.ts" "$remote_project/src/second.ts" && break
  sleep 0.05
done
cmp "$project/src/second.ts" "$remote_project/src/second.ts"
# Recovery retains identity/metadata options but omits all projection flags.
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --git-metadata not-required >/dev/null
cp "$binding_dir/policy.tsv" "$fixture/policy.before"
cp "$binding_dir/managed_manifest.tsv" "$fixture/pre-crash-manifest"
printf 'crash candidate\n' > "$project/src/interrupted.ts"
policy_gate=$fixture/policy-flush-gate
env "${common_env[@]}" REMOTE_DEV_TEST_POLICY_FLUSH_GATE="$policy_gate" \
  "$tool" allow-untracked --for-context "$project" --path src/interrupted.ts \
  > "$fixture/interrupted.out" 2>&1 &
policy_pid=$!
wait_for_file "$policy_gate"
kill -KILL "$policy_pid"
wait "$policy_pid" 2>/dev/null || true
stop_test_pid "$(<"$policy_gate")"
expect_refusal OPERATION_LOCK_STALE src/interrupted.ts
[[ -d $binding_dir/operation.lock ]]
snapshots=("$binding_dir"/.policy-update.*)
[[ ${#snapshots[@]} == 1 && -d ${snapshots[0]} ]]
cmp "$fixture/policy.before" "${snapshots[0]}/policy.tsv"
cmp "$fixture/pre-crash-manifest" "${snapshots[0]}/managed_manifest.tsv"
printf 'additive policy lifecycle: passed\n'
