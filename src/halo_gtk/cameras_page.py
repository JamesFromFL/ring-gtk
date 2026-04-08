"""Cameras page — deterministic camera grid with size modes and drag-to-reorder."""

from __future__ import annotations

import io
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from halo_gtk import config as _cfg  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)

# Families that support snapshot capture.
_SNAPSHOT_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})

# Default tile dimensions (16:9) before any snapshot is loaded.
_DEFAULT_NATIVE_W = 320
_DEFAULT_NATIVE_H = 180
_FIXED_TILE_W = 16.0
_FIXED_TILE_H = 9.0

# Size mode → tiles per row.
_SIZE_MODES: dict[str, int] = {
    "small": 4,
    "medium": 2,
    "large": 1,
}

# Module-level drag state — safe for same-process DnD.
_dnd_src_id: int | None = None


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


async def _async_fetch_cached_snapshot(ring, device_id: int) -> bytes | None:
    try:
        from ring_doorbell.const import SNAPSHOT_ENDPOINT

        resp = await ring.async_query(SNAPSHOT_ENDPOINT.format(device_id))
        if resp.status_code == 200 and resp.content:
            return bytes(resp.content)
    except Exception as exc:
        _log.debug("Cached snapshot fetch failed for device %s: %s", device_id, exc)
    return None


def _make_grey_placeholder() -> bytes:
    try:
        from PIL import Image

        img = Image.new("RGB", (_DEFAULT_NATIVE_W, _DEFAULT_NATIVE_H), color=(48, 48, 48))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("placeholder creation failed: %s", exc)
        return b""


