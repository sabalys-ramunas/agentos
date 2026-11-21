// A clip of the live view, recorded in this browser and saved on this computer.
//
// Nothing is uploaded and nothing new is asked of the node: the clip is made from what this page already
// draws. While a clip records (and only then), a canvas of its own composes, a few dozen times a second,
// the screen's own picture (noVNC's canvas, at the stream's full size) and the agent's pointer (drawn by
// the page over the screen, so the screen's canvas does not have it), framed as the page shows them: the
// screen as a rounded card on the dark stage, with a small openajar mark under it. MediaRecorder encodes
// that canvas's stream: MP4 (H.264) where the browser can, WebM otherwise.
//
// The pointer is read from the page as it is drawn at that moment (its place mid-glide, its fade, a press),
// so the clip moves exactly as the page does and nothing of the pointer's logic is copied here.

export const LIMIT_S = 180;        // a clip stops by itself after three minutes
export const FPS = 30;
export const BITRATE = 6_000_000;  // a ceiling: a quiet screen takes far less (docs/LOCAL.md, "Recording a clip")

const FORMATS = [
  ['video/mp4;codecs=avc1.640028', 'mp4'],
  ['video/mp4;codecs=avc1.4d0028', 'mp4'],
  ['video/mp4;codecs=avc1', 'mp4'],
  ['video/mp4', 'mp4'],
  ['video/webm;codecs=vp9', 'webm'],
  ['video/webm;codecs=vp8', 'webm'],
  ['video/webm', 'webm'],
];

// The first format this browser records, or null when it records none (the button is not shown then).
export function recordFormat() {
  if (typeof MediaRecorder === 'undefined' || typeof HTMLCanvasElement === 'undefined'
    || typeof HTMLCanvasElement.prototype.captureStream !== 'function') return null;
  for (const [mime, ext] of FORMATS) {
    try {
      if (MediaRecorder.isTypeSupported(mime)) return { mime, ext };
    } catch { /* an old browser that throws on a type it does not know */ }
  }
  return null;
}

