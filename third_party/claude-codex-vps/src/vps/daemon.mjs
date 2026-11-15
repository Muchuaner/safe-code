#!/usr/bin/env node
// VPS-side minimal component ("daemon"). One per VPS user, started on demand by connect.mjs.
//
// What it does, and nothing more:
//   1. Starts the unmodified `claude` CLI with arguments it builds itself from a validated spec
//      (never bypass permissions, built-in tools limited to the policy, --strict-mcp-config).
//   2. Relays Claude Code's stdout lines to the Windows adapter and the user message to its stdin.
//   3. Relays Claude's MCP tool calls to the Windows adapter and the results back (mcpRelay.mjs).
//   4. Keeps a content-addressed in-memory blob cache (images/PDFs), verified by SHA-256.
//   5. Buffers each turn's events with sequence numbers, so an SSH reconnect resumes the same
//      Claude process instead of starting a second model call.
//
// It does NOT understand Codex's protocol, does not convert conversations, does not decide which
// Windows tools exist, and never reads Claude credentials (Claude Code reads its own login).
// It strips SSH_* variables (they contain the client's address) from its own and Claude's env.
// The log holds lifecycle metadata only: no prompts, no outputs, no addresses, no environment.
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import zlib from 'node:zlib';
import { spawn, execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { FrameDecoder, FrameWriter, PROTOCOL_VERSION } from '../shared/frame.mjs';
import { createProcessStopper, closeStdioAfterExit } from './processControl.mjs';

export const SENSITIVE_ENV = /^(SSH_|SSH2_|DISPLAY$|XAUTHORITY$|WINDOWID$|TERM_SESSION_ID$|REMOTEHOST$|SUDO_)/;

/** Removes variables that can carry the SSH client's address or session identity. */
export function sanitizedEnv(env = process.env) {
  const out = {};
  for (const [key, value] of Object.entries(env)) if (!SENSITIVE_ENV.test(key)) out[key] = value;
  return out;
}

export const STATE_HOME = process.env.CODEX_CLAUDE_WIN_HOME || path.join(os.homedir(), '.codex-claude-win');

export const DEFAULT_POLICY = {
  claudePath: 'claude',
  // Claude Code's own (VPS-side) tools a client may request. "default" (Bash/Read/Edit on the VPS) is
  // deliberately absent: a VPS shell can read sshd logs or `who`, which show the client's address.
  allowedNativeTools: ['none', 'web'],
  maxBlobCacheBytes: 256 * 1024 * 1024,
  maxMessageBytes: 30 * 1024 * 1024,
  detachGraceMs: 10 * 60 * 1000, // keep a disconnected client's Claude turns alive this long
  retainFinishedMs: 10 * 60 * 1000, // finished turns' events, for a reconnect that missed the end
  idleExitMs: 30 * 60 * 1000,
  maxConcurrentTurns: 16,
  claudeStopGraceMs: 4000,
  toolTimeoutMs: 24 * 60 * 60 * 1000,
};

export function loadPolicy(home = STATE_HOME) {
  let user = {};
  try {
    user = JSON.parse(fs.readFileSync(path.join(home, 'policy.json'), 'utf8'));
  } catch (err) {
    if (err.code !== 'ENOENT') throw new Error(`policy.json unreadable: ${err.message}`);
  }
  const policy = { ...DEFAULT_POLICY, ...user };
  for (const t of policy.allowedNativeTools) if (!['none', 'web', 'default'].includes(t)) throw new Error(`unknown native tool set ${t}`);
  if (policy.allowedNativeTools.includes('default') && user.iUnderstandVpsShellCanSeeClientAddress !== true) {
    throw new Error('allowedNativeTools "default" needs "iUnderstandVpsShellCanSeeClientAddress": true in policy.json');
  }
  return policy;
}

const MODEL_RE = /^(?:claude-[a-z0-9]+(?:-[a-z0-9]+)*|fable|opus|sonnet|haiku)(?:\[1m\])?$/;
const SID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/;
const ID_RE = /^[A-Za-z0-9_-]{8,80}$/;
const HASH_RE = /^[a-f0-9]{64}$/;
const EFFORTS = new Set(['low', 'medium', 'high', 'xhigh', 'max']);
const TOOL_NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;

class SpecError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.code = code;
    Object.assign(this, extra);
  }
}

