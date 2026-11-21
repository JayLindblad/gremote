// Players for the live H.264 screen stream. Both take whole NAL units (Annex B) from the
// WebSocket and keep the picture as close to "now" as they can, dropping old frames.
//
//   WebCodecsPlayer: decodes each frame immediately and paints only the newest one on a canvas.
//                    Lowest delay, but browsers only allow WebCodecs on HTTPS or localhost.
//   MsePlayer:       jmuxer feeding a <video> through Media Source Extensions; works on plain
//                    HTTP, and jumps to the live edge whenever playback falls behind.

const START_CODE = new Uint8Array([0, 0, 0, 1]);

function splitNals(u8) {
  const nals = [];
  let start = -1;
  for (let i = 0; i + 2 < u8.length; i++) {
    if (u8[i] === 0 && u8[i + 1] === 0 && u8[i + 2] === 1) {
      if (start >= 0) {
        let end = i;
        if (end > start && u8[end - 1] === 0) end--;     // the 4-byte form of the start code
        nals.push(u8.subarray(start, end));
      }
      start = i + 3;
      i += 2;
    }
  }
  if (start >= 0 && start < u8.length) nals.push(u8.subarray(start));
  return nals;
}

// Counts of NAL types seen since the last stats line, for diagnosing a black picture.
const NAL_NAMES = { 1: 'p', 5: 'idr', 7: 'sps', 8: 'pps' };
function countNal(counts, type) { counts[type] = (counts[type] || 0) + 1; }
function describeNals(counts) {
  const out = Object.entries(counts).map(([t, n]) => `${NAL_NAMES[t] || `type${t}`}:${n}`).join(' ');
  Object.keys(counts).forEach((t) => delete counts[t]);
  return out || 'no video data';
}

function startsWithStartCode(u8) {
  return u8.length > 3 && u8[0] === 0 && u8[1] === 0 && (u8[2] === 1 || (u8[2] === 0 && u8[3] === 1));
}

function concatBytes(parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  parts.forEach((p) => { out.set(p, at); at += p.length; });
  return out;
}

class WebCodecsPlayer {
  static supported() { return window.isSecureContext && 'VideoDecoder' in window; }

  constructor(canvas, onError) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.onError = onError;
    this.sps = null;
    this.pps = null;
    this.codec = null;
    this.decoder = null;
    this.gotKey = false;
    this.latest = null;      // newest decoded frame, not yet painted
    this.timestamp = 0;
    this.frames = 0;
    this.skipped = 0;
    this.dead = false;
    this.nals = {};
    canvas.hidden = false;
    const paint = () => {
      if (this.dead) return;
      if (this.latest) {
        const f = this.latest;
        this.latest = null;
        if (canvas.width !== f.displayWidth || canvas.height !== f.displayHeight) {
          canvas.width = f.displayWidth;
          canvas.height = f.displayHeight;
        }
        this.ctx.drawImage(f, 0, 0);
        f.close();
      }
      this.raf = requestAnimationFrame(paint);
    };
    this.raf = requestAnimationFrame(paint);
  }

  configure(sps) {
    const hex = [sps[1], sps[2], sps[3]].map((b) => b.toString(16).padStart(2, '0')).join('');
    const codec = `avc1.${hex}`;
    if (codec === this.codec && this.decoder && this.decoder.state === 'configured') return;
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.codec = codec;
    this.gotKey = false;
    this.decoder = new VideoDecoder({
      output: (frame) => {
        if (this.latest) { this.latest.close(); this.skipped++; }   // a newer frame won: drop this one
        this.latest = frame;
        this.frames++;
      },
      error: (e) => this.onError(`Video decoder: ${e.message}`),
    });
    this.decoder.configure({ codec, optimizeForLatency: true, avc: { format: 'annexb' } });
  }

  feed(u8) {
    if (!startsWithStartCode(u8)) return;      // tail of a frame we already passed on: unusable alone
    for (const nal of splitNals(u8)) {
      const type = nal[0] & 31;
      countNal(this.nals, type);
      if (type === 7) { this.sps = nal; this.configure(nal); }
      else if (type === 8) this.pps = nal;
      else if (type === 5 || type === 1) this.decode(nal, type === 5);
    }
  }

  decode(nal, key) {
    if (!this.decoder || this.decoder.state !== 'configured') return;
    if (key) this.gotKey = true;
    if (!this.gotKey) return;                       // cannot start on a delta frame
    const data = key && this.sps && this.pps
      ? concatBytes([START_CODE, this.sps, START_CODE, this.pps, START_CODE, nal])
      : concatBytes([START_CODE, nal]);
    this.timestamp += 33333;
    this.decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: this.timestamp, data }));
  }

  stats() {
    const s = `WebCodecs · ${this.frames} decoded, ${this.skipped} skipped · ${describeNals(this.nals)}`;
    this.frames = 0;
    this.skipped = 0;
    return `${s} · ${this.canvas.width ? `${this.canvas.width}×${this.canvas.height}` : 'waiting for the first frame…'}`;
  }

  destroy() {
    this.dead = true;
    cancelAnimationFrame(this.raf);
    if (this.latest) this.latest.close();
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.canvas.hidden = true;
  }
}

