#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
helper=$repo_root/bin/remote-dev-local-agent-working-set.py
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT

anchor=$fixture/aggregate
wrapper=$anchor/wrapper.git
app=$anchor/app.git
api=$anchor/api.git
mkdir -p "$wrapper/.remote-dev" "$app/src" "$api/src"

for repository in "$wrapper" "$app" "$api"; do
  git -C "$repository" init -q
  git -C "$repository" config user.name remote-dev-smoke
  git -C "$repository" config user.email remote-dev-smoke@example.invalid
done

printf 'wrapper\n' > "$wrapper/compose.yaml"
printf 'app-v1\n' > "$app/src/app.txt"
printf 'api-v1\n' > "$api/src/api.txt"
printf '.env\nnode_modules/\n' > "$app/.gitignore"
mkdir -p "$app/packages/legacy/node_modules"
printf 'tracked runtime residue\n' > "$app/packages/legacy/node_modules/tracked.txt"
git -C "$app" add .gitignore src/app.txt
git -C "$app" add -f packages/legacy/node_modules/tracked.txt
git -C "$app" commit -qm initial
git -C "$api" add src/api.txt
git -C "$api" commit -qm initial

manifest=$wrapper/.remote-dev/local-agent-working-set.tsv
write_manifest() {
  printf '%s\n' \
    $'schema\t1' \
    $'anchor\t../..' \
    $'execution_member\twrapper' \
    $'member\twrapper\twrapper.git' \
    $'member\tapp\tapp.git' \
    $'member\tapi\tapi.git' \
    $'allow_untracked\tapp\tsrc/generated.txt' \
    $'remote_writable\tapp\t.env' \
    > "$manifest"
}
write_manifest
git -C "$wrapper" add compose.yaml .remote-dev/local-agent-working-set.tsv
git -C "$wrapper" commit -qm initial

printf 'generated\n' > "$app/src/generated.txt"
printf 'must-stay-local\n' > "$app/.env"
mkdir -p "$app/node_modules"
printf 'cache\n' > "$app/node_modules/cache.txt"

snapshot=$fixture/working-set.snapshot.tsv
policy=$fixture/policy.tsv
topology_digest=$($helper normalize --manifest "$manifest" --snapshot-out "$snapshot" --policy-out "$policy")
[[ $topology_digest =~ ^[a-f0-9]{64}$ ]]
[[ $(awk -F '\t' '$1 == "anchor" {print $2; exit}' "$snapshot") == "$anchor" ]]
[[ $(grep -c $'^member\t' "$snapshot") == 3 ]]
grep -Fqx $'allowed_untracked\tapp\tsrc/generated.txt' "$policy"
grep -Fqx $'remote_writable\tapp\t.env' "$policy"

anchor_context=$($helper context --snapshot "$snapshot" --path "$anchor" --remote-root /home/coder/projects/example-project)
[[ $anchor_context == *$'member\twrapper'* ]]
[[ $anchor_context == *$'remote_cwd\t/home/coder/projects/example-project/wrapper.git'* ]]
mkdir -p "$app/src/nested"
member_context=$($helper context --snapshot "$snapshot" --path "$app/src/nested" --remote-root /home/coder/projects/example-project)
[[ $member_context == *$'member\tapp'* ]]
[[ $member_context == *$'remote_cwd\t/home/coder/projects/example-project/app.git/src/nested'* ]]

stage=$fixture/projection
managed=$fixture/managed.tsv
excluded=$fixture/excluded.tsv
member_digests=$fixture/member-digests.tsv
$helper materialize --snapshot "$snapshot" --policy "$policy" --stage "$stage" \
  --managed "$managed" --excluded "$excluded" --member-digests "$member_digests"