/** Validates a turn spec from the Windows adapter. Throws SpecError. */
export function validateSpec(spec, policy) {
  if (!spec || typeof spec !== 'object') throw new SpecError('bad_spec', 'spec missing');
  const kind = spec.kind ?? 'turn';
  if (!['turn', 'compact'].includes(kind)) throw new SpecError('bad_spec', 'unknown kind');
  if (typeof spec.model !== 'string' || spec.model.length > 128 || !MODEL_RE.test(spec.model)) {
    if (kind === 'turn') throw new SpecError('bad_model', 'model must be a Claude model identifier');
  }
  if (spec.effort != null && !EFFORTS.has(spec.effort)) throw new SpecError('bad_spec', 'bad effort');
  const permissionMode = spec.permissionMode ?? 'dontAsk';
  if (!['dontAsk', 'plan'].includes(permissionMode)) throw new SpecError('bad_spec', 'permission mode must be dontAsk or plan');
  const nativeTools = spec.nativeTools ?? 'none';
  if (!policy.allowedNativeTools.includes(nativeTools)) throw new SpecError('native_tools_refused', `native tool set "${nativeTools}" is not allowed by the VPS policy`);
  if (spec.resume != null && (typeof spec.resume !== 'string' || !SID_RE.test(spec.resume))) throw new SpecError('bad_spec', 'bad resume id');
  if (kind === 'compact' && !spec.resume) throw new SpecError('bad_spec', 'compact needs a session');
  if (typeof spec.workspaceKey !== 'string' || !/^[a-f0-9]{16,64}$/.test(spec.workspaceKey)) throw new SpecError('bad_spec', 'bad workspace key');
  if (spec.systemPrompt != null && (typeof spec.systemPrompt !== 'string' || spec.systemPrompt.length > 512 * 1024)) throw new SpecError('bad_spec', 'system prompt too long');
  const tools = Array.isArray(spec.tools) ? spec.tools : [];
  if (tools.length > 128) throw new SpecError('bad_spec', 'too many tools');
  for (const t of tools) {
    if (!t || !TOOL_NAME_RE.test(t.name) || typeof t.description !== 'string' || t.description.length > 128 * 1024 || typeof t.inputSchema !== 'object' || !t.inputSchema) {
      throw new SpecError('bad_spec', 'bad tool definition');
    }
  }
  if (kind === 'turn' && (!spec.userMessage || typeof spec.userMessage !== 'object' || !Array.isArray(spec.userMessage.message?.content))) throw new SpecError('bad_spec', 'user message missing');
  return { kind, permissionMode, nativeTools, tools };
}

export function nativeToolArgs(nativeTools) {
  if (nativeTools === 'none') return ['--tools', ''];
  if (nativeTools === 'web') return ['--tools', 'WebSearch,WebFetch'];
  return [];
}

/** Claude Code arguments for a validated spec. The client never supplies raw arguments. */
export function buildClaudeArgs(spec, v, caps, mcpConfig) {
  if (v.kind === 'compact') {
    return ['-p', '--resume', spec.resume, ...(spec.fork ? ['--fork-session'] : []), '--permission-mode', 'dontAsk', ...nativeToolArgs(v.nativeTools), '--strict-mcp-config', '--output-format', 'json', '/compact'];
  }
  const args = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--model', spec.model, '--permission-mode', v.permissionMode, ...nativeToolArgs(v.nativeTools), '--strict-mcp-config'];
  if (caps.partial) args.push('--include-partial-messages');
  if (caps.permissionPrompts) args.push('--permission-prompts', 'none');
  if (caps.effort && spec.effort) args.push('--effort', spec.effort);
  if (spec.resume) args.push('--resume', spec.resume);
  if (spec.resume && spec.fork) args.push('--fork-session');
  if (spec.disallowAskUserQuestion) args.push('--disallowedTools', 'AskUserQuestion');
  if (spec.systemPrompt) args.push('--append-system-prompt', spec.systemPrompt);
  const allowed = [];
  if (v.nativeTools === 'web') allowed.push('WebSearch', 'WebFetch');
  if (v.tools.length && v.permissionMode !== 'plan') allowed.push('mcp__codex');
  if (allowed.length) args.push('--allowedTools', allowed.join(','));
  if (mcpConfig) args.push('--mcp-config', JSON.stringify(mcpConfig));
  return args;
}

