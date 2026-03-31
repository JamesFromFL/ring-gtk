"""Main application window — Nautilus-style Adw.OverlaySplitView layout.

Left sidebar: navigation list (Home / Cameras / Event History).
Right content: Gtk.Stack switching between HomePage, CamerasPage, HistoryPage.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk  # noqa: E402

from ring_gtk.cameras_page import CamerasPage  # noqa: E402
from ring_gtk.history_page import HistoryPage  # noqa: E402
from ring_gtk.home_page import HomePage  # noqa: E402
from ring_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)

# Navigation entries: (page_name, label, icon_name)
_NAV_ITEMS = [
    ("home", "Home", "go-home-symbolic"),
    ("cameras", "Cameras", "camera-photo-symbolic"),
    ("history", "Event History", "document-open-recent-symbolic"),
]


class RingWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Ring",
            default_width=1100,
            default_height=720,
            **kwargs,
        )
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Root: OverlaySplitView — collapsible sidebar + content area.
        self._split_view = Adw.OverlaySplitView(
            sidebar_width_fraction=0.20,
            min_sidebar_width=180,
            max_sidebar_width=240,
            collapsed=False,
        )

        # Outer ToolbarView holds the header bar and the split view.
        outer_toolbar = Adw.ToolbarView()
        self.set_content(outer_toolbar)

        header = Adw.HeaderBar()
        outer_toolbar.add_top_bar(header)

        # Hamburger / sidebar toggle button.
        toggle_btn = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic",
            tooltip_text="Toggle sidebar",
            active=True,
        )
        toggle_btn.connect("toggled", self._on_sidebar_toggled)
        header.pack_start(toggle_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        menu_btn.set_menu_model(self._build_menu())
        header.pack_end(menu_btn)

        # Sign-in banner (shown when not authenticated).
        self._banner = Adw.Banner(title="Not signed in to Ring", button_label="Sign In")
        self._banner.connect("button-clicked", self._on_sign_in)
        outer_toolbar.add_top_bar(self._banner)

        outer_toolbar.set_content(self._split_view)

        # ------------------------------------------------------------------
        # Sidebar — navigation list only (no branding icon)
        # ------------------------------------------------------------------

        sidebar_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )

        self._nav_list = Gtk.ListBox(
            css_classes=["navigation-sidebar"],
            selection_mode=Gtk.SelectionMode.SINGLE,
            margin_top=8,
            margin_bottom=8,
            vexpand=True,
        )
        self._nav_list.connect("row-selected", self._on_nav_selected)
        sidebar_box.append(self._nav_list)

        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        for name, label, icon in _NAV_ITEMS:
            row = self._make_nav_row(label, icon)
            self._nav_list.append(row)
            self._nav_rows[name] = row

        self._split_view.set_sidebar(sidebar_box)

        # ------------------------------------------------------------------
        # Content area — stack
        # ------------------------------------------------------------------

        self._content_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            hexpand=True,
            vexpand=True,
        )
        self._split_view.set_content(self._content_stack)

        self._home_page = HomePage()
        self._content_stack.add_named(self._home_page, "home")

        self._cameras_page = CamerasPage(
            on_navigate_to_history=self._navigate_to_history,
        )
        self._content_stack.add_named(self._cameras_page, "cameras")

        self._history_page = HistoryPage()
        self._content_stack.add_named(self._history_page, "history")

        # Default to Home.
        self._nav_list.select_row(self._nav_rows["home"])

    def _make_nav_row(self, label: str, icon: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=label, halign=Gtk.Align.START, hexpand=True))
        row.set_child(box)
        return row

    def _build_menu(self):
        from gi.repository import Gio

        menu = Gio.Menu()
        menu.append("About Ring", "app.about")
        menu.append("Quit", "app.quit")
        return menu

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_nav_selected(self, list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        for name, nav_row in self._nav_rows.items():
            if nav_row is row:
                self._content_stack.set_visible_child_name(name)
                if name == "history":
                    self._history_page.refresh()
                break

    def _navigate_to_history(self, device_id: int) -> None:
        """Switch to the history page pre-filtered to *device_id*."""
        self._nav_list.select_row(self._nav_rows["history"])
        self._content_stack.set_visible_child_name("history")
        self._history_page.refresh(filter_device_id=device_id)

    def _on_sidebar_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._split_view.set_show_sidebar(btn.get_active())

    # ------------------------------------------------------------------
    # Refresh — called after auth and by the refresh button
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        client = get_client()
        if client is None or not client.is_authenticated:
            self._banner.set_revealed(True)
            return

        self._banner.set_revealed(False)
        self._cameras_page.refresh()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _on_sign_in(self, *_) -> None:
        from ring_gtk.auth_dialog import AuthDialog

        dialog = AuthDialog()
        dialog.present(self)
