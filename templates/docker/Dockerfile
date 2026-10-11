# syntax=docker/dockerfile:1.7

FROM node:24@sha256:235600a8101ab264e117b1768e925532262668dc9b581ef1dd7d96ced463b8e7 AS node_toolchain
# Bun is copied from this stage; Renovate updates its digest.
FROM oven/bun:1@sha256:e10577f0db68676a7024391c6e5cb4b879ebd17188ab750cf10024a6d700e5c4 AS bun_toolchain
# Docker Engine, CLI and Compose are copied together from this stage.
FROM docker:29@sha256:12e683a161823b2a839aeea999b9d960e6e1f9a97b1679ad6b441982e2d9cf07 AS docker_toolchain

FROM codercom/enterprise-base:ubuntu@sha256:93e359e99a6fdaf6b11e3394fe77b0833bf3b6615b89737665aae1ed20abdb33

ARG REMOTE_DEV_WORKSPACE_SOURCE_SHA256
LABEL id0.remote-dev.workspace-source-sha256="$REMOTE_DEV_WORKSPACE_SOURCE_SHA256"

USER root

# renovate: datasource=npm depName=pnpm
ARG PNPM_VERSION=11.5.1
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.147.0

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    iproute2 \
    iptables \
    python3-minimal \
    xz-utils \
  && rm -rf /var/lib/apt/lists/*

COPY --from=node_toolchain /usr/local/bin/node /usr/local/bin/node
COPY --from=node_toolchain /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
  && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
  && corepack enable --install-directory /usr/local/bin pnpm \
  && COREPACK_HOME=/usr/local/share/corepack corepack install --global pnpm@${PNPM_VERSION} \
  && test -x /usr/local/bin/pnpx \
  && npm install --global @openai/codex@${CODEX_VERSION} \
  && npm cache clean --force

COPY --from=bun_toolchain /usr/local/bin/bun /usr/local/bin/bun
RUN ln -s /usr/local/bin/bun /usr/local/bin/bunx

COPY --from=docker_toolchain /usr/local/bin/containerd /usr/local/bin/containerd
COPY --from=docker_toolchain /usr/local/bin/containerd-shim-runc-v2 /usr/local/bin/containerd-shim-runc-v2
COPY --from=docker_toolchain /usr/local/bin/ctr /usr/local/bin/ctr
COPY --from=docker_toolchain /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_toolchain /usr/local/bin/docker-init /usr/local/bin/docker-init
COPY --from=docker_toolchain /usr/local/bin/docker-proxy /usr/local/bin/docker-proxy
COPY --from=docker_toolchain /usr/local/bin/dockerd /usr/local/bin/dockerd
COPY --from=docker_toolchain /usr/local/bin/runc /usr/local/bin/runc
COPY --from=docker_toolchain /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

COPY start-docker.sh /usr/local/sbin/remote-dev-start-docker
COPY codex-start.sh /usr/local/bin/remote-dev-codex
COPY project-setup.sh /usr/local/bin/remote-dev-project-setup
COPY project-contract.py /usr/local/bin/remote-dev-project-contract
COPY mode-guard.py /usr/local/bin/remote-dev-mode-guard
COPY worktree-vars.sh /usr/local/bin/remote-dev-worktree-vars
COPY workspace-role.sh /usr/local/bin/remote-dev-workspace-role
COPY managed-unlink.py /usr/local/bin/remote-dev-managed-unlink
COPY skills /usr/local/share/remote-dev/skills

RUN chmod 0755 /usr/local/sbin/remote-dev-start-docker \
      /usr/local/bin/remote-dev-codex /usr/local/bin/remote-dev-project-setup \
      /usr/local/bin/remote-dev-project-contract \
      /usr/local/bin/remote-dev-mode-guard \
      /usr/local/bin/remote-dev-worktree-vars \
      /usr/local/bin/remote-dev-workspace-role \
      /usr/local/bin/remote-dev-managed-unlink \
  && mkdir -p /var/lib/docker /var/run/remote-dev \
  && node --version \
  && npm --version \
  && corepack --version \
  && COREPACK_HOME=/usr/local/share/corepack pnpm --version \
  && bun --version \
  && codex --version \
  && remote-dev-codex --help >/dev/null \
  && remote-dev-project-setup --help >/dev/null \
  && remote-dev-project-contract --help >/dev/null \
  && remote-dev-mode-guard --help >/dev/null \
  && remote-dev-worktree-vars --help >/dev/null \
  && remote-dev-workspace-role --help >/dev/null \
  && remote-dev-managed-unlink --help >/dev/null \
  && docker --version \
  && docker compose version

ENV COREPACK_HOME=/home/coder/.cache/node/corepack

USER coder
