"""Home page — app branding and information panel."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from ring_gtk import APP_ID  # noqa: E402


class HomePage(Gtk.Box):
    """Centered branding shown when the app first opens."""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            spacing=20,
            margin_top=48,
            margin_bottom=48,
            margin_start=32,
            margin_end=32,
        )
        self._build_ui()

    def _build_ui(self) -> None:
        title = Gtk.Label(
            label="Ring GTK",
            css_classes=["title-1"],
            halign=Gtk.Align.CENTER,
        )
        self.append(title)

        icon = Gtk.Image(
            icon_name=APP_ID,
            pixel_size=128,
            halign=Gtk.Align.CENTER,
        )
        self.append(icon)

        description = Gtk.Label(
            label=("A native GTK4 + libadwaita Linux desktop client\nfor Ring home security"),
            css_classes=["body"],
            halign=Gtk.Align.CENTER,
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=50,
        )
        self.append(description)

        notice = Gtk.Label(
            label=("This application is in early development and is actively being worked on."),
            css_classes=["dim-label", "caption"],
            halign=Gtk.Align.CENTER,
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=50,
        )
        self.append(notice)
