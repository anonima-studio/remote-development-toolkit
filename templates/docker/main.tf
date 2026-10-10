terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "2.18.0"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = "4.5.0"
    }
  }
}

provider "docker" {}

provider "coder" {
  url = "http://coder-control:7080"
}

variable "remote_dev_egress_mtu" {
  description = "MTU enforced on the workspace default-route interface and nested Docker networks."
  type        = number
  default     = 1420

  validation {
    condition     = var.remote_dev_egress_mtu >= 1280 && var.remote_dev_egress_mtu <= 1500
    error_message = "remote_dev_egress_mtu must be between 1280 and 1500."
  }
}

variable "remote_dev_coder_access_url" {
  description = "Coder origin used by desktop clients: a public HTTPS origin or the local SSH forward."
  type        = string

  validation {
    condition     = can(regex("^(https://[A-Za-z0-9.-]+(:[0-9]+)?|http://127\\.0\\.0\\.1:[0-9]+)$", var.remote_dev_coder_access_url))
    error_message = "remote_dev_coder_access_url must be an HTTPS origin or http://127.0.0.1:<port>, without a path."
  }
}

variable "jetbrains_webstorm_build" {
  description = "Exact WebStorm build opened by the JetBrains Toolbox app."
  type        = string
  default     = "262.9437.145"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.jetbrains_webstorm_build))
    error_message = "jetbrains_webstorm_build must be an exact numeric JetBrains build."
  }
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_external_auth" "github" {
  id       = "github"
  optional = true
}

data "coder_parameter" "workspace_size" {
  name         = "workspace_size"
  display_name = "Workspace size"
  description  = "Hard Docker limits. Standard leaves room for two active workspaces on a 12 GiB host."
  type         = "string"
  default      = "standard"
  mutable      = true

  option {
    name  = "Small — 1 CPU / 2 GiB"
    value = "small"
  }

  option {
    name  = "Standard — 2 CPU / 4 GiB"
    value = "standard"
  }

  option {
    name  = "Large — 3 CPU / 6 GiB"
    value = "large"
  }

  option {
    name  = "JetBrains — 4 CPU / 8 GiB"
    value = "jetbrains"
  }

  option {
    name  = "JetBrains XL — 4 CPU / 12 GiB"
    value = "jetbrains-xl"
  }
}

data "coder_parameter" "docker_capability" {
  name         = "docker_capability"
  display_name = "Docker capability"
  description  = "Core provides the pinned Docker CLI only. Containers adds an isolated internal daemon through sysbox-runc."
  type         = "string"
  default      = "core"
  mutable      = true

  option {
    name  = "Core — CLI only"
    value = "core"
  }

  option {
    name  = "Containers — internal Docker daemon"
    value = "containers"
  }
}

data "coder_parameter" "workspace_role" {
  name         = "workspace_role"
  display_name = "Workspace role"
  description  = "Permanent workspace boundary. Development hosts the authoritative remote checkout and IDE; runner executes a Git-less projection from a local Codex agent."
  type         = "string"
  default      = "development"
  mutable      = false

  option {
    name  = "Development — remote checkout and IDE"
    value = "development"
  }

  option {
    name  = "Runner — local-agent execution"
    value = "runner"
  }
}