/** Content-addressed blob cache with LRU eviction; pinned blobs are never evicted. */
export class BlobStore {
  constructor(limit) {
    this.limit = limit;
    this.bytes = 0;
    this.map = new Map(); // hash -> { buf, pins }
    this.uploads = new Map(); // hash -> { size, parts, received, owner }
    this.stats = { stored: 0, hits: 0, evicted: 0, rejected: 0 };
  }

  has(hash) {
    const e = this.map.get(hash);
    if (!e) return false;
    this.map.delete(hash);
    this.map.set(hash, e); // recency
    return true;
  }

  get(hash) {
    return this.has(hash) ? this.map.get(hash).buf : null;
  }

  pin(hash) {
    const e = this.map.get(hash);
    if (e) e.pins++;
    return Boolean(e);
  }

  unpin(hash) {
    const e = this.map.get(hash);
    if (e && e.pins > 0) e.pins--;
    this.evict();
  }

  put(hash, buf) {
    if (this.map.has(hash)) return true;
    if (buf.length > this.limit) return false;
    this.map.set(hash, { buf, pins: 0 });
    this.bytes += buf.length;
    this.stats.stored++;
    this.evict();
    return true;
  }

  evict() {
    for (const [hash, e] of this.map) {
      if (this.bytes <= this.limit) break;
      if (e.pins > 0) continue;
      this.map.delete(hash);
      this.bytes -= e.buf.length;
      this.stats.evicted++;
    }
  }

  /** Receives one chunk. Returns { done, ok, reason }. */
  chunk(owner, header, body) {
    const { hash, size, offset } = header;
    if (!HASH_RE.test(hash) || !Number.isSafeInteger(size) || size < 0 || size > this.limit) return { done: true, ok: false, reason: 'bad_blob_header' };
    if (this.map.has(hash)) {
      this.uploads.delete(hash);
      return header.last ? { done: true, ok: true } : { done: false };
    }
    let up = this.uploads.get(hash);
    if (!up || offset === 0) {
      up = { size, parts: [], received: 0, owner };
      this.uploads.set(hash, up);
    }
    if (offset !== up.received || up.size !== size) {
      this.uploads.delete(hash);
      return { done: true, ok: false, reason: 'out_of_order' };
    }
    if (body) {
      up.parts.push(body);
      up.received += body.length;
    }
    if (up.received > size) {
      this.uploads.delete(hash);
      return { done: true, ok: false, reason: 'too_long' };
    }
    if (!header.last) return { done: false };
    this.uploads.delete(hash);
    const buf = Buffer.concat(up.parts, up.received);
    if (buf.length !== size || crypto.createHash('sha256').update(buf).digest('hex') !== hash) {
      this.stats.rejected++;
      return { done: true, ok: false, reason: 'hash_mismatch' };
    }
    return { done: true, ok: this.put(hash, buf), reason: 'cache_full' };
  }

  dropUploadsOf(owner) {
    for (const [hash, up] of this.uploads) if (up.owner === owner) this.uploads.delete(hash);
  }
}

/** Replaces blob references with base64 data. Returns { message, missing, pinned }. */
export function resolveBlobs(message, blobs) {
  const missing = new Set();
  const pinned = [];
  const visit = (block) => {
    if (!block || typeof block !== 'object') return block;
    if (Array.isArray(block)) return block.map(visit);
    if ((block.type === 'image' || block.type === 'document') && block.source?.type === 'blob') {
      const buf = blobs.get(block.source.hash);
      if (!buf) {
        missing.add(block.source.hash);
        return block;
      }
      blobs.pin(block.source.hash);
      pinned.push(block.source.hash);
      return { ...block, source: { type: 'base64', media_type: block.source.media_type, data: buf.toString('base64') } };
    }
    const out = {};
    for (const [k, v] of Object.entries(block)) out[k] = typeof v === 'object' ? visit(v) : v;
    return out;
  };
  const resolved = visit(message);
  if (missing.size) for (const h of pinned) blobs.unpin(h);
  return { message: resolved, missing: [...missing], pinned };
}

