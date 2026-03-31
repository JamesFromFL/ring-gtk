"""Event History page — Adw.NavigationSplitView with event list and GStreamer player."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gst, Gtk  # noqa: E402

from ring_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)

_SEEK_STEP_NS = 10 * Gst.SECOND  # 10-second seek step


def _relative_time(dt: datetime) -> str:
    """Return a human-readable relative timestamp, e.g. '3 minutes ago'."""
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    diff = now - dt
    secs = int(diff.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = secs // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


_KIND_ICON = {
    "ding": "audio-input-microphone-symbolic",
    "motion": "camera-video-symbolic",
    "on_demand": "video-display-symbolic",
}


# ---------------------------------------------------------------------------
# GStreamer-based video player widget
# ---------------------------------------------------------------------------


class _VideoPlayer(Gtk.Box):
    """Simple GStreamer playbin player with gtk4paintablesink."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._duration_ns: int = -1
        self._poll_id: int | None = None
        self._seeking = False
        # Initialise to None so _build_ui() can safely check even if
        # _build_pipeline() returns early due to a missing GStreamer element.
        self._player: Gst.Element | None = None
        self._paintable = None
        self._build_pipeline()
        self._build_ui()

    def _build_pipeline(self) -> None:
        self._player = Gst.ElementFactory.make("playbin", "player")
        if self._player is None:
            _log.warning("playbin not available")
            return

        # Connect gtk4paintablesink directly as the video sink.  Avoid wrapping
        # it in a videorate bin — the hard 30 fps cap caused playbin to stall on
        # recordings whose timestamps don't align with the pipeline clock,
        # resulting in the first frame being displayed but never advanced.
        # playbin's own internal queue and decodebin handle frame pacing.
        video_sink = Gst.ElementFactory.make("gtk4paintablesink", "vsink")
        if video_sink is None:
            _log.warning("gtk4paintablesink not available")
            return

        video_sink.set_property("sync", True)
        self._paintable = video_sink.get_property("paintable")
        self._player.set_property("video-sink", video_sink)

        bus = self._player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::duration-changed", self._on_duration_changed)

    def _build_ui(self) -> None:
        overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.append(overlay)

        if self._paintable is not None:
            video_widget = Gtk.Picture(
                paintable=self._paintable,
                content_fit=Gtk.ContentFit.CONTAIN,
                hexpand=True,
                vexpand=True,
            )
        else:
            video_widget = Gtk.Label(label="Video unavailable")

        overlay.set_child(video_widget)

        self._placeholder = Gtk.Label(
            label="Select an event to begin playback",
            css_classes=["dim-label", "title-4"],
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        overlay.add_overlay(self._placeholder)

        # Scrubber.
        self._scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.001)
        self._scrubber.set_draw_value(False)
        self._scrubber.set_margin_start(12)
        self._scrubber.set_margin_end(12)
        # GTK4: use GestureClick instead of the removed button-press/release-event signals.
        press_gesture = Gtk.GestureClick.new()
        press_gesture.connect("pressed", lambda *_: setattr(self, "_seeking", True))
        press_gesture.connect("released", lambda *_: self._on_scrubber_released())
        self._scrubber.add_controller(press_gesture)
        self._scrubber.connect("change-value", self._on_scrubber_changed)
        self.append(self._scrubber)

        # Playback controls + volume on the same row.
        controls_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self.append(controls_row)

        # Centred playback buttons.
        ctrl_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.CENTER,
            hexpand=True,
        )
        controls_row.append(ctrl_box)

        def _btn(icon, tip, cb):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", cb)
            ctrl_box.append(b)
            return b

        _btn("media-skip-backward-symbolic", "Skip to start", self._on_skip_start)
        _btn("media-seek-backward-symbolic", "Seek back 10 s", self._on_seek_back)
        self._play_btn = _btn("media-playback-start-symbolic", "Play / Pause", self._on_play_pause)
        _btn("media-seek-forward-symbolic", "Seek forward 10 s", self._on_seek_fwd)
        _btn("media-skip-forward-symbolic", "Skip to end", self._on_skip_end)

        # Volume control, right-aligned.
        vol_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
        )
        vol_box.append(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._vol_scale.set_value(1.0)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.set_size_request(100, -1)
        self._vol_scale.connect("value-changed", self._on_volume_changed)
        vol_box.append(self._vol_scale)
        controls_row.append(vol_box)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_url(self, url: str) -> None:
        """Load and begin playing *url*."""
        self._stop_poll()
        self._duration_ns = -1
        self._scrubber.set_value(0)

        if self._player is None:
            return

        self._player.set_state(Gst.State.NULL)
        self._player.set_property("uri", url)
        self._player.set_state(Gst.State.PLAYING)
        self._play_btn.set_icon_name("media-playback-pause-symbolic")
        self._placeholder.set_visible(False)
        # Poll position every 500 ms to drive the scrubber during playback.
        self._start_poll()

    def stop(self) -> None:
        self._stop_poll()
        if self._player is not None:
            self._player.set_state(Gst.State.NULL)
        self._play_btn.set_icon_name("media-playback-start-symbolic")

    def get_current_frame_png(self) -> bytes | None:
        """Grab the current video frame as PNG bytes via GStreamer sample."""
        if self._player is None:
            return None
        try:
            sample = self._player.emit("convert-sample", Gst.Caps.from_string("image/png"))
            if sample is None:
                return None
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            data = bytes(info.data)
            buf.unmap(info)
            return data
        except Exception as exc:
            _log.debug("Frame capture failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        if self._player is not None:
            # playbin exposes a native `volume` property (0.0 = muted, 1.0 = 100%).
            self._player.set_property("volume", scale.get_value())

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def _on_play_pause(self, *_) -> None:
        if self._player is None:
            return
        _, state, _ = self._player.get_state(0)
        if state == Gst.State.PLAYING:
            self._player.set_state(Gst.State.PAUSED)
            self._play_btn.set_icon_name("media-playback-start-symbolic")
            self._stop_poll()
        else:
            self._player.set_state(Gst.State.PLAYING)
            self._play_btn.set_icon_name("media-playback-pause-symbolic")
            self._start_poll()

    def _on_skip_start(self, *_) -> None:
        self._seek_to_ns(0)

    def _on_skip_end(self, *_) -> None:
        if self._duration_ns > 0:
            self._seek_to_ns(self._duration_ns)

    def _on_seek_back(self, *_) -> None:
        pos = self._get_position_ns()
        self._seek_to_ns(max(0, pos - _SEEK_STEP_NS))

    def _on_seek_fwd(self, *_) -> None:
        pos = self._get_position_ns()
        dur = self._duration_ns if self._duration_ns > 0 else 0
        self._seek_to_ns(min(dur, pos + _SEEK_STEP_NS))

    def _seek_to_ns(self, ns: int) -> None:
        if self._player is not None:
            self._player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, ns)

    def _get_position_ns(self) -> int:
        if self._player is None:
            return 0
        ok, pos = self._player.query_position(Gst.Format.TIME)
        return pos if ok else 0

    def _on_scrubber_changed(self, scale, scroll, value) -> bool:
        return False

    def _on_scrubber_released(self) -> None:
        self._seeking = False
        if self._duration_ns > 0:
            frac = self._scrubber.get_value()
            self._seek_to_ns(int(frac * self._duration_ns))

    # ------------------------------------------------------------------
    # GStreamer bus messages
    # ------------------------------------------------------------------

    def _on_eos(self, *_) -> None:
        GLib.idle_add(self._handle_eos)

    def _handle_eos(self) -> bool:
        self._stop_poll()
        self._scrubber.set_value(1.0)
        self._play_btn.set_icon_name("media-playback-start-symbolic")
        return GLib.SOURCE_REMOVE

    def _on_bus_error(self, bus, msg) -> None:
        err, _ = msg.parse_error()
        _log.warning("GStreamer player error: %s", err)

    def _on_duration_changed(self, *_) -> None:
        if self._player is None:
            return
        ok, dur = self._player.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            self._duration_ns = dur

    # ------------------------------------------------------------------
    # Position polling — drives the scrubber during playback
    # ------------------------------------------------------------------

    def _start_poll(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(500, self._poll_position)

    def _stop_poll(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _poll_position(self) -> bool:
        """Query pipeline position every 500 ms and advance the scrubber."""
        if self._player is None:
            return GLib.SOURCE_REMOVE
        if self._seeking:
            return GLib.SOURCE_CONTINUE

        # Try to learn duration if it wasn't available at load time
        # (common for HTTP streams where Content-Length is returned after buffering).
        if self._duration_ns <= 0:
            ok, dur = self._player.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self._duration_ns = dur

        ok, pos = self._player.query_position(Gst.Format.TIME)
        if ok and self._duration_ns > 0:
            self._scrubber.set_value(pos / self._duration_ns)

        return GLib.SOURCE_CONTINUE


# ---------------------------------------------------------------------------
# History page
# ---------------------------------------------------------------------------


class HistoryPage(Gtk.Box):
    """Adw.NavigationSplitView — event list on left, player on right."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._events: list = []
        self._devices: list = []
        self._selected_device_id: int | None = None  # None = all cameras
        self._current_event = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._nav_split = Adw.NavigationSplitView(
            hexpand=True,
            vexpand=True,
            min_sidebar_width=280,
            max_sidebar_width=340,
        )
        self.append(self._nav_split)

        # --- Sidebar navigation page ---
        sidebar_page = Adw.NavigationPage(title="Event History")
        self._nav_split.set_sidebar(sidebar_page)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)

        sidebar_toolbar = Adw.ToolbarView()
        sidebar_toolbar.add_top_bar(Adw.HeaderBar())
        sidebar_toolbar.set_content(sidebar_box)
        sidebar_page.set_child(sidebar_toolbar)

        # Camera filter combo row.
        filter_group = Adw.PreferencesGroup(margin_top=8, margin_start=8, margin_end=8)
        sidebar_box.append(filter_group)

        self._camera_filter = Adw.ComboRow(title="Camera")
        self._camera_filter.connect("notify::selected", self._on_filter_changed)
        filter_group.add(self._camera_filter)

        # Event list.
        scroll = Gtk.ScrolledWindow(vexpand=True)
        sidebar_box.append(scroll)

        self._event_list = Gtk.ListBox(
            css_classes=["navigation-sidebar"],
            selection_mode=Gtk.SelectionMode.SINGLE,
            vexpand=True,
        )
        self._event_list.connect("row-activated", self._on_event_selected)
        scroll.set_child(self._event_list)

        self._list_placeholder = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="No events",
            description="No recorded events found.",
        )
        self._event_list.set_placeholder(self._list_placeholder)

        # --- Content navigation page ---
        content_page = Adw.NavigationPage(title="Player")
        self._nav_split.set_content(content_page)

        content_toolbar = Adw.ToolbarView()
        content_toolbar.add_top_bar(Adw.HeaderBar())

        content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        content_toolbar.set_content(content_box)
        content_page.set_child(content_toolbar)

        # Player widget.
        self._player = _VideoPlayer()
        content_box.append(self._player)

        # Action buttons row.
        action_bar = Gtk.ActionBar()
        content_box.append(action_bar)

        def _action_btn(icon, tip, cb):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", cb)
            return b

        action_bar.pack_start(_action_btn("starred-symbolic", "Favourite", self._on_favourite))
        action_bar.pack_start(_action_btn("share-symbolic", "Copy recording URL", self._on_share))
        action_bar.pack_start(
            _action_btn(
                "document-save-symbolic", "Download to ~/Videos/ring-gtk/", self._on_download
            )
        )
        action_bar.pack_start(
            _action_btn("camera-photo-symbolic", "Save screenshot", self._on_screenshot)
        )
        action_bar.pack_end(_action_btn("edit-delete-symbolic", "Delete event", self._on_delete))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, filter_device_id: int | None = None) -> None:
        """Load event history.  Optionally pre-select *filter_device_id*."""
        client = get_client()
        if client is None or not client.is_authenticated:
            return

        if filter_device_id is not None:
            self._selected_device_id = filter_device_id

        self._list_placeholder.set_title("Loading…")
        self._list_placeholder.set_description("")
        self._clear_event_list()

        threading.Thread(target=self._fetch_history, daemon=True).start()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_history(self) -> None:
        client = get_client()
        try:
            devices = client.all_devices
            events = []
            for device in devices:
                try:
                    history = client._run(device.async_history(limit=50))
                    for ev in history:
                        ev["_device"] = device
                    events.extend(history)
                except Exception as exc:
                    _log.debug("History fetch failed for %s: %s", device.name, exc)

            events.sort(key=lambda e: e.get("created_at", datetime.min), reverse=True)
            GLib.idle_add(self._populate_events, devices, events)
        except Exception as exc:
            GLib.idle_add(self._show_fetch_error, str(exc))

    def _populate_events(self, devices: list, events: list) -> bool:
        self._devices = devices
        self._events = events
        self._clear_event_list()

        # Rebuild camera filter combo.
        store = Gtk.StringList()
        store.append("All cameras")
        for dev in devices:
            store.append(dev.name)
        self._camera_filter.set_model(store)

        # If we have a pre-selected device, find its index.
        if self._selected_device_id is not None:
            for i, dev in enumerate(devices):
                if dev.id == self._selected_device_id:
                    self._camera_filter.set_selected(i + 1)  # +1 for "All cameras"
                    break
            self._selected_device_id = None

        self._fill_event_rows()
        return GLib.SOURCE_REMOVE

    def _fill_event_rows(self) -> None:
        self._clear_event_list()
        selected_idx = self._camera_filter.get_selected()
        filter_device = None
        if selected_idx > 0 and selected_idx - 1 < len(self._devices):
            filter_device = self._devices[selected_idx - 1]

        visible_events = [
            e for e in self._events if filter_device is None or e.get("_device") is filter_device
        ]

        if not visible_events:
            self._list_placeholder.set_title("No events")
            self._list_placeholder.set_description("No recorded events found.")
            return

        for ev in visible_events:
            row = self._make_event_row(ev)
            self._event_list.append(row)

    def _make_event_row(self, event: dict) -> Adw.ActionRow:
        device = event.get("_device")
        kind = event.get("kind", "unknown")
        created_at = event.get("created_at")

        row = Adw.ActionRow(
            title=device.name if device else "Unknown camera",
            subtitle=kind.replace("_", " ").capitalize(),
            activatable=True,
        )
        row._event_data = event  # type: ignore[attr-defined]

        icon = Gtk.Image(icon_name=_KIND_ICON.get(kind, "security-high-symbolic"))
        row.add_prefix(icon)

        if created_at is not None:
            ts_label = Gtk.Label(
                label=_relative_time(created_at),
                css_classes=["dim-label", "caption"],
                valign=Gtk.Align.CENTER,
            )
            row.add_suffix(ts_label)

        return row

    def _show_fetch_error(self, message: str) -> bool:
        self._list_placeholder.set_title("Failed to load events")
        self._list_placeholder.set_description(message)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Event selection → playback
    # ------------------------------------------------------------------

    def _on_event_selected(self, list_box: Gtk.ListBox, row) -> None:
        if row is None:
            return
        event = getattr(row, "_event_data", None)
        if event is None:
            return
        self._current_event = event
        self._nav_split.set_show_content(True)

        threading.Thread(target=self._load_and_play, args=(event,), daemon=True).start()

    def _load_and_play(self, event: dict) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client._run(device.async_recording_url(event_id))
            if url:
                GLib.idle_add(self._player.load_url, url)
        except Exception as exc:
            _log.debug("Failed to get recording URL for event %s: %s", event_id, exc)

    # ------------------------------------------------------------------
    # Action callbacks
    # ------------------------------------------------------------------

    def _on_favourite(self, *_) -> None:
        # Ring API does not expose a public favourite endpoint; no-op for now.
        _log.debug("Favourite action — not supported by Ring API")

    def _on_share(self, *_) -> None:
        """Copy the recording URL to the clipboard."""
        ev = self._current_event
        if ev is None:
            return
        threading.Thread(target=self._copy_url_to_clipboard, args=(ev,), daemon=True).start()

    def _copy_url_to_clipboard(self, event: dict) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client._run(device.async_recording_url(event_id))
            if url:
                GLib.idle_add(self._do_copy_clipboard, url)
        except Exception as exc:
            _log.debug("Failed to fetch URL for share: %s", exc)

    def _do_copy_clipboard(self, text: str) -> bool:
        display = self.get_display()
        if display is not None:
            clipboard = display.get_clipboard()
            clipboard.set(text)
        return GLib.SOURCE_REMOVE

    def _on_download(self, *_) -> None:
        ev = self._current_event
        if ev is None:
            return
        threading.Thread(target=self._download_recording, args=(ev,), daemon=True).start()

    def _download_recording(self, event: dict) -> None:
        import urllib.request

        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client._run(device.async_recording_url(event_id))
            if not url:
                return
            dest = Path.home() / "Videos" / "ring-gtk"
            dest.mkdir(parents=True, exist_ok=True)
            kind = event.get("kind", "event")
            fname = dest / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{kind}.mp4"
            _log.debug("Downloading recording to %s", fname)
            urllib.request.urlretrieve(url, fname)
            _log.debug("Download complete: %s", fname)
        except Exception as exc:
            _log.debug("Download failed: %s", exc)

    def _on_screenshot(self, *_) -> None:
        png = self._player.get_current_frame_png()
        if png is None:
            _log.debug("No frame available for screenshot")
            return
        dest = Path.home() / "Pictures" / "ring-gtk"
        dest.mkdir(parents=True, exist_ok=True)
        fname = dest / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png"
        fname.write_bytes(png)
        _log.debug("Screenshot saved to %s", fname)

    def _on_delete(self, *_) -> None:
        ev = self._current_event
        if ev is None:
            return

        dialog = Adw.AlertDialog(
            heading="Delete this event?",
            body="The recording will be permanently deleted from Ring.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_confirmed, ev)
        dialog.present(self)

    def _on_delete_confirmed(self, dialog, response: str, event: dict) -> None:
        if response != "delete":
            return
        threading.Thread(target=self._do_delete, args=(event,), daemon=True).start()

    def _do_delete(self, event: dict) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            client._run(device.async_delete_recording(event_id))
            _log.debug("Deleted event %s", event_id)
            GLib.idle_add(self._after_delete, event)
        except Exception as exc:
            _log.debug("Delete failed: %s", exc)

    def _after_delete(self, event: dict) -> bool:
        self._events = [e for e in self._events if e.get("id") != event.get("id")]
        self._current_event = None
        self._player.stop()
        self._fill_event_rows()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Filter change
    # ------------------------------------------------------------------

    def _on_filter_changed(self, *_) -> None:
        self._fill_event_rows()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_event_list(self) -> None:
        while (row := self._event_list.get_first_child()) is not None:
            self._event_list.remove(row)
