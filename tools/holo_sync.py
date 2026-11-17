"""Upload Lump's quilt videos to HoloPi's library: those it does not have, or has otherwise.

    python3 tools/holo_sync.py                        # to the HoloPi of config.json's holo_url
    python3 tools/holo_sync.py --url http://holo.local:8095
    python3 tools/holo_sync.py --prune                # and remove from HoloPi the videos of Lump no longer baked

The videos are those tools/export_quilt.py baked into ~/.desk-buddy/quilts, listed in its index.json. A video
HoloPi has under the same name and size is not sent again; one of another size, baked again, replaces it
there and keeps its id. Uploading changes nothing on the Looking Glass: the bridge shows them. With --prune,
a video of HoloPi whose name the index once had (synced.json remembers which) but no longer has is removed;
the other media of HoloPi are never touched.
"""
import argparse, json, os, sys, time, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_quilt import OUT  # noqa: E402

CONFIG = os.path.expanduser("~/.desk-buddy/config.json")


def call(method, url, body=None, headers=None, timeout=60):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        return json.loads(data) if data else None


def upload(url, path):
    """POST /api/media, without play: kept, not shown."""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as file:
        data = file.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: video/mp4\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", f"{url}/api/media", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 600)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="HoloPi, e.g. http://holo.local:8095; default: config.json's holo_url")
    parser.add_argument("--dir", default=OUT, help=f"the baked videos; default: {OUT}")
    parser.add_argument("--prune", action="store_true", help="remove from HoloPi the videos of Lump no longer baked")
    arguments = parser.parse_args()
    url = arguments.url
    if not url and os.path.exists(CONFIG):
        url = json.load(open(CONFIG)).get("holo_url")
    if not url:
        sys.exit("no HoloPi: give --url, or holo_url in ~/.desk-buddy/config.json")
    url = url.rstrip("/")
    index_path = os.path.join(arguments.dir, "index.json")
    if not os.path.exists(index_path):
        sys.exit(f"no {index_path}: bake the videos first, python3 tools/export_quilt.py --all")
    files = sorted({entry["file"] for entry in json.load(open(index_path)).values()})
    there = {item["name"]: item for item in call("GET", f"{url}/api/media")["items"]}

    start, sent, size = time.perf_counter(), 0, 0
    for number, name in enumerate(files, 1):
        path = os.path.join(arguments.dir, name)
        if not os.path.exists(path):
            print(f"  missing here: {name}", flush=True)
            continue
        if name in there and there[name]["size"] == os.path.getsize(path):
            continue
        medium = upload(url, path)
        sent, size = sent + 1, size + medium["size"]
        warning = f"; {medium['warning']}" if medium.get("warning") else ""
        print(f"  {number}/{len(files)} {name}: {medium['size'] / 1e6:.1f} MB, as {medium['id']}{warning}", flush=True)
    print(f"sent {sent} of {len(files)} videos, {size / 1e6:.0f} MB, in {time.perf_counter() - start:.0f} s; "
          f"the others were there", flush=True)

    synced_path = os.path.join(arguments.dir, "synced.json")
    synced = json.load(open(synced_path)) if os.path.exists(synced_path) else {}
    known = set(synced.get(url, [])) | set(files)
    if arguments.prune:
        gone = [item for name, item in there.items() if name in known and name not in files]
        for item in gone:
            call("DELETE", f"{url}/api/media/{item['id']}")
            print(f"  removed {item['name']}", flush=True)
        print(f"removed {len(gone)} videos of Lump no longer baked", flush=True)
        known = set(files)
    synced[url] = sorted(known)
    json.dump(synced, open(synced_path, "w"), indent=1)


if __name__ == "__main__":
    main()
