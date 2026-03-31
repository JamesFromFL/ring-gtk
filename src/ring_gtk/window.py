"""Main application window (Adw.ApplicationWindow)."""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ring_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)


def _apply_motion_off_overlay(png_bytes: bytes) -> bytes:
    """Return a blurred copy of *png_bytes* with a 'Motion Detection Off' label.

    Called in a background thread — Pillow I/O is safe here.
    Falls back to the original bytes if Pillow is unavailable.
    """
    try:
        import io

        from PIL import Image, ImageDraw, ImageFilter

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        img = img.filter(ImageFilter.GaussianBlur(radius=4))
        draw = ImageDraw.Draw(img)
        text = "Motion Detection Off"
        w, h = img.size
        bbox = draw.textbbox((0, 0), text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = (w - tw) // 2, (h - th) // 2
        # Shadow for legibility on any background.
        draw.text((x + 1, y + 1), text, fill=(0, 0, 0))
        draw.text((x, y), text, fill=(255, 255, 255))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("motion overlay failed: %s", exc)
        return png_bytes


# Map ring-doorbell device family names to symbolic icon names.
_FAMILY_ICON: dict[str, str] = {
    "doorbots": "video-display-symbolic",
    "authorized_doorbots": "video-display-symbolic",
    "stickup_cams": "camera-video-symbolic",
    "chimes": "audio-speakers-symbolic",
    "base_stations": "network-wired-symbolic",
    "other": "security-high-symbolic",
}

# Device families that support async_get_snapshot().
_SNAPSHOT_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})

# Thumbnail dimensions (16:9) shown in the device list row.
_THUMB_W = 80
_THUMB_H = 45


class RingWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Ring",
            default_width=480,
            default_height=640,
            **kwargs,
        )
        # device_id → (device_obj, Gtk.Picture | None)
        self._device_rows: dict[int, tuple] = {}
        # device_id → GLib source_id for 30-second fallback snapshot refresh.
        self._refresh_timers: dict[int, int] = {}
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # Header bar
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        menu_btn.set_menu_model(self._build_menu())
        header.pack_end(menu_btn)

        # Sign-in banner (shown when not authenticated)
        self._banner = Adw.Banner(title="Not signed in to Ring", button_label="Sign In")
        self._banner.connect("button-clicked", self._on_sign_in)
        toolbar_view.add_top_bar(self._banner)

        # Main scroll area
        scroll = Gtk.ScrolledWindow(vexpand=True)
        toolbar_view.set_content(scroll)

        self._list_box = Gtk.ListBox(
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
            css_classes=["boxed-list"],
            selection_mode=Gtk.SelectionMode.NONE,
        )
        scroll.set_child(self._list_box)

        # Placeholder shown while loading / no devices
        self._status_page = Adw.StatusPage(
            icon_name="security-high-symbolic",
            title="No devices",
            description="Sign in to see your Ring devices.",
        )
        self._list_box.set_placeholder(self._status_page)

    def _build_menu(self):
        from gi.repository import Gio

        menu = Gio.Menu()
        menu.append("About Ring", "app.about")
        menu.append("Quit", "app.quit")
        return menu

    # ------------------------------------------------------------------
    # Public refresh — called after auth and by the refresh button
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        client = get_client()
        if client is None or not client.is_authenticated:
            self._banner.set_revealed(True)
            self._status_page.set_description("Sign in to see your Ring devices.")
            return

        self._banner.set_revealed(False)
        self._status_page.set_title("Loading…")
        self._status_page.set_description("")
        self._clear_list()

        threading.Thread(target=self._fetch_and_populate, daemon=True).start()

    def _fetch_and_populate(self) -> None:
        client = get_client()
        try:
            client._run(client._ring.async_update_data())
            devices = client.all_devices
            GLib.idle_add(self._populate_devices, devices)
        except Exception as exc:
            GLib.idle_add(self._show_fetch_error, str(exc))

    def _populate_devices(self, devices: list) -> bool:
        self._clear_list()

        if not devices:
            self._status_page.set_title("No devices found")
            self._status_page.set_description("No Ring devices are linked to your account.")
            return GLib.SOURCE_REMOVE

        for device in devices:
            family = getattr(device, "family", "other") or "other"
            icon = _FAMILY_ICON.get(family, "security-high-symbolic")
            kind = getattr(device, "kind", "") or ""
            has_snapshot = family in _SNAPSHOT_FAMILIES

            row = Adw.ActionRow(title=device.name, subtitle=kind)

            if has_snapshot:
                picture = self._make_thumbnail()
                row.add_prefix(picture)
                row.set_activatable(True)
                device_id = device.id
                row.connect(
                    "activated",
                    lambda _r, did=device_id: self._on_row_activated(did),
                )
                self._device_rows[device.id] = (device, picture)
                threading.Thread(
                    target=self._load_snapshot,
                    args=(device,),
                    daemon=True,
                ).start()
                self._start_refresh_timer(device.id)
            else:
                row.set_icon_name(icon)
                self._device_rows[device.id] = (device, None)

            self._list_box.append(row)

        self._status_page.set_title("No devices")
        self._status_page.set_description("No Ring devices are linked to your account.")

        # Register for FCM events so snapshots refresh on ding/motion.
        client = get_client()
        if client is not None:
            client.add_event_callback(self._on_ring_event)

        return GLib.SOURCE_REMOVE

    def _make_thumbnail(self) -> Gtk.Picture:
        return Gtk.Picture(
            width_request=_THUMB_W,
            height_request=_THUMB_H,
            content_fit=Gtk.ContentFit.COVER,
            can_shrink=True,
            margin_top=6,
            margin_bottom=6,
            margin_end=6,
        )

    # ------------------------------------------------------------------
    # Snapshot loading
    # ------------------------------------------------------------------

    def _load_snapshot(self, device) -> None:
        """Fetch snapshot bytes in a background thread, marshal result to GTK thread."""
        client = get_client()
        if client is None:
            return
        try:
            png_bytes = client._run(device.async_get_snapshot())
            if png_bytes:
                img = bytes(png_bytes)
                if not getattr(device, "motion_detection", True):
                    img = _apply_motion_off_overlay(img)
                GLib.idle_add(self._set_snapshot, device.id, img)
            else:
                _log.debug("No snapshot returned for %s", device.name)
        except Exception as exc:
            _log.debug("Snapshot fetch failed for %s: %s", device.name, exc)

    def _set_snapshot(self, device_id: int, png_bytes: bytes) -> bool:
        """Decode PNG bytes and paint the thumbnail (GTK main thread)."""
        entry = self._device_rows.get(device_id)
        if entry is None:
            return GLib.SOURCE_REMOVE
        _, picture = entry
        if picture is None:
            return GLib.SOURCE_REMOVE
        try:
            from gi.repository import Gdk, GdkPixbuf

            loader = GdkPixbuf.PixbufLoader()
            loader.write(png_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None:
                picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        except Exception as exc:
            _log.debug("Failed to set snapshot texture for device %s: %s", device_id, exc)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # FCM event → snapshot refresh (no polling)
    # ------------------------------------------------------------------

    def _on_ring_event(self, event) -> None:
        """Called on the GTK main thread when an FCM ding/motion event arrives."""
        kind = getattr(event, "kind", None)
        if kind not in ("ding", "motion"):
            return
        device_id = getattr(event, "doorbot_id", None)
        if device_id is None:
            return
        entry = self._device_rows.get(device_id)
        if entry is None or entry[1] is None:
            return
        device, _ = entry
        _log.debug("Refreshing snapshot for %s after %s event", device.name, kind)
        threading.Thread(
            target=self._load_snapshot,
            args=(device,),
            daemon=True,
        ).start()
        # Reset the 30-second fallback timer so it counts from this event.
        self._start_refresh_timer(device_id)

    # ------------------------------------------------------------------
    # Row click → live stream dialog
    # ------------------------------------------------------------------

    def _on_row_activated(self, device_id: int) -> None:
        entry = self._device_rows.get(device_id)
        if entry is None:
            return
        device, _ = entry
        self._open_live_stream(device)

    def _open_live_stream(self, device) -> None:
        from ring_gtk.live_stream import LiveStreamDialog

        LiveStreamDialog(device).present(self)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_fetch_error(self, message: str) -> bool:
        self._status_page.set_title("Failed to load devices")
        self._status_page.set_description(message)
        return GLib.SOURCE_REMOVE

    def _clear_list(self) -> None:
        self._cancel_all_refresh_timers()
        self._device_rows.clear()
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)

    # ------------------------------------------------------------------
    # Fallback snapshot refresh timers (30s per camera)
    # ------------------------------------------------------------------

    def _start_refresh_timer(self, device_id: int) -> None:
        """(Re)start the 30-second fallback timer for *device_id*."""
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
        """Timer callback: fetch a fresh snapshot if the device is still in the list."""
        entry = self._device_rows.get(device_id)
        if entry is None or entry[1] is None:
            self._refresh_timers.pop(device_id, None)
            return GLib.SOURCE_REMOVE
        device, _ = entry
        _log.debug("Fallback snapshot refresh for %s (no FCM event in 30 s)", device.name)
        threading.Thread(target=self._load_snapshot, args=(device,), daemon=True).start()
        self._refresh_timers.pop(device_id, None)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _on_sign_in(self, *_) -> None:
        from ring_gtk.auth_dialog import AuthDialog

        dialog = AuthDialog()
        dialog.present(self)