locals {
  workspace_sizes = {
    small = {
      cpu_cores = 1
      memory_mb = 2048
    }
    standard = {
      cpu_cores = 2
      memory_mb = 4096
    }
    large = {
      cpu_cores = 3
      memory_mb = 6144
    }
    jetbrains = {
      cpu_cores = 4
      memory_mb = 8192
    }
    jetbrains-xl = {
      cpu_cores = 4
      memory_mb = 12288
    }
  }
  workspace_size       = local.workspace_sizes[data.coder_parameter.workspace_size.value]
  workspace_pids_limit = data.coder_parameter.docker_capability.value == "containers" ? 2048 : 1024
}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"

  startup_script_behavior = "blocking"

  display_apps {
    vscode                 = data.coder_parameter.workspace_role.value == "development"
    vscode_insiders        = false
    web_terminal           = true
    ssh_helper             = true
    port_forwarding_helper = true
  }

  startup_script = <<-EOT
    set -eu

    # remote-dev-workspace-role:start
    requested_workspace_role='${data.coder_parameter.workspace_role.value}'
    case "$requested_workspace_role" in
      development|runner) ;;
      *)
        printf 'WORKSPACE_ROLE_INVALID: expected development or runner.\n' >&2
        exit 1
        ;;
    esac

    role_state_dir="$HOME/.local/share/remote-dev"
    role_marker="$HOME/.local/share/remote-dev/workspace-role"
    if [ -L "$role_state_dir" ] || { [ -e "$role_state_dir" ] && [ ! -d "$role_state_dir" ]; }; then
      printf 'WORKSPACE_ROLE_STATE_INVALID: %s must be a real directory.\n' "$role_state_dir" >&2
      exit 1
    fi
    mkdir -p "$role_state_dir"
    chmod 0700 "$role_state_dir"

    verify_workspace_role_marker() {
      if [ -L "$role_marker" ] || [ ! -f "$role_marker" ]; then
        printf 'WORKSPACE_ROLE_STATE_INVALID: %s must be a regular file.\n' "$role_marker" >&2
        exit 1
      fi

      actual_workspace_role=$(cat "$role_marker"; printf x)
      expected_workspace_role=$(printf '%s\nx' "$requested_workspace_role")
      if [ "$actual_workspace_role" != "$expected_workspace_role" ]; then
        printf 'WORKSPACE_ROLE_MISMATCH: persistent workspace role does not match requested role %s.\n' "$requested_workspace_role" >&2
        printf 'Recovery: create a new workspace with the required role; do not reuse this workspace home volume.\n' >&2
        exit 1
      fi
      chmod 0600 "$role_marker"
    }

    if [ -e "$role_marker" ] || [ -L "$role_marker" ]; then
      verify_workspace_role_marker
    else
      role_marker_tmp=$(mktemp "$role_state_dir/.workspace-role.XXXXXX")
      printf '%s\n' "$requested_workspace_role" > "$role_marker_tmp"
      chmod 0600 "$role_marker_tmp"
      mv -n "$role_marker_tmp" "$role_marker"
      rm -f "$role_marker_tmp"
      verify_workspace_role_marker
    fi
    # remote-dev-workspace-role:end

    if [ ! -f "$HOME/.remote_dev_initialized" ]; then
      cp -rT /etc/skel "$HOME"
      mkdir -p "$HOME/projects"
      touch "$HOME/.remote_dev_initialized"
    fi

    # remote-dev-development-only:start
    if [ "$requested_workspace_role" = 'development' ]; then
      mkdir -p "$HOME/projects/.codex-worktrees" "$HOME/.agents/skills"
      chmod 0700 "$HOME/projects/.codex-worktrees" "$HOME/.agents" "$HOME/.agents/skills"
      skill_source=/usr/local/share/remote-dev/skills/worktree-branch-sync
      skill_target="$HOME/.agents/skills/worktree-branch-sync"
      if [ ! -f "$skill_source/SKILL.md" ]; then
        printf 'WARNING: image-owned branch-sync skill is missing: %s\n' "$skill_source" >&2
      elif [ -L "$skill_target" ]; then
        ln -sfn "$skill_source" "$skill_target"
      elif [ ! -e "$skill_target" ]; then
        ln -s "$skill_source" "$skill_target"
      else
        printf 'WARNING: branch-sync skill path conflicts with image ownership: %s\n' "$skill_target" >&2
      fi
    fi
    # remote-dev-development-only:end

    printf '%s\n' '${data.coder_parameter.workspace_size.value}' > "$HOME/.remote_dev_size"

    if [ ! -f "$COREPACK_HOME/lastKnownGood.json" ]; then
      mkdir -p "$COREPACK_HOME"
      cp -a /usr/local/share/corepack/. "$COREPACK_HOME/"
    fi

    if [ '${data.coder_parameter.docker_capability.value}' = 'containers' ]; then
      /usr/local/sbin/remote-dev-start-docker
    fi
  EOT

  env = {
    GIT_AUTHOR_NAME           = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_AUTHOR_EMAIL          = data.coder_workspace_owner.me.email
    GIT_COMMITTER_NAME        = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_COMMITTER_EMAIL       = data.coder_workspace_owner.me.email
    REMOTE_DEV_DOCKER_MTU     = tostring(var.remote_dev_egress_mtu)
    REMOTE_DEV_WORKSPACE_ROLE = data.coder_parameter.workspace_role.value
  }

  metadata {
    display_name = "CPU"
    key          = "0_cpu"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "RAM"
    key          = "1_ram"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Home disk"
    key          = "2_home_disk"
    script       = "coder stat disk --path $${HOME}"
    interval     = 60
    timeout      = 1
  }

  metadata {
    display_name = "Workspace role"
    key          = "3_workspace_role"
    script       = "printf '%s\\n' '${data.coder_parameter.workspace_role.value}'"
    interval     = 60
    timeout      = 1
  }

}

