#!/usr/bin/env node

import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { createHash, randomBytes } from "node:crypto";

const sectionName = "mcp_servers.remote_dev_local_agent";

function fail(message, code = 1) {
  process.stderr.write(`ERROR    ${message}\n`);
  process.exit(code);
}

function usage() {
  process.stderr.write(
    "Usage: remote-dev-local-agent-config install --config <path> --state <path> --node <path> --server <path>\n" +
      "       remote-dev-local-agent-config uninstall --config <path> --state <path>\n" +
      "       remote-dev-local-agent-config status --config <path> --state <path>\n",
  );
}

function parseOptions(values) {
  const options = new Map();
  for (let index = 0; index < values.length; index += 2) {
    const key = values[index];
    const value = values[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      usage();
      fail("invalid local-agent config arguments", 2);
    }
    if (options.has(key)) fail(`duplicate option: ${key}`, 2);
    options.set(key, value);
  }
  return options;
}

function required(options, key) {
  const value = options.get(key);
  if (!value) fail(`${key} is required`, 2);
  return resolve(value);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function assertSafeRegular(path, allowMissing = false) {
  if (!existsSync(path)) {
    if (allowMissing) return;
    fail(`required file is missing: ${path}`);
  }
  const metadata = lstatSync(path);
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    fail(`path must be a regular non-symlink file: ${path}`);
  }
}

function readUtf8(path, allowMissing = false) {
  assertSafeRegular(path, allowMissing);
  if (!existsSync(path)) return "";
  const bytes = readFileSync(path);
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    fail(`file is not valid UTF-8: ${path}`);
  }
}

function atomicWrite(path, content, mode = 0o600) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.remote-dev-${process.pid}-${randomBytes(6).toString("hex")}`;
  writeFileSync(temporary, content, { encoding: "utf8", mode });
  chmodSync(temporary, mode);
  renameSync(temporary, path);
}

function sectionRange(content) {
  const header = `[${sectionName}]`;
  const lines = [...content.matchAll(/^[ \t]*\[[^\]\r\n]+\][ \t]*(?:#.*)?(?:\r?\n|$)/gm)];
  const matches = lines.filter((match) => match[0].trimEnd().split(/[ \t]+#/u, 1)[0].trim() === header);
  if (matches.length > 1) fail(`duplicate [${sectionName}] sections`);
  if (matches.length === 0) return null;
  const start = matches[0].index;
  const next = lines.find((match) => match.index > start);
  return { start, end: next?.index ?? content.length };
}

function renderBlock(nodePath, serverPath) {
  return (
    `[${sectionName}]\n` +
    `command = ${JSON.stringify(nodePath)}\n` +
    `args = [${JSON.stringify(serverPath)}]\n`
  );
}

function loadState(path) {
  const raw = readUtf8(path);
  try {
    const state = JSON.parse(raw);
    if (state.schema !== 1 || state.section !== sectionName) throw new Error("schema");
    return state;
  } catch {
    fail(`invalid local-agent MCP install state: ${path}`);
  }
}

function install(configPath, statePath, nodePath, serverPath) {
  assertSafeRegular(nodePath);
  assertSafeRegular(serverPath);
  const configMode = existsSync(configPath) ? lstatSync(configPath).mode & 0o777 : 0o600;
  const original = readUtf8(configPath, true);
  const block = renderBlock(nodePath, serverPath);
  const range = sectionRange(original);

  if (range) {
    const current = original.slice(range.start, range.end).trimEnd() + "\n";
    if (current !== block) {
      fail(`MCP_ENTRY_CONFLICT [${sectionName}] already exists with different ownership`);
    }
    if (existsSync(statePath)) {
      const state = loadState(statePath);
      if (state.blockSha256 !== sha256(block)) fail("local-agent MCP install state does not match the configured entry");
    } else {
      atomicWrite(
        statePath,
        `${JSON.stringify({ schema: 1, section: sectionName, blockSha256: sha256(block), ownedText: block }, null, 2)}\n`,
      );
    }
    process.stdout.write(`READY    MCP [${sectionName}] is already installed\n`);
    return;
  }

  if (existsSync(statePath)) fail("local-agent MCP install state exists but its config entry is missing");
  const ownedPrefix = original.length === 0 ? "" : original.endsWith("\n\n") ? "" : original.endsWith("\n") ? "\n" : "\n\n";
  const installed = `${original}${ownedPrefix}${block}`;
  atomicWrite(configPath, installed, configMode);
  atomicWrite(
    statePath,
    `${JSON.stringify({ schema: 1, section: sectionName, blockSha256: sha256(block), ownedText: ownedPrefix + block }, null, 2)}\n`,
  );
  process.stdout.write(`READY    MCP [${sectionName}] installed; restart Codex Desktop to load it\n`);
}

function uninstall(configPath, statePath) {
  const configMode = existsSync(configPath) ? lstatSync(configPath).mode & 0o777 : 0o600;
  const content = readUtf8(configPath, true);
  if (!existsSync(statePath)) {
    if (!sectionRange(content)) {
      process.stdout.write(`READY    MCP [${sectionName}] is already absent\n`);
      return;
    }
    fail("MCP entry exists without local-agent ownership state; refusing to remove it");
  }
  const state = loadState(statePath);
  const range = sectionRange(content);
  if (!range) fail("local-agent MCP ownership state exists but its config entry is missing");
  const current = content.slice(range.start, range.end).trimEnd() + "\n";
  if (sha256(current) !== state.blockSha256) fail("local-agent MCP entry changed since install; refusing to remove it");

  if (typeof state.ownedText !== "string" || !state.ownedText.endsWith(current)) {
    fail("local-agent MCP ownership state has no exact reversible config fragment");
  }
  const first = content.indexOf(state.ownedText);
  const second = first < 0 ? -1 : content.indexOf(state.ownedText, first + 1);
  if (first < 0 || second >= 0) fail("owned local-agent MCP config fragment is missing or ambiguous");
  const updated = content.slice(0, first) + content.slice(first + state.ownedText.length);
  atomicWrite(configPath, updated, configMode);
  unlinkSync(statePath);
  process.stdout.write(`READY    MCP [${sectionName}] removed; restart Codex Desktop to unload it\n`);
}

function status(configPath, statePath) {
  const content = readUtf8(configPath, true);
  const range = sectionRange(content);
  const state = existsSync(statePath) ? loadState(statePath) : null;
  if (!range && !state) {
    process.stdout.write("mcp=absent\n");
    return;
  }
  if (!range || !state) fail("local-agent MCP config and ownership state disagree");
  const current = content.slice(range.start, range.end).trimEnd() + "\n";
  if (sha256(current) !== state.blockSha256) fail("local-agent MCP entry changed since install");
  process.stdout.write("mcp=ready\n");
}

const [command, ...rawOptions] = process.argv.slice(2);
if (!command || command === "-h" || command === "--help") {
  usage();
  process.exit(command ? 0 : 2);
}
const options = parseOptions(rawOptions);
const configPath = required(options, "--config");
const statePath = required(options, "--state");

switch (command) {
  case "install":
    install(configPath, statePath, required(options, "--node"), required(options, "--server"));
    break;
  case "uninstall":
    uninstall(configPath, statePath);
    break;
  case "status":
    status(configPath, statePath);
    break;
  default:
    usage();
    fail(`unknown local-agent config command: ${command}`, 2);
}
