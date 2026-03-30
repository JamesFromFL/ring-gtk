# ring-gtk

Native GTK4 + libadwaita Linux desktop client for [Ring](https://ring.com) home security.

> **Status:** Early development — authentication and device listing work; camera feeds and arm/disarm controls are planned.

## Features

- Sign in with your Ring account (email + password + 2FA)
- View all Ring devices (doorbells, cameras, chimes, base stations)
- Real-time doorbell and motion notifications via desktop libnotify
- System tray icon (AyatanaAppIndicator3)
- Persistent token cache — only sign in once
- Follows GNOME HIG; adapts to light/dark system theme

## Planned

- [ ] Live camera feeds (GStreamer / WebRTC)
- [ ] Arm / disarm Ring Alarm
- [ ] Event history timeline
- [ ] Flatpak packaging

## Requirements

### System packages

PyGObject and the GTK/GNOME introspection libraries must be installed from
pacman — they cannot be installed via pip.

```bash
sudo pacman -S python-gobject gtk4 libadwaita libnotify
```

The systray icon requires `libayatana-appindicator`, which is in the AUR:

```bash
yay -S libayatana-appindicator
```

### Python dependencies (managed by uv)

- [`ring-doorbell[listen]`](https://github.com/tchellomello/python-ring-doorbell) ≥ 0.8

## Installation

```bash
# Clone
git clone https://github.com/JamesFromFL/ring-gtk
cd ring-gtk

# Expose system-installed PyGObject to the uv virtualenv
uv sync --system-site-packages

# Run
uv run ring-gtk
```

> `--system-site-packages` is required so the virtualenv can find
> `gi` (PyGObject) and the GObject introspection libraries installed
> via pacman. Without it, `import gi` will fail at runtime.

## Development

```bash
uv sync --system-site-packages --dev
uv run ruff check src tests
uv run pytest
```

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
