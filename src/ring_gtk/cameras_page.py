"""Cameras page — FlowBox grid of camera cards + embedded live stream view."""

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

from ring_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)

# Families that support snapshot capture.
_SNAPSHOT_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})

# Card thumbnail dimensions (16:9).
_THUMB_W = 240
_THUMB_H = 135


# ---------------------------------------------------------------------------
# Snapshot helpers (same logic as the old window.py)
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

        img = Image.new("RGB", (240, 135), color=(48, 48, 48))
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
# Camera card widget
# ---------------------------------------------------------------------------


class CameraCard(Gtk.FlowBoxChild):
    """A single camera card shown in the grid."""

    def __init__(self, device) -> None:
        super().__init__()
        self.device = device
        self.set_focusable(True)

        frame = Gtk.Frame(css_classes=["card"])
        self.set_child(frame)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        frame.set_child(box)

        # Thumbnail picture.
        self._picture = Gtk.Picture(
            width_request=_THUMB_W,
            height_request=_THUMB_H,
            content_fit=Gtk.ContentFit.COVER,
            can_shrink=True,
        )
        box.append(self._picture)

        # Name + subtitle row.
        label_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=8,
            margin_bottom=8,
            margin_start=10,
            margin_end=10,
        )
        box.append(label_box)

        name_label = Gtk.Label(
            label=device.name,
            halign=Gtk.Align.START,
            ellipsize=3,  # PANGO_ELLIPSIZE_END
            css_classes=["heading"],
        )
        label_box.append(name_label)

        kind = getattr(device, "kind", "") or ""
        sub_label = Gtk.Label(
            label=kind,
            halign=Gtk.Align.START,
            ellipsize=3,
            css_classes=["dim-label", "caption"],
        )
        label_box.append(sub_label)

    def set_snapshot(self, png_bytes: bytes) -> None:
        """Update the thumbnail from raw PNG bytes (GTK main thread)."""
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(png_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None:
                self._picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        except Exception as exc:
            _log.debug("CameraCard.set_snapshot failed for %s: %s", self.device.name, exc)


# ---------------------------------------------------------------------------
# Live stream panel (embedded inside the cameras page stack)
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
        from ring_gtk.live_stream import LiveStreamView

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
        dest = Path.home() / "Pictures" / "ring-gtk"
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
    """Two-state cameras panel: FlowBox grid ↔ embedded live stream."""

    def __init__(self, on_navigate_to_history=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_navigate_to_history = on_navigate_to_history

        # device_id → CameraCard
        self._cards: dict[int, CameraCard] = {}
        # device_id → GLib source_id for 30-second fallback refresh.
        self._refresh_timers: dict[int, int] = {}

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

        # Status / placeholder when grid is empty.
        self._status_page = Adw.StatusPage(
            icon_name="camera-video-symbolic",
            title="No cameras",
            description="Sign in to see your Ring cameras.",
            vexpand=True,
        )
        self._status_page.set_visible(True)
        grid_box.append(self._status_page)

        scroll = Gtk.ScrolledWindow(vexpand=True, visible=False)
        grid_box.append(scroll)
        self._scroll = scroll

        self._flow_box = Gtk.FlowBox(
            column_spacing=12,
            row_spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
            homogeneous=True,
            max_children_per_line=6,
            min_children_per_line=1,
            selection_mode=Gtk.SelectionMode.NONE,
        )
        self._flow_box.connect("child-activated", self._on_card_activated)
        scroll.set_child(self._flow_box)

        # --- Live page ---
        self._live_panel = _LivePanel(
            on_back=self._show_grid,
            on_go_history=self._go_history,
        )
        self._stack.add_named(self._live_panel, "live")

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

        for device in devices:
            card = CameraCard(device)
            self._flow_box.append(card)
            self._cards[device.id] = card

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
        card = self._cards.get(device_id)
        if card is not None:
            card.set_snapshot(png_bytes)
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
        card = self._cards[device_id]
        _log.debug("Refreshing snapshot for %s after %s event", card.device.name, kind)
        threading.Thread(
            target=self._load_snapshot,
            args=(card.device,),
            daemon=True,
        ).start()
        self._start_refresh_timer(device_id)

    # ------------------------------------------------------------------
    # Card activation → live stream
    # ------------------------------------------------------------------

    def _on_card_activated(self, flow_box: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        if not isinstance(child, CameraCard):
            return
        self._show_live(child.device)

    def _show_live(self, device) -> None:
        self._live_panel.start_for_device(device)
        self._stack.set_visible_child_name("live")

    def _show_grid(self) -> None:
        self._live_panel.stop()
        self._stack.set_visible_child_name("grid")

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
        card = self._cards.get(device_id)
        if card is None:
            self._refresh_timers.pop(device_id, None)
            return GLib.SOURCE_REMOVE
        _log.debug("Fallback snapshot refresh for %s", card.device.name)
        threading.Thread(
            target=self._load_snapshot,
            args=(card.device,),
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
