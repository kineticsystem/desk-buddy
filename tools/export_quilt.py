"""Bake Lump as quilt videos for a Looking Glass, played by HoloPi: Lump in 3D, in layers, in a little room.

    python3 tools/export_quilt.py --all                  # every sequence: moods, transitions, events, clips
    python3 tools/export_quilt.py coffee                 # one clip, or one sequence, e.g. idle or idle>working
    python3 tools/export_quilt.py coffee --upload holo.local    # ...and upload it to HoloPi, and show it

The videos go into ~/.desk-buddy/quilts, or --out-dir: one per sequence, <name>_qs8x6a0.75.mp4, where a
transition's ">" is "-to-", e.g. idle-to-working_qs8x6a0.75.mp4, with index.json, which says each one's
kind (0 a mood's loop, 1 a transition, 2 an event or a clip), the moods it goes from and to, and its length.
tools/holo_sync.py uploads them to HoloPi, and the bridge plays them there, as the board plays his frames.
A sequence whose sources have not changed, the page, the clip kit, its clip and this script, is not baked
again: manifest.json keeps their fingerprint.

The page draws each tick once per layer, on a clear canvas: his shadow, the props behind him, his body, his
face, and what is in front of him (his arms, the props in his hands, the marks), with render()'s
opts.layer. Each layer lies at its own depth: each of the quilt's 48 views shifts it sideways by an amount
that grows with its depth, so that through the lenses his mug comes out of the glass and the props behind
him go into it. He is drawn at the quilt's size, not enlarged: the page scales every drawing.

He stands in a little grey room, which shows the depth: a wall well behind the glass, with seams and a
soft texture, since the lenses show depth only where something moves sideways between the views; a floor
that comes forward, from the wall to the glass under his feet and out of it, whose lines lean as one moves;
his shadow cast on the wall, which slides against him; and a dark shadow under his feet.

A quilt is a Portrait's: 8 x 6 views of 420 x 560, view 0, the leftmost camera, at the bottom left. A video
is HEVC at 24 frames per second, a tick a frame, as the board plays him, which a Raspberry Pi 5's hardware
decoder decodes. The views are composed on the GPU with PyTorch when it has one, CUDA or a Mac's MPS, else
with numpy, about a second a frame. It needs FFmpeg, with NVENC, VideoToolbox or libx265, and the Chrome of
tools/export_frames.py.
"""
import argparse, glob, hashlib, http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.request, uuid
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_frames as ef  # noqa: E402  The page, the clip kit, the clips and Chrome.

COLUMNS, ROWS, VIEW_W, VIEW_H = 8, 6, 420, 560
VIEWS = COLUMNS * ROWS
SIZE = VIEW_W                    # Lump's 240 x 240 drawing, at the quilt's size
FPS = 24
SUFFIX = "_qs8x6a0.75.mp4"       # the layout, in the name, as HoloPi reads it
OUT = os.path.expanduser("~/.desk-buddy/quilts")
LAYERS = ["shadow", "back", "body", "face", "front"]   # back to front, the order render() draws them in
# How far each layer lies from the screen, in pixels of a view: what the leftmost and rightmost views shift it
# by, either way. Positive comes out of the glass, negative goes into it.
DEPTHS = {"shadow": 0, "back": -20, "body": 0, "face": 5, "front": 16}
WALL = -36                       # the wall behind him
TOP = VIEW_H - SIZE              # his drawing's top in the view: he stands at its bottom
HORIZON = 400                    # where the floor meets the wall, in the view's rows
FEET = TOP + round(201 * SIZE / 240)   # the floor's row under his feet, y = 201 of his drawing: on the glass
WALL_GREY, FLOOR_GREY = (0.36, 0.37, 0.40), (0.45, 0.46, 0.48)
SHADOW_OFFSET = (34, -14)        # his shadow on the wall, from him, in pixels: lit from the upper left
SHADOW_BLUR, SHADOW_DARK = 7, 0.45

