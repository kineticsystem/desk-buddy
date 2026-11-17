"""Bake Lump's browser animation into a binary the firmware plays back.

    python3 tools/export_frames.py --bake                 # headless Chrome: writes firmware/data/lump.bin
    python3 tools/export_frames.py --clip encore          # just the sequences named encore / encore_*,
                                                          # as ~/.desk-buddy/clips/encore.clip (sent over Wi-Fi).
                                                          # design/clips/encore.js, if there is one, is loaded
                                                          # first: it adds the clip's drawings and its sequence
    python3 tools/export_frames.py --clip all             # every design/clips/*.js
    python3 tools/export_frames.py --clip encore --sheet encore.png
                                                          # don't bake: save its drawings side by side to look at,
                                                          # 24 to a picture (encore.png, encore_2.png, ...)
    python3 tools/export_frames.py --clip encore --define my_clips.json
                                                          # ...where the JSON adds sequences built from his
                                                          # existing drawings: {"encore": {"home": "idle", "seq": [["S1", 4], ...]}}
    python3 tools/export_frames.py 8767                   # serve design/ for editing; run EXPORT_JS in the
                                                          # page's console to bake from your own browser

The page posts its mood/sequence list to /meta, then every 24 fps frame of every sequence: the
picture (raw RGBA) and the canvas calls that drew it. Identical frames are stored once. Each frame
is kept as drawing instructions (about a kilobyte, drawn on the board by firmware/src/vec.h) when
that renderer reproduces the browser's picture closely enough, and as pixels otherwise.

lump.bin layout (little-endian), version 5:
  "LUMP" u16 version=5  u16 nFrames  u8 nMoods  u8 fps
  per mood:      u8 base mood index, u8 nameLen, name
  u16 nSeqs (at most 750 in lump.bin: the board keeps room for that many)
  per sequence:  u8 kind (0 loop, 1 transition, 2 event), u8 from, u8 to,
                 u8 nameLen, name, u16 length, u16 frame index per tick
  u32 offset[nFrames]  (from start of frame data; top bit set = an instruction frame)
  frame data: a pixel frame is runs of [u8 count (1..255)][u16 rgb565 big-endian] until 57600 px;
              an instruction frame is u16 length, then the instructions (see encode())
A clip is the same layout holding only its own sequences and frames.
"""
import argparse, glob, hashlib, http.server, json, math, os, re, shutil, socket, struct, subprocess, sys, tempfile, threading, zlib
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESIGN = os.path.join(ROOT, "design")
OUT = os.path.join(ROOT, "firmware", "data", "lump.bin")
KIT = os.path.join(DESIGN, "clip_kit.js")          # props and helpers every clip script may use
CLIPS = os.path.expanduser("~/.desk-buddy/clips")
CHROME = os.environ.get("CHROME") or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome"),
                                                        shutil.which("chromium")) if p and os.path.exists(p)), "google-chrome")
PIXELS = 240 * 240 * 4
MAX_SEQS = 750                    # sequences the board has room for in lump.bin (MAX_SEQ in firmware/src/main.cpp)
MAX_MEAN, MAX_FAR = 2.0, 300      # an instruction frame may differ from the browser's by this much: mean % of full
                                  # scale, and pixels more than a quarter of full scale apart (thin edge fringes)

units = {}    # frame id -> (rgba bytes, canvas calls)
where = {}    # (sequence key, tick) -> frame id
named = {}    # (sequence key, tick) -> the named drawing shown there
meta = {}
done = threading.Event()
result = {}
out_path = OUT
clip_name = None   # the clip being baked, if it is one

