# Sourced by tests/remote-dev-local-agent-smoke.sh with REMOTE_DEV_TEST_SLICE=reanchor.
# Moves a working-set binding onto a new aggregate anchor and member set on the
# same runner workspace and project ID, first from a binding that was already
# retired (the state a repository move leaves behind), then from a live one.

# The main test exercises the one-time ghost Mutagen create; this slice does not.
common_env+=(REMOTE_DEV_TEST_MUTAGEN_GHOST_CREATE_ONCE=)
reanchor_root=$fixture/reanchor
mkdir -p "$reanchor_root/cp/dev"
reanchor_root=$(cd "$reanchor_root" && pwd -P)
cp_root=$reanchor_root/cp
dev_root=$cp_root/dev
ra_wrapper=$dev_root/wrapper.git
ra_app=$dev_root/app.git
ra_api=$dev_root/api.git
ra_product=$cp_root/product.git
mkdir -p "$ra_wrapper/.remote-dev/next" "$ra_app/src" "$ra_api/src" "$ra_product/src"
for repository in "$ra_wrapper" "$ra_app" "$ra_api" "$ra_product"; do
  git -C "$repository" init -q
  git -C "$repository" config user.name remote-dev-smoke
  git -C "$repository" config user.email remote-dev-smoke@example.invalid
done
printf 'services: {}\n' > "$ra_wrapper/compose.yaml"
printf 'app v1\n' > "$ra_app/src/app.txt"
printf 'api v1\n' > "$ra_api/src/api.txt"
printf 'product v2\n' > "$ra_product/src/product.txt"
printf '.env\nnode_modules/\n' | tee "$ra_app/.gitignore" > "$ra_product/.gitignore"
git -C "$ra_app" add .gitignore src/app.txt
git -C "$ra_app" commit -qm initial
git -C "$ra_api" add src/api.txt
git -C "$ra_api" commit -qm initial
git -C "$ra_product" add .gitignore src/product.txt
git -C "$ra_product" commit -qm initial
ra_manifest=$ra_wrapper/.remote-dev/local-agent-working-set.tsv
printf '%s\n' $'schema\t1' $'anchor\t../..' $'execution_member\twrapper' \
  $'member\twrapper\twrapper.git' $'member\tapp\tapp.git' $'member\tapi\tapi.git' \
  $'remote_writable\tapp\t.env' > "$ra_manifest"
git -C "$ra_wrapper" add compose.yaml .remote-dev/local-agent-working-set.tsv
git -C "$ra_wrapper" commit -qm 'dev-root working set'

ra_origin=$(REMOTE_DEV_PROFILE=example-profile /bin/bash -c \
  '. "$1/deploy/host-profiles/load.sh"; remote_dev_load_profile "$1" "$REMOTE_DEV_PROFILE"; printf %s "$REMOTE_DEV_CODER_ACCESS_URL"' \
  _ "$repo_root")
ra_tool() { run_tool --profile example-profile --coder-url "$ra_origin" "$@"; }
ra_remote=$remote_home/projects/multi
ra_control=$remote_home/.local/share/remote-dev/local-agent/multi
ra_runner_hash() { awk -F '\t' '$1 == "root_hash" {print $2; exit}' "$ra_control/binding.tsv"; }
ra_hash() { printf '%s' "$1" | shasum -a 256 | awk '{print $1}'; }
ra_refused() {
  local expected=$1 output status
  shift
  set +e
  output=$("$@" 2>&1)
  status=$?
  set -e
  if (( status == 0 )) || [[ $output != *"$expected"* ]]; then
    printf 'reanchor did not refuse with %s (exit %s): %s\n' "$expected" "$status" "$output" >&2
    exit 1
  fi
}

ra_tool up multi-runner --project-id multi --working-set "$ra_manifest" >/dev/null
[[ $(<"$ra_remote/app.git/src/app.txt") == 'app v1' ]]
# Runtime state that belongs to the runner, not to the projection.
ra_runner_secret='RUNNER_ONLY_SECRET_4419'
printf '%s\n' "$ra_runner_secret" > "$ra_remote/app.git/.env"
mkdir -p "$ra_remote/app.git/node_modules/dep" "$ra_remote/api.git/node_modules/dep"
printf 'app dependency\n' > "$ra_remote/app.git/node_modules/dep/index.js"
printf 'api dependency\n' > "$ra_remote/api.git/node_modules/dep/index.js"

