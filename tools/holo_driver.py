"""Lump on a Looking Glass, through HoloPi: the board's routing, played with quilt videos.

The board plays Lump from his drawings and routes him itself: it finishes what it is playing, then walks
toward the mood the bridge asked for through the hand-drawn transitions, plays an event in its home mood,
and escalates by itself, needs-you ("alert") to "waiting" after 45 s, "working" to "longhaul" after 10
minutes. HoloPi only plays videos, so HoloDriver does that routing for it, with the quilt videos of
tools/export_quilt.py, which tools/holo_sync.py uploads, and their index.json: each step is one
PUT /api/display, a transition or an event played once, then the loop it arrives in, with HoloPi's then.

The bridge calls set_mood() when the mood changes, event() for an event or a clip, and tick() every second.
HoloLink sends the requests to HoloPi on a thread of its own, so that a slow or absent HoloPi never holds
the bridge up. What the board draws from live data, his colour and the counters in the corners, the
videos cannot show.
"""
import json, logging, os, queue, random, threading, time, urllib.request

log = logging.getLogger("holo")

FPS = 24
ESCALATE_S, LONGHAUL_S = 45, 10 * 60      # as the board's ESCALATE_MS and LONGHAUL_MS
# The mood whose drawings a mood borrows, and whose transitions it may take, as the board's BASE.
BASE = {"idle": "idle", "working": "working", "alert": "alert", "done": "done", "waiting": "alert",
        "longhaul": "working", "night": "idle", "meeting": "meeting", "awake": "awake", "letter": "letter"}
TAP_OWN = ("startle", "prep")             # the taps of the night and of a meeting, not picked at random
INDEX = os.path.expanduser("~/.desk-buddy/quilts/index.json")


class HoloDriver:
    """What HoloPi shows, step by step: `index`, index.json, says each video's file, kind, home mood and length;
    `send` takes the body of a PUT /api/display. Every method takes the time, `now`, in seconds."""

    def __init__(self, index, send, rng=random):
        self.index = index
        self.send = send
        self.rng = rng
        self.mood = None          # the mood the bridge asked for
        self.mood_since = 0
        self.loop = None          # the loop HoloPi plays, or will once the one-off before it has played
        self.busy_until = 0       # when the one-off on the screen ends
        self.pending = []         # events waiting for their turn

    # What the bridge says.

    def set_mood(self, mood, now):
        if mood != self.mood:
            self.mood, self.mood_since = mood, now
        self.step(now)

    def event(self, name, now):
        """An event or a clip, e.g. oops, coffee, tap or tap_plane: played in its home mood, once the one-off on
        the screen has played. Returns the sequence it plays, or None if there is none for it."""
        sequence = self.resolve(name)
        if sequence is None:
            log.info("no video for %s", name)
            return None
        self.pending.append(sequence)
        self.step(now)
        return sequence

    def tick(self, now):
        self.step(now)

    # The routing.

    def target(self, now):
        """The mood to head for: the bridge's, escalated as the board escalates it."""
        mood = self.mood or "idle"
        if mood == "alert" and now - self.mood_since >= ESCALATE_S and "waiting" in self.index:
            return "waiting"
        if mood == "working" and now - self.mood_since >= LONGHAUL_S and "longhaul" in self.index:
            return "longhaul"
        return mood if mood in self.index else BASE.get(mood, "idle")

    def resolve(self, name):
        """The sequence an event plays here, as the board picks it: a tap, a gag of his mood; a named gag, e.g.
        tap_plane, its version for his mood, or his base mood's."""
        if name in self.index and self.index[name]["kind"] == 2:
            return name
        mood = self.loop or self.target(time.time())
        if name == "tap":
            if mood == "night":
                gag = "startle"
            elif mood == "meeting":
                gag = "prep"
            else:
                gags = sorted({key.split("_")[1] for key in self.index if key.startswith("tap_") and key.count("_") >= 2
                               and key.split("_")[1] not in TAP_OWN})
                if not gags:
                    return None
                gag = self.rng.choice(gags)
            name = f"tap_{gag}"
        for candidate in (f"{name}_{mood}", f"{name}_{BASE.get(mood, mood)}"):
            if candidate in self.index:
                return candidate
        return None

    def next_hop(self, here, there):
        """The transition to take from loop `here` towards `there`, as the board's nextHop(), or None."""
        if f"{here}>{there}" in self.index:
            return f"{here}>{there}"
        if BASE.get(here, here) != here and f"{here}>{BASE[here]}" in self.index:
            return f"{here}>{BASE[here]}"
        if BASE.get(there, there) != there and f"{here}>{BASE[there]}" in self.index:
            return f"{here}>{BASE[there]}"
        return None

    def step(self, now):
        """Once the one-off on the screen has played: an event waiting, in its home mood, or the next step
        towards the mood."""
        if now < self.busy_until:
            return
        target = self.target(now)
        if self.loop is None:                     # the first time: his mood's loop, at once
            self._loop(target)
            return
        if self.pending:
            home = self.index[self.pending[0]]["from"]
            if self.loop == home:
                self._once(self.pending.pop(0), home, now)
                return
            target = home                         # first, to the event's home
        if self.loop == target:
            return
        hop = self.next_hop(self.loop, target)
        if hop is None:
            self._loop(target)                    # no drawn way there: straight to it
            return
        self._once(hop, self.index[hop]["to"], now)

    def _loop(self, mood):
        self.loop = mood
        self.send({"media_name": self.index[mood]["file"]})

    def _once(self, sequence, then, now):
        """Plays a transition or an event once, then the loop of `then`."""
        self.loop = then
        self.busy_until = now + self.index[sequence]["ticks"] / FPS
        self.send({"media_name": self.index[sequence]["file"], "then": {"media_name": self.index[then]["file"]}})


class HoloLink:
    """Sends PUT /api/display to HoloPi at `url`, e.g. http://holo.local:8095, on a thread of its own: the newest
    request first, the ones it replaces dropped, as only the last one matters to what is shown."""

    def __init__(self, url):
        self.url = url.rstrip("/")
        self.requests = queue.Queue()
        self.ok = None
        threading.Thread(target=self._run, name="holo", daemon=True).start()

    def send(self, body):
        self.requests.put(body)

    def _run(self):
        while True:
            body = self.requests.get()
            while not self.requests.empty():          # only the newest matters: it replaces what is shown
                body = self.requests.get_nowait()
            try:
                request = urllib.request.Request(f"{self.url}/api/display", data=json.dumps(body).encode(), method="PUT",
                                                 headers={"Content-Type": "application/json"})
                urllib.request.urlopen(request, timeout=10).read()
                if self.ok is not True:
                    log.info("HoloPi at %s plays %s", self.url, body["media_name"])
                self.ok = True
            except Exception as error:  # noqa: BLE001 - a HoloPi switched off must not stop the bridge.
                detail = error.read().decode(errors="replace").strip() if hasattr(error, "read") else str(error)
                if self.ok is not False:
                    log.info("HoloPi at %s: %s", self.url, detail)
                self.ok = False


def load_index(path=INDEX):
    """index.json of the quilt videos, or {} if there is none yet."""
    try:
        with open(path) as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}
