"""Ring API integration layer.

Wraps python-ring-doorbell (0.9.x async API) and provides a synchronous
interface for use from GTK main-thread code.

Architecture
------------
A single asyncio event loop runs in a persistent daemon thread.  All
ring-doorbell coroutines are submitted to that loop via
``asyncio.run_coroutine_threadsafe`` and awaited synchronously from the
calling thread.  The FCM event listener also runs as a long-lived async
task on the same loop.

ring-doorbell manages its own aiohttp ClientSession internally (created
lazily in Auth._session on first use).  We do not inject a custom session —
doing so caused 406 Not Acceptable on /clients_api/session because the
default Accept header we were adding conflicted with Ring's CDN/load balancer
behaviour on that endpoint.

Usage
-----
    from ring_gtk.ring_client import get_client, init_client, init_client_from_cache

    # Fresh login (raises Requires2FAError if 2FA needed)
    client = init_client(email, password)

    # Second attempt after 2FA prompt
    client = init_client(email, password, otp_code="123456")

    # Restore from cache on startup (returns None if no cache)
    client = init_client_from_cache()

    client.start()   # start FCM event listener
    client.stop()    # call on app shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from gi.repository import GLib

_log = logging.getLogger(__name__)

_client: RingClient | None = None

# XDG data home — matches the path documented in README / pyproject comment.
TOKEN_CACHE_PATH = Path.home() / ".local" / "share" / "ring-gtk" / "token.cache"

# Ring validates device_model in the session POST body, which ring-doorbell
# constructs as "ring-doorbell:<user_agent>".  Ring's backend only accepts
# Android-style identifiers here — any custom UA produces an unrecognised
# device_model that Ring rejects with 406.
_APP_USER_AGENT = "android:com.ringapp"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def get_client() -> RingClient | None:
    """Return the active RingClient, or None if not yet initialised."""
    return _client


def init_client(
    username: str,
    password: str,
    otp_code: str | None = None,
) -> RingClient:
    """Authenticate and return a RingClient singleton.

    Raises ``ring_doorbell.Requires2FAError`` if the account requires 2FA
    and *otp_code* was not supplied.  Call again with the code to complete.
    Raises ``ring_doorbell.AuthenticationError`` on bad credentials.
    """
    global _client
    # Reuse the existing client's event loop on OTP retries; create fresh otherwise.
    if _client is None:
        client = RingClient()
    else:
        client = _client
        client._ring = None  # reset auth state, keep loop alive
    client.authenticate(username, password, otp_code)
    _client = client
    return _client


def init_client_from_cache() -> RingClient | None:
    """Restore a RingClient from a cached token without re-authenticating.

    Returns None if no token cache exists or if the cached token is invalid.
    """
    global _client
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        client = RingClient()
        client.authenticate_from_cache()
        _client = client
        _log.info("Restored Ring session from cache")
        return _client
    except Exception as exc:
        _log.warning("Cache restore failed (%s) — will require fresh login", exc)
        # Remove stale / invalid cache so the next sign-in starts clean.
        TOKEN_CACHE_PATH.unlink(missing_ok=True)
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RingClient:
    """Thin synchronous wrapper around the async ring-doorbell Ring object."""

    def __init__(self) -> None:
        self._ring = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._listener_future = None  # concurrent.futures.Future from run_coroutine_threadsafe
        self._stop_event = threading.Event()
        self._event_callbacks: list = []

    # ------------------------------------------------------------------
    # Async event loop management
    # ------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Return the background loop, starting it if necessary."""
        if self._loop is None or self._loop.is_closed():
            ready = threading.Event()
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._run_loop,
                args=(ready,),
                daemon=True,
                name="ring-asyncio",
            )
            self._loop_thread.start()
            ready.wait()  # don't submit coroutines before the loop is running
        return self._loop

    def _run_loop(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(ready.set)
        self._loop.run_forever()

    def _run(self, coro) -> Any:
        """Submit *coro* to the background loop and block until it completes."""
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return future.result()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str, otp_code: str | None = None) -> None:
        """Authenticate with Ring.

        Raises ``Requires2FAError`` if 2FA is required; call again with the
        OTP to complete.  Raises ``AuthenticationError`` on bad credentials.
        """
        self._run(self._async_authenticate(username, password, otp_code))

    def authenticate_from_cache(self) -> None:
        """Restore session from cached token.  Raises if cache is missing/invalid."""
        self._run(self._async_authenticate_from_cache())

    async def _async_authenticate(self, username: str, password: str, otp_code: str | None) -> None:
        from ring_doorbell import Auth, Ring

        # Always do a fresh OAuth exchange — never shortcut via the cache on an
        # explicit sign-in.  Pass None for the token so ring-doorbell doesn't
        # try to reuse a stale cached credential.
        auth = Auth(_APP_USER_AGENT, None, _save_token)

        # May raise Requires2FAError or AuthenticationError — let propagate.
        await auth.async_fetch_token(username, password, otp_code)

        self._ring = Ring(auth)
        _log.debug(
            "OAuth token obtained — hardware_id=%s user_agent=%s",
            auth.get_hardware_id(),
            _APP_USER_AGENT,
        )
        await self._ring.async_update_data()
        _log.info(
            "Ring authenticated — %d device(s) found",
            len(self._ring.devices().all_devices),
        )

    async def _async_authenticate_from_cache(self) -> None:
        from ring_doorbell import Auth, AuthenticationError, Ring

        token = _load_token()
        if not token:
            raise RuntimeError("No cached token found")

        auth = Auth(_APP_USER_AGENT, token, _save_token)
        self._ring = Ring(auth)

        try:
            await self._ring.async_update_data()
        except AuthenticationError:
            # Cached token expired and refresh failed — bubble up so
            # init_client_from_cache() can delete the stale file.
            raise

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._ring is not None

    @property
    def all_devices(self) -> list:
        if self._ring is None:
            return []
        return list(self._ring.devices().all_devices)

    # ------------------------------------------------------------------
    # Background FCM listener
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the real-time FCM event listener (non-blocking)."""
        if self._ring is None:
            return
        self._stop_event.clear()
        self._listener_future = asyncio.run_coroutine_threadsafe(
            self._async_listen(), self._ensure_loop()
        )
        _log.debug("FCM listener task submitted")

    def stop(self) -> None:
        """Stop the listener, close the ring auth session, shut down the loop."""
        self._stop_event.set()

        if self._listener_future and not self._listener_future.done():
            self._listener_future.cancel()

        if self._loop and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(timeout=5)
            except Exception as exc:
                _log.debug("Error during async close: %s", exc)
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _async_close(self) -> None:
        """Close the ring auth session (which owns the aiohttp ClientSession)."""
        if self._ring is not None:
            await self._ring.auth.async_close()

    async def _async_listen(self) -> None:
        from ring_doorbell import RingEventListener

        listener = RingEventListener(self._ring)
        listener.add_notification_callback(self._on_ring_event)

        started = await listener.start()
        if not started:
            _log.warning("FCM listener failed to start — no real-time events")
            return

        _log.info("FCM event listener started")
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await listener.stop()
            _log.info("FCM event listener stopped")

    async def async_get_turn_servers(self) -> list[dict]:
        """Fetch TURN server credentials from Ring's API.

        Returns a list of dicts with keys ``url``, ``username``, ``credential``
        (matching the shape aiortc's RTCIceServer expects).  Returns an empty
        list on any error so callers can fall back to STUN-only gracefully.
        """
        if self._ring is None:
            return []
        try:
            resp = await self._ring.async_query("/clients_api/turn_servers")
            data = resp.json()
            return data.get("servers", [])
        except Exception as exc:
            _log.debug("TURN server fetch skipped: %s", exc)
            return []

    def add_event_callback(self, callback) -> None:
        """Register a callable to be invoked (on the GTK main thread) for every FCM event."""
        if callback not in self._event_callbacks:
            self._event_callbacks.append(callback)

    def _on_ring_event(self, event) -> None:
        """Called from the asyncio thread; marshal to GTK main loop."""
        GLib.idle_add(self._dispatch_event, event)

    def _dispatch_event(self, event) -> bool:
        from ring_gtk.notifications import send_ring_notification

        send_ring_notification(event)
        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception as exc:
                _log.debug("Event callback error: %s", exc)
        return GLib.SOURCE_REMOVE


# ---------------------------------------------------------------------------
# Token persistence (module-level so they can be passed as plain callbacks)
# ---------------------------------------------------------------------------


def _load_token() -> dict | None:
    if TOKEN_CACHE_PATH.exists():
        try:
            return json.loads(TOKEN_CACHE_PATH.read_text())
        except Exception as exc:
            _log.warning("Failed to read token cache: %s", exc)
    return None


def _save_token(token: dict) -> None:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_PATH.write_text(json.dumps(token))
    _log.debug("Token cached to %s", TOKEN_CACHE_PATH)
