#!/usr/bin/env node
// Started by Claude Code as its "codex" MCP server (stdio). It forwards JSON-RPC lines to the
// daemon's socket, which answers tools/list from the turn's validated tool list and relays
// tools/call to the Windows adapter. The token ties this relay to exactly one Claude turn.
import net from 'node:net';
import readline from 'node:readline';
import { FrameDecoder, FrameWriter } from '../shared/frame.mjs';

const socketPath = process.env.CCW_SOCKET;
const token = process.env.CCW_TURN_TOKEN;
if (!socketPath || !token) {
  console.error('relay: missing socket or token');
  process.exit(1);
}
const sock = net.createConnection(socketPath);
const writer = new FrameWriter(sock);
const decoder = new FrameDecoder();
sock.on('error', () => process.exit(1));
sock.on('close', () => process.exit(0));
sock.on('data', (d) => decoder.push(d));
decoder.on('frame', ({ header }) => {
  if (header.op === 'mcp' && header.msg) process.stdout.write(`${JSON.stringify(header.msg)}\n`);
});
writer.send({ op: 'hello', role: 'mcp', token }).catch(() => {});
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  writer.send({ op: 'mcp', msg }).catch(() => {});
}).on('close', () => sock.end());