resource "coder_app" "webstorm" {
  count        = data.coder_parameter.workspace_role.value == "development" ? data.coder_workspace.me.start_count : 0
  agent_id     = coder_agent.main.id
  slug         = "jetbrains-ws"
  display_name = "WebStorm"
  icon         = "/icon/webstorm.svg"
  external     = true
  tooltip      = "Open /home/coder/projects in WebStorm through JetBrains Toolbox."
  url = join("", [
    "jetbrains://gateway/coder?workspace=",
    urlencode(data.coder_workspace.me.name),
    "&owner=",
    urlencode(data.coder_workspace_owner.me.name),
    "&folder=",
    urlencode("/home/coder/projects"),
    "&url=",
    urlencode(var.remote_dev_coder_access_url),
    "&token=$SESSION_TOKEN",
    "&ide_product_code=WS",
    "&ide_build_number=",
    urlencode(var.jetbrains_webstorm_build),
  ])
}

resource "docker_volume" "home" {
  name = "coder-${data.coder_workspace.me.id}-home"

  lifecycle {
    ignore_changes = all
  }

  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.owner_id"
    value = data.coder_workspace_owner.me.id
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  labels {
    label = "coder.workspace_name_at_creation"
    value = data.coder_workspace.me.name
  }
  labels {
    label = "remote-dev.workspace-role"
    value = data.coder_parameter.workspace_role.value
  }
}

resource "docker_volume" "docker_data" {
  name = "coder-${data.coder_workspace.me.id}-docker"

  lifecycle {
    ignore_changes = all
  }

  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  labels {
    label = "remote-dev.purpose"
    value = "workspace-docker-data"
  }
  labels {
    label = "remote-dev.workspace-role"
    value = data.coder_parameter.workspace_role.value
  }
}

resource "docker_container" "workspace" {
  count = data.coder_workspace.me.start_count

  image    = "remote-dev-workspace:node24-20260906-cf2a26a364e"
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name
  runtime  = data.coder_parameter.docker_capability.value == "containers" ? "sysbox-runc" : "runc"

  # The agent cannot start until the provisioner has installed the cgroup PID
  # limit through Docker and verified it. /tmp is ephemeral with the container.
  entrypoint = [
    "sh",
    "-c",
    "i=0; until [ -f /tmp/.remote-dev-pids-ready ]; do if [ \"$i\" -ge 60 ]; then echo PID_LIMIT_NOT_READY >&2; exit 1; fi; i=$((i+1)); sleep 1; done; exec sh -c \"$0\"",
    replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal"),
  ]
  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    "REMOTE_DEV_WORKSPACE_ROLE=${data.coder_parameter.workspace_role.value}",
  ]
  dns = ["1.1.1.1", "1.0.0.1"]

  memory = local.workspace_size.memory_mb
  cpus   = tostring(local.workspace_size.cpu_cores)

  # nproc counts every thread with this real UID across runc workspaces.
  # Keep it unlimited; the workspace's own cgroup is bounded below.
  ulimit {
    name = "nproc"
    soft = -1
    hard = -1
  }

  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }

  networks_advanced {
    name        = "remote-dev-egress"
    gw_priority = 1
  }

  networks_advanced {
    name        = "remote-dev-control"
    gw_priority = 0
  }

  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home.name
    read_only      = false
  }


  volumes {
    container_path = "/var/lib/docker"
    volume_name    = docker_volume.docker_data.name
    read_only      = false
  }

  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.owner_id"
    value = data.coder_workspace_owner.me.id
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  labels {
    label = "coder.workspace_name"
    value = data.coder_workspace.me.name
  }
  labels {
    label = "remote-dev.managed"
    value = "true"
  }
  labels {
    label = "remote-dev.size"
    value = data.coder_parameter.workspace_size.value
  }
  labels {
    label = "remote-dev.docker-capability"
    value = data.coder_parameter.docker_capability.value
  }
  labels {
    label = "remote-dev.workspace-role"
    value = data.coder_parameter.workspace_role.value
  }
}

