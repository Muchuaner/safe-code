#!/usr/bin/env node
// SSH entry point (the forced command for the Windows adapter's key). Connects this SSH
// session's stdin/stdout to the per-user daemon, starting the daemon if it is not running.
// The daemon is started with SSH_* variables removed, so it never inherits the client's address.
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { STATE_HOME, sanitizedEnv } from './daemon.mjs';

const socketPath = path.join(STATE_HOME, 'run', 'daemon.sock');
const daemonPath = fileURLToPath(new URL('./daemon.mjs', import.meta.url));

function tryConnect() {
  return new Promise((resolve) => {
    const s = net.createConnection(socketPath);
    s.once('connect', () => resolve(s));
    s.once('error', () => resolve(null));
  });
}

async function main() {
  let sock = await tryConnect();
  if (!sock) {
    fs.mkdirSync(path.dirname(socketPath), { recursive: true, mode: 0o700 });
    // Only one connector may start the daemon; others wait for its socket.
    const lock = path.join(path.dirname(socketPath), 'spawn.lock');
    let owner = false;
    try {
      fs.closeSync(fs.openSync(lock, 'wx', 0o600));
      owner = true;
    } catch {
      try {
        if (Date.now() - fs.statSync(lock).mtimeMs > 15000) {
          fs.unlinkSync(lock); // left behind by a crashed connector
          fs.closeSync(fs.openSync(lock, 'wx', 0o600));
          owner = true;
        }
      } catch {
        // someone else holds it
      }
    }
    if (owner) {
      try {
        fs.unlinkSync(socketPath); // stale socket of a daemon that is gone
      } catch {
        // none
      }
      const child = spawn(process.execPath, [daemonPath, socketPath], { detached: true, stdio: 'ignore', env: sanitizedEnv(), cwd: STATE_HOME });
      child.unref();
    }
    for (let i = 0; i < 100 && !sock; i++) {
      await new Promise((r) => setTimeout(r, 100));
      sock = await tryConnect();
    }
    if (owner) fs.rmSync(lock, { force: true });
  }
  if (!sock) {
    process.stderr.write('codex-claude-win: could not start the VPS daemon\n');
    process.exit(2);
  }
  sock.on('close', () => process.exit(0));
  sock.on('error', () => process.exit(0));
  process.stdin.on('end', () => sock.end());
  process.stdin.pipe(sock);
  sock.pipe(process.stdout);
}
main();