def _apply_motion_off_overlay(png_bytes: bytes) -> bytes:
    try:
        import subprocess  # noqa: PLC0415

        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont  # noqa: PLC0415

        font = None
        try:
            result = subprocess.run(
                ["fc-match", "--format=%{file}", "sans-serif:bold"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            font_path = result.stdout.strip()
            if font_path:
                font = ImageFont.truetype(font_path, 140)
        except Exception:
            pass
        if font is None:
            font = ImageFont.load_default()

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        for _ in range(3):
            img = img.filter(ImageFilter.GaussianBlur(radius=8))
        img = ImageEnhance.Brightness(img).enhance(0.75)
        draw = ImageDraw.Draw(img)
        text = "Motion Detection Off"
        w, h = img.size
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = (w - tw) // 2, (h - th) // 2
        for dx, dy in ((-4, -4), (4, -4), (-4, 4), (4, 4)):
            draw.text((x + dx, y + dy), text, fill=(0, 0, 0), font=font)
        draw.text((x, y), text, fill=(255, 255, 255), font=font)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("motion overlay failed: %s", exc)
        return png_bytes


def _make_dark_placeholder() -> bytes:
    """1280×720 near-black placeholder for motion-off cameras with no event frame."""
    try:
        from PIL import Image

        img = Image.new("RGB", (1280, 720), color=(26, 26, 26))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("dark placeholder creation failed: %s", exc)
        return b""


_TIMER_CSS_PROVIDER: Gtk.CssProvider | None = None


def _get_timer_css_provider() -> Gtk.CssProvider:
    global _TIMER_CSS_PROVIDER
    if _TIMER_CSS_PROVIDER is None:
        _TIMER_CSS_PROVIDER = Gtk.CssProvider()
        _TIMER_CSS_PROVIDER.load_from_string(
            ".snapshot-timer {"
            " background-color: rgba(0,0,0,0.5);"
            " border-radius: 4px;"
            " padding: 2px 6px;"
            " color: white;"
            "}"
        )
    return _TIMER_CSS_PROVIDER


async def _fetch_last_event_frame(client, device) -> bytes | None:
    """Return PNG bytes of the first decoded frame from the device's most
    recent recorded event, falling back to the cached snapshot endpoint."""
    try:
        import av  # noqa: PLC0415

        history = await device.async_history(limit=1)
        if not history:
            _log.debug("No history for %s — using cached snapshot", device.name)
            return await _async_fetch_cached_snapshot(client._ring, device.id)

        event_id = history[0].get("id")
        if event_id is None:
            return await _async_fetch_cached_snapshot(client._ring, device.id)

        url = await device.async_recording_url(event_id)
        if not url:
            _log.debug("No recording URL for event %s — using cached snapshot", event_id)
            return await _async_fetch_cached_snapshot(client._ring, device.id)

        container = av.open(url, options={"timeout": "5000000"})
        try:
            frame = next(container.decode(video=0))
            arr = frame.to_ndarray(format="rgb24")
        finally:
            container.close()

        from PIL import Image  # noqa: PLC0415

        out = io.BytesIO()
        Image.fromarray(arr).save(out, format="PNG")
        _log.debug("Decoded first frame from recording for %s", device.name)
        return out.getvalue()

    except Exception as exc:
        _log.debug("_fetch_last_event_frame failed for %s: %s", device.name, exc)
        return await _async_fetch_cached_snapshot(client._ring, device.id)


# ---------------------------------------------------------------------------
# AspectBox — custom Gtk.Widget that enforces a fixed h:w ratio via measure()
# ---------------------------------------------------------------------------


class AspectBox(Gtk.Widget):
    """A single-child widget that enforces a height-for-width aspect ratio.

    The parent grid queries each child's natural size via measure() before it
    decides row heights. By returning a fixed h = w * _h_ratio from
    do_measure(VERTICAL, for_size=allocated_width) we guarantee all tiles in a
    row share the same height regardless of the image's native resolution.

    The inner Gtk.Picture is managed via set_parent / unparent (GTK4 custom
    widget pattern) and receives the full allocation in do_size_allocate.
    """

    def __init__(self) -> None:
        super().__init__()
        self._h_ratio: float = _FIXED_TILE_H / _FIXED_TILE_W
        self._picture = Gtk.Picture(
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=True,
            hexpand=True,
            vexpand=True,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.FILL,
        )
        self._picture.set_parent(self)

    # ------------------------------------------------------------------
    # GTK4 vfunc overrides
    # ------------------------------------------------------------------

    def do_dispose(self) -> None:
        child = self._picture
        self._picture = None  # type: ignore[assignment]
        if child is not None:
            child.unparent()
        Gtk.Widget.do_dispose(self)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        """Return (minimum, natural, min_baseline, nat_baseline).

        HORIZONTAL: can shrink to 1 px; the grid controls the width.
        VERTICAL:   fixed height = for_size * _h_ratio so all row tiles
                    report the same height to the parent layout.
        """
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (1, 1, -1, -1)
        # VERTICAL
        if for_size <= 0:
            return (1, 1, -1, -1)
        fixed_h = max(1, int(for_size * self._h_ratio))
        return (fixed_h, fixed_h, -1, -1)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._picture is not None:
            self._picture.allocate(width, height, baseline, None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_ratio(self, h_ratio: float) -> None:
        """Update the height:width ratio and trigger a remeasure."""
        self._h_ratio = max(h_ratio, 0.01)
        self.queue_resize()

    def set_paintable(self, paintable) -> None:
        if self._picture is not None:
            self._picture.set_paintable(paintable)


# ---------------------------------------------------------------------------
# Camera tile widget
# ---------------------------------------------------------------------------


class CameraTile(Gtk.Frame):
    """A single camera tile in the camera grid."""

    def __init__(self, device, on_reorder, on_activate) -> None:
        super().__init__()
        self.device = device
        self._on_reorder = on_reorder
        self._on_activate = on_activate
        self._native_w = _DEFAULT_NATIVE_W
        self._native_h = _DEFAULT_NATIVE_H
        self._size_mode = "medium"
        self.set_focusable(True)
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.START)

        # Snapshot age timer state.
        self._snapshot_loaded_at: float = 0.0
        self._motion_detection_off: bool = False
        self._timer_source_id: int | None = None

        self.add_css_class("card")

        # Inner box — 5px padding on all sides.
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=5,
            margin_top=5,
            margin_bottom=5,
            margin_start=5,
            margin_end=5,
        )
        self.set_child(box)

        # AspectBox enforces the tile height via GTK4 measure().
        # The Gtk.Picture lives inside AspectBox, not directly in the tree.
        self.aspect_box = AspectBox()

        # Timer badge floats over the aspect_box.
        self._timer_label = Gtk.Label(
            css_classes=["caption", "numeric", "snapshot-timer"],
            halign=Gtk.Align.START,
            valign=Gtk.Align.END,
            margin_start=4,
            margin_bottom=4,
            visible=False,
        )
        self._timer_label.get_style_context().add_provider(
            _get_timer_css_provider(), Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        picture_overlay = Gtk.Overlay()
        picture_overlay.set_hexpand(True)
        picture_overlay.set_child(self.aspect_box)
        picture_overlay.add_overlay(self._timer_label)
        box.append(picture_overlay)

        # Camera name label, centered.
        self._name_label = Gtk.Label(
            label=device.name,
            halign=Gtk.Align.CENTER,
            ellipsize=3,  # PANGO_ELLIPSIZE_END
            css_classes=["heading"],
        )
        box.append(self._name_label)

        # Start the 1-second tick; it is a no-op while the timer label is hidden.
        self._timer_source_id = GLib.timeout_add_seconds(1, self._update_timer)

        # Hover state.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.add_css_class("activatable"))
        motion.connect("leave", lambda *_: self.remove_css_class("activatable"))
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect("released", self._on_click_released)
        self.add_controller(click)

        # Drag source — set module-level ID so the drop target can read it.
        drag_src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag_src.connect("prepare", self._on_drag_prepare)
        drag_src.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag_src)

        # Drop target — accepts a string (the source device ID).
        drop_tgt = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        drop_tgt.connect("drop", self._on_drop)
        self.add_controller(drop_tgt)

    # ------------------------------------------------------------------
    # Ratio update — called by CamerasPage when mode or snapshot changes
    # ------------------------------------------------------------------

    def update_ratio(self, mode: str) -> None:
        """Update the measured display box for the active size mode."""
        self._size_mode = mode
        if mode in {"small", "medium"}:
            self.aspect_box.set_ratio(_FIXED_TILE_H / _FIXED_TILE_W)
        else:
            self.aspect_box.set_ratio(self._native_h / self._native_w)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def set_snapshot(self, png_bytes: bytes, *, motion_detection_off: bool = False) -> None:
        """Update the thumbnail from raw PNG bytes (GTK main thread)."""
        try:
            from PIL import Image  # noqa: PLC0415

            with Image.open(io.BytesIO(png_bytes)) as im:
                self._native_w, self._native_h = im.size

            loader = GdkPixbuf.PixbufLoader()
            loader.write(png_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None:
                self.aspect_box.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        except Exception as exc:
            _log.debug("CameraTile.set_snapshot failed for %s: %s", self.device.name, exc)

        self._motion_detection_off = motion_detection_off
        if motion_detection_off:
            self._timer_label.set_visible(False)
        else:
            self._snapshot_loaded_at = time.monotonic()
            self._timer_label.set_visible(True)
            self._update_timer()

    # ------------------------------------------------------------------
    # Snapshot age timer
    # ------------------------------------------------------------------

    def _update_timer(self) -> bool:
        if not self._timer_label.get_visible():
            return GLib.SOURCE_CONTINUE
        elapsed = int(time.monotonic() - self._snapshot_loaded_at)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        if h > 0:
            text = f"{h}h {m}m {s}s"
        elif m > 0:
            text = f"{m}m {s}s"
        else:
            text = f"{s}s"
        self._timer_label.set_label(text)
        return GLib.SOURCE_CONTINUE

    def cleanup(self) -> None:
        if self._timer_source_id is not None:
            GLib.source_remove(self._timer_source_id)
            self._timer_source_id = None

    # ------------------------------------------------------------------
    # Drag source
    # ------------------------------------------------------------------

    def _on_drag_prepare(self, source, x, y):
        global _dnd_src_id
        _dnd_src_id = self.device.id
        return Gdk.ContentProvider.new_for_value(str(self.device.id))

    def _on_drag_begin(self, source, drag) -> None:
        paintable = Gtk.WidgetPaintable.new(self)
        source.set_icon(paintable, 0, 0)

    # ------------------------------------------------------------------
    # Drop target
    # ------------------------------------------------------------------

    def _on_drop(self, target, value, x, y) -> bool:
        global _dnd_src_id
        src_id = _dnd_src_id
        _dnd_src_id = None
        if src_id is None or src_id == self.device.id:
            return False
        self._on_reorder(src_id, self.device.id)
        return True

    def _on_click_released(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float
    ) -> None:
        if n_press == 1:
            self._on_activate(self.device)


# ---------------------------------------------------------------------------
# Live stream panel
# ---------------------------------------------------------------------------


class _LivePanel(Gtk.Box):
    """Full-width live stream panel with back button, controls, and embedded view."""

    def __init__(self, on_back, on_go_history) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_back = on_back
        self._on_go_history = on_go_history
        self._device = None

        top_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self.append(top_bar)

        back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back to cameras")
        back_btn.connect("clicked", lambda *_: self._on_back())
        top_bar.append(back_btn)

        self._title_label = Gtk.Label(
            label="",
            hexpand=True,
            halign=Gtk.Align.START,
            css_classes=["title-4"],
        )
        top_bar.append(self._title_label)

        vol_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
        )
        vol_box.append(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._vol_scale.set_value(1.0)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.set_size_request(80, -1)
        self._vol_scale.connect("value-changed", self._on_volume_changed)
        vol_box.append(self._vol_scale)
        top_bar.append(vol_box)

        screenshot_btn = Gtk.Button(
            icon_name="camera-photo-symbolic",
            tooltip_text="Save screenshot",
        )
        screenshot_btn.connect("clicked", self._on_screenshot)
        top_bar.append(screenshot_btn)

        history_btn = Gtk.Button(
            icon_name="document-open-recent-symbolic",
            tooltip_text="Event history for this camera",
        )
        history_btn.connect("clicked", self._on_history)
        top_bar.append(history_btn)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        from halo_gtk.live_stream import LiveStreamView

        self._live_view = LiveStreamView()
        self.append(self._live_view)

    def start_for_device(self, device) -> None:
        self._device = device
        self._title_label.set_label(device.name)
        self._vol_scale.set_value(1.0)
        self._live_view.start_for_device(device)

    def stop(self) -> None:
        self._live_view.stop()

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        self._live_view.set_volume(scale.get_value())

    def _on_screenshot(self, *_) -> None:
        png = self._live_view.get_current_frame_png()
        if png is None:
            _log.debug("No frame available for screenshot")
            return
        dest = Path.home() / "Pictures" / "halo-gtk"
        dest.mkdir(parents=True, exist_ok=True)
        fname = dest / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png"
        fname.write_bytes(png)
        _log.debug("Screenshot saved to %s", fname)

    def _on_history(self, *_) -> None:
        if self._device is not None:
            self._on_go_history(self._device.id)


# ---------------------------------------------------------------------------
# Cameras page
# ---------------------------------------------------------------------------


class CamerasPage(Gtk.Box):
    """Two-state cameras panel: camera grid ↔ embedded live stream."""

    def __init__(self, on_navigate_to_history=None, on_title_change=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_navigate_to_history = on_navigate_to_history
        self._on_title_change = on_title_change

        # device_id → CameraTile
        self._cards: dict[int, CameraTile] = {}
        # device_id → GLib source_id for 30-second fallback refresh.
        self._refresh_timers: dict[int, int] = {}
        # device_id → last good PNG bytes; survives grid rebuilds for cache-first display.
        self._snapshot_cache: dict[int, bytes] = {}

        cfg = _cfg.load()
        self._size_mode: str = cfg.get("camera_grid_size", "medium")
        if self._size_mode not in _SIZE_MODES:
            self._size_mode = "medium"
        self._order: list[int] = cfg.get("camera_order", [])

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
            hexpand=True,
            vexpand=True,
        )
        self.append(self._stack)

        # --- Grid page ---
        grid_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        self._stack.add_named(grid_box, "grid")

        toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        grid_box.append(toolbar)
        toolbar.append(Gtk.Box(hexpand=True))

        size_box = Gtk.Box(css_classes=["linked"], spacing=0)
        self._size_btns: dict[str, Gtk.ToggleButton] = {}
        first_btn = None
        for mode, label, tip in [
            ("small", "S", "Small tiles — 4 per row"),
            ("medium", "M", "Medium tiles — 2 per row"),
            ("large", "L", "Large tiles — 1 per row, full width"),
        ]:
            btn = Gtk.ToggleButton(label=label, tooltip_text=tip)
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.set_active(mode == self._size_mode)
            btn.connect("toggled", self._on_size_toggled, mode)
            size_box.append(btn)
            self._size_btns[mode] = btn

        toolbar.append(size_box)
        grid_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._status_page = Adw.StatusPage(
            icon_name="camera-video-symbolic",
            title="No cameras",
            description="Sign in to see your Ring cameras.",
            vexpand=True,
        )
        self._status_page.set_visible(True)
        grid_box.append(self._status_page)

        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            visible=False,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        grid_box.append(scroll)
        self._scroll = scroll

        self._grid = Gtk.Grid(
            column_spacing=5,
            row_spacing=5,
            margin_top=5,
            margin_bottom=5,
            margin_start=5,
            margin_end=5,
            hexpand=True,
            vexpand=False,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.START,
        )
        self._apply_size_mode_layout(self._size_mode)
        scroll.set_child(self._grid)

        # --- Live page ---
        self._live_panel = _LivePanel(
            on_back=self._show_grid,
            on_go_history=self._go_history,
        )
        self._stack.add_named(self._live_panel, "live")

    # ------------------------------------------------------------------
    # Size mode
    # ------------------------------------------------------------------

    def _on_size_toggled(self, button: Gtk.ToggleButton, mode: str) -> None:
        if not button.get_active():
            return
        self._size_mode = mode
        self._apply_size_mode_layout(mode)
        for tile in self._cards.values():
            tile.update_ratio(mode)
        cfg = _cfg.load()
        cfg["camera_grid_size"] = mode
        _cfg.save(cfg)

    def _apply_size_mode_layout(self, mode: str) -> None:
        self._grid.set_column_homogeneous(mode in {"small", "medium"})
        self._grid.set_row_homogeneous(False)
        self._rebuild_grid()

    # ------------------------------------------------------------------
    # Drag-and-drop reorder
    # ------------------------------------------------------------------

    def _on_reorder(self, src_id: int, dst_id: int) -> None:
        """Move the tile for *src_id* to the position of *dst_id*."""
        order = [did for did in self._order if did in self._cards]

        if src_id not in order or dst_id not in order:
            return

        src_idx = order.index(src_id)
        dst_idx = order.index(dst_id)
        order.pop(src_idx)
        order.insert(dst_idx, src_id)

        self._order = order
        self._rebuild_grid()
        cfg = _cfg.load()
        cfg["camera_order"] = order
        _cfg.save(cfg)

    # ------------------------------------------------------------------
    # Public refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-fetch device list and repopulate the grid."""
        client = get_client()
        if client is None or not client.is_authenticated:
            self._status_page.set_title("No cameras")
            self._status_page.set_description("Sign in to see your Ring cameras.")
            self._status_page.set_visible(True)
            self._scroll.set_visible(False)
            return

        self._status_page.set_title("Loading…")
        self._status_page.set_description("")
        self._status_page.set_visible(True)
        self._scroll.set_visible(False)
        self._clear_grid()
        self._show_grid()

        threading.Thread(target=self._fetch_and_populate, daemon=True).start()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_and_populate(self) -> None:
        client = get_client()
        try:
            client._run(client._ring.async_update_data())
            devices = [
                d
                for d in client.all_devices
                if (getattr(d, "family", None) or "other") in _SNAPSHOT_FAMILIES
            ]
            GLib.idle_add(self._populate_devices, devices)
        except Exception as exc:
            GLib.idle_add(self._show_fetch_error, str(exc))

    def _populate_devices(self, devices: list) -> bool:
        self._clear_grid()

        if not devices:
            self._status_page.set_title("No cameras found")
            self._status_page.set_description("No Ring cameras are linked to your account.")
            self._status_page.set_visible(True)
            self._scroll.set_visible(False)
            return GLib.SOURCE_REMOVE

        self._status_page.set_visible(False)
        self._scroll.set_visible(True)

        known = {did: i for i, did in enumerate(self._order)}
        ordered = sorted(
            devices,
            key=lambda d: (known.get(d.id, len(self._order)), d.name),
        )

        for device in ordered:
            tile = CameraTile(
                device,
                on_reorder=self._on_reorder,
                on_activate=self._show_live,
            )
            tile.update_ratio(self._size_mode)
            self._cards[device.id] = tile

            cached = self._snapshot_cache.get(device.id)
            if cached is not None:
                self._set_card_snapshot(device.id, cached)

            threading.Thread(
                target=self._load_snapshot,
                args=(device,),
                daemon=True,
            ).start()
            self._start_refresh_timer(device.id)

        client = get_client()
        if client is not None:
            client.add_event_callback(self._on_ring_event)

        self._order = [device.id for device in ordered]
        self._rebuild_grid()

        return GLib.SOURCE_REMOVE

    def _show_fetch_error(self, message: str) -> bool:
        self._status_page.set_title("Failed to load cameras")
        self._status_page.set_description(message)
        self._status_page.set_visible(True)
        self._scroll.set_visible(False)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Snapshot loading
    # ------------------------------------------------------------------

    def _load_snapshot(self, device) -> None:
        client = get_client()
        if client is None:
            return

        if not getattr(device, "motion_detection", True):
            png_bytes = client._run(_fetch_last_event_frame(client, device))
            base = bytes(png_bytes) if png_bytes else _make_dark_placeholder()
            img = _apply_motion_off_overlay(base)
            if img:
                GLib.idle_add(self._set_card_snapshot, device.id, img, True)
            return

        png_bytes: bytes | None = None
        try:
            png_bytes = client._run(device.async_get_snapshot())
        except Exception as exc:
            _log.debug("Snapshot fetch failed for %s: %s", device.name, exc)

        if not png_bytes and client._ring is not None:
            _log.debug("Trying cached snapshot fallback for %s", device.name)
            png_bytes = client._run(_async_fetch_cached_snapshot(client._ring, device.id))

        if png_bytes:
            GLib.idle_add(self._set_card_snapshot, device.id, bytes(png_bytes))
        else:
            _log.debug("No snapshot available for %s", device.name)

    def _set_card_snapshot(
        self, device_id: int, png_bytes: bytes, motion_off: bool = False
    ) -> bool:
        self._snapshot_cache[device_id] = png_bytes
        tile = self._cards.get(device_id)
        if tile is not None:
            tile.set_snapshot(png_bytes, motion_detection_off=motion_off)
            # Native dimensions are now known — update the AspectBox ratio.
            tile.update_ratio(self._size_mode)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # FCM event → snapshot refresh
    # ------------------------------------------------------------------

    def _on_ring_event(self, event) -> None:
        kind = getattr(event, "kind", None)
        if kind not in ("ding", "motion"):
            return
        device_id = getattr(event, "doorbot_id", None)
        if device_id is None or device_id not in self._cards:
            return
        tile = self._cards[device_id]
        _log.debug("Refreshing snapshot for %s after %s event", tile.device.name, kind)
        threading.Thread(
            target=self._load_snapshot,
            args=(tile.device,),
            daemon=True,
        ).start()
        self._start_refresh_timer(device_id)

    # ------------------------------------------------------------------
    # Tile activation → live stream
    # ------------------------------------------------------------------

    def _show_live(self, device) -> None:
        self._live_panel.start_for_device(device)
        self._stack.set_visible_child_name("live")
        if self._on_title_change is not None:
            self._on_title_change(device.name)

    def _show_grid(self) -> None:
        self._live_panel.stop()
        self._stack.set_visible_child_name("grid")
        if self._on_title_change is not None:
            self._on_title_change(None)

    def _go_history(self, device_id: int) -> None:
        self._show_grid()
        if self._on_navigate_to_history is not None:
            self._on_navigate_to_history(device_id)

    # ------------------------------------------------------------------
    # Fallback refresh timers
    # ------------------------------------------------------------------

    def _start_refresh_timer(self, device_id: int) -> None:
        self._cancel_refresh_timer(device_id)
        source_id = GLib.timeout_add_seconds(30, self._fallback_refresh, device_id)
        self._refresh_timers[device_id] = source_id

    def _cancel_refresh_timer(self, device_id: int) -> None:
        source_id = self._refresh_timers.pop(device_id, None)
        if source_id is not None:
            GLib.source_remove(source_id)

    def _cancel_all_refresh_timers(self) -> None:
        for source_id in self._refresh_timers.values():
            GLib.source_remove(source_id)
        self._refresh_timers.clear()

    def _fallback_refresh(self, device_id: int) -> bool:
        tile = self._cards.get(device_id)
        if tile is None:
            self._refresh_timers.pop(device_id, None)
            return GLib.SOURCE_REMOVE
        _log.debug("Fallback snapshot refresh for %s", tile.device.name)
        threading.Thread(
            target=self._load_snapshot,
            args=(tile.device,),
            daemon=True,
        ).start()
        self._refresh_timers.pop(device_id, None)  # clear stale id before re-arming
        self._start_refresh_timer(device_id)  # arm the next 30-second cycle
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_grid(self) -> None:
        self._cancel_all_refresh_timers()
        for tile in self._cards.values():
            tile.cleanup()
        self._cards.clear()
        while (child := self._grid.get_first_child()) is not None:
            self._grid.remove(child)

    def _rebuild_grid(self) -> None:
        while (child := self._grid.get_first_child()) is not None:
            self._grid.remove(child)

        children_per_line = _SIZE_MODES[self._size_mode]
        ordered_ids = [did for did in self._order if did in self._cards]

        for index, device_id in enumerate(ordered_ids):
            tile = self._cards[device_id]
            tile.update_ratio(self._size_mode)
            self._grid.attach(tile, index % children_per_line, index // children_per_line, 1, 1)