EXPORT_JS = r"""
(async () => {
  // Body colour is exported as pure magenta, a marker the firmware swaps for the real colour
  // at runtime (so his colour can change on the fly).
  const cfg = window.__lumpExport || {};
  const TINT = cfg.tint || '#FF00FF';
  const L = window.__lump, cv = document.createElement('canvas'); cv.width = cv.height = 240;
  const c = cv.getContext('2d', { willReadFrequently: true });
  L.setLine('glow'); L.setTint(TINT);
  for (const [name, def] of Object.entries(cfg.define || {})) L.CLIPS[name] = def;
  // draw through a recorder, so each frame is also kept as the canvas calls that made it
  let ops = [];
  const rec = new Proxy(c, {
    get(t, p) { const v = t[p]; if (typeof v !== 'function') return v;
      return (...a) => { ops.push([p, ...a.map(x => (typeof x === 'number' || typeof x === 'string' || typeof x === 'boolean' || Array.isArray(x)) ? x : null)]); return v.apply(t, a); }; },
    set(t, p, v) { t[p] = v; ops.push(['=' + p, (p === 'fillStyle' || p === 'strokeStyle') ? t[p] : v]); return true; }
  });
  const meta = L.meta();
  if (cfg.only) meta.seqs = meta.seqs.filter(s => cfg.only.some(n => s.name === n || s.name.startsWith(n + '_')));
  await fetch('/meta', { method: 'POST', body: JSON.stringify(meta) });
  const seen = new Map(); let n = 0;
  L.render(c, 'idle', L.at('idle', 0).id, 0, { dim: false });     // one frame first, as the page has always drawn one before
  for (const s of meta.seqs) {
    for (let t = 0; t < L.seqLen(s.key); t++) {
      const a = s.kind === 0 ? L.at(s.key, t) : L.atSeq(s.key, t);
      // the page doesn't reset the canvas between frames, so a frame starts with whatever the last one left set
      ops = [['=lineCap', c.lineCap], ['=lineJoin', c.lineJoin], ['=lineWidth', c.lineWidth], ['=fillStyle', c.fillStyle], ['=strokeStyle', c.strokeStyle],
             ['=globalAlpha', c.globalAlpha], ['setLineDash', c.getLineDash()], ['=lineDashOffset', c.lineDashOffset]];
      L.render(rec, s.kind === 0 ? s.key : L.fxFor(a.id), a.id, t, { dim: false });
      const calls = JSON.stringify(ops), q = `seq=${encodeURIComponent(s.key)}&tick=${t}`, was = seen.get(calls);
      const q2 = q + '&d=' + encodeURIComponent(a.id);
      if (was !== undefined && !calls.includes('"drawImage"')) await fetch(`/same?${q2}&as=${was}`, { method: 'POST' });   // same calls, same picture
      else { seen.set(calls, n); await fetch(`/frame?${q2}&id=${n}`, { method: 'POST', body: new Blob([c.getImageData(0, 0, 240, 240).data, calls]) }); }
      n++;
    }
  }
  return n + ' frames; ' + await (await fetch('/finish', { method: 'POST' })).text();
})().catch(e => fetch('/error', { method: 'POST', body: String(e && e.stack || e) }))
"""


def rgb565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def rle(rgba):
    px = [rgb565(rgba[i], rgba[i + 1], rgba[i + 2]) for i in range(0, len(rgba), 4)]
    out = bytearray(); i = 0; n = len(px)
    while i < n:
        v = px[i]; j = i + 1
        while j < n and px[j] == v and j - i < 255:
            j += 1
        out += bytes([j - i]) + struct.pack(">H", v)
        i = j
    return bytes(out)


# ---- instructions: one byte saying which canvas call, then its numbers. Lengths are in 1/16 px,
# angles in 1/4096 radian, scales in 1/256, colours RGBA. firmware/src/vec.h reads exactly this. ----
OP = {"beginPath": 1, "closePath": 2, "moveTo": 3, "lineTo": 4, "quadraticCurveTo": 5, "bezierCurveTo": 6, "arc": 7,
      "ellipse": 8, "rect": 9, "fillRect": 10, "fill": 11, "stroke": 12, "clip": 13, "save": 14, "restore": 15,
      "translate": 16, "rotate": 17, "scale": 18, "setTransform": 19, "=fillStyle": 20, "=strokeStyle": 21,
      "=lineWidth": 22, "=globalAlpha": 23, "setLineDash": 24, "roundRect": 25, "=lineDashOffset": 26}