[[ $(<"$stage/wrapper.git/compose.yaml") == wrapper ]]
[[ $(<"$stage/app.git/src/app.txt") == app-v1 ]]
[[ $(<"$stage/app.git/src/generated.txt") == generated ]]
[[ $(<"$stage/api.git/src/api.txt") == api-v1 ]]
[[ ! -e $stage/app.git/.env && ! -e $stage/app.git/node_modules && \
   ! -e $stage/app.git/packages/legacy/node_modules ]]
if grep -Fq 'app.git/packages/legacy/node_modules/tracked.txt' "$managed"; then
  echo 'tracked nested runtime path entered the working-set managed manifest' >&2
  exit 1
fi
grep -Fq $'app.git/.env\tremote-writable' "$excluded"
[[ $(grep -c $'^app\t\|^api\t\|^wrapper\t' "$member_digests") == 3 ]]

set +e
mutation_output=$(REMOTE_DEV_LOCAL_AGENT_TEST_MUTATE_SOURCE="$app/src/app.txt" \
  $helper materialize --snapshot "$snapshot" --policy "$policy" --stage "$fixture/unstable" \
    --managed "$fixture/unstable-managed" --excluded "$fixture/unstable-excluded" \
    --member-digests "$fixture/unstable-digests" 2>&1)
mutation_status=$?
set -e
if (( mutation_status != 79 )) || [[ $mutation_output != SOURCE_CHANGED:* ]]; then
  printf 'working-set materialization accepted a mixed source revision: %s\n' "$mutation_output" >&2
  exit 1
fi

printf '# semantic comment\n' >> "$manifest"
$helper verify --snapshot "$snapshot" >/dev/null
printf '%s\n' $'remote_writable\tapi\t.env' >> "$manifest"
set +e
drift_output=$($helper verify --snapshot "$snapshot" 2>&1)
drift_status=$?
set -e
if (( drift_status != 79 )) || [[ $drift_output != WORKING_SET_TOPOLOGY_DRIFT:* ]]; then
  printf 'working-set topology drift was not fail-closed: %s\n' "$drift_output" >&2
  exit 1
fi

write_manifest
nested=$app/nested.git
mkdir -p "$nested"
git -C "$nested" init -q
git -C "$nested" config user.name remote-dev-smoke
git -C "$nested" config user.email remote-dev-smoke@example.invalid
printf 'nested\n' > "$nested/file.txt"
git -C "$nested" add file.txt
git -C "$nested" commit -qm initial
printf '%s\n' $'member\tnested\tapp.git/nested.git' >> "$manifest"
set +e
overlap_output=$($helper normalize --manifest "$manifest" --snapshot-out "$fixture/overlap" \
  --policy-out "$fixture/overlap-policy" 2>&1)
overlap_status=$?
set -e
if (( overlap_status == 0 )) || [[ $overlap_output != WORKING_SET_MEMBER_OVERLAP:* ]]; then
  printf 'nested working-set members were not rejected: %s\n' "$overlap_output" >&2
  exit 1
fi

# Re-anchoring decides where the previous binding's remote-owned content ends
# up: inherited by a member at the same path, retained where it is, or refused.
reanchor_snapshot() {
  local destination=$1
  shift
  { printf 'schema\t1\nmanifest_path\t/synthetic/manifest.tsv\nmanifest_digest\t0\ntopology_digest\t0\n'
    printf 'anchor\t/synthetic\nexecution_member\twrapper\n'
    printf '%s\n' "$@"; } > "$destination"
}
reanchor_snapshot "$fixture/reanchor-old.tsv" \
  $'member\twrapper\t/synthetic/wrapper.git\twrapper.git\tnot-required' \
  $'member\tapp\t/synthetic/app.git\tapp.git\tnot-required'
printf 'path\treason\nwrapper.git/.env\tremote-writable\napp.git/.env\tremote-writable\napp.git/node_modules\tstandard\nlegacy.git\tretained\n' \
  > "$fixture/reanchor-old-excluded.tsv"