/** Strips image/document bytes Claude Code echoes back in `user` events: the client never needs them. */
export function slimLine(line) {
  if (line.length < 4096 || !line.includes('"type":"user"')) return line;
  try {
    const ev = JSON.parse(line);
    if (ev.type !== 'user') return line;
    const strip = (v) => {
      if (Array.isArray(v)) return v.map(strip);
      if (!v || typeof v !== 'object') return v;
      if ((v.type === 'image' || v.type === 'document') && v.source?.data) return { ...v, source: { ...v.source, data: '[omitted]' } };
      if (v.type === 'image' && typeof v.data === 'string') return { ...v, data: '[omitted]' };
      const o = {};
      for (const [k, x] of Object.entries(v)) o[k] = strip(x);
      return o;
    };
    return JSON.stringify(strip(ev));
  } catch {
    return line;
  }
}

/** Inverse of the client's packJson; bounded so a bad frame cannot exhaust memory. */
export function unpackJson(body) {
  if (!body) return null;
  try {
    return JSON.parse(zlib.gunzipSync(body, { maxOutputLength: 64 * 1024 * 1024 }).toString('utf8'));
  } catch {
    return null;
  }
}

export async function claudeCapabilities(claudePath, env) {
  const help = await new Promise((resolve) => {
    execFile(claudePath, ['--help'], { timeout: 20000, maxBuffer: 4 * 1024 * 1024, env }, (err, stdout, stderr) => resolve(err ? '' : `${stdout}\n${stderr}`));
  });
  return {
    ok: help.length > 0,
    tools: help.includes('--tools'),
    strictMcp: help.includes('--strict-mcp-config'),
    partial: help.includes('--include-partial-messages'),
    permissionPrompts: help.includes('--permission-prompts'),
    effort: help.includes('--effort'),
  };
}

function makeLog(file) {
  let stream = null;
  if (file) {
    try {
      if (fs.statSync(file).size > 5 * 1024 * 1024) fs.renameSync(file, `${file}.1`);
    } catch {
      // first run
    }
    stream = fs.createWriteStream(file, { flags: 'a', mode: 0o600 });
  }
  return (event, fields = {}) => {
    // Metadata only. Callers never pass content, addresses, or environment values.
    const line = `${new Date().toISOString()} ${event} ${Object.entries(fields).map(([k, v]) => `${k}=${v}`).join(' ')}\n`;
    if (stream) stream.write(line);
  };
}

export class Daemon {
  constructor({ home = STATE_HOME, policy = loadPolicy(home), log = null, now = () => Date.now() } = {}) {
    this.home = home;
    this.policy = policy;
    this.now = now;
    this.instanceId = crypto.randomBytes(12).toString('hex');
    this.blobs = new BlobStore(policy.maxBlobCacheBytes);
    this.clients = new Map(); // clientId -> { conn, detachTimer }
    this.turns = new Map(); // turnId -> turn
    this.mcpByToken = new Map(); // turn token -> turn
    this.env = sanitizedEnv();
    this.log = log || makeLog(path.join(home, 'daemon.log'));
    this.caps = null;
    this.counters = { connections: 0, turnsStarted: 0, turnsDeduplicated: 0, toolCalls: 0, duplicateToolResults: 0, blobBytesReceived: 0, refusedSpecs: 0 };
    this.workspaceRoot = path.join(home, 'workspaces');
    this.relayPath = fileURLToPath(new URL('./mcpRelay.mjs', import.meta.url));
  }

  async start(socketPath = path.join(this.home, 'run', 'daemon.sock')) {
    for (const k of Object.keys(process.env)) if (SENSITIVE_ENV.test(k)) delete process.env[k];
    fs.mkdirSync(path.dirname(socketPath), { recursive: true, mode: 0o700 });
    fs.chmodSync(path.dirname(socketPath), 0o700);
    fs.mkdirSync(this.workspaceRoot, { recursive: true, mode: 0o700 });
    this.socketPath = socketPath;
    this.caps = await claudeCapabilities(this.policy.claudePath, this.env);
    this.server = net.createServer((sock) => this.accept(sock));
    await new Promise((resolve, reject) => {
      this.server.once('error', reject);
      this.server.listen(socketPath, () => {
        fs.chmodSync(socketPath, 0o600);
        resolve();
      });
    });
    this.pidFile = path.join(path.dirname(socketPath), 'daemon.pid');
    fs.writeFileSync(this.pidFile, String(process.pid), { mode: 0o600 });
    this.idleTimer = setInterval(() => this.checkIdle(), 60 * 1000);
    this.idleTimer.unref();
    this.lastActive = this.now();
    this.log('start', { instance: this.instanceId, claude: this.caps.ok ? 'ok' : 'missing' });
    return this;
  }

