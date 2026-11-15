// Binary framing for the SSH stdio channel between the Windows adapter and the VPS component.
//
// Frame = u32 header length | u32 body length | header (UTF-8 JSON) | body (raw bytes)
//
// Raw bytes travel as the body, so images and files are never base64-encoded on the wire.
// Both directions use the same format. No frame ever carries credentials: the channel is
// already authenticated and encrypted by SSH.
import { EventEmitter } from 'node:events';

export const MAX_HEADER_BYTES = 16 * 1024 * 1024;
export const MAX_BODY_BYTES = 4 * 1024 * 1024;
export const PROTOCOL_VERSION = 1;

export function encodeFrame(header, body = null) {
  const head = Buffer.from(JSON.stringify(header), 'utf8');
  const bodyLength = body ? body.length : 0;
  if (head.length > MAX_HEADER_BYTES) throw new Error(`frame header too large (${head.length} bytes)`);
  if (bodyLength > MAX_BODY_BYTES) throw new Error(`frame body too large (${bodyLength} bytes)`);
  const prefix = Buffer.allocUnsafe(8);
  prefix.writeUInt32BE(head.length, 0);
  prefix.writeUInt32BE(bodyLength, 4);
  return bodyLength ? [prefix, head, body] : [prefix, head];
}

/** Incremental decoder: push() raw chunks, receive 'frame' events ({ header, body }). */
export class FrameDecoder extends EventEmitter {
  constructor() {
    super();
    this.chunks = [];
    this.buffered = 0;
    this.need = null; // { head, body } once the prefix has been read
  }

  push(chunk) {
    this.chunks.push(chunk);
    this.buffered += chunk.length;
    for (;;) {
      if (!this.need) {
        if (this.buffered < 8) return;
        const prefix = this.take(8);
        const head = prefix.readUInt32BE(0);
        const body = prefix.readUInt32BE(4);
        if (head > MAX_HEADER_BYTES || body > MAX_BODY_BYTES) {
          this.emit('error', new Error('frame exceeds limits; channel is out of sync'));
          this.chunks = [];
          this.buffered = 0;
          return;
        }
        this.need = { head, body };
      }
      const { head, body } = this.need;
      if (this.buffered < head + body) return;
      this.need = null;
      const headerBytes = this.take(head);
      const bodyBytes = body ? this.take(body) : null;
      let header;
      try {
        header = JSON.parse(headerBytes.toString('utf8'));
      } catch {
        this.emit('error', new Error('frame header is not JSON'));
        continue;
      }
      this.emit('frame', { header, body: bodyBytes });
    }
  }

  take(n) {
    let out;
    const first = this.chunks[0];
    if (first.length >= n) {
      out = first.subarray(0, n);
      if (first.length === n) this.chunks.shift();
      else this.chunks[0] = first.subarray(n);
    } else {
      out = Buffer.allocUnsafe(n);
      let off = 0;
      while (off < n) {
        const c = this.chunks[0];
        const k = Math.min(c.length, n - off);
        c.copy(out, off, 0, k);
        off += k;
        if (k === c.length) this.chunks.shift();
        else this.chunks[0] = c.subarray(k);
      }
    }
    this.buffered -= n;
    return out;
  }
}

/**
 * Writes frames to a stream with two priorities and backpressure.
 * Control frames (small JSON) always go before queued bulk frames (blob chunks), so a large
 * upload never delays a tool result, cancel, or ping. Bulk frames are only written while the
 * stream accepts data ('drain'), so memory stays bounded.
 */
export class FrameWriter {
  constructor(stream) {
    this.stream = stream;
    this.control = [];
    this.bulk = [];
    this.waiting = false;
    this.closed = false;
    this.bytesOut = 0;
    stream.on('drain', () => {
      this.waiting = false;
      this.pump();
    });
    stream.on('close', () => this.fail(new Error('channel closed')));
    stream.on('error', (err) => this.fail(err));
  }

  send(header, body = null) {
    return this.enqueue(this.control, header, body);
  }

  sendBulk(header, body = null) {
    return this.enqueue(this.bulk, header, body);
  }

  enqueue(queue, header, body) {
    if (this.closed) return Promise.reject(new Error('channel closed'));
    return new Promise((resolve, reject) => {
      queue.push({ parts: encodeFrame(header, body), resolve, reject });
      this.pump();
    });
  }

  pump() {
    while (!this.waiting && !this.closed) {
      const item = this.control.shift() || this.bulk.shift();
      if (!item) return;
      let ok = true;
      for (const part of item.parts) {
        this.bytesOut += part.length;
        ok = this.stream.write(part) && ok;
      }
      item.resolve();
      if (!ok) this.waiting = true;
    }
  }

  fail(err) {
    if (this.closed) return;
    this.closed = true;
    for (const item of [...this.control.splice(0), ...this.bulk.splice(0)]) item.reject(err);
  }
}
