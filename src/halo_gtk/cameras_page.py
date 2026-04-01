"""Cameras page — responsive FlowBox camera grid with size modes and drag-to-reorder."""

from __future__ import annotations

import io
import logging
import threading
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
# Small placeholder so tiles impose no hard minimum on the window before a
# real snapshot arrives.
_DEFAULT_NATIVE_W = 320
_DEFAULT_NATIVE_H = 180

# Size mode: name → (fraction of native width, min tiles per row)
_SIZE_MODES: dict[str, tuple[float, int]] = {
    "small": (0.25, 4),
    "medium": (0.50, 2),
    "large": (1.00, 1),
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
        from PIL import Image, ImageDraw, ImageFilter

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        img = img.filter(ImageFilter.GaussianBlur(radius=4))
        draw = ImageDraw.Draw(img)
        text = "Motion Detection Off"
        w, h = img.size
        bbox = draw.textbbox((0, 0), text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = (w - tw) // 2, (h - th) // 2
        draw.text((x + 1, y + 1), text, fill=(0, 0, 0))
        draw.text((x, y), text, fill=(255, 255, 255))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("motion overlay failed: %s", exc)
        return png_bytes


# ---------------------------------------------------------------------------
# Camera tile widget
# ---------------------------------------------------------------------------


class CameraTile(Gtk.FlowBoxChild):
    """A single camera tile in the FlowBox grid."""

    def __init__(self, device, on_reorder) -> None:
        super().__init__()
        self.device = device
        self._on_reorder = on_reorder
        self._native_w = _DEFAULT_NATIVE_W
        self._native_h = _DEFAULT_NATIVE_H
        self._current_mode = "medium"
        self.set_focusable(True)
        # Prevent tiles from stretching beyond their size_request in the FlowBox.
        self.set_hexpand(False)
        # Track last requested width to avoid redundant queue_resize calls from
        # set_size_request() inside the FlowBox size-allocate handler.
        self._req_w: int = 0

        # Card frame.
        self._frame = Gtk.Frame(css_classes=["card"])
        self.set_child(self._frame)

        # Inner box — 5px padding on all sides.
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=5,
            margin_top=5,
            margin_bottom=5,
            margin_start=5,
            margin_end=5,
        )
        self._frame.set_child(box)

        # Snapshot picture at native aspect ratio.
        self._picture = Gtk.Picture(
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=True,
            hexpand=True,
        )
        box.append(self._picture)

        # Camera name label, centered.
        self._name_label = Gtk.Label(
            label=device.name,
            halign=Gtk.Align.CENTER,
            ellipsize=3,  # PANGO_ELLIPSIZE_END
            css_classes=["heading"],
        )
        box.append(self._name_label)

        # Hover state.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._frame.add_css_class("activatable"))
        motion.connect("leave", lambda *_: self._frame.remove_css_class("activatable"))
        self._frame.add_controller(motion)

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
    # Sizing
    # ------------------------------------------------------------------

    def apply_size_mode(self, mode: str) -> None:
        """Store the new size mode; actual pixel width is set by the FlowBox
        size-allocate callback so the tile never imposes a hard minimum on the
        window width."""
        self._current_mode = mode
        self._set_req_width(1)

    def _set_req_width(self, w: int) -> None:
        """Call set_size_request only when the value changes to avoid an
        infinite layout loop from the size-allocate handler."""
        if w != self._req_w:
            self._req_w = w
            self.set_size_request(w, -1)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def set_snapshot(self, png_bytes: bytes) -> None:
        """Update the thumbnail from raw PNG bytes (GTK main thread)."""
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(png_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None:
                self._native_w = pixbuf.get_width()
                self._native_h = pixbuf.get_height()
                self._picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
                # Re-apply size mode now that we have real dimensions.
                self.apply_size_mode(self._current_mode)
        except Exception as exc:
            _log.debug("CameraTile.set_snapshot failed for %s: %s", self.device.name, exc)

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

        # --- top bar ---
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

        # Volume slider.
        vol_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
        )
        vol_box.append(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._vol_scale.set_value(1.0)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.set_size_request(120, -1)
        self._vol_scale.connect("value-changed", self._on_volume_changed)
        vol_box.append(self._vol_scale)
        top_bar.append(vol_box)

        # Screenshot button.
        screenshot_btn = Gtk.Button(
            icon_name="camera-photo-symbolic",
            tooltip_text="Save screenshot",
        )
        screenshot_btn.connect("clicked", self._on_screenshot)
        top_bar.append(screenshot_btn)

        # Event history button.
        history_btn = Gtk.Button(
            icon_name="document-open-recent-symbolic",
            tooltip_text="Event history for this camera",
        )
        history_btn.connect("clicked", self._on_history)
        top_bar.append(history_btn)

        # Separator.
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Embedded live stream view.
        from halo_gtk.live_stream import LiveStreamView

        self._live_view = LiveStreamView()
        self.append(self._live_view)

    # ------------------------------------------------------------------

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
    """Two-state cameras panel: responsive FlowBox grid ↔ embedded live stream."""

    def __init__(self, on_navigate_to_history=None, on_title_change=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_navigate_to_history = on_navigate_to_history
        self._on_title_change = on_title_change

        # device_id → CameraTile
        self._cards: dict[int, CameraTile] = {}
        # device_id → GLib source_id for 30-second fallback refresh.
        self._refresh_timers: dict[int, int] = {}

        # Restore persisted preferences.
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

        # In-page toolbar: size mode toggle buttons on the right.
        toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        grid_box.append(toolbar)

        # Spacer pushes buttons to the right.
        toolbar.append(Gtk.Box(hexpand=True))

        size_box = Gtk.Box(css_classes=["linked"], spacing=0)

        self._size_btns: dict[str, Gtk.ToggleButton] = {}
        first_btn = None
        for mode, label, tip in [
            ("small", "S", "Small tiles — 4 per row minimum"),
            ("medium", "M", "Medium tiles — 2 per row minimum"),
            ("large", "L", "Large tiles — 1 per row minimum"),
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

        # Status page shown when list is empty or loading.
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
        )
        grid_box.append(scroll)
        self._scroll = scroll

        _, min_per_line = _SIZE_MODES[self._size_mode]
        self._flow_box = Gtk.FlowBox(
            column_spacing=5,
            row_spacing=5,
            margin_top=5,
            margin_bottom=5,
            margin_start=5,
            margin_end=5,
            homogeneous=False,
            selection_mode=Gtk.SelectionMode.NONE,
        )
        self._flow_box.set_min_children_per_line(min_per_line)
        self._flow_box.set_max_children_per_line(100)
        self._flow_box.connect("child-activated", self._on_child_activated)
        scroll.set_child(self._flow_box)

        # --- Live page ---
        self._live_panel = _LivePanel(
            on_back=self._show_grid,
            on_go_history=self._go_history,
        )
        self._stack.add_named(self._live_panel, "live")

    # ------------------------------------------------------------------
    # Size allocation — drives dynamic tile widths
    # ------------------------------------------------------------------

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        """Recompute tile widths on every allocation so tiles fill available
        space without imposing a hard minimum on the window."""
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        self._recalc_tile_widths(width)

    def _recalc_tile_widths(self, available_width: int) -> None:
        if not self._cards:
            return
        fraction, min_per_line = _SIZE_MODES[self._size_mode]
        # FlowBox has margin_start/end of 5 px each and column_spacing of 5 px.
        inner_w = available_width - 10
        gap_total = max(0, min_per_line - 1) * 5
        tile_w = max(1, (inner_w - gap_total) // min_per_line)
        for tile in self._cards.values():
            max_w = max(1, int(tile._native_w * fraction))
            tile._set_req_width(min(tile_w, max_w))

    # ------------------------------------------------------------------
    # Size mode
    # ------------------------------------------------------------------

    def _on_size_toggled(self, button: Gtk.ToggleButton, mode: str) -> None:
        if not button.get_active():
            return
        self._size_mode = mode
        _, min_per_line = _SIZE_MODES[mode]
        self._flow_box.set_min_children_per_line(min_per_line)
        for tile in self._cards.values():
            tile.apply_size_mode(mode)
        # Recalculate immediately; do_size_allocate only fires if the window
        # width changes, but the mode change needs an instant tile resize.
        self._recalc_tile_widths(self.get_width())
        cfg = _cfg.load()
        cfg["camera_grid_size"] = mode
        _cfg.save(cfg)

    # ------------------------------------------------------------------
    # Drag-and-drop reorder
    # ------------------------------------------------------------------

    def _on_reorder(self, src_id: int, dst_id: int) -> None:
        """Move the tile for *src_id* to the position of *dst_id*."""
        # Build current order from FlowBox children.
        order: list[int] = []
        child = self._flow_box.get_first_child()
        while child is not None:
            if isinstance(child, CameraTile):
                order.append(child.device.id)
            child = child.get_next_sibling()

        if src_id not in order or dst_id not in order:
            return

        src_idx = order.index(src_id)
        dst_idx = order.index(dst_id)
        order.pop(src_idx)
        order.insert(dst_idx, src_id)

        # Remove all tiles, then re-add in new order.
        for did in list(order):
            tile = self._cards.get(did)
            if tile is not None:
                self._flow_box.remove(tile)

        for did in order:
            tile = self._cards.get(did)
            if tile is not None:
                self._flow_box.append(tile)

        self._order = order
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

        # Apply saved order: known IDs first (in saved order), new IDs appended.
        known = {did: i for i, did in enumerate(self._order)}
        ordered = sorted(
            devices,
            key=lambda d: (known.get(d.id, len(self._order)), d.name),
        )

        for device in ordered:
            tile = CameraTile(device, on_reorder=self._on_reorder)
            tile.apply_size_mode(self._size_mode)
            self._flow_box.append(tile)
            self._cards[device.id] = tile

            threading.Thread(
                target=self._load_snapshot,
                args=(device,),
                daemon=True,
            ).start()
            self._start_refresh_timer(device.id)

        # Register for FCM events.
        client = get_client()
        if client is not None:
            client.add_event_callback(self._on_ring_event)

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
        png_bytes: bytes | None = None
        try:
            png_bytes = client._run(device.async_get_snapshot())
        except Exception as exc:
            _log.debug("Snapshot fetch failed for %s: %s", device.name, exc)

        if not png_bytes and client._ring is not None:
            _log.debug("Trying cached snapshot fallback for %s", device.name)
            png_bytes = client._run(_async_fetch_cached_snapshot(client._ring, device.id))

        if png_bytes:
            img = bytes(png_bytes)
            if not getattr(device, "motion_detection", True):
                img = _apply_motion_off_overlay(img)
            GLib.idle_add(self._set_card_snapshot, device.id, img)
        else:
            _log.debug("No snapshot available for %s", device.name)
            if not getattr(device, "motion_detection", True):
                img = _apply_motion_off_overlay(_make_grey_placeholder())
                if img:
                    GLib.idle_add(self._set_card_snapshot, device.id, img)

    def _set_card_snapshot(self, device_id: int, png_bytes: bytes) -> bool:
        tile = self._cards.get(device_id)
        if tile is not None:
            tile.set_snapshot(png_bytes)
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

    def _on_child_activated(self, flow_box: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        if isinstance(child, CameraTile):
            self._show_live(child.device)

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
        self._refresh_timers.pop(device_id, None)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_grid(self) -> None:
        self._cancel_all_refresh_timers()
        self._cards.clear()
        while (child := self._flow_box.get_first_child()) is not None:
            self._flow_box.remove(child)