  checkIdle() {
    const active = [...this.clients.values()].some((c) => c.conn) || [...this.turns.values()].some((t) => !t.exited);
    if (active) this.lastActive = this.now();
    else if (this.now() - this.lastActive > this.policy.idleExitMs) this.stop('idle');
  }

  stop(reason = 'stop') {
    this.log('stop', { reason });
    clearInterval(this.idleTimer);
    for (const t of this.turns.values()) if (!t.exited) t.stopper?.stop();
    this.server?.close();
    for (const f of [this.socketPath, this.pidFile]) {
      try {
        if (f) fs.unlinkSync(f);
      } catch {
        // already gone
      }
    }
    this.onStop?.();
  }

  accept(sock) {
    const decoder = new FrameDecoder();
    const writer = new FrameWriter(sock);
    const conn = { sock, writer, role: null, clientId: null, turn: null, id: crypto.randomBytes(6).toString('hex') };
    sock.on('data', (d) => decoder.push(d));
    sock.on('error', () => {});
    decoder.on('error', () => sock.destroy());
    decoder.on('frame', ({ header, body }) => {
      try {
        this.onFrame(conn, header, body);
      } catch (err) {
        this.log('frame-error', { op: String(header?.op).slice(0, 32), err: String(err.code || 'internal') });
      }
    });
    sock.on('close', () => this.onClose(conn));
  }

  send(conn, header, body) {
    if (!conn || conn.writer.closed) return;
    conn.writer.send(header, body).catch(() => {});
  }

  onFrame(conn, h, body) {
    if (!conn.role) {
      if (h.op === 'hello' && h.role === 'mcp') return this.helloMcp(conn, h);
      if (h.op === 'hello' && (h.role ?? 'client') === 'client') return this.helloClient(conn, h);
      conn.sock.destroy();
      return;
    }
    if (conn.role === 'mcp') return this.onMcp(conn, h);
    switch (h.op) {
      case 'ping':
        return this.send(conn, { op: 'pong', id: h.id });
      case 'blob.query':
        return this.send(conn, { op: 'blob.status', id: h.id, missing: (h.hashes || []).filter((x) => HASH_RE.test(x) && !this.blobs.has(x)) });
      case 'blob.put': {
        if (body) this.counters.blobBytesReceived += body.length;
        const r = this.blobs.chunk(conn.id, h, body);
        if (r.done) this.send(conn, { op: r.ok ? 'blob.ok' : 'blob.err', hash: h.hash, reason: r.ok ? undefined : r.reason });
        return;
      }
      case 'turn.start':
        if (h.specEncoding === 'gzip') h.spec = unpackJson(body);
        return this.startTurn(conn, h);
      case 'turn.toolresult':
        if (h.contentEncoding === 'gzip') h.content = unpackJson(body);
        return this.toolResult(conn, h);
      case 'turn.cancel':
        return this.cancelTurn(conn, h);
      case 'status':
        return this.send(conn, { op: 'status', id: h.id, status: this.status() });
      default:
        return this.send(conn, { op: 'error', id: h.id, code: 'unknown_op' });
    }
  }

  status() {
    return {
      instanceId: this.instanceId,
      clients: [...this.clients.values()].filter((c) => c.conn).length,
      runningTurns: [...this.turns.values()].filter((t) => !t.exited).length,
      blobCache: { entries: this.blobs.map.size, bytes: this.blobs.bytes, limit: this.blobs.limit, ...this.blobs.stats },
      counters: this.counters,
      policy: { allowedNativeTools: this.policy.allowedNativeTools },
    };
  }

