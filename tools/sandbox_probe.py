"""What a process can reach: run by tools/install_bridge_linux.sh inside the bridge's sandbox, to show it works.

Each line is something the bridge needs, which must say "yes", or something it must not do, which must say
"no". It tries only harmless things: listing folders, opening files to read, writing and removing a file of
its own, and asking HoloPi and Slack's api.test what they say.
"""
import glob, os, socket, urllib.request

HOME = os.environ.get("HOME", os.path.expanduser("~"))


def can(action):
    try:
        action()
        return True
    except Exception:  # noqa: BLE001 - any failure means it cannot.
        return False


def write_and_remove(path):
    with open(path, "w") as file:
        file.write("probe")
    os.remove(path)


def config_url():
    try:
        import json
        with open(os.path.join(HOME, ".desk-buddy", "config.json")) as file:
            return json.load(file).get("holo_url")
    except (OSError, ValueError):
        return None


CHECKS = [
    ("needs", "read and write ~/.desk-buddy", lambda: write_and_remove(os.path.join(HOME, ".desk-buddy", "probe"))),
    ("needs", "listen on 127.0.0.1", lambda: socket.create_server(("127.0.0.1", 0)).close()),
    ("needs", "the internet (Slack, your calendar)", lambda: urllib.request.urlopen("https://slack.com/api/api.test", timeout=8).read()),
    ("must not", "see ~/.ssh", lambda: os.listdir(os.path.join(HOME, ".ssh"))),
    ("must not", "read ~/.claude/settings.json", lambda: open(os.path.join(HOME, ".claude", "settings.json")).read()),
    ("must not", "read ~/.bash_history", lambda: open(os.path.join(HOME, ".bash_history")).read()),
    ("must not", "see the rest of your home", lambda: [None for name in os.listdir(HOME) if name != ".desk-buddy"][0]),
    ("must not", "write in your home", lambda: write_and_remove(os.path.join(HOME, "deskbuddy-probe"))),
    ("must not", "write in /etc", lambda: write_and_remove("/etc/deskbuddy-probe")),
    ("must not", "write in /usr", lambda: write_and_remove("/usr/deskbuddy-probe")),
    ("must not", "read /etc/shadow", lambda: open("/etc/shadow").read()),
    ("must not", "see other processes", lambda: [None for pid in os.listdir("/proc") if pid.isdigit() and int(pid) != os.getpid()
                                                 and os.path.exists(f"/proc/{pid}/cmdline") and open(f"/proc/{pid}/cmdline").read()][0]),
    ("must not", "open a disk", lambda: [open(path, "rb").close() for path in glob.glob("/dev/[sn][dv]*")][0]),
]
if config_url():
    CHECKS.insert(3, ("needs", "HoloPi", lambda: urllib.request.urlopen(config_url().rstrip("/") + "/api/display", timeout=5).read()))
if glob.glob(os.environ.get("XDG_RUNTIME_DIR", "/run/user/0") + "/pipewire-0"):
    CHECKS.insert(3, ("needs", "PipeWire, for his voice", lambda: socket.socket(socket.AF_UNIX).connect(
        os.environ["XDG_RUNTIME_DIR"] + "/pipewire-0")))

bad = 0
for kind, what, action in CHECKS:
    reached = can(action)
    good = reached if kind == "needs" else not reached
    bad += not good
    print(f"  {'ok  ' if good else 'FAIL'} {kind:8} {what}: {'yes' if reached else 'no'}")
print("  The sandbox works." if not bad else f"  {bad} checks failed: see above.")
raise SystemExit(1 if bad else 0)