LAYER_JS = r"""
(async () => {
  const cfg = window.__holo, L = window.__lump, S = cfg.size / 240;
  L.setLine('glow');
  const cv = document.createElement('canvas'); cv.width = cv.height = cfg.size;
  const c = cv.getContext('2d', { willReadFrequently: true });
  // render() sets its transform for a 240 x 240 canvas: scale it, so that he is drawn at the quilt's size.
  const scaled = new Proxy(c, {
    get(t, p) {
      if (p === 'setTransform') return (a, b, cc, d, e, f) => t.setTransform(a * S, b * S, cc * S, d * S, e * S, f * S);
      const v = t[p]; return typeof v === 'function' ? v.bind(t) : v;
    },
    set(t, p, v) { t[p] = v; return true; },
  });
  const every = L.meta().seqs;
  const seqs = cfg.only ? every.filter(s => cfg.only.some(n => s.name === n || s.name.startsWith(n + '_'))) : every;
  const pixels = cfg.size * cfg.size * 4;
  await fetch('/seqs', { method: 'POST', body: JSON.stringify(seqs.map(s => ({ ...s, ticks: L.seqLen(s.key) }))) });
  for (const s of seqs) {
    const ticks = L.seqLen(s.key);
    await fetch(`/start?name=${encodeURIComponent(s.name)}`, { method: 'POST' });
    for (let t = 0; t < ticks; t++) {
      const a = s.kind === 0 ? L.at(s.key, t) : L.atSeq(s.key, t);
      const all = new Uint8Array(pixels * cfg.layers.length);   // the tick's layers, one after another
      cfg.layers.forEach((layer, k) => {
        c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, cfg.size, cfg.size);
        L.render(scaled, s.kind === 0 ? s.key : L.fxFor(a.id), a.id, t, { dim: false, layer });
        all.set(c.getImageData(0, 0, cfg.size, cfg.size).data, k * pixels);
      });
      await fetch('/tick', { method: 'POST', body: all });
    }
    await fetch('/end', { method: 'POST' });
  }
  await fetch('/finish', { method: 'POST' });
})().catch(e => fetch('/error', { method: 'POST', body: String(e && e.stack || e) }));
"""


def file_name(sequence):
    """The video of a sequence: its name, a transition's ">" as "-to-", and the layout."""
    return sequence.replace(">", "-to-") + SUFFIX


# ---- The room, and the composition of a quilt.

def floor_depth(row):
    """The floor's depth at a row of the view: the wall's at the horizon, the glass's under his feet, out of it
    below, evenly, as a plane seen from the front: its lines stay straight in every view."""
    return WALL + (row - HORIZON) * (0 - WALL) / (FEET - HORIZON)


def side_of(view):
    """Where a view's camera is: -1, the leftmost, to 1, the rightmost."""
    return (view - (VIEWS - 1) / 2) / ((VIEWS - 1) / 2)