  helloClient(conn, h) {
    if (h.v !== PROTOCOL_VERSION) {
      this.send(conn, { op: 'hello.err', code: 'protocol_version', want: PROTOCOL_VERSION });
      conn.sock.end();
      return;
    }
    if (typeof h.clientId !== 'string' || !ID_RE.test(h.clientId)) {
      this.send(conn, { op: 'hello.err', code: 'bad_client_id' });
      conn.sock.end();
      return;
    }
    conn.role = 'client';
    conn.clientId = h.clientId;
    this.counters.connections++;
    let client = this.clients.get(h.clientId);
    if (!client) {
      client = { conn: null, detachTimer: null };
      this.clients.set(h.clientId, client);
    }
    if (client.conn && client.conn !== conn) client.conn.sock.destroy(); // a stale connection the client already gave up on
    client.conn = conn;
    clearTimeout(client.detachTimer);
    this.log('client-attach', { client: h.clientId.slice(0, 8) });
    const turns = [...this.turns.values()].filter((t) => t.clientId === h.clientId).map((t) => ({ turnId: t.turnId, lastSeq: t.seq, exited: t.exited }));
    this.send(conn, { op: 'hello.ok', instanceId: this.instanceId, caps: this.caps, policy: { allowedNativeTools: this.policy.allowedNativeTools, maxBlobCacheBytes: this.policy.maxBlobCacheBytes }, turns });
    // Replay what the client missed, in order.
    for (const r of Array.isArray(h.resume) ? h.resume : []) {
      const t = this.turns.get(r.turnId);
      if (!t || t.clientId !== h.clientId) {
        this.send(conn, { op: 'turn.lost', turnId: r.turnId });
        continue;
      }
      for (const ev of t.events) if (ev.seq > (Number(r.after) || 0)) this.send(conn, ev);
    }
  }

  onClose(conn) {
    if (conn.role === 'mcp') {
      if (conn.turn) conn.turn.mcpConns.delete(conn);
      return;
    }
    if (conn.role !== 'client') return;
    this.blobs.dropUploadsOf(conn.id);
    const client = this.clients.get(conn.clientId);
    if (!client || client.conn !== conn) return;
    client.conn = null;
    this.log('client-detach', { client: conn.clientId.slice(0, 8) });
    clearTimeout(client.detachTimer);
    client.detachTimer = setTimeout(() => {
      for (const t of this.turns.values()) {
        if (t.clientId === conn.clientId && !t.exited) {
          this.log('turn-stop', { turn: t.turnId, reason: 'client-gone' });
          t.stopper.stop();
        }
      }
    }, this.policy.detachGraceMs);
    client.detachTimer.unref?.();
  }

  emit(turn, header) {
    const ev = { ...header, turnId: turn.turnId, seq: ++turn.seq };
    turn.events.push(ev);
    const client = this.clients.get(turn.clientId);
    if (client?.conn) this.send(client.conn, ev);
  }

  workspaceFor(clientId, key) {
    const dir = path.join(this.workspaceRoot, crypto.createHash('sha256').update(clientId).digest('hex').slice(0, 12), key);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    return fs.realpathSync(dir);
  }

