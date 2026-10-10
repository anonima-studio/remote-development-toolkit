#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
helper=$repo_root/templates/docker/worktree-vars.sh
fixture=$(mktemp -d)
cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT

role_marker=$fixture/workspace-role
printf 'development\n' > "$role_marker"
export REMOTE_DEV_WORKSPACE_ROLE_HELPER=$repo_root/templates/docker/workspace-role.sh
export REMOTE_DEV_WORKSPACE_ROLE_MARKER=$role_marker

projects_root=$fixture/projects
source_tree=$projects_root/demo
worktree=$projects_root/.codex-worktrees/demo-feature
mkdir -p "$source_tree/.remote-dev" "$source_tree/config" \
  "$projects_root/.codex-worktrees"
git -C "$source_tree" init -q
git -C "$source_tree" config user.name 'Remote Dev Test'
git -C "$source_tree" config user.email remote-dev@example.test
cat > "$source_tree/.gitignore" <<'EOF'
.env
config/runtime.env
EOF
cat > "$source_tree/.remote-dev/worktree-vars.tsv" <<'EOF'
# source<TAB>destination
.env	.env
config/runtime.env	config/runtime.env
EOF
printf 'tracked\n' > "$source_tree/README.md"
git -C "$source_tree" add .gitignore .remote-dev/worktree-vars.tsv README.md
git -C "$source_tree" commit -qm initial
git -C "$source_tree" worktree add -qb feature "$worktree"
source_tree=$(cd "$source_tree" && pwd -P)
worktree=$(cd "$worktree" && pwd -P)

secret_value='WORKTREE_SECRET=never-print-this-value'
printf '%s\n' "$secret_value" > "$source_tree/.env"
printf 'API_ORIGIN=http://127.0.0.1:3000\n' > "$source_tree/config/runtime.env"

sync_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree demo --worktree .codex-worktrees/demo-feature)
for expected in \
  'READY    Worktree variables are synchronized without printing their values.' \
  "Source   $source_tree" \
  "Target   $worktree" \
  'Files    copied=2 unchanged=0'; do
  [[ $sync_output == *"$expected"* ]] || {
    printf 'worktree variable output omitted %s:\n%s\n' "$expected" "$sync_output" >&2
    exit 1
  }
done
if [[ $sync_output == *never-print-this-value* ]]; then
  echo 'worktree variable synchronization printed a secret value' >&2
  exit 1
fi
cmp -s "$source_tree/.env" "$worktree/.env"
cmp -s "$source_tree/config/runtime.env" "$worktree/config/runtime.env"
if [[ $(stat -f '%Lp' "$worktree/.env" 2>/dev/null || stat -c '%a' "$worktree/.env") != 600 ]]; then
  echo 'worktree variable destination is not owner-only' >&2
  exit 1
fi

repeat_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree "$source_tree" --worktree "$worktree")
if [[ $repeat_output != *'Files    copied=0 unchanged=2'* ]]; then
  printf 'repeat sync was not idempotent: %s\n' "$repeat_output" >&2
  exit 1
fi

printf 'WORKTREE_SECRET=target-edit\n' > "$worktree/.env"
set +e
conflict_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree demo --worktree .codex-worktrees/demo-feature 2>&1)
conflict_status=$?
set -e
if (( conflict_status != 1 )) || \
   [[ $conflict_output != *'Refusing to replace a differing worktree variable file'* ]] || \
   [[ $conflict_output != *'rerun with --replace'* ]]; then
  printf 'differing variable file had no safe recovery: %s\n' "$conflict_output" >&2
  exit 1
fi
if [[ $(<"$worktree/.env") != 'WORKTREE_SECRET=target-edit' ]]; then
  echo 'conflict path changed the target without --replace' >&2
  exit 1
fi

replace_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree demo --worktree .codex-worktrees/demo-feature --replace)
if [[ $replace_output != *'Files    copied=1 unchanged=1'* ]]; then
  printf 'explicit replacement reported the wrong result: %s\n' \
    "$replace_output" >&2
  exit 1
fi
cmp -s "$source_tree/.env" "$worktree/.env"

expect_route_failure() {
  local label=$1 expected_status=$2 expected_text=$3 output status
  set +e
  output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
    "$helper" --source-tree demo --worktree .codex-worktrees/demo-feature 2>&1)
  status=$?
  set -e
  if (( status != expected_status )) || [[ $output != *"$expected_text"* ]]; then
    printf '%s guard failed: status=%s output=%s\n' "$label" "$status" "$output" >&2
    exit 1
  fi
}