def room():
    """The room as each of the 48 views sees it, without his shadow: the wall shifted by its depth, the floor
    row by row by its own, so that its lines lean. 48 x VIEW_H x VIEW_W x 3, drawn once: it does not move."""
    rng = np.random.default_rng(7)
    reach = abs(WALL) + round(abs(floor_depth(VIEW_H)))   # the widest shift, the wall's or the floor's front
    width = VIEW_W + 2 * reach                # wider than a view, so that what a shift brings in is room too
    blotches = Image.fromarray((rng.random((VIEW_H // 8, width // 8)) * 255).astype(np.uint8))
    blotches = np.asarray(blotches.resize((width, VIEW_H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(10)),
                          np.float32) / 255
    wall = np.ones((VIEW_H, width, 3), np.float32) * WALL_GREY * (0.92 + 0.16 * blotches[..., None])
    for x in range(reach % 84, width, 84):      # the seams of the wall's panels
        wall[:, x:x + 2] *= 0.82
    floor = np.ones((VIEW_H, width, 3), np.float32) * FLOOR_GREY
    # Boards running into the room, towards the middle of the wall: smooth lines, drawn twice as large.
    lines = Image.new("L", (width * 2, VIEW_H * 2), 0)
    draw = ImageDraw.Draw(lines)
    for k in range(-16, 17):
        draw.line([(width + k * 46 * 0.2 * 2, HORIZON * 2), (width + k * 46 * 2, VIEW_H * 2)], fill=255, width=3)
    lines = np.asarray(lines.resize((width, VIEW_H), Image.LANCZOS), np.float32)[..., None] / 255
    floor *= 1 - 0.18 * lines
    floor[HORIZON:] *= np.linspace(0.85, 1.1, VIEW_H - HORIZON)[:, None, None]   # lighter towards us
    views = np.empty((VIEWS, VIEW_H, VIEW_W, 3), np.float32)
    for view in range(VIEWS):
        side = side_of(view)
        x = reach - round(-side * WALL)
        views[view, :HORIZON] = wall[:HORIZON, x:x + VIEW_W]
        for row in range(HORIZON, VIEW_H):
            x = reach - round(-side * floor_depth(row))
            views[view, row] = floor[row, x:x + VIEW_W]
        views[view, HORIZON - 2:HORIZON] *= 0.6   # the skirting, where the floor meets the wall
    return views


def shift(lib, image, dx):
    """The image moved dx pixels sideways, what comes in transparent: lib is numpy or torch."""
    out = lib.zeros_like(image)
    if dx > 0:
        out[:, dx:] = image[:, :-dx]
    elif dx < 0:
        out[:, :dx] = image[:, -dx:]
    else:
        out[:] = image
    return out


class Composer:
    """Composes a tick's layers into a quilt: on the GPU with PyTorch if there is one, else with numpy."""

    def __init__(self):
        self.torch = None
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else None
            if device:
                self.torch, self.device = torch, torch.device(device)
        except ImportError:
            pass
        rooms = room()
        if self.torch:
            self.rooms = self.torch.tensor(rooms, device=self.device)
            radius = SHADOW_BLUR * 3
            kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / SHADOW_BLUR) ** 2)
            self.kernel = self.torch.tensor(kernel / kernel.sum(), dtype=self.torch.float32, device=self.device)
        else:
            self.rooms = rooms
        self.where = f"the GPU, {self.device}" if self.torch else "the processor, numpy"

    def quilt(self, layers):
        """layers: len(LAYERS) x SIZE x SIZE x 4, uint8 RGBA. Returns the quilt, ROWS*VIEW_H x COLUMNS*VIEW_W x 3."""
        return self._torch(layers) if self.torch else self._numpy(layers)

    def _compose(self, lib, rgba, cast, quilt):
        """The 48 views: the room, darkened by his shadow on the wall, then his layers, each shifted by its depth."""
        for view in range(VIEWS):
            side = side_of(view)
            out = self.rooms[view] * (1 - SHADOW_DARK * shift(lib, cast, round(-side * WALL))[..., None])
            for index, name in enumerate(LAYERS):
                # Seen from the left, what is in front of the screen moves right, what is behind it left.
                layer = shift(lib, rgba[index], round(-side * DEPTHS[name]))
                alpha = layer[..., 3:]
                out = layer[..., :3] * alpha + out * (1 - alpha)
            row, column = ROWS - 1 - view // COLUMNS, view % COLUMNS
            quilt[row * VIEW_H:(row + 1) * VIEW_H, column * VIEW_W:(column + 1) * VIEW_W] = out

    def _torch(self, layers):
        torch = self.torch
        rgba = torch.zeros((len(LAYERS), VIEW_H, VIEW_W, 4), device=self.device)
        rgba[:, TOP:] = torch.from_numpy(layers.copy()).to(self.device).float() / 255
        # His shadow on the wall: his outline, everything of him but what is behind him, blurred, offset.
        him = rgba[[LAYERS.index(n) for n in ("body", "face", "front")], ..., 3].amax(0)
        k = self.kernel
        blurred = torch.nn.functional.conv2d(him[None, None], k[None, None, None, :], padding=(0, len(k) // 2))
        blurred = torch.nn.functional.conv2d(blurred, k[None, None, :, None], padding=(len(k) // 2, 0))[0, 0]
        cast = torch.zeros_like(blurred)
        dx, dy = SHADOW_OFFSET
        cast[max(0, dy):VIEW_H + min(0, dy), dx:] = blurred[max(0, -dy):VIEW_H - max(0, dy), :VIEW_W - dx]
        cast[HORIZON:] = 0                       # on the wall only
        shadow = rgba[LAYERS.index("shadow")]    # under his feet: dark, from the page's faint light one
        shadow[..., :3] = 0
        shadow[..., 3] = (shadow[..., 3] * 5).clamp(max=1)
        quilt = torch.empty((ROWS * VIEW_H, COLUMNS * VIEW_W, 3), device=self.device)
        self._compose(torch, rgba, cast, quilt)
        return (quilt * 255 + 0.5).clamp(0, 255).to(torch.uint8).cpu().numpy()

    def _numpy(self, layers):
        rgba = np.zeros((len(LAYERS), VIEW_H, VIEW_W, 4), np.float32)
        rgba[:, TOP:] = layers.astype(np.float32) / 255
        him = rgba[[LAYERS.index(n) for n in ("body", "face", "front")], ..., 3].max(0)
        blurred = Image.fromarray((him * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
        cast = np.roll(shift(np, np.asarray(blurred, np.float32) / 255, SHADOW_OFFSET[0]), SHADOW_OFFSET[1], axis=0)
        cast[HORIZON:] = 0
        shadow = rgba[LAYERS.index("shadow")]
        shadow[..., :3] = 0
        shadow[..., 3] = np.minimum(1, shadow[..., 3] * 5)
        quilt = np.empty((ROWS * VIEW_H, COLUMNS * VIEW_W, 3), np.float32)
        self._compose(np, rgba, cast, quilt)
        return (quilt * 255 + 0.5).clip(0, 255).astype(np.uint8)


def encoder():
    names = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "hevc_nvenc" in names:
        return ["-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
    if "hevc_videotoolbox" in names:             # a Mac's
        return ["-c:v", "hevc_videotoolbox", "-q:v", "60"]
    return ["-c:v", "libx265", "-crf", "20", "-x265-params", "log-level=error"]


# ---- The page, in headless Chrome: it draws the layers; the server composes and encodes them as they come.

class Baker(http.server.BaseHTTPRequestHandler):
    page = b""
    composer = None
    out_dir = OUT
    codec = []
    seqs = []
    current = None    # [name, ffmpeg, ticks so far, started]
    baked = []
    done = threading.Event()
    error = None

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(self.page)

    def do_POST(self):
        url = urlparse(self.path); query = parse_qs(url.query)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            if url.path == "/seqs":
                Baker.seqs = json.loads(body)
            elif url.path == "/start":
                name = query["name"][0]
                path = os.path.join(Baker.out_dir, file_name(name))
                ffmpeg = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                           "-s", f"{COLUMNS * VIEW_W}x{ROWS * VIEW_H}", "-r", str(FPS), "-i", "-",
                                           *Baker.codec, "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-movflags",
                                           "+faststart", path + ".part.mp4"], stdin=subprocess.PIPE)
                Baker.current = [name, ffmpeg, 0, time.perf_counter()]
            elif url.path == "/tick":
                layers = np.frombuffer(body, np.uint8).reshape(len(LAYERS), SIZE, SIZE, 4)
                Baker.current[1].stdin.write(Baker.composer.quilt(layers).tobytes())
                Baker.current[2] += 1
            elif url.path == "/end":
                name, ffmpeg, ticks, started = Baker.current
                ffmpeg.stdin.close()
                if ffmpeg.wait():
                    raise RuntimeError(f"FFmpeg failed on {name}")
                path = os.path.join(Baker.out_dir, file_name(name))
                os.replace(path + ".part.mp4", path)
                Baker.baked.append(name)
                print(f"  {file_name(name)}: {ticks} frames, {os.path.getsize(path) / 1e6:.1f} MB, "
                      f"{time.perf_counter() - started:.0f} s", flush=True)
            elif url.path == "/error":
                Baker.error = body.decode(errors="replace"); Baker.done.set()
            elif url.path == "/finish":
                Baker.done.set()
        except Exception as error:  # noqa: BLE001 - stop the page, and say why.
            Baker.error = f"{type(error).__name__}: {error}"; Baker.done.set()
        self.send_response(200); self.end_headers()


def run_page(page, handler, timeout):
    """Serves the page to headless Chrome with this handler until handler.done, or the timeout."""
    handler.page = page
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="lump-quilt-")
    chrome = subprocess.Popen([ef.CHROME, "--headless=new", "--disable-gpu", *(["--no-sandbox"] if sys.platform == "linux" else []),
                               "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile}", f"http://127.0.0.1:{port}/"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not handler.done.wait(timeout):
            sys.exit("Chrome didn't finish")
    finally:
        chrome.terminate(); chrome.wait(10); server.shutdown(); shutil.rmtree(profile, ignore_errors=True)


def page_with(scripts):
    report = "<script>addEventListener('error', e => fetch('/error', { method: 'POST', body: e.message + ' (line ' + e.lineno + ')' }));</script>"
    return open(os.path.join(ef.DESIGN, "lump.html")).read() + report + "".join(f"<script>{open(p).read()}</script>" for p in scripts)


def bake_page(scripts, only, out_dir, composer, codec):
    """Loads the page with these clip scripts, bakes its sequences (only those named, or NAME_*, if given).
    Returns the sequences baked, as the page describes them."""
    config = {"size": SIZE, "layers": LAYERS, "only": only}
    Baker.composer, Baker.out_dir, Baker.codec = composer, out_dir, codec
    Baker.seqs, Baker.baked, Baker.error, Baker.current = [], [], None, None
    Baker.done = threading.Event()
    run_page((page_with(scripts) + f"<script>window.__holo = {json.dumps(config)};{LAYER_JS}</script>").encode(),
             Baker, 6 * 3600)
    if Baker.error:
        sys.exit("the page stopped with an error: " + Baker.error)
    return [s for s in Baker.seqs if s["name"] in Baker.baked]


class Lister(http.server.BaseHTTPRequestHandler):
    page = b""
    names = []
    done = threading.Event()

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(self.page)

    def do_POST(self):
        Lister.names = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        Lister.done.set(); self.send_response(200); self.end_headers()


def board_sequences():
    """The names of the board's own sequences, with no clip loaded: moods, transitions, fidgets, taps."""
    script = "<script>fetch('/', { method: 'POST', body: JSON.stringify(window.__lump.meta().seqs.map(s => s.name)) })</script>"
    run_page((page_with([ef.KIT]) + script).encode(), Lister, 120)
    return Lister.names


def jobs(names, everything):
    """What to bake, as (job, scripts, only): the board's own sequences in one page, and each clip in a page of
    its own, as export_frames.py bakes them."""
    clips = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(ef.DESIGN, "clips", "*.js")))
    if everything:
        yield "board", [ef.KIT], board_sequences()
        for clip in clips:
            yield clip, [ef.KIT, os.path.join(ef.DESIGN, "clips", f"{clip}.js")], [clip]
        return
    for name in names:
        if name in clips:
            yield name, [ef.KIT, os.path.join(ef.DESIGN, "clips", f"{name}.js")], [name]
        else:
            yield name, [ef.KIT], [name]


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in paths:
        with open(path, "rb") as file:
            digest.update(file.read())
    return digest.hexdigest()[:16]


def upload(path, host, play=True):
    """Uploads a video to HoloPi's library, and shows it with play: POST /api/media. Returns its answer."""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as file:
        data = file.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"play\"\r\n\r\n{str(play).lower()}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: video/mp4\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(f"http://{host}:8095/api/media", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="clips or sequences, e.g. coffee, idle, idle>working")
    parser.add_argument("--all", action="store_true", help="every sequence: the board's own and every clip's")
    parser.add_argument("--out-dir", default=OUT, help=f"where the videos go; default: {OUT}")
    parser.add_argument("--force", action="store_true", help="bake again what has not changed")
    parser.add_argument("--upload", metavar="HOST", help="upload what was baked to HoloPi on HOST, and show the last")
    arguments = parser.parse_args()
    if not arguments.names and not arguments.all:
        parser.error("name a clip or a sequence, or give --all")
    os.makedirs(arguments.out_dir, exist_ok=True)
    manifest_path = os.path.join(arguments.out_dir, "manifest.json")
    index_path = os.path.join(arguments.out_dir, "index.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    composer, codec = Composer(), encoder()
    print(f"composing on {composer.where}, encoding with {codec[1]}", flush=True)
    start, baked = time.perf_counter(), []
    for job, scripts, only in jobs(arguments.names, arguments.all):
        sources = fingerprint([os.path.join(ef.DESIGN, "lump.html"), os.path.abspath(__file__), *scripts])
        files = manifest.get(job, {}).get("files", [])
        if (not arguments.force and manifest.get(job, {}).get("sources") == sources and files
                and all(os.path.exists(os.path.join(arguments.out_dir, f)) for f in files)):
            continue
        print(f"{job}:", flush=True)
        sequences = bake_page(scripts, only, arguments.out_dir, composer, codec)
        for s in sequences:
            index[s["name"]] = {"file": file_name(s["name"]), "kind": s["kind"], "from": s["from"], "to": s["to"],
                                "ticks": s["ticks"], "job": job}
        manifest[job] = {"sources": sources, "files": [file_name(s["name"]) for s in sequences]}
        baked += [s["name"] for s in sequences]
        json.dump(manifest, open(manifest_path, "w"), indent=1)
        json.dump(index, open(index_path, "w"), indent=1)
    print(f"{len(baked)} videos baked in {time.perf_counter() - start:.0f} s, into {arguments.out_dir}", flush=True)
    if arguments.upload and baked:
        for number, name in enumerate(baked):
            medium = upload(os.path.join(arguments.out_dir, file_name(name)), arguments.upload, number == len(baked) - 1)
        print(f"uploaded {len(baked)} to {arguments.upload}; showing {medium['name']}")


if __name__ == "__main__":
    main()
