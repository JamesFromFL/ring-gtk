# halo-gtk

<p align="center">
  <img src="https://raw.githubusercontent.com/JamesFromFL/halo-gtk/main/data/icons/hicolor/256x256/apps/io.github.JamesFromFL.HaloGtk.png" width="256" alt="halo-gtk icon"/>
</p>

Native GTK4 + libadwaita Linux desktop client for [Ring](https://ring.com) home security.

> **Status:** Active development — authentication, device listing, camera snapshot thumbnails, live camera feeds (WebRTC), and event history playback are all working.

## Roadmap

### Completed

- [x] Project scaffold — GTK4 + libadwaita, system tray, .desktop file, GNOME light/dark theme support
- [x] Ring API authentication — email, password, 2FA, token cache, device list, FCM real-time event listener
- [x] App icon — full hicolor set (256×256 down to 32×32), displayed in README
- [x] Camera snapshot thumbnails — loads on startup, auto-refreshes on FCM motion/ding event, 30-second fallback refresh
- [x] Live camera feed playback — WebRTC via aiortc, GStreamer gtk4paintablesink, audio via PipeWire, all Ring camera types supported
- [x] Nautilus-style sidebar navigation — Home page, Cameras grid view, Event History, collapsible sidebar
- [x] Camera grid view — snapshot thumbnails in a flow grid, click to expand to full-width live view with back navigation
- [x] Event History — two-panel layout, event list with camera filter, GStreamer video playback, scrubber, volume control, favourite, share, download, screenshot, and delete actions
- [x] Motion Detection Off overlay — blurred snapshot with "Motion Detection Off" label for cameras with motion detection disabled
- [x] Two-way audio through camera speakers from PC

### Planned

- [ ] Desktop notifications — wire FCM events to libnotify
- [ ] Systray state icons — blue (disarmed), red (armed), yellow (activity), with flashing states for alarm events
- [ ] Ring Alarm sensor status panel — contact sensors, motion sensors
- [ ] Arm / disarm Ring Alarm
- [ ] Recorded clip download and local storage
- [ ] AUR PKGBUILD for Arch Linux distribution
- [ ] Flatpak packaging for Flathub distribution

## Requirements

### System packages

PyGObject and the GTK/GNOME introspection libraries must be installed from
pacman — they cannot be installed via pip.

```bash
sudo pacman -S python-gobject gtk4 libadwaita libnotify libayatana-appindicator
```

### Python dependencies (managed by uv)

- [`ring-doorbell`](https://github.com/tchellomello/python-ring-doorbell) ≥ 0.8
- [`aiortc`](https://github.com/aiortc/aiortc) ≥ 1.14 — WebRTC for live camera feeds
- [`Pillow`](https://python-pillow.org/) ≥ 10.0 — snapshot overlays and screenshots

## Installation

```bash
# Clone
git clone https://github.com/JamesFromFL/halo-gtk
cd halo-gtk

# Create venv with access to system-installed PyGObject, then install deps
uv venv --system-site-packages
uv sync

# Run
uv run halo-gtk
```

> `uv venv --system-site-packages` is required so the virtualenv can find
> `gi` (PyGObject) and the GObject introspection libraries installed via
> pacman. `--system-site-packages` is a venv creation flag, not a sync flag.

## Development

```bash
uv venv --system-site-packages
uv sync
uv run ruff check src tests
uv run pytest
```

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