CLIP_OUT = 27     # keep what is outside the path. The canvas has no such call: see hole()
# every stroke is drawn with round ends and corners, which is all his drawings use bar a few pointed corners
SAME = {"=lineCap": ("round",), "=lineJoin": ("round", "miter"), "=globalCompositeOperation": ("source-over",)}


def q(v, scale=16):
    return struct.pack("<h", max(-32768, min(32767, round(v * scale))))


def colour(s):
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", s)
    if m: return bytes.fromhex(m.group(1)) + b"\xff"
    p = [float(x) for x in re.fullmatch(r"rgba?\(([^)]+)\)", s).group(1).split(",")]
    return bytes([int(p[0]), int(p[1]), int(p[2]), round((p[3] if len(p) > 3 else 1) * 255)])


def turn(a):
    """An angle brought within half a turn of zero, so any amount of spinning fits in 16 bits."""
    return (a + math.pi) % math.tau - math.pi


def arc_angles(a0, a1, anticlockwise):
    """The same arc as the canvas would draw, with both angles within one turn of zero."""
    d = a1 - a0
    if anticlockwise: d = -math.tau if d <= -math.tau else d % math.tau - math.tau if d > 0 else d
    else: d = math.tau if d >= math.tau else d % -math.tau + math.tau if d < 0 else d
    a0 %= math.tau
    if d > 0: a0 -= math.tau
    return q(a0, 4096) + q(a0 + d, 4096) + bytes([1 if anticlockwise else 0])


def hole(calls, i):
    """The page cuts a round hole in what follows with beginPath, rect(whole screen), arc(anticlockwise), clip.
    If that is what starts at calls[i], the arc."""
    c = calls[i:i + 4]
    if [o[0] for o in c] == ["beginPath", "rect", "arc", "clip"] and c[1][1:] == [0, 0, 240, 240] and len(c[2]) > 6 and c[2][6] and len(c[3]) == 1:
        return c[2]


def encode(calls):
    """The instructions for one frame's canvas calls, or None if it uses something vec.h can't draw."""
    out = bytearray(); i = 0; calls = list(calls)
    try:
        while i < len(calls):
            if hole(calls, i):                           # a run of holes becomes a path and a cut (a few, if there are many)
                out.append(OP["beginPath"]); room = 800  # points a path may hold, with some to spare
                while hole(calls, i) and room > 0:
                    a = hole(calls, i)[1:]; i += 4; room -= 8 + 12 * abs(a[2]) ** 0.5
                    out.append(OP["moveTo"]); out += q(a[0] + a[2]) + q(a[1])
                    out.append(OP["arc"]); out += q(a[0]) + q(a[1]) + q(a[2]) + arc_angles(a[3], a[4], True)
                out.append(CLIP_OUT); continue
            o = calls[i]; i += 1
            n, a = o[0], o[1:]
            if n in SAME:
                if a[0] not in SAME[n]: return None
                continue
            if n not in OP or (n in ("fill", "clip") and a): return None
            out.append(OP[n])
            if n in ("moveTo", "lineTo", "translate"): out += q(a[0]) + q(a[1])
            elif n == "quadraticCurveTo": out += b"".join(q(v) for v in a[:4])
            elif n == "bezierCurveTo": out += b"".join(q(v) for v in a[:6])
            elif n == "arc": out += q(a[0]) + q(a[1]) + q(a[2]) + arc_angles(a[3], a[4], len(a) > 5 and a[5])
            elif n == "ellipse": out += b"".join(q(v) for v in a[:4]) + q(turn(a[4]), 4096) + arc_angles(a[5], a[6], len(a) > 7 and a[7])
            elif n in ("rect", "fillRect"): out += b"".join(q(v) for v in a[:4])
            elif n == "roundRect": out += b"".join(q(v) for v in a[:4]) + q(a[4][0] if isinstance(a[4], list) else a[4])
            elif n == "rotate": out += q(turn(a[0]), 4096)
            elif n == "scale": out += q(a[0], 256) + q(a[1], 256)
            elif n == "setTransform": out += b"".join(q(v, 256) for v in a[:4]) + q(a[4]) + q(a[5])
            elif n in ("=fillStyle", "=strokeStyle"): out += colour(a[0])
            elif n == "=lineWidth": out += struct.pack("<H", min(65535, round(a[0] * 16)))
            elif n == "=globalAlpha": out.append(max(0, min(255, round(a[0] * 255))))
            elif n == "setLineDash":
                if len(a[0]) > 8: return None
                out.append(len(a[0])); out += b"".join(struct.pack("<H", min(65535, round(v * 16))) for v in a[0])
            elif n == "=lineDashOffset": out += q(a[0])
    except (TypeError, AttributeError, IndexError):      # a gradient, an image, an argument that isn't a number
        return None
    return bytes(out) if len(out) < 65536 else None


