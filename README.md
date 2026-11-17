# Lump, a desk buddy for Claude

<img src="design/media/lump-header.jpg" alt="Lump wearing sunglasses under a disco ball inside a small glass display" width="100%">

Lump is a little orange blob that sits on my desk and tells me what Claude is doing. He works while Claude works, waves when it needs me, and celebrates when it finishes.

I started with [Joe Bentham’s Lump](https://github.com/joebentham/desk-buddy). In this fork, I added a custom flipbook-style vector renderer that redraws each frame on the board from drawing instructions, a way to send him new animations over Wi-Fi, and a Claude Code skill that can make one and play it on the device. There are over 300 animations now, so depending on when you look over, he might be reacting to a failed test, having coffee, or riding a bike across the moon.

**[See Lump in action in the animation gallery.](https://csimpkins22.github.io/desk-buddy/)**

## What he does

- Shows whether Claude is working, waiting for you, or done. The counters in the corners tell you how many sessions are in each state.
- Goes from tangerine to red as you get close to your Claude usage limits.
- Reacts when you tap the glass. Sometimes it’s a banana peel. Sometimes it’s a cartoon bomb.
- Can also warn you about an upcoming meeting, hold up a letter for a Slack VIP, make little ocarina sounds, and go to sleep at night.

<img src="design/media/scenes-morning.jpg" alt="Lump waking up, drinking coffee, staring, and winking" width="100%">
<img src="design/media/scenes-daydream.jpg" alt="Lump bouncing to the moon, riding a rainbow, parachuting, and cycling" width="100%">
<img src="design/media/scenes-evening.jpg" alt="Lump sitting in flames, flipping a table, surviving a cartoon bomb, and stargazing" width="100%">

## Build one

You’ll need:

- A **Waveshare / SpotPear ESP32-S3-LCD-1.3** prism display. The one used here is the **ESP32-S3-LCD-1.3-C** option from [this listing](https://www.aliexpress.com/item/1005009887388696.html). Keep the stock case if you want to use the [3D-printable monitor mount](enclosure/print/).
- A **USB-C data cable** for the first flash.
- A **Mac** or a **Linux** computer to run the bridge, and **Claude Code**. On a Mac, the Claude desktop app is optional and adds Chat and Cowork status.
- **Python 3**, [PlatformIO](https://platformio.org/) (`brew install platformio`), and the Xcode command line tools (`xcode-select --install`).

```bash
git clone https://github.com/csimpkins22/desk-buddy.git
cd desk-buddy
```

1. **Flash the display.** Plug it in and run `tools/flash.sh`. The script paces the upload because this board’s USB-serial chip can drop bytes during a standard PlatformIO upload on macOS.
2. **Install the bridge.** Run `tools/install_bridge.sh`. On a Mac, it installs the bridge and a helper under `~/.desk-buddy`. If you use the Claude desktop app, allow `deskbuddy-appwatch` under **System Settings → Privacy & Security → Accessibility** so it can read Chat and Cowork status. On Linux, it installs the bridge as the systemd user service `deskbuddy-bridge`, which starts at login; Lump speaks through `pw-play`, `paplay` or `aplay`. Chat and Cowork status, and knowing whether Claude’s window is in front, are Mac-only. For the USB cable, add yourself to the `dialout` group (`sudo usermod -aG dialout $USER`) and log in again. The logs are in `~/.desk-buddy/bridge.log`, or `journalctl --user -u deskbuddy-bridge`.
3. **Connect Claude Code.** Run `python3 tools/install_hooks.py`. This backs up `~/.claude/settings.json`, keeps your existing hooks, and adds the session hooks and usage status line. Start a Claude Code session and Lump should wake up.
4. **Set up Wi-Fi if you want clips or wireless updates.** Leave the USB cable connected and run `~/.desk-buddy/venv/bin/python tools/wifi_setup.py`. Use a 2.4 GHz network. The script sends the password directly to the board over USB. Once Lump shows up as `lump.local`, you can power him from a USB charger or a monitor port and update him with `python3 tools/ota.py`.

### Calendar, Slack, and sound

The bridge picks up changes to `~/.desk-buddy/config.json` as you make them.

| Setting | What it does |
|---|---|
| `calendar_ics` | Takes a private iCal URL. Lump turns blue over the 10 minutes before a meeting and plays an alert. |
| `my_email` | Lets the calendar integration skip meetings you’ve declined. |
| `slack_token` | Takes a Slack user token (`xoxp-…`) from your private Slack app. DMs and mentions can trigger a paper plane. The read-only scopes are `im:read`, `im:history`, `mpim:read`, `mpim:history`, `users:read`, and `search:read`. |
| `slack_vips` | Takes names as they appear in Slack. Lump holds up a letter while a DM from one of them is unread. |
| `auto_clips` | Set to `false` to turn off automatic clip playback. |

Sound settings live in `~/.desk-buddy/sounds.json`. Set `"enabled": false` to mute him. For a second Claude Code profile, run `python3 tools/install_hooks.py --config-dir ~/.claude-work`.

### Lump on your Mac

There’s also a tiny version that sits by your MacBook’s notch, or in the menu bar on a Mac without one. It mirrors the desk display and hides when your desk monitor is connected and the physical Lump is online. Right-click to choose a desk monitor or select **All the Time**. You can also open **Lump** from Spotlight to get to that menu.

```bash
python3 tools/lumpbar/bake.py  # Requires Google Chrome
zsh tools/lumpbar/build.sh     # Builds and opens ~/Applications/Lump.app
```

To start it at login, add `Lump.app` under **System Settings → General → Login Items**.

### Lump on a Looking Glass

He can also live in 3D on a [Looking Glass Portrait](https://lookingglassfactory.com/), driven by a Raspberry Pi 5 running [HoloPi](https://github.com/kineticsystem/holo-pi). Each of his drawings is split into layers that sit at different depths, his shadow, the props behind him, his body, his face, and his arms and props in front, so his coffee mug comes out of the glass. He stands in a little grey room whose wall sits behind the glass, with his shadow cast on it, so the depth shows as you move your head.

Every animation he has, moods, transitions, gags, and clips, is baked into a quilt video, about 4 GB in all, and uploaded once to HoloPi's library. The bridge then plays them there as the board plays his frames, at once, with nothing uploaded again:

```bash
python3 tools/export_quilt.py --all   # Requires Google Chrome, FFmpeg, and ideally PyTorch with a GPU
python3 tools/holo_sync.py --url http://holo.local:8095
```

Then add `"holo_url": "http://holo.local:8095"` to `~/.desk-buddy/config.json`. The bridge drives the Looking Glass alongside the desk display, which doesn’t need to be there. The bridge reads the list of videos from `~/.desk-buddy/quilts/index.json`, so if it runs on another computer than the bake, copy that file there. Baking skips what hasn't changed, so after a new clip, run both commands again. One clip is enough to try it: `python3 tools/export_quilt.py coffee --upload holo.local`.

The videos can't show what the board draws from live data: his usage colour and the counters in the corners.

## Make him do something new

Open Claude Code in this repo and ask it to make Lump do something. “Make him juggle” is enough to get started. The `lump-clip` skill writes the clip, bakes it, and plays it on the display. Fast mode can get an idea onto the screen in about a minute. There’s also a mode that reviews every frame.

If you want to write one yourself, start with [`coffee.js`](design/clips/coffee.js) and the [clip guide](design/clips/README.md). [`design/clip_kit.js`](design/clip_kit.js) has props, faces, and camera moves you can reuse.

```bash
python3 tools/export_frames.py --clip coffee --sheet coffee.png
python3 tools/export_frames.py --clip coffee --gif coffee.gif
python3 tools/export_frames.py --clip all
curl -X POST --data coffee 127.0.0.1:47823/play
```

GIF export needs ffmpeg. Clips need Wi-Fi and an awake Mac. The display can keep up to 32 clips in memory until it restarts. The bridge can also play clips automatically when Claude finishes, runs tests, needs an answer, or sits idle.

## How the drawing works

Claude drew Lump in code. His face, transitions, and tap reactions all live in [`design/lump.html`](design/lump.html). When you bake the art, the exporter records the Canvas drawing calls and checks that the ESP32 renderer can reproduce each frame. Most frames fit in about a kilobyte, roughly an eighth of what they took as pixels. Frames the renderer can’t reproduce are stored as pixels. The board plays them at 24 fps.

To work on his core animations, start the preview and open <http://localhost:8767/lump.html#gags>. The page reloads when you save, and you can pause and step through the frames.

```bash
python3 tools/export_frames.py 8767
```

When you’re happy with the result, bake it with `python3 tools/export_frames.py --bake`. You’ll need Google Chrome and the Xcode command line tools. That writes `firmware/data/lump.bin`. Send it over Wi-Fi with `python3 tools/ota.py --art`, or use `tools/flash.sh` over USB.

## Where things live

| Path | What’s there |
|---|---|
| [`tools/buddy_bridge.py`](tools/buddy_bridge.py) | The bridge, on a Mac or Linux. It combines Claude’s session events and optional inputs into a state for the display. |
| [`tools/appwatch/`](tools/appwatch/) | The Swift helper that reads Chat and Cowork status. |
| [`firmware/src/main.cpp`](firmware/src/main.cpp) and [`firmware/src/vec.h`](firmware/src/vec.h) | The frame player, drawing code, and device protocol. |
| [`tools/export_frames.py`](tools/export_frames.py) | The frame exporter and renderer checks. |
| [`tools/export_quilt.py`](tools/export_quilt.py), [`tools/holo_sync.py`](tools/holo_sync.py) and [`tools/holo_driver.py`](tools/holo_driver.py) | Lump on a Looking Glass: the quilt videos in layers, their upload to HoloPi, and the bridge's routing there. |
| [`design/device/`](design/device/) | The Blender model and video scripts. The model uses the real dimensions of the prism and plays Lump’s actual animations on its screen. |
| [`enclosure/`](enclosure/) | The adjustable monitor mount and [ready-to-print files](enclosure/print/). |

The bridge exposes `http://127.0.0.1:47823/status` for debugging and `http://127.0.0.1:47823/play` for listing animations. You can run the bridge tests without the display.

```bash
~/.desk-buddy/venv/bin/python -m unittest discover -s tests
```

## Credit and license

Lump was created by [Joe Bentham](https://github.com/joebentham/desk-buddy). The code and 3D models are [MIT](LICENSE). The character, drawings, animations, and sounds are [CC BY-NC 4.0](LICENSE-ART.md). Credit Joe if you share your build or remix.