# The product repository moved outside the old anchor: the tracked manifest now
# anchors one level higher and names the moved member.
printf '%s\n' $'schema\t1' $'anchor\t../../..' $'execution_member\twrapper' \
  $'member\twrapper\tdev/wrapper.git' $'member\tapp\tproduct.git' $'member\tapi\tdev/api.git' \
  $'remote_writable\tapp\t.env' > "$ra_manifest"
git -C "$ra_wrapper" commit -qam 'anchor the working set at cp'
ra_down=$(run_tool down --for-context "$dev_root" 2>/dev/null)
ra_retired=$(sed -n 's/^Retired  *//p' <<<"$ra_down")
[[ -d $ra_retired && $(ra_runner_hash) == "$(ra_hash "$dev_root")" ]]
ra_refused RUNNER_BINDING_CONFLICT ra_tool up multi-runner --project-id multi --working-set "$ra_manifest"

ra_reanchor() { ra_tool reanchor multi-runner --working-set "$ra_manifest" "$@"; }
ra_refused REANCHOR_CONFIRMATION_REQUIRED ra_reanchor --project-id multi \
  --confirm-anchor "$cp_root" --retired-binding "$ra_retired"
ra_refused REANCHOR_CONFIRMATION_REQUIRED ra_reanchor --project-id multi \
  --confirm-anchor "$dev_root" --retired-binding "$ra_retired" --confirm-retired "$ra_retired"
ra_refused REANCHOR_IDENTITY_MISMATCH ra_reanchor --project-id other \
  --confirm-anchor "$cp_root" --retired-binding "$ra_retired" --confirm-retired "$ra_retired"
if find "$state_root/reanchor" -name journal.tsv 2>/dev/null | grep -q .; then
  echo 'a refused re-anchor left a journal behind' >&2; exit 1
fi

ra_retired_args=(--project-id multi --confirm-anchor "$cp_root" --retired-binding "$ra_retired" --confirm-retired "$ra_retired")
mkdir -p "$ra_remote/api.git/src"
printf 'written on the runner\n' > "$ra_remote/api.git/src/rogue.txt"
ra_refused REMOTE_DRIFT ra_reanchor "${ra_retired_args[@]}"
[[ $(ra_runner_hash) == "$(ra_hash "$dev_root")" && -f $ra_remote/api.git/src/rogue.txt ]] || {
  echo 'a drifted re-anchor changed the runner' >&2; exit 1;
}
rm "$ra_remote/api.git/src/rogue.txt"
mkdir -p "$ra_control/executions"
printf 'schema\t2\n' > "$ra_control/executions/lost.running"
ra_refused RECOVERY_REQUIRED ra_reanchor "${ra_retired_args[@]}"
[[ $(ra_runner_hash) == "$(ra_hash "$dev_root")" ]]
rm "$ra_control/executions/lost.running"

ra_output=$(ra_reanchor "${ra_retired_args[@]}")
[[ $ra_output == *"REANCHORED     $dev_root -> $cp_root"* && \
   $ra_output == *'Retained       api.git app.git wrapper.git'* ]] || {
  printf 'unexpected re-anchor result: %s\n' "$ra_output" >&2; exit 1;
}
[[ $(ra_runner_hash) == "$(ra_hash "$cp_root")" ]]
[[ $(<"$ra_remote/dev/wrapper.git/compose.yaml") == 'services: {}' && \
   $(<"$ra_remote/product.git/src/product.txt") == 'product v2' && \
   $(<"$ra_remote/dev/api.git/src/api.txt") == 'api v1' ]] || {
  echo 're-anchor did not project the new members' >&2; exit 1;
}
[[ $(<"$ra_remote/app.git/.env") == "$ra_runner_secret" && \
   -f $ra_remote/app.git/node_modules/dep/index.js && -f $ra_remote/api.git/node_modules/dep/index.js ]] || {
  echo 're-anchor removed runner-owned secrets or dependencies' >&2; exit 1;
}
[[ ! -e $ra_remote/app.git/src && ! -e $ra_remote/api.git/src && ! -e $ra_remote/wrapper.git ]] || {
  echo 're-anchor left previous managed source or its empty directories' >&2; exit 1;
}
[[ ! -e $state_root/bindings/by-context/$(ra_hash "$dev_root").claim ]]
# The harness disables the watcher, so status reports Health blocked; the
# projection and topology lines are what re-anchoring owns.
ra_status=$(run_tool status --for-context "$ra_product" || true)
[[ $ra_status == *'Projection     clean'* && $ra_status == *'Topology       clean'* && \
   $ra_status == *'Retained       api.git app.git wrapper.git'* ]] || {
  printf 'status after re-anchor is not clean: %s\n' "$ra_status" >&2; exit 1;
}
[[ $(run_tool exec --for-context "$ra_product/src" -- /bin/pwd) == "$ra_remote/product.git/src" ]]
[[ $(ra_reanchor "${ra_retired_args[@]}") == *"REANCHORED     $dev_root -> $cp_root"* ]] || {
  echo 'repeating a completed re-anchor was not idempotent' >&2; exit 1;
}

