#!/usr/bin/env node

import { spawn } from "node:child_process";
import { lstatSync, readFileSync } from "node:fs";
import { realpath } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import readline from "node:readline";

const SERVER_NAME = "remote-dev-local-agent";
const SERVER_VERSION = "1.1.0";
const SUPPORTED_PROTOCOL_VERSIONS = new Set([
  "2025-06-18",
  "2025-03-26",
  "2024-11-05",
]);
const DEFAULT_PROTOCOL_VERSION = "2025-06-18";
const MAX_CAPTURE_BYTES = 1024 * 1024;
const MAX_RECEIPT_BYTES = 64 * 1024;
const cwd = process.cwd();
const remoteDevCli = fileURLToPath(new URL("./remote-dev", import.meta.url));

const argvProperty = {
  type: "array",
  minItems: 1,
  items: { type: "string" },
  description: "Command argument vector. The first item is the executable.",
};

const runnerExecInputSchema = {
  type: "object",
  properties: {
    argv: argvProperty,
    timeout_seconds: {
      type: "integer",
      minimum: 1,
      maximum: 2147483647,
      description: "Optional remote command timeout in seconds.",
    },
  },
  required: ["argv"],
  additionalProperties: false,
};

const runnerStartInputSchema = {
  type: "object",
  properties: { argv: argvProperty },
  required: ["argv"],
  additionalProperties: false,
};

const emptyInputSchema = {
  type: "object",
  properties: {},
  additionalProperties: false,
};

const processHandleInputSchema = {
  type: "object",
  properties: {
    handle: {
      type: "string",
      pattern: "^proc-[0-9]{8}T[0-9]{6}-[0-9]+-[0-9]+$",
      description: "Managed process handle returned by runner_start.",
    },
  },
  required: ["handle"],
  additionalProperties: false,
};

const runnerResultOutputSchema = {
  type: "object",
  properties: {
    stdout: { type: "string" },
    stderr: { type: "string" },
    stdoutTruncated: { type: "boolean" },
    stderrTruncated: { type: "boolean" },
    exitCode: { anyOf: [{ type: "integer" }, { type: "null" }] },
    signal: { anyOf: [{ type: "string" }, { type: "null" }] },
    timedOut: { type: "boolean" },
    receipt: {
      anyOf: [{ type: "object" }, { type: "string" }, { type: "null" }],
    },
  },
  required: [
    "stdout",
    "stderr",
    "stdoutTruncated",
    "stderrTruncated",
    "exitCode",
    "signal",
    "timedOut",
    "receipt",
  ],
  additionalProperties: false,
};

const captureDescription =
  "This stdio MCP call does not stream child output: it returns one result after the core command completes, " +
  "with stdout and stderr each capped at 1 MiB and explicit truncation flags.";