reanchor_snapshot "$fixture/reanchor-new.tsv" \
  $'member\twrapper\t/synthetic/wrapper.git\twrapper.git\tnot-required' \
  $'member\tapp\t/synthetic/cp/app.git\tcp/app.git\tnot-required'
: > "$fixture/reanchor-new-policy.tsv"
reanchor_plan() {
  $helper reanchor-plan --old-snapshot "$fixture/reanchor-old.tsv" \
    --old-excluded "$fixture/reanchor-old-excluded.tsv" --new-snapshot "$fixture/reanchor-new.tsv" \
    --new-policy "$fixture/reanchor-new-policy.tsv" --retained-out "$fixture/reanchor-retained.tsv"
}
set +e
uncovered_output=$(reanchor_plan 2>&1)
uncovered_status=$?
set -e
if (( uncovered_status == 0 )) || [[ $uncovered_output != REANCHOR_WRITABLE_UNCOVERED:*wrapper.git/.env* ]]; then
  printf 'an inherited member lost its remote-writable scope silently: %s\n' "$uncovered_output" >&2
  exit 1
fi
printf 'remote_writable\twrapper\t.env\n' > "$fixture/reanchor-new-policy.tsv"
plan_output=$(reanchor_plan)
[[ $plan_output == $'inherited\twrapper.git\nretained\tapp.git\nretained\tlegacy.git' ]] || {
  printf 'unexpected re-anchor plan: %s\n' "$plan_output" >&2; exit 1;
}
[[ $(<"$fixture/reanchor-retained.tsv") == $'retained\tapp.git\nretained\tlegacy.git' ]]
reanchor_snapshot "$fixture/reanchor-new.tsv" \
  $'member\twrapper\t/synthetic/wrapper.git\twrapper.git\tnot-required' \
  $'member\tapp\t/synthetic/app.git/nested\tapp.git/nested\tnot-required'
set +e
overlap_plan_output=$(reanchor_plan 2>&1)
overlap_plan_status=$?
set -e
if (( overlap_plan_status == 0 )) || [[ $overlap_plan_output != REANCHOR_REMOTE_OVERLAP:* ]]; then
  printf 'a new member nested in a previous remote area was accepted: %s\n' "$overlap_plan_output" >&2
  exit 1
fi

header=$'path\ttype\tmode\tdigest\tsymlink_target\tstate'
printf '%s\n%s\n%s\n' "$header" $'a/one\tfile\t0644\taaaa\t-\tpresent' $'a/two\tfile\t0644\tbbbb\t-\tpresent' \
  > "$fixture/reanchor-expected.tsv"
printf '%s\n%s\n%s\n' "$header" $'a/one\tfile\t0644\taaaa\t-\tpresent' $'a/two\tmissing\t0000\t-\t-\tdeleted' \
  > "$fixture/reanchor-actual.tsv"
[[ $($helper reanchor-verify --expected "$fixture/reanchor-expected.tsv" --actual "$fixture/reanchor-actual.tsv") == \
   $'present\t1\nabsent\t1' ]]
for actual in $'a/one\tfile\t0644\tcccc\t-\tpresent\na/two\tmissing\t0000\t-\t-\tdeleted' \
              $'a/one\tfile\t0644\taaaa\t-\tpresent\na/two\tmissing\t0000\t-\t-\tdeleted\na/rogue\tunexpected-file\t0644\tdddd\t-\tpresent'; do
  printf '%s\n%s\n' "$header" "$actual" > "$fixture/reanchor-actual.tsv"
  set +e
  verify_output=$($helper reanchor-verify --expected "$fixture/reanchor-expected.tsv" --actual "$fixture/reanchor-actual.tsv" 2>&1)
  verify_status=$?
  set -e
  if (( verify_status != 74 )) || [[ $verify_output != REMOTE_DRIFT:* ]]; then
    printf 're-anchor verification accepted a changed runner tree: %s\n' "$verify_output" >&2
    exit 1
  fi
done

printf 'PASS     Local Agent working-set topology and stable projection are deterministic\n'