# docker_container in the pinned Docker provider has no PidsLimit argument.
# Apply the Docker API update before releasing the agent startup gate.
resource "terraform_data" "workspace_pids" {
  count = data.coder_workspace.me.start_count

  triggers_replace = [
    docker_container.workspace[count.index].id,
    local.workspace_pids_limit,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/sh", "-c"]
    command     = <<-EOT
      set -eu
      container_id='${docker_container.workspace[count.index].id}'
      limit='${local.workspace_pids_limit}'
      case "$container_id" in ""|*[!0-9a-f]*) echo "PID_LIMIT_INVALID_CONTAINER" >&2; exit 1;; esac
      case "$limit" in ""|*[!0-9]*) echo "PID_LIMIT_INVALID_VALUE" >&2; exit 1;; esac
      api=http://docker
      request() {
        curl --fail --silent --show-error --max-time 30 --unix-socket /var/run/docker.sock "$@"
      }
      update_payload=$(printf '{"PidsLimit":%s}' "$limit")
      request -X POST -H 'Content-Type: application/json' --data "$update_payload" "$api/containers/$container_id/update" >/dev/null
      inspect=$(request "$api/containers/$container_id/json")
      printf '%s' "$inspect" | grep -q "\"PidsLimit\":$limit" || {
        echo "PID_LIMIT_VERIFY_FAILED" >&2
        exit 1
      }
      exec_payload='{"AttachStdout":false,"AttachStderr":false,"Cmd":["sh","-c","touch /tmp/.remote-dev-pids-ready"]}'
      exec_response=$(request -X POST -H 'Content-Type: application/json' --data "$exec_payload" "$api/containers/$container_id/exec")
      exec_id=$(printf '%s' "$exec_response" | sed -n 's/^.*"Id"[[:space:]]*:[[:space:]]*"\([0-9a-f]*\)".*$/\1/p')
      case "$exec_id" in ""|*[!0-9a-f]*) echo "PID_LIMIT_EXEC_ID_INVALID" >&2; exit 1;; esac
      request -X POST -H 'Content-Type: application/json' --data '{"Detach":false,"Tty":false}' "$api/exec/$exec_id/start" >/dev/null
      # An unattached exec start can return before Docker publishes its exit status.
      # Wait for Running=false before interpreting ExitCode; fail closed on timeout.
      attempts=0
      while :; do
        exec_inspect=$(request "$api/exec/$exec_id/json")
        if printf '%s' "$exec_inspect" | grep -q '"Running"[[:space:]]*:[[:space:]]*false'; then
          exec_exit=$(printf '%s' "$exec_inspect" | sed -n 's/^.*"ExitCode"[[:space:]]*:[[:space:]]*\(-*[0-9][0-9]*\).*$/\1/p')
          if [ "$exec_exit" != 0 ]; then
            echo "PID_LIMIT_RELEASE_FAILED: exec=$exec_id exit=$exec_exit" >&2
            exit 1
          fi
          break
        fi
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 30 ]; then
          echo "PID_LIMIT_RELEASE_TIMEOUT: exec=$exec_id" >&2
          exit 1
        fi
        sleep 1
      done
    EOT
  }
}