class MsePlayer {
  static supported() { return !!(window.JMuxer && window.MediaSource); }

  constructor(video) {
    this.video = video;
    this.mux = new JMuxer({ node: video.id, mode: 'video', fps: 30, flushingTime: 0, debug: false });
    video.hidden = false;
    this.jumps = 0;
    this.nals = {};
    this.timer = setInterval(() => this.catchUp(), 250);
  }

  catchUp() {
    const v = this.video;
    const b = v.buffered;
    if (!b.length) return;
    const end = b.end(b.length - 1);
    this.lag = end - v.currentTime;
    if (this.lag > 0.6) { v.currentTime = end - 0.03; this.jumps++; }    // far behind: skip ahead
    else v.playbackRate = this.lag > 0.12 ? 1.2 : 1;                       // a little behind: catch up smoothly
    if (v.paused) v.play().catch(() => {});
  }

  feed(u8) {
    if (!startsWithStartCode(u8)) return;      // tail of a frame we already passed on: unusable alone
    splitNals(u8).forEach((n) => countNal(this.nals, n[0] & 31));
    this.mux.feed({ video: u8 });
  }

  stats() {
    const v = this.video;
    const s = `MSE · lag ${Math.round((this.lag || 0) * 1000)} ms, ${this.jumps} jumps to live · ${describeNals(this.nals)}`;
    this.jumps = 0;
    return `${s} · ${v.videoWidth ? `${v.videoWidth}×${v.videoHeight}` : 'waiting for the first frame…'}`;
  }

  destroy() {
    clearInterval(this.timer);
    try { this.mux.destroy(); } catch { /* already gone */ }
    this.video.hidden = true;
    this.video.removeAttribute('src');
  }
}

// Player internals for the recorded diagnostic log.
WebCodecsPlayer.prototype.debug = function debug() {
  const d = this.decoder;
  return `decoder=${d ? d.state : 'none'} queue=${d ? d.decodeQueueSize : '-'} codec=${this.codec || '-'} `
    + `gotKey=${this.gotKey} canvas=${this.canvas.width}x${this.canvas.height}`;
};

MsePlayer.prototype.debug = function debug() {
  const v = this.video;
  const ranges = [];
  for (let i = 0; i < v.buffered.length; i++) ranges.push(`${v.buffered.start(i).toFixed(2)}-${v.buffered.end(i).toFixed(2)}`);
  return `readyState=${v.readyState} networkState=${v.networkState} paused=${v.paused} `
    + `t=${v.currentTime.toFixed(2)} buffered=[${ranges.join(', ')}] size=${v.videoWidth}x${v.videoHeight} `
    + `error=${v.error ? `${v.error.code} ${v.error.message}` : 'none'}`;
};