// "Launch checklist 2026-11-20 at 22.31.05.mp4", as a Mac names its own screen recordings.
export function clipName(title, ext, when = new Date()) {
  const two = (n) => String(n).padStart(2, '0');
  const date = `${when.getFullYear()}-${two(when.getMonth() + 1)}-${two(when.getDate())}`;
  const time = `${two(when.getHours())}.${two(when.getMinutes())}.${two(when.getSeconds())}`;
  const safe = String(title || '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80);
  return `${safe || 'openajar clip'} ${date} at ${time}.${ext}`;
}

// "0:07", "2:41": the time a clip has run.
export function elapsed(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// The frame: the clip is 1920x1080 (1080x1920 for a screen taller than wide), the screen a card in it.
const LONG = 1920;
const SHORT = 1080;
const STAGE = '#0a0a0c';
const AGENT = '#6d4aff';
const RADIUS = 18;
const ARROW = 'M3 2.5 V31 L10.4 24.2 L15.4 35.6 L20.6 33.4 L15.7 22.2 H26 Z';
const FONT = '"Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif';

function rounded(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (typeof ctx.roundRect === 'function') {
    ctx.roundRect(x, y, w, h, r);
    return;
  }
  const [tl, tr, br, bl] = Array.isArray(r) ? r : [r, r, r, r];
  ctx.moveTo(x + tl, y);
  ctx.arcTo(x + w, y, x + w, y + h, tr);
  ctx.arcTo(x + w, y + h, x, y + h, br);
  ctx.arcTo(x, y + h, x, y, bl);
  ctx.arcTo(x, y, x + w, y, tl);
  ctx.closePath();
}

// Where the card sits in the clip for a screen of `sw` by `sh`.
export function layout(sw, sh) {
  const tall = sh > sw;
  const W = tall ? SHORT : LONG;
  const H = tall ? LONG : SHORT;
  const side = tall ? 48 : 96;
  const top = tall ? 96 : 44;
  const foot = tall ? 140 : 92;  // room under the card for the mark
  const k = Math.min((W - 2 * side) / sw, (H - top - foot) / sh);
  const w = Math.round(sw * k);
  const h = Math.round(sh * k);
  return { W, H, card: { x: Math.round((W - w) / 2), y: top + Math.round((H - top - foot - h) / 2), w, h }, foot };
}

export class Clip {
  // screen(): the canvas noVNC draws (null while it reconnects); card, cursor, arrow, tag: the page's own
  // elements; interactive(): whether the white ring of "you have control" shows; markUrl: the openajar mark.
  constructor({ format, screen, card, cursor, arrow, tag, interactive, markUrl, onTick, onStop }) {
    Object.assign(this, { format, screen, cardEl: card, cursorEl: cursor, arrowEl: arrow, tagEl: tag, interactive });
    this.onTick = onTick;
    this.onStop = onStop;
    this.markUrl = markUrl;
    this.chunks = [];
    this.stopped = false;
    this.frames = 0;
  }

  async start() {
    const src = this.screen();
    const sw = src?.width || 1920;
    const sh = src?.height || 1080;
    this.size = { sw, sh };
    this.box = layout(sw, sh);
    const { W, H } = this.box;
    this.canvas = document.createElement('canvas');
    this.canvas.width = W;
    this.canvas.height = H;
    this.ctx = this.canvas.getContext('2d', { alpha: false });
    try { await document.fonts?.load(`600 20px ${FONT}`); } catch { /* the system's font then */ }
    this.mark = await new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => resolve(null);
      img.src = this.markUrl;
    });
    this.backdrop = this.drawBackdrop();
    this.draw(performance.now());
    const stream = this.canvas.captureStream(FPS);
    this.recorder = new MediaRecorder(stream, { mimeType: this.format.mime, videoBitsPerSecond: BITRATE });
    this.recorder.addEventListener('dataavailable', (e) => { if (e.data?.size) this.chunks.push(e.data); });
    this.done = new Promise((resolve) => this.recorder.addEventListener('stop', resolve, { once: true }));
    this.recorder.start(1000);
    this.startedAt = performance.now();
    this.last = 0;
    const loop = (now) => {
      if (this.stopped) return;
      if (now - this.last >= 1000 / FPS - 4) {  // the page's frames come at 60 or more: every other one
        this.last = now;
        this.draw(now);
      }
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
    this.ticker = setInterval(() => {
      const s = (performance.now() - this.startedAt) / 1000;
      this.onTick?.(s);
      if (s >= LIMIT_S) this.stop('limit');
    }, 250);
    this.onTick?.(0);
  }

  // The stage, the card's shadow and the mark: drawn once, then copied under every frame.
  drawBackdrop() {
    const { W, H, card, foot } = this.box;
    const layer = document.createElement('canvas');
    layer.width = W;
    layer.height = H;
    const ctx = layer.getContext('2d', { alpha: false });
    ctx.fillStyle = STAGE;
    ctx.fillRect(0, 0, W, H);
    // the agent's violet, faint, behind the card
    const glow = ctx.createRadialGradient(W / 2, card.y + card.h * 0.55, 0, W / 2, card.y + card.h * 0.55, Math.max(W, H) * 0.62);
    glow.addColorStop(0, 'rgba(109, 74, 255, 0.16)');
    glow.addColorStop(1, 'rgba(109, 74, 255, 0)');
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, H);
    ctx.save();
    ctx.shadowColor = 'rgba(0, 0, 0, 0.75)';
    ctx.shadowBlur = 60;
    ctx.shadowOffsetY = 24;
    ctx.fillStyle = '#121215';
    rounded(ctx, card.x, card.y, card.w, card.h, RADIUS);
    ctx.fill();
    ctx.restore();
    // the mark and the name, small, centred under the card
    const size = 30;
    ctx.font = `600 21px ${FONT}`;
    const word = 'openajar';
    const tw = ctx.measureText(word).width;
    const gap = 10;
    const total = (this.mark ? size + gap : 0) + tw;
    const x = Math.round((W - total) / 2);
    const cy = Math.round(card.y + card.h + foot / 2 + 2);
    if (this.mark) ctx.drawImage(this.mark, x, cy - size / 2, size, size);
    ctx.fillStyle = 'rgba(250, 250, 250, 0.78)';
    ctx.textBaseline = 'middle';
    ctx.fillText(word, x + (this.mark ? size + gap : 0), cy + 1);
    return layer;
  }

  draw() {
    const { ctx } = this;
    const { card } = this.box;
    this.frames += 1;
    ctx.drawImage(this.backdrop, 0, 0);
    const src = this.screen();
    ctx.save();
    rounded(ctx, card.x, card.y, card.w, card.h, RADIUS);
    ctx.clip();
    if (src && src.width && src.height) {
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(src, card.x, card.y, card.w, card.h);
    }
    this.drawPointer(ctx, card);
    ctx.restore();
    // a hairline edge, and the white ring with a small gap while you have control
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.09)';
    rounded(ctx, card.x + 0.5, card.y + 0.5, card.w - 1, card.h - 1, RADIUS);
    ctx.stroke();
    if (this.interactive()) {
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = '#fafafa';
      rounded(ctx, card.x - 4, card.y - 4, card.w + 8, card.h + 8, RADIUS + 4);
      ctx.stroke();
    }
  }

  drawPointer(ctx, card) {
    const el = this.cursorEl;
    const style = getComputedStyle(el);
    const alpha = parseFloat(style.opacity);
    if (!(alpha > 0.01)) return;
    let at;
    try { at = new DOMMatrixReadOnly(style.transform === 'none' ? undefined : style.transform); } catch { return; }
    const pageW = this.cardEl.clientWidth;
    if (!pageW) return;
    const k = card.w / pageW;
    const s = Math.min(1, Math.max(0.62, card.w / 1552));
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(card.x + at.m41 * k, card.y + at.m42 * k);
    ctx.scale(s, s);
    // the arrow (34 by 40, at -3, -2.5), with its press: the dip the page plays, read as it plays
    ctx.save();
    ctx.translate(-3, -2.5);
    const arrowStyle = getComputedStyle(this.arrowEl);
    if (arrowStyle.transform && arrowStyle.transform !== 'none') {
      try {
        const dip = new DOMMatrixReadOnly(arrowStyle.transform).a;
        if (dip > 0 && dip !== 1) {
          ctx.translate(17, 20);
          ctx.scale(dip, dip);
          ctx.translate(-17, -20);
        }
      } catch { /* drawn without the dip */ }
    }
    const arrow = new Path2D(ARROW);
    ctx.lineJoin = 'round';
    ctx.shadowColor = 'rgba(0, 0, 0, 0.35)';
    ctx.shadowBlur = 6 * s;
    ctx.shadowOffsetY = 3 * s;
    ctx.fillStyle = AGENT;
    ctx.fill(arrow);
    ctx.shadowColor = 'transparent';
    ctx.lineWidth = 2.6;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke(arrow);  // over the fill, as the page's SVG paints it
    ctx.restore();
    // the name tag
    const name = this.tagEl.textContent;
    if (name) {
      ctx.font = `600 16px ${FONT}`;
      const w = ctx.measureText(name).width + 26;
      const h = 32;
      const x = 22;
      const y = 28.5;
      ctx.shadowColor = 'rgba(0, 0, 0, 0.45)';
      ctx.shadowBlur = 20 * s;
      ctx.shadowOffsetY = 8 * s;
      ctx.fillStyle = '#ffffff';
      rounded(ctx, x - 2, y - 2, w + 4, h + 4, [6, 16, 16, 16]);
      ctx.fill();
      ctx.shadowColor = 'transparent';
      ctx.fillStyle = AGENT;
      rounded(ctx, x, y, w, h, [4, 14, 14, 14]);
      ctx.fill();
      ctx.fillStyle = '#ffffff';
      ctx.textBaseline = 'middle';
      ctx.fillText(name, x + 13, y + h / 2 + 0.5);
    }
    ctx.restore();
  }

  // Stops, and hands the clip over as a Blob (with why it stopped: 'person', 'limit' or 'ended').
  async stop(why = 'person') {
    if (this.stopped) return;
    this.stopped = true;
    cancelAnimationFrame(this.raf);
    clearInterval(this.ticker);
    const seconds = (performance.now() - this.startedAt) / 1000;
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    await this.done;
    this.recorder.stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(this.chunks, { type: this.format.mime.split(';')[0] });
    this.chunks = [];
    this.canvas.width = 0;  // the frame's memory goes now, not at the next collection
    this.backdrop.width = 0;
    this.onStop?.({ blob, why, seconds, frames: this.frames });
  }
}

// Saves a Blob as a file on this computer, through the browser's own download.
export function save(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  a.hidden = true;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