printf '../escape.env\tescape.env\n' > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure malformed-route 2 'invalid worktree variable route'

printf '# no routes\n' > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure empty-manifest 2 'worktree variable manifest has no routes'

printf '.env\tshared.env\nconfig/runtime.env\tshared.env\n' \
  > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure duplicate-destination 2 'duplicate worktree variable destination'

printf '.env\tconfig\nconfig/runtime.env\tconfig/runtime.env\n' \
  > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure overlapping-destination 2 'worktree variable destinations overlap'

printf 'missing.env\tmissing.env\n' > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure missing-source 2 'worktree variable source must be a regular non-symlink file'

ln -s .env "$source_tree/link.env"
printf 'link.env\tlink.env\n' > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure symlink-source 2 'worktree variable source must be a regular non-symlink file'
rm -f "$source_tree/link.env"

mkdir -p "$worktree/nonregular"
printf '.env\tnonregular\n' > "$source_tree/.remote-dev/worktree-vars.tsv"
expect_route_failure nonregular-destination 2 'refusing non-regular worktree variable destination'
rm -rf -- "$worktree/nonregular"

git -C "$source_tree" checkout -q -- .remote-dev/worktree-vars.tsv

printf 'FIRST=value\n' > "$source_tree/first.env"
printf 'SECOND=source\n' > "$source_tree/second.env"
printf 'SECOND=target\n' > "$worktree/second.env"
printf 'first.env\tfirst.env\nsecond.env\tsecond.env\n' \
  > "$source_tree/.remote-dev/worktree-vars.tsv"
set +e
transaction_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree demo --worktree .codex-worktrees/demo-feature 2>&1)
transaction_status=$?
set -e
if (( transaction_status != 1 )) || \
   [[ $transaction_output != *'Refusing to replace a differing worktree variable file'* ]] || \
   [[ -e $worktree/first.env ]]; then
  printf 'late conflict caused a partial variable sync: %s\n' \
    "$transaction_output" >&2
  exit 1
fi

outside=$fixture/outside
mkdir -p "$outside"
ln -s "$outside" "$worktree/escape"
printf '.env\tescape/new/.env\n' > "$source_tree/.remote-dev/worktree-vars.tsv"
set +e
symlink_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree demo --worktree .codex-worktrees/demo-feature 2>&1)
symlink_status=$?
set -e
if (( symlink_status != 2 )) || \
   [[ $symlink_output != *'refusing symlinked worktree variable destination parent'* ]] || \
   [[ -e $outside/new ]]; then
  printf 'symlink escape mutated an outside directory: %s\n' "$symlink_output" >&2
  exit 1
fi
git -C "$source_tree" checkout -q -- .remote-dev/worktree-vars.tsv

other_tree=$projects_root/other
mkdir -p "$other_tree"
git -C "$other_tree" init -q
set +e
unrelated_output=$(env REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --source-tree demo --worktree other 2>&1)
unrelated_status=$?
set -e
if (( unrelated_status != 2 )) || \
   [[ $unrelated_output != *'do not belong to the same linked-worktree repository'* ]]; then
  printf 'unrelated checkout was not refused: %s\n' "$unrelated_output" >&2
  exit 1
fi

fake_bin=$fixture/bin
command_log=$fixture/commands.log
mkdir -p "$fake_bin"
cat > "$fake_bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'ssh %s\n' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
exit 0
EOF
chmod +x "$fake_bin/ssh"
env PATH="$fake_bin:$PATH" REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  worktree-vars example-app \
  --source-dir example-app \
  --worktree-dir .codex-worktrees/example-app-feature
for expected in \
  '/usr/local/bin/remote-dev-workspace-role\ require\ development' \
  '/usr/local/bin/remote-dev-worktree-vars\ --help' \
  '/usr/local/bin/remote-dev-worktree-vars --source-tree example-app --worktree .codex-worktrees/example-app-feature'; do
  grep -Fq "$expected" "$command_log" || {
    printf 'worktree-vars CLI omitted %s:\n' "$expected" >&2
    cat "$command_log" >&2
    exit 1
  }
done

printf 'PASS     Variable files synchronize safely into every linked worktree\n'
