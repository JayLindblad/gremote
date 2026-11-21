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
    this.chunks = 0;
    this.configKey = null;
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

  // Browsers agree on the AVCC form (length-prefixed NAL units plus an avcC description built
  // from the stream's SPS/PPS); the raw Annex B form is not accepted everywhere.
  configure() {
    const { pps } = this;
    const original = this.sps;
    const sps = patchSpsLowLatency(original) || original;     // see patchSpsLowLatency
    this.lowLatencySps = sps !== original;
    const hex = [sps[1], sps[2], sps[3]].map((b) => b.toString(16).padStart(2, '0')).join('');
    const codec = `avc1.${hex}`;
    const description = concatBytes([
      new Uint8Array([1, sps[1], sps[2], sps[3], 0xff, 0xe1, sps.length >> 8, sps.length & 255]), sps,
      new Uint8Array([1, pps.length >> 8, pps.length & 255]), pps,
    ]);
    const key = `${codec}/${original.length}/${pps.length}/${Array.from(original.subarray(4, 12)).join(',')}`;
    if (key === this.configKey && this.decoder && this.decoder.state === 'configured') return;
    this.configKey = key;
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.codec = codec;
    this.gotKey = false;
    this.decoder = new VideoDecoder({
      output: (frame) => {
        if (this.latest) { this.latest.close(); this.skipped++; }   // a newer frame won: drop this one
        this.latest = frame;
        this.frames++;
        if (!this.sawFrame) {
          this.sawFrame = true;
          if (typeof diagLine === 'function') diagLine(`decoder produced its first frame (low-latency SPS: ${this.lowLatencySps})`);
        }
      },
      error: (e) => this.onError(`Video decoder: ${e.name || 'error'}: ${e.message} (codec ${codec}, ${this.chunks} chunks sent)`),
    });
    const config = { codec, description, optimizeForLatency: true };
    VideoDecoder.isConfigSupported(config).then((res) => {
      if (!res.supported) this.onError(`This browser cannot decode ${codec}`);
    }).catch(() => {});
    this.decoder.configure(config);
  }

  feed(u8) {
    if (!startsWithStartCode(u8)) return;      // tail of a frame we already passed on: unusable alone
    for (const nal of splitNals(u8)) {
      const type = nal[0] & 31;
      countNal(this.nals, type);
      if (type === 7) { this.sps = nal.slice(); }
      else if (type === 8) { this.pps = nal.slice(); if (this.sps) this.configure(); }
      else if (type === 5 || type === 1) this.decode(nal, type === 5);
    }
  }

  decode(nal, key) {
    if (!this.decoder || this.decoder.state !== 'configured') return;
    if (key) this.gotKey = true;
    if (!this.gotKey) return;                       // cannot start on a delta frame
    const data = new Uint8Array(4 + nal.length);
    new DataView(data.buffer).setUint32(0, nal.length);     // 4-byte length prefix
    data.set(nal, 4);
    this.timestamp += 33333;
    this.chunks++;
    if (this.chunks <= 25 && typeof diagLine === 'function') {
      const hex = (a) => Array.from(a, (b) => b.toString(16).padStart(2, '0')).join(' ');
      diagLine(`decoder chunk #${this.chunks} ${key ? 'KEY' : 'delta'} nal=${nal.length} bytes `
        + `head=${hex(nal.subarray(0, 8))} tail=${hex(nal.subarray(Math.max(0, nal.length - 4)))}`);
    }
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

// ---- Low-latency SPS ------------------------------------------------------------------
// Decoders hold back frames for reordering unless the stream's SPS says none is needed.
// screenrecord's SPS says nothing, so Chrome waits to fill a large buffer (~8 frames of delay).
// This rewrites the SPS to declare max_num_reorder_frames = 0 (the stream has no B-frames).
// Returns a new NAL unit, or null when the SPS is not a plain Baseline/Main/Extended one.
function patchSpsLowLatency(nal) {
  try {
    const profile = nal[1];
    if (![66, 77, 88].includes(profile)) return null;
    // Remove emulation-prevention bytes.
    const rbsp = [];
    for (let i = 1; i < nal.length; i++) {
      if (i >= 3 && nal[i] === 3 && nal[i - 1] === 0 && nal[i - 2] === 0) continue;
      rbsp.push(nal[i]);
    }
    let pos = 0;
    const bit = () => { const v = (rbsp[pos >> 3] >> (7 - (pos & 7))) & 1; pos++; return v; };
    const bits = (n) => { let v = 0; for (let i = 0; i < n; i++) v = v * 2 + bit(); return v; };
    const ue = () => { let z = 0; while (bit() === 0 && z < 32) z++; return 2 ** z - 1 + bits(z); };
    bits(24); ue();                                   // profile, constraints, level, sps id
    ue();                                             // log2_max_frame_num_minus4
    const pocType = ue();
    if (pocType === 0) ue();
    else if (pocType === 1) return null;
    const numRef = ue(); bit();                       // num_ref_frames, gaps flag
    ue(); ue();                                       // width, height
    if (!bit()) bit();                                // frame_mbs_only (+ mb_adaptive)
    bit();                                            // direct_8x8
    if (bit()) { ue(); ue(); ue(); ue(); }            // cropping
    const vuiFlagPos = pos;
    const hasVui = bit();
    let restrictionFlagPos = pos;                     // where bitstream_restriction_flag lives
    if (hasVui) {
      if (bit()) { if (bits(8) === 255) bits(32); }   // aspect ratio
      if (bit()) bit();                               // overscan
      if (bit()) { bits(4); if (bit()) bits(24); }    // video signal
      if (bit()) { ue(); ue(); }                      // chroma location
      if (bit()) bits(65);                            // timing info
      if (bit() || bit()) return null;                // HRD parameters: not handled
      bit();                                          // pic_struct_present
      restrictionFlagPos = pos;
      if (bit()) return null;                         // already has bitstream restrictions
    }
    // Rebuild: copy bits up to the insertion point, then append the restriction block.
    const out = [];
    const put = (v, n) => { for (let i = n - 1; i >= 0; i--) out.push((v >> i) & 1); };
    const putUe = (v) => { const x = v + 1; const len = Math.floor(Math.log2(x)); put(0, len); put(x, len + 1); };
    const copyTo = hasVui ? restrictionFlagPos : vuiFlagPos;
    for (let i = 0; i < copyTo; i++) out.push((rbsp[i >> 3] >> (7 - (i & 7))) & 1);
    if (!hasVui) {
      put(1, 1);                                      // vui_parameters_present_flag
      put(0, 1); put(0, 1); put(0, 1); put(0, 1);     // aspect, overscan, video signal, chroma loc
      put(0, 1); put(0, 1); put(0, 1);                // timing, nal hrd, vcl hrd
      put(0, 1);                                      // pic_struct_present
    }
    put(1, 1);                                        // bitstream_restriction_flag
    put(1, 1);                                        // motion_vectors_over_pic_boundaries
    putUe(0); putUe(0);                               // max bytes per pic / bits per mb
    putUe(15); putUe(15);                             // log2 max mv length h / v
    putUe(0);                                         // max_num_reorder_frames
    putUe(Math.max(1, numRef));                       // max_dec_frame_buffering
    put(1, 1);                                        // rbsp stop bit
    while (out.length % 8) out.push(0);
    const bytes = [nal[0]];
    let zeros = 0;
    for (let i = 0; i < out.length; i += 8) {
      let b = 0;
      for (let j = 0; j < 8; j++) b = (b << 1) | out[i + j];
      if (zeros >= 2 && b <= 3) { bytes.push(3); zeros = 0; }   // re-add emulation prevention
      bytes.push(b);
      zeros = b === 0 ? zeros + 1 : 0;
    }
    return new Uint8Array(bytes);
  } catch {
    return null;
  }
}