const runnerTools = [
  {
    name: "runner_exec",
    title: "Execute on the bound Remote Dev runner",
    description:
      "Execute one terminating argument vector in the dedicated runner bound to the current local context. " +
      "The trusted binding selects the single-repo or multi-repo member and remote working directory. " + captureDescription,
    inputSchema: runnerExecInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
  {
    name: "runner_status",
    title: "Inspect the bound Remote Dev runner",
    description:
      "Return synchronization, topology, lease, and managed-process status for the runner bound to the current local context. " +
      captureDescription,
    inputSchema: emptyInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
  {
    name: "runner_resume",
    title: "Resume the bound Remote Dev runner",
    description:
      "Explicitly start only the workspace selected by the trusted Local Agent binding, re-verify its immutable runner role, " +
      "and restore its lease, synchronization barrier, and watcher. Use after runner_status returns WORKSPACE_STOPPED, then retry status. " +
      "This does not accept routing arguments, recover drift or unknown execution, or restart managed processes. " + captureDescription,
    inputSchema: emptyInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
  {
    name: "runner_start",
    title: "Start a managed process on the bound runner",
    description:
      "Start one long-running argument vector on the bound runner and return its stable process handle. " +
      "Use runner_logs, runner_restart, and runner_stop with that handle. " + captureDescription,
    inputSchema: runnerStartInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
  {
    name: "runner_logs",
    title: "Read a managed runner process log",
    description:
      "Return the current bounded log snapshot for one managed process handle; this is not a follow or streaming call. " +
      captureDescription,
    inputSchema: processHandleInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
  {
    name: "runner_stop",
    title: "Stop a managed runner process",
    description:
      "Stop the exact bridge-owned managed process identified by its handle on the bound runner. " +
      captureDescription,
    inputSchema: processHandleInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
  {
    name: "runner_restart",
    title: "Restart a managed runner process",
    description:
      "Restart the exact bridge-owned managed process from its stored argument vector on the bound runner. " +
      captureDescription,
    inputSchema: processHandleInputSchema,
    outputSchema: runnerResultOutputSchema,
  },
];

const toolsByName = new Map(runnerTools.map((tool) => [tool.name, tool]));

async function resolveTrustedContext() {
  return realpath(cwd);
}

const trustedContextState = await resolveTrustedContext().then(
  (context) => ({ context, error: null }),
  (error) => ({
    context: null,
    error: error instanceof Error ? error.message : String(error),
  }),
);

function initializeInstructions() {
  if (trustedContextState.context) {
    return [
      `MCP cwd: ${cwd}`,
      `Trusted local context: ${trustedContextState.context}`,
      "Routing contract: local tools own source reading and editing, Git, skills, MCP/configuration, credentials, and other Mac-owned agent work.",
      "Dependency installation, build, lint, typecheck, test, run, debug, and Docker workloads must use the context-bound runner tools.",
      "Use runner_exec for terminating commands. Use runner_start for a long-running process, then runner_logs, runner_restart, or runner_stop with its returned handle.",
      "Call runner_status before the first workload. If it returns WORKSPACE_STOPPED, call runner_resume once, call runner_status again, then retry the workload only when health is ready.",
      "runner_resume starts only the workspace selected by the trusted binding; it does not accept routing fields, recover drift or unknown execution, or restart managed processes.",
      "Every runner tool resolves the active binding, working-set member, and remote cwd from the MCP startup context; the model cannot select them.",
      "Runner tool output is not streamed through MCP: each call returns one bounded result after the core command completes, with stdout and stderr capped at 1 MiB and explicit truncation flags.",
    ].join("\n");
  }
  return [
    `MCP cwd: ${cwd}`,
    "Trusted local context: unavailable",
    `Recovery: start this MCP server from an existing directory inside the intended Local Agent binding (${trustedContextState.error}).`,
  ].join("\n");
}

function negotiateProtocolVersion(requested) {
  return SUPPORTED_PROTOCOL_VERSIONS.has(requested)
    ? requested
    : DEFAULT_PROTOCOL_VERSION;
}

function isRequest(message) {
  return Object.prototype.hasOwnProperty.call(message, "id");
}

function send(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function sendResult(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function sendError(id, code, message, data) {
  const error = { code, message };
  if (data !== undefined) {
    error.data = data;
  }
  send({ jsonrpc: "2.0", id, error });
}

function validateArgumentObject(args, allowedKeys) {
  if (args === null || typeof args !== "object" || Array.isArray(args)) {
    return "arguments must be an object";
  }
  const unexpected = Object.keys(args).filter((key) => !allowedKeys.includes(key));
  if (unexpected.length > 0) {
    return `unsupported argument field: ${unexpected[0]}`;
  }
  return null;
}

function validateArgvArguments(args, allowTimeout) {
  const allowedKeys = allowTimeout ? ["argv", "timeout_seconds"] : ["argv"];
  const objectError = validateArgumentObject(args, allowedKeys);
  if (objectError) return objectError;
  if (!Array.isArray(args.argv) || args.argv.length === 0) {
    return "argv must be a non-empty array";
  }
  if (args.argv.some((value) => typeof value !== "string" || value.includes("\0"))) {
    return "every argv item must be a string without NUL bytes";
  }
  if (
    allowTimeout &&
    args.timeout_seconds !== undefined &&
    (!Number.isSafeInteger(args.timeout_seconds) ||
      args.timeout_seconds < 1 ||
      args.timeout_seconds > 2147483647)
  ) {
    return "timeout_seconds must be an integer from 1 through 2147483647";
  }
  return null;
}

function validateHandleArguments(args) {
  const objectError = validateArgumentObject(args, ["handle"]);
  if (objectError) return objectError;
  if (
    typeof args.handle !== "string" ||
    !/^proc-[0-9]{8}T[0-9]{6}-[0-9]+-[0-9]+$/u.test(args.handle)
  ) {
    return "handle must be a managed process handle returned by runner_start";
  }
  return null;
}

function validateToolArguments(toolName, args) {
  switch (toolName) {
    case "runner_exec":
      return validateArgvArguments(args, true);
    case "runner_start":
      return validateArgvArguments(args, false);
    case "runner_status":
    case "runner_resume":
      return validateArgumentObject(args, []);
    case "runner_logs":
    case "runner_stop":
    case "runner_restart":
      return validateHandleArguments(args);
    default:
      return "unknown tool";
  }
}

function readReceipt(pathText) {
  const lines = pathText.split(/\r?\n/u).filter(Boolean);
  if (lines.length !== 1 || !path.isAbsolute(lines[0]) || lines[0].includes("\0")) {
    return null;
  }
  const receiptPath = lines[0];
  let metadata;
  try {
    metadata = lstatSync(receiptPath);
  } catch {
    return null;
  }
  if (!metadata.isFile() || metadata.isSymbolicLink()) return null;
  const fields = new Map();
  for (const line of readFileSync(receiptPath, "utf8").split(/\r?\n/u)) {
    if (!line) continue;
    const separator = line.indexOf("\t");
    if (separator <= 0) return null;
    const key = line.slice(0, separator);
    if (!fields.has(key)) fields.set(key, line.slice(separator + 1));
  }
  if (fields.get("schema") !== "1" || fields.get("kind") !== "exec") return null;
  return {
    path: receiptPath,
    id: fields.get("receipt_id") ?? null,
    manifestId: fields.get("manifest_id") ?? null,
    result: fields.get("result") ?? null,
    timedOut: fields.get("timed_out") === "true",
    signal: fields.get("signal") && fields.get("signal") !== "-" ? fields.get("signal") : null,
  };
}

function cappedCapture(maxBytes) {
  const chunks = [];
  let bytes = 0;
  let truncated = false;
  return {
    add(chunk) {
      const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      const available = maxBytes - bytes;
      if (available > 0) {
        const captured = value.length <= available ? value : value.subarray(0, available);
        chunks.push(captured);
        bytes += captured.length;
      }
      if (value.length > available) truncated = true;
    },
    text() {
      return Buffer.concat(chunks, bytes).toString("utf8");
    },
    get truncated() {
      return truncated;
    },
  };
}

function coreArguments(toolName, args) {
  const base = ["local-agent", toolName.slice("runner_".length), "--for-context", trustedContextState.context];
  switch (toolName) {
    case "runner_exec":
      if (args.timeout_seconds !== undefined) {
        base.push("--timeout-seconds", String(args.timeout_seconds));
      }
      base.push("--receipt-fd", "3", "--", ...args.argv);
      return { cliArgs: base, receiptFd: true };
    case "runner_start":
      base.push("--", ...args.argv);
      return { cliArgs: base, receiptFd: false };
    case "runner_logs":
    case "runner_stop":
    case "runner_restart":
      base.push(args.handle);
      return { cliArgs: base, receiptFd: false };
    case "runner_status":
    case "runner_resume":
      return { cliArgs: base, receiptFd: false };
    default:
      throw new Error(`unsupported runner tool: ${toolName}`);
  }
}

function executeRunnerTool(toolName, args) {
  const { cliArgs, receiptFd } = coreArguments(toolName, args);
  return new Promise((resolve) => {
    const stdoutCapture = cappedCapture(MAX_CAPTURE_BYTES);
    const stderrCapture = cappedCapture(MAX_CAPTURE_BYTES);
    const receiptCapture = cappedCapture(MAX_RECEIPT_BYTES);
    let spawnError = null;
    const child = spawn(remoteDevCli, cliArgs, {
      cwd: trustedContextState.context,
      env: process.env,
      shell: false,
      stdio: receiptFd ? ["ignore", "pipe", "pipe", "pipe"] : ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    child.stdout.on("data", (chunk) => stdoutCapture.add(chunk));
    child.stderr.on("data", (chunk) => stderrCapture.add(chunk));
    if (receiptFd) child.stdio[3].on("data", (chunk) => receiptCapture.add(chunk));
    child.on("error", (error) => {
      spawnError = error;
      stderrCapture.add(`${error.message}\n`);
    });
    child.on("close", (code, processSignal) => {
      const receipt = receiptFd && !receiptCapture.truncated
        ? readReceipt(receiptCapture.text())
        : null;
      resolve({
        stdout: stdoutCapture.text(),
        stderr: stderrCapture.text(),
        stdoutTruncated: stdoutCapture.truncated,
        stderrTruncated: stderrCapture.truncated,
        exitCode: !spawnError && Number.isInteger(code) ? code : null,
        signal: processSignal ?? receipt?.signal ?? null,
        timedOut: receipt?.timedOut ?? false,
        receipt,
      });
    });
  });
}

function toolResult(structuredContent, isError) {
  return {
    content: [{ type: "text", text: JSON.stringify(structuredContent) }],
    structuredContent,
    isError,
  };
}

function unavailableRootResult() {
  return {
    stdout: "",
    stderr:
      "TRUSTED_CONTEXT_UNAVAILABLE: start the Local Agent MCP server from an existing directory inside the intended binding.\n" +
      `${trustedContextState.error}\n`,
    stdoutTruncated: false,
    stderrTruncated: false,
    exitCode: null,
    signal: null,
    timedOut: false,
    receipt: null,
  };
}

let initialized = false;
let toolQueue = Promise.resolve();

async function handleRequest(message) {
  const id = message.id;
  const method = message.method;

  if (method === "initialize") {
    const requestedVersion = message.params?.protocolVersion;
    if (typeof requestedVersion !== "string") {
      sendError(id, -32602, "initialize requires params.protocolVersion");
      return;
    }
    initialized = true;
    sendResult(id, {
      protocolVersion: negotiateProtocolVersion(requestedVersion),
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
      instructions: initializeInstructions(),
    });
    return;
  }

  if (method === "ping") {
    sendResult(id, {});
    return;
  }

  if (!initialized) {
    sendError(id, -32002, "server is not initialized");
    return;
  }

  if (method === "tools/list") {
    sendResult(id, { tools: runnerTools });
    return;
  }

  if (method === "tools/call") {
    const toolName = message.params?.name;
    if (!toolsByName.has(toolName)) {
      sendError(id, -32602, "unknown tool", { name: message.params?.name ?? null });
      return;
    }
    const args = message.params?.arguments ?? {};
    const validationError = validateToolArguments(toolName, args);
    if (validationError) {
      sendError(id, -32602, validationError);
      return;
    }
    if (!trustedContextState.context) {
      const result = unavailableRootResult();
      sendResult(id, toolResult(result, true));
      return;
    }

    const run = toolQueue.then(() => executeRunnerTool(toolName, args));
    toolQueue = run.catch(() => undefined);
    const result = await run;
    sendResult(
      id,
      toolResult(
        result,
        result.exitCode !== 0 || result.signal !== null || result.timedOut,
      ),
    );
    return;
  }

  sendError(id, -32601, "method not found", { method });
}

async function handleMessage(line) {
  if (!line.trim()) {
    return;
  }
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    sendError(null, -32700, "parse error");
    return;
  }
  if (
    message === null ||
    typeof message !== "object" ||
    Array.isArray(message) ||
    message.jsonrpc !== "2.0" ||
    typeof message.method !== "string"
  ) {
    sendError(null, -32600, "invalid request");
    return;
  }
  if (!isRequest(message)) {
    return;
  }
  await handleRequest(message);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  void handleMessage(line).catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${SERVER_NAME}: ${message}\n`);
  });
});