def check(frames):
    """How well vec.h redraws each (instructions, rgba): a list of (mean % difference, far pixels, overflowed)."""
    # In a folder of our own: a file another user left in /tmp under this name would run as us.
    exe = os.path.join(os.path.expanduser("~/.cache/desk-buddy"), "lump_vec_check")
    os.makedirs(os.path.dirname(exe), mode=0o700, exist_ok=True)
    src = os.path.join(ROOT, "tools", "vec_check.cpp"); hdr = os.path.join(ROOT, "firmware", "src", "vec.h")
    if not os.path.exists(exe) or os.path.getmtime(exe) < max(os.path.getmtime(src), os.path.getmtime(hdr)):
        subprocess.run(["clang++", "-O2", "-std=c++17", "-I", os.path.dirname(hdr), src, "-o", exe], check=True)
    feed = b"".join(struct.pack("<I", len(v)) + v + rgba for v, rgba in frames)
    res = subprocess.run([exe], input=feed, capture_output=True, check=True).stdout.decode().split("\n")
    return [(float(a), int(b), c == "1") for a, b, c in (line.split() for line in res if line)]


def lint(name, pics, calls, stuck, shows, size):
    """What bakes but wastes space or will look wrong on him, for a clip: a list of plain sentences."""
    notes = []
    for s in meta["seqs"]:
        ticks = sorted(t for (k, t) in where if k == s["key"]); d = [named[(s["key"], t)] for t in ticks]
        odd = [d[i] for i in range(1, len(d)) if d[i] != d[i - 1] and i % 2 and d[i].startswith(name + "_")][1:]
        if odd: notes.append(f"{len(odd)} drawings start on an odd tick, so they straddle two of his wobbles and are stored twice (first: {odd[0]}). "
                             "Hold the first drawing after WAKE for 3 ticks and every other for an even number.")
    pinks = set()
    for frame in calls:
        for c in frame:
            if c[0] in ("=fillStyle", "=strokeStyle") and isinstance(c[1], str):
                r, g, b, _ = colour(c[1]); beta = g / 241; alpha = r / 255 - beta
                if max(r, g, b) >= 8 and alpha > 0.02 and b >= alpha * 255 + beta * 220 - 48 and abs(r - b) > 24: pinks.add(c[1])
    if pinks: notes.append(f"The board will repaint {', '.join(sorted(pinks))} in his body colour: it takes pinks and purples for him.")
    lit = lambda rgba, x, y: max(rgba[(y * 240 + x) * 4:(y * 240 + x) * 4 + 3]) > 110     # brighter than a wall or a sky
    for side, cells in (("left", [(0, y) for y in range(186)]), ("right", [(239, y) for y in range(186)]), ("top", [(x, 0) for x in range(240)])):
        framed = lambda f: any(c[0] == "scale" for c in calls[f][:14])      # seen through kit.view: the edges are meant to be crossed
        cut = sorted(d for d, frames in shows.items() if d.startswith(name + "_") and any(not framed(f) and sum(lit(pics[f], x, y) for x, y in cells) > 10 for f in frames))
        if cut: notes.append(f"Something bright touches the {side} edge in {', '.join(cut[:10])}{' and more' if len(cut) > 10 else ''}. "
                             "Fine for a setting or a slide in or out; anywhere else a prop or a Lump is cut off.")
    if stuck: notes.append(f"Stored as pixels: {', '.join(stuck[:8])}. Look for a NaN or undefined coordinate, a call the board can't draw, "
                           "a translucent full-screen fill, or a scale that isn't a whole number of 256ths.")
    if size > 150000: notes.append(f"{size} bytes: over 150000 he takes longer to send and crowds the board's memory. Reuse drawings or simplify props.")
    return notes