  startTurn(conn, h) {
    const { turnId, spec } = h;
    if (typeof turnId !== 'string' || !ID_RE.test(turnId)) return this.send(conn, { op: 'turn.error', turnId, code: 'bad_turn_id' });
    const existing = this.turns.get(turnId);
    if (existing) {
      // A resent start (reconnect race): never spawn twice. Replay instead.
      if (existing.clientId !== conn.clientId) return this.send(conn, { op: 'turn.error', turnId, code: 'turn_id_in_use' });
      this.counters.turnsDeduplicated++;
      for (const ev of existing.events) if (ev.seq > (Number(h.after) || 0)) this.send(conn, ev);
      return;
    }
    if (!this.caps?.ok) return this.send(conn, { op: 'turn.error', turnId, code: 'claude_unavailable', message: 'claude CLI is not usable on the VPS' });
    if (!this.caps.tools || !this.caps.strictMcp) return this.send(conn, { op: 'turn.error', turnId, code: 'claude_too_old', message: 'this Claude Code cannot restrict its tools (--tools/--strict-mcp-config); refusing to run' });
    if ([...this.turns.values()].filter((t) => !t.exited).length >= this.policy.maxConcurrentTurns) return this.send(conn, { op: 'turn.error', turnId, code: 'busy', message: 'too many concurrent Claude turns on the VPS' });
    let v;
    try {
      v = validateSpec(spec, this.policy);
    } catch (err) {
      this.counters.refusedSpecs++;
      this.log('spec-refused', { code: err.code || 'bad_spec' });
      return this.send(conn, { op: 'turn.error', turnId, code: err.code || 'bad_spec', message: err.message });
    }
    let stdinLine = null;
    let pinned = [];
    if (v.kind === 'turn') {
      const r = resolveBlobs(spec.userMessage, this.blobs);
      if (r.missing.length) return this.send(conn, { op: 'turn.error', turnId, code: 'missing_blobs', missing: r.missing });
      stdinLine = `${JSON.stringify(r.message)}\n`;
      pinned = r.pinned;
      if (stdinLine.length > this.policy.maxMessageBytes * 1.4) {
        for (const hsh of pinned) this.blobs.unpin(hsh);
        return this.send(conn, { op: 'turn.error', turnId, code: 'message_too_large', message: 'the message is larger than the VPS policy allows' });
      }
    }
    const token = crypto.randomBytes(18).toString('hex');
    const turn = { turnId, clientId: conn.clientId, token, kind: v.kind, spec, v, seq: 0, events: [], exited: false, pending: new Map(), answered: new Set(), mcpConns: new Set(), child: null, stopper: null };
    this.turns.set(turnId, turn);
    this.mcpByToken.set(token, turn);
    this.counters.turnsStarted++;

    let mcpConfig = null;
    const env = { ...this.env, CODEX_CLAUDE_WIN: '1' };
    if (v.kind === 'turn' && v.tools.length) {
      mcpConfig = { mcpServers: { codex: { command: process.execPath, args: [this.relayPath], env: { CCW_SOCKET: this.socketPath, CCW_TURN_TOKEN: token }, timeout: this.policy.toolTimeoutMs } } };
      env.MCP_TOOL_TIMEOUT = String(this.policy.toolTimeoutMs);
      env.CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH = String(Math.max(2048, ...v.tools.map((t) => t.description.length)));
    }
    const args = buildClaudeArgs(spec, v, this.caps, mcpConfig);
    const cwd = this.workspaceFor(conn.clientId, spec.workspaceKey);
    const child = spawn(this.policy.claudePath, args, { cwd, env, stdio: [v.kind === 'turn' ? 'pipe' : 'ignore', 'pipe', 'pipe'] });
    turn.child = child;
    turn.stopper = createProcessStopper(child, { graceMs: this.policy.claudeStopGraceMs, killGraceMs: this.policy.claudeStopGraceMs });
    closeStdioAfterExit(child, 5000);
    this.log('turn-spawn', { turn: turnId, kind: v.kind, pid: child.pid, native: v.nativeTools, tools: v.tools.length, resume: spec.resume ? 1 : 0, fork: spec.fork ? 1 : 0 });
    this.emit(turn, { op: 'turn.started', pid: Boolean(child.pid) });
    if (stdinLine) {
      child.stdin.on('error', () => {});
      child.stdin.end(stdinLine, () => {
        for (const hsh of pinned) this.blobs.unpin(hsh);
      });
    }
    let stderr = '';
    child.stderr.on('data', (d) => {
      stderr = (stderr + d).slice(-8000);
    });
    let stdoutAll = '';
    readline.createInterface({ input: child.stdout }).on('line', (line) => {
      if (!line.trim()) return;
      if (v.kind === 'compact') stdoutAll = (stdoutAll + line).slice(-1024 * 1024);
      else this.emit(turn, { op: 'turn.event', line: slimLine(line) });
    });
    let spawnError = null;
    child.on('error', (err) => {
      spawnError = err.code || 'spawn_error';
    });
    child.on('close', (code, signal) => {
      turn.exited = true;
      this.mcpByToken.delete(token);
      for (const c of turn.mcpConns) c.sock.destroy();
      for (const hsh of pinned) this.blobs.unpin(hsh);
      this.log('turn-exit', { turn: turnId, code, signal: signal || '-', reason: turn.stopReason || (code === 0 ? 'completed' : 'failed') });
      this.emit(turn, { op: 'turn.exit', code, signal, spawnError, stopReason: turn.stopReason || null, stderrTail: stderr.split('\n').slice(-6).join('\n'), stdout: v.kind === 'compact' ? stdoutAll : undefined });
      setTimeout(() => this.turns.delete(turnId), this.policy.retainFinishedMs).unref?.();
    });
  }