# Secrets move with the version-2 flow: the new anchor, topology digest and
# member-prefixed route are accepted by the reviewed plan. The upload itself is
# unchanged by re-anchoring; stand in for it on the runner.
ra_product_secret='MOVED_PRODUCT_SECRET_5120'
printf '%s\n' "$ra_product_secret" > "$ra_product/.env"
chmod 0600 "$ra_product/.env"
ra_binding=$state_root/bindings/by-root/$(ra_hash "$cp_root")
ra_secrets=$fixture/reanchor-secrets.tsv
printf 'manifest_version\t2\nproject_id\tmulti\nworkspace\tmulti-runner\nworkspace_role\trunner\nlocal_root\t%s\nremote_root\t/home/coder/projects/multi\ntopology_digest\t%s\nfile\tproduct.git/.env\tproduct.git/.env\n' \
  "$cp_root" "$(awk -F '\t' '$1 == "topology_digest" {print $2; exit}' "$ra_binding/binding.tsv")" > "$ra_secrets"
mkdir -p "$remote_home/.local/share/remote-dev"
printf 'runner\n' > "$remote_home/.local/share/remote-dev/workspace-role"
env "${common_env[@]}" REMOTE_DEV_PROFILE=example-profile "$repo_root/bin/remote-dev-secrets" plan "$ra_secrets" >/dev/null
sed 's/\tproduct.git\/.env$/\tapp.git\/.env/' "$ra_secrets" > "$ra_secrets.retained"
ra_refused 'not declared remote-writable' env "${common_env[@]}" REMOTE_DEV_PROFILE=example-profile \
  "$repo_root/bin/remote-dev-secrets" plan "$ra_secrets.retained"
cp -p "$ra_product/.env" "$ra_remote/product.git/.env"
[[ $(run_tool status --for-context "$ra_product" || true) == *'Projection     clean'* ]]

# A live binding re-anchors too: `down` and `retire` run first, and the runner
# verifies the full previous projection before removing it.
ra_next=$ra_wrapper/.remote-dev/next/local-agent-working-set.tsv
printf '%s\n' $'schema\t1' $'anchor\t../../../../..' $'execution_member\twrapper' \
  $'member\twrapper\tcp/dev/wrapper.git' $'member\tapp\tcp/product.git' $'member\tapi\tcp/dev/api.git' \
  $'remote_writable\tapp\t.env' > "$ra_next"
git -C "$ra_wrapper" add .remote-dev/next/local-agent-working-set.tsv
git -C "$ra_wrapper" commit -qm 'next anchor'
ra_output=$(ra_tool reanchor multi-runner --project-id multi --working-set "$ra_next" \
  --confirm-anchor "$reanchor_root" --for-context "$ra_product")
[[ $ra_output == *"REANCHORED     $cp_root -> $reanchor_root"* && $ra_output == *'DOWN     '* ]]
[[ $(ra_runner_hash) == "$(ra_hash "$reanchor_root")" && \
   $(<"$ra_remote/cp/product.git/src/product.txt") == 'product v2' && \
   ! -e $ra_remote/product.git/src && ! -e $ra_remote/dev ]] || {
  echo 'live re-anchor did not move the projection' >&2; exit 1;
}
[[ $(<"$ra_remote/product.git/.env") == "$ra_product_secret" && \
   $(<"$ra_remote/app.git/.env") == "$ra_runner_secret" ]] || {
  echo 'live re-anchor lost retained runner secrets' >&2; exit 1;
}
[[ $(run_tool status --for-context "$ra_product" || true) == *'Projection     clean'* ]]
run_tool down --for-context "$reanchor_root" >/dev/null

[[ ! -e $remote_syntax_marker ]] || { echo 'a remote re-anchor command emitted a shell syntax error' >&2; exit 1; }
if grep -R -Fq -e "$ra_product_secret" -e "$ra_runner_secret" "$state_root" "$mutagen_state" "$command_log"; then
  echo 're-anchor leaked a secret into local state, Mutagen state, or logs' >&2
  exit 1
fi
printf 'PASS     Re-anchoring moves a working-set binding to a new anchor and member set\n'