def staging(name, pics, shows):
    """Where he was and how big, across a clip's own drawings: to see at a glance whether a batch all looks alike."""
    xs, hs = [], []
    for d, frames in shows.items():
        if not d.startswith(name + "_"): continue
        rgba = pics[min(frames)]; hit = [(i % 80 * 3, i // 80 * 3) for i in range(6400) if rgba[(i // 80 * 720 + i % 80 * 3) * 4:(i // 80 * 720 + i % 80 * 3) * 4 + 3] == b"\xff\x00\xff"]
        if hit: xs.append(sum(x for x, _ in hit) / len(hit)); hs.append(max(y for _, y in hit) - min(y for _, y in hit))
    if not xs: return {}
    side = lambda x: "left" if x < 100 else "right" if x > 156 else "centre"
    return {"seconds": round(len(where) / 24, 1), "he_stands": sorted({side(x) for x in xs}), "his_size": [round(min(hs) / 128, 2), round(max(hs) / 128, 2)],
            "drawings_without_him": sum(1 for d in shows if d.startswith(name + "_")) - len(xs)}


def pstr(s):
    b = s.encode(); return bytes([len(b)]) + b


def build():
    moods = [m["name"] for m in meta["moments"]]
    if len(meta["seqs"]) > MAX_SEQS: sys.exit(f"{len(meta['seqs'])} sequences is more than the board's {MAX_SEQS}")
    uniq, pics, calls, body, shows = {}, [], [], bytearray(), {}
    for s in meta["seqs"]:
        ticks = sorted(t for (k, t) in where if k == s["key"])
        seq = []
        for t in ticks:
            rgba, ops = units[where[(s["key"], t)]]; h = hashlib.sha1(rgba).digest()
            if h not in uniq:
                uniq[h] = len(pics); pics.append(rgba); calls.append(ops)
            seq.append(uniq[h]); shows.setdefault(named[(s["key"], t)], set()).add(uniq[h])
        body += struct.pack("<BBB", s["kind"], moods.index(s["from"]), moods.index(s["to"])) + pstr(s["name"])
        body += struct.pack("<H", len(seq)) + struct.pack(f"<{len(seq)}H", *seq)
    vec = [encode(c) for c in calls]
    scores = iter(check([(v, pics[i]) for i, v in enumerate(vec) if v is not None]))
    blobs, why = [], {"instructions": 0, "pixels: uses something the renderer can't draw": 0, "pixels: didn't match closely enough": 0}
    worst = (0.0, 0)
    for i, v in enumerate(vec):
        if v is None:
            why["pixels: uses something the renderer can't draw"] += 1; blobs.append((0, rle(pics[i]))); continue
        mean, far, overflowed = next(scores)
        if overflowed or mean > MAX_MEAN or far > MAX_FAR:
            why["pixels: didn't match closely enough"] += 1; blobs.append((0, rle(pics[i]))); continue
        worst = (max(worst[0], mean), max(worst[1], far))
        why["instructions"] += 1; blobs.append((1, struct.pack("<H", len(v)) + v))
    out = bytearray(b"LUMP") + struct.pack("<HHBB", 5, len(blobs), len(moods), 24)
    for m in meta["moments"]:
        out += bytes([m["base"]]) + pstr(m["name"])
    out += struct.pack("<H", len(meta["seqs"])) + body
    off = 0
    for kind, blob in blobs:
        out += struct.pack("<I", off | (kind << 31)); off += len(blob)
    for _, blob in blobs:
        out += blob
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(out)
    as_pixels = sorted(d for d, frames in shows.items() if any(not blobs[f][0] for f in frames))
    return {**({"lint": lint(clip_name, pics, calls, [d for d in as_pixels if d.startswith(clip_name + "_")], shows, len(out)), "staging": staging(clip_name, pics, shows)} if clip_name else {}),
            "frames_rendered": len(where), "unique_frames": len(blobs), "bytes": len(out), "moods": len(moods),
            "sequences": len(meta["seqs"]), "kept_as": why,
            "named_drawings": {"total": len(shows), "all_instructions": len(shows) - len(as_pixels), "with_pixel_frames": as_pixels},
            "instruction_bytes": sum(len(b) for k, b in blobs if k),
            "pixel_bytes": sum(len(b) for k, b in blobs if not k),
            "worst_instruction_frame": {"mean_difference_pct": worst[0], "pixels_far_off": worst[1]}, "out": out_path}


def sheet():
    """Instead of baking: every distinct drawing, in the order they first appear, side by side, 24 to a PNG
    (name.png, then name_2.png, name_3.png ...)."""
    order, seen = [], set()
    for s in meta["seqs"]:
        for t in sorted(t for (k, t) in where if k == s["key"]):
            i = where[(s["key"], t)]
            if i not in seen: seen.add(i); order.append(i)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    root, ext = os.path.splitext(out_path); outs = []
    for page in range(0, len(order), 24):
        ids = order[page:page + 24]; cols = min(6, len(ids)); rows = -(-len(ids) // cols)
        px = bytearray(cols * 240 * rows * 240 * 3)
        for n, i in enumerate(ids):
            rgba = units[i][0]; x0, y0 = (n % cols) * 240, (n // cols) * 240
            for y in range(240):
                row = rgba[y * 960:(y + 1) * 960]; at = ((y0 + y) * cols * 240 + x0) * 3
                px[at:at + 720] = bytes(b for k in range(0, 960, 4) for b in row[k:k + 3])
        raw = b"".join(b"\x00" + bytes(px[y * cols * 720:(y + 1) * cols * 720]) for y in range(rows * 240))
        outs.append(out_path if not page else f"{root}_{page // 24 + 1}{ext}")
        with open(outs[-1], "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cols * 240, rows * 240, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return {"drawings": len(order), "ticks": len(where), "seconds": round(len(where) / 24, 1), "out": outs}


def gif():
    """Instead of baking: the sequences played through once as an animated GIF (needs ffmpeg)."""
    raw = b"".join(units[where[(s["key"], t)]][0] for s in meta["seqs"] for t in sorted(t for (k, t) in where if k == s["key"]))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "240x240", "-r", "24", "-i", "-",
                    "-vf", "split[a][b];[a]palettegen[p];[b][p]paletteuse=dither=none", out_path], input=raw, check=True)
    return {"ticks": len(where), "seconds": round(len(where) / 24, 1), "bytes": os.path.getsize(out_path), "out": out_path}


finish = build


class H(http.server.SimpleHTTPRequestHandler):
    page = None                                    # headless: the page to serve at /, with the export script added

    def __init__(self, *a, **k):
        super().__init__(*a, directory=DESIGN, **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.page is not None and urlparse(self.path).path == "/":
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(self.page); return
        super().do_GET()

    def do_POST(self):
        u = urlparse(self.path); n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        q = parse_qs(u.query)
        if u.path == "/meta":
            meta.clear(); meta.update(json.loads(body)); units.clear(); where.clear(); named.clear(); res = b"ok"
        elif u.path == "/frame":
            at = (q["seq"][0], int(q["tick"][0])); named[at] = q["d"][0]
            units[int(q["id"][0])] = (body[:PIXELS], json.loads(body[PIXELS:])); where[at] = int(q["id"][0]); res = b"ok"
        elif u.path == "/same":
            at = (q["seq"][0], int(q["tick"][0])); named[at] = q["d"][0]; where[at] = int(q["as"][0]); res = b"ok"
        elif u.path == "/error":                    # the page hit a script error (headless: say so and stop)
            result["error"] = body.decode(errors="replace"); done.set(); res = b"ok"
        elif u.path == "/finish":
            info = finish(); print(json.dumps(info, indent=1), flush=True); res = json.dumps(info).encode()
            result.update(info); done.set()
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(res)


def headless(config, scripts=()):
    """Run the export in headless Chrome (after any clip scripts) and wait for it to finish."""
    report = "<script>addEventListener('error', e => fetch('/error', { method: 'POST', body: e.message + ' (line ' + e.lineno + ')' }));</script>"
    page = open(os.path.join(DESIGN, "lump.html")).read() + report + "".join(f"<script>{open(p).read()}</script>" for p in scripts)
    H.page = (page + f"<script>window.__lumpExport = {json.dumps(config)};{EXPORT_JS}</script>").encode()
    done.clear(); result.clear()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="lump-export-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", *(["--no-sandbox"] if sys.platform == "linux" else []), "--no-first-run", "--no-default-browser-check",
                               f"--user-data-dir={profile}", f"http://127.0.0.1:{port}/"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not done.wait(900): sys.exit("Chrome didn't finish the export")
    finally:
        chrome.terminate(); chrome.wait(10); srv.shutdown(); shutil.rmtree(profile, ignore_errors=True)
    if "error" in result: sys.exit("the page stopped with an error: " + result["error"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?", type=int, default=8765, help="serve design/ here (when not baking headless)")
    ap.add_argument("--bake", action="store_true", help="bake everything into firmware/data/lump.bin with headless Chrome")
    ap.add_argument("--clip", metavar="NAME", help="bake only the sequences called NAME or NAME_*, as a clip ('all': every design/clips/*.js)")
    ap.add_argument("--sheet", metavar="PNG", help="with --clip: save its drawings as one picture instead of baking")
    ap.add_argument("--gif", metavar="GIF", help="with --clip: save it as an animated GIF instead of baking")
    ap.add_argument("--define", metavar="JSON", help="a file of extra sequences made from existing drawings")
    ap.add_argument("--out", help="where to write (default: lump.bin, or ~/.desk-buddy/clips/NAME.clip)")
    args = ap.parse_args()
    if args.clip:
        source = lambda n: os.path.join(DESIGN, "clips", n + ".js")
        names = sorted(os.path.basename(p)[:-3] for p in glob.glob(source("*"))) if args.clip == "all" else [args.clip]
        for name in names:
            config = {"define": json.load(open(args.define)) if args.define else {}, "only": [name]}
            out_path = args.out or os.path.join(CLIPS, name + ".clip")
            if args.sheet or args.gif: config["tint"] = "#FF7A45"; out_path = args.sheet or args.gif; finish = sheet if args.sheet else gif
            clip_name = name
            headless(config, [KIT, source(name)] if os.path.exists(source(name)) else [])
    elif args.bake:
        if args.out: out_path = args.out
        headless({})
    else:
        print(f"serving {DESIGN} on http://localhost:{args.port}", flush=True)
        http.server.ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