  cancelTurn(conn, h) {
    const t = this.turns.get(h.turnId);
    if (!t || t.clientId !== conn.clientId || t.exited) return;
    t.stopReason = String(h.reason || 'cancel').slice(0, 32);
    this.log('turn-stop', { turn: t.turnId, reason: t.stopReason });
    t.stopper.stop();
  }

  toolResult(conn, h) {
    const t = this.turns.get(h.turnId);
    if (!t || t.clientId !== conn.clientId) return;
    if (t.answered.has(h.callId)) {
      this.counters.duplicateToolResults++; // a retry after reconnect: Claude already has this result
      return;
    }
    const call = t.pending.get(h.callId);
    if (!call) return;
    // Blob references inside the result (images returned by a Windows tool).
    const content = [];
    const missing = [];
    for (const block of Array.isArray(h.content) ? h.content : []) {
      if (block?.type === 'image' && typeof block.blob === 'string') {
        const buf = this.blobs.get(block.blob);
        if (!buf) missing.push(block.blob);
        else content.push({ type: 'image', mimeType: block.mimeType, data: buf.toString('base64') });
      } else if (block?.type === 'text' && typeof block.text === 'string') {
        content.push({ type: 'text', text: block.text });
      }
    }
    if (missing.length) return this.send(conn, { op: 'turn.toolresult.missing', turnId: t.turnId, callId: h.callId, missing });
    t.pending.delete(h.callId);
    t.answered.add(h.callId);
    this.send(conn, { op: 'turn.toolresult.ok', turnId: t.turnId, callId: h.callId });
    const result = { content: content.length ? content : [{ type: 'text', text: '(no output)' }] };
    if (h.isError) result.isError = true;
    call.conn.writer.send({ op: 'mcp', msg: { jsonrpc: '2.0', id: call.rpcId, result } }).catch(() => {});
  }

  helloMcp(conn, h) {
    const turn = this.mcpByToken.get(h.token);
    if (!turn) {
      conn.sock.destroy();
      return;
    }
    conn.role = 'mcp';
    conn.turn = turn;
    turn.mcpConns.add(conn);
  }

  onMcp(conn, h) {
    if (h.op !== 'mcp' || !h.msg) return;
    const { id, method, params } = h.msg;
    const turn = conn.turn;
    const reply = (msg) => this.send(conn, { op: 'mcp', msg: { jsonrpc: '2.0', id, ...msg } });
    if (id === undefined || id === null) return;
    if (method === 'initialize') return reply({ result: { protocolVersion: params?.protocolVersion || '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'codex', version: '1' } } });
    if (method === 'ping') return reply({ result: {} });
    if (method === 'tools/list') return reply({ result: { tools: turn.v.tools } });
    if (method === 'tools/call') {
      const name = params?.name;
      if (!turn.v.tools.some((t) => t.name === name)) {
        return reply({ result: { content: [{ type: 'text', text: `No tool named ${String(name).slice(0, 64)} is available.` }], isError: true } });
      }
      const callId = `call_ccw_${crypto.randomBytes(10).toString('hex')}`;
      turn.pending.set(callId, { rpcId: id, conn });
      this.counters.toolCalls++;
      this.emit(turn, { op: 'turn.toolcall', callId, name, args: params.arguments || {}, toolUseId: params._meta?.['claudecode/toolUseId'] || null });
      return;
    }
    reply({ error: { code: -32601, message: 'method not found' } });
  }
}

const isMain = process.argv[1] && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const d = new Daemon();
  d.onStop = () => setTimeout(() => process.exit(0), 100);
  d.start(process.argv[2]).catch((err) => {
    if (err.code === 'EADDRINUSE') process.exit(0); // another daemon won the race
    console.error(`daemon failed: ${err.code || err.message}`);
    process.exit(1);
  });
  process.on('SIGTERM', () => d.stop('sigterm'));
  process.on('SIGINT', () => d.stop('sigint'));
}
