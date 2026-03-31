"""Live stream viewer — WebRTC negotiation via ring-doorbell + aiortc, rendered
through a dual-chain GStreamer pipeline:

  Video: appsrc → videoconvert → gtk4paintablesink
  Audio: appsrc → audioconvert → audioresample → volume → autoaudiosink

Flow
----
1. Dialog opens; GStreamer pipeline is created and the gtk4paintablesink
   paintable is immediately wired to a Gtk.Picture (blank until frames arrive).
2. _async_start() runs on the ring-client background asyncio loop:
   - Creates an aiortc RTCPeerConnection with Ring's ICE servers.
   - Adds recvonly transceivers for video and audio.
   - Creates the SDP offer and calls generate_async_webrtc_stream() so Ring
     negotiates asynchronously via the on_rtc_message callback.
3. on_rtc_message is called from the websocket reader coroutine (on the asyncio
   loop), so loop.create_task() safely schedules setRemoteDescription /
   addIceCandidate without blocking the reader.
4. When @pc.on("track") fires, _receive_frames() starts for video and
   _receive_audio_frames() starts for audio. Each pull frames from their
   respective aiortc tracks and push raw buffers into the matching appsrc.
   Caps on the audio appsrc are set dynamically from the first frame so any
   format aiortc delivers (s16 packed or fltp planar) is handled correctly;
   audioconvert takes it from there.
5. On dialog close the "closed" signal fires _on_closed(), which submits
   _async_cleanup() to cancel both tasks, close the WebRTC stream, and close
   the RTCPeerConnection; the GStreamer pipeline is also set to NULL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

import gi

gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gst, Gtk  # noqa: E402

_log = logging.getLogger(__name__)


def _patch_aiortc_h264() -> None:
    """Monkey-patch aiortc's H264Decoder to relax error detection.

    Ring cameras (particularly newer stickup_cam_mini variants) often send
    H264 bitstreams that use Main or High profile features (e.g. B-frames)
    even though aiortc negotiates Baseline profile in the SDP offer.  FFmpeg
    rejects these packets with "Invalid data found when processing input"
    because they violate the negotiated profile constraints.

    Setting err_detect=0 tells FFmpeg not to treat profile violations as
    fatal errors, allowing it to decode the frames best-effort.  The patch
    is applied once when this module is imported, before any RTCPeerConnection
    is created.
    """
    try:
        from aiortc.codecs.h264 import H264Decoder

        _orig_init = H264Decoder.__init__

        def _permissive_init(self) -> None:
            _orig_init(self)
            # Disable bitstream error detection so non-baseline streams
            # (B-frames, high profile) are decoded rather than dropped.
            self.codec.options = {"err_detect": "0"}

        H264Decoder.__init__ = _permissive_init
        _log.debug("Applied permissive H264 decoder patch")
    except Exception as exc:
        _log.debug("Could not patch H264Decoder: %s", exc)


_patch_aiortc_h264()

# Map av AudioFrame format names to GStreamer format strings.
_AV_TO_GST_FMT: dict[str, str] = {
    "s16": "S16LE",
    "s16p": "S16LE",
    "s32": "S32LE",
    "s32p": "S32LE",
    "flt": "F32LE",
    "fltp": "F32LE",
    "dbl": "F64LE",
    "dblp": "F64LE",
}


class LiveStreamDialog(Adw.Dialog):
    """Adw.Dialog that shows a live Ring camera feed with audio."""

    def __init__(self, device) -> None:
        super().__init__(title=device.name, content_width=854, content_height=520)
        self._device = device
        self._session_id: str | None = None
        self._pc = None
        self._video_task: asyncio.Task | None = None
        self._audio_task: asyncio.Task | None = None
        self._pipeline: Gst.Pipeline | None = None
        self._video_appsrc: Gst.Element | None = None
        self._audio_appsrc: Gst.Element | None = None
        self._vol_element: Gst.Element | None = None
        self._video_caps_set = False
        self._audio_caps_set = False

        self._build_ui()
        self.connect("closed", self._on_closed)
        self._start_stream()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Two independent chains in one GStreamer pipeline.
        # Video: appsrc → videoconvert → gtk4paintablesink
        # Audio: appsrc → audioconvert → audioresample → volume → autoaudiosink
        self._pipeline = Gst.parse_launch(
            "appsrc name=vsrc format=time is-live=true do-timestamp=true "
            "! videoconvert "
            "! gtk4paintablesink name=vsink sync=false  "
            "appsrc name=asrc format=time is-live=true do-timestamp=true "
            "! audioconvert "
            "! audioresample "
            "! volume name=vol "
            "! autoaudiosink sync=false"
        )
        self._video_appsrc = self._pipeline.get_by_name("vsrc")
        self._audio_appsrc = self._pipeline.get_by_name("asrc")
        self._vol_element = self._pipeline.get_by_name("vol")
        paintable = self._pipeline.get_by_name("vsink").get_property("paintable")

        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # Volume control: speaker icon + horizontal scale packed into the header.
        vol_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
        )
        vol_box.append(Gtk.Image(icon_name="audio-volume-high-symbolic"))
        vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        vol_scale.set_value(1.0)
        vol_scale.set_draw_value(False)
        vol_scale.set_size_request(120, -1)
        vol_scale.connect("value-changed", self._on_volume_changed)
        vol_box.append(vol_scale)
        header.pack_end(vol_box)

        overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        toolbar_view.set_content(overlay)

        video = Gtk.Picture(
            paintable=paintable,
            content_fit=Gtk.ContentFit.CONTAIN,
            hexpand=True,
            vexpand=True,
        )
        overlay.set_child(video)

        self._status_label = Gtk.Label(
            label="Connecting…",
            css_classes=["dim-label"],
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        overlay.add_overlay(self._status_label)

        self._pipeline.set_state(Gst.State.PLAYING)

    # ------------------------------------------------------------------
    # Volume control
    # ------------------------------------------------------------------

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        if self._vol_element is not None:
            self._vol_element.set_property("volume", scale.get_value())

    # ------------------------------------------------------------------
    # Stream startup
    # ------------------------------------------------------------------

    def _start_stream(self) -> None:
        from ring_gtk.ring_client import get_client

        client = get_client()
        if client is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._async_start(client),
            client._ensure_loop(),
        )

    async def _async_start(self, client) -> None:
        from aiortc import (
            RTCConfiguration,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp

        loop = asyncio.get_running_loop()
        session_id = str(uuid.uuid4())
        self._session_id = session_id

        # Build ICE server list: always include Ring's STUN servers; merge in
        # any TURN credentials returned by the Ring API so that cameras behind
        # strict NAT (where STUN peer-to-peer fails) can still stream via relay.
        ice_servers = [RTCIceServer(urls=self._device.get_ice_servers())]
        turn_servers = await client.async_get_turn_servers()
        for srv in turn_servers:
            url = srv.get("url") or srv.get("urls")
            if url:
                ice_servers.append(
                    RTCIceServer(
                        urls=[url] if isinstance(url, str) else url,
                        username=srv.get("username"),
                        credential=srv.get("credential"),
                    )
                )
        if turn_servers:
            _log.debug("Added %d TURN server(s) to ICE configuration", len(turn_servers))

        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        self._pc = pc

        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")

        # ---- callbacks from the ring-doorbell websocket reader (asyncio loop) ----

        def on_rtc_message(msg) -> None:
            if msg.answer:
                loop.create_task(
                    pc.setRemoteDescription(RTCSessionDescription(sdp=msg.answer, type="answer"))
                )
            elif msg.candidate is not None and msg.sdp_m_line_index is not None:
                try:
                    candidate = candidate_from_sdp(msg.candidate)
                    candidate.sdpMLineIndex = msg.sdp_m_line_index
                    candidate.sdpMid = str(msg.sdp_m_line_index)
                    loop.create_task(pc.addIceCandidate(candidate))
                except Exception as exc:
                    _log.debug("ICE candidate parse error: %s", exc)
            elif msg.error_code:
                GLib.idle_add(
                    self._set_status,
                    f"Stream error {msg.error_code}: {msg.error_message}",
                )

        # ---- aiortc event handlers ----

        @pc.on("icecandidate")
        def on_icecandidate(candidate) -> None:
            if candidate is not None:
                loop.create_task(
                    self._device.on_webrtc_candidate(
                        session_id,
                        candidate.candidate,
                        candidate.sdpMLineIndex or 0,
                    )
                )

        @pc.on("track")
        def on_track(track) -> None:
            _log.debug("Received %s track from Ring", track.kind)
            if track.kind == "video":
                self._video_task = loop.create_task(self._receive_frames(track))
                GLib.idle_add(self._on_connected)
            elif track.kind == "audio":
                self._audio_task = loop.create_task(self._receive_audio_frames(track))

        # ---- SDP offer/answer ----

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            await self._device.generate_async_webrtc_stream(
                pc.localDescription.sdp,
                session_id,
                on_rtc_message,
                keep_alive_timeout=300,
            )
            _log.debug(
                "WebRTC stream initiated for %s (session %s)",
                self._device.name,
                session_id[:8],
            )
        except Exception as exc:
            _log.warning("Stream start failed for %s: %s", self._device.name, exc)
            GLib.idle_add(self._set_status, f"Failed to connect: {exc}")

    # ------------------------------------------------------------------
    # Video frame loop — runs on the asyncio loop
    # ------------------------------------------------------------------

    async def _receive_frames(self, track) -> None:
        from aiortc.mediastreams import MediaStreamError

        _log.debug("Video frame receiver started")
        while True:
            try:
                frame = await track.recv()  # av.VideoFrame (decoded H.264)
                rgb = frame.to_ndarray(format="rgb24")
                h, w = rgb.shape[:2]
                raw: bytes = rgb.tobytes()

                if not self._video_caps_set:
                    caps = Gst.Caps.from_string(
                        f"video/x-raw,format=RGB,width={w},height={h},framerate=0/1"
                    )
                    self._video_appsrc.set_property("caps", caps)
                    self._video_caps_set = True
                    _log.debug("Video stream: %dx%d", w, h)

                self._video_appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(raw))

            except MediaStreamError:
                _log.debug("Video track ended")
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.debug("Video frame error: %s", exc)
                break

        GLib.idle_add(self._on_stream_ended)

    # ------------------------------------------------------------------
    # Audio frame loop — runs on the asyncio loop
    # ------------------------------------------------------------------

    async def _receive_audio_frames(self, track) -> None:
        from aiortc.mediastreams import MediaStreamError

        _log.debug("Audio frame receiver started")
        while True:
            try:
                frame = await track.recv()  # av.AudioFrame (decoded Opus)
                arr = frame.to_ndarray()
                raw: bytes = arr.tobytes()

                if not self._audio_caps_set:
                    fmt_name = frame.format.name
                    gst_fmt = _AV_TO_GST_FMT.get(fmt_name, "S16LE")
                    layout = "non-interleaved" if frame.format.is_planar else "interleaved"
                    # Derive channel count from array shape:
                    #   planar  → shape (channels, samples)
                    #   packed  → shape (1, samples * channels)
                    if frame.format.is_planar:
                        channels = arr.shape[0]
                    else:
                        channels = arr.shape[1] // frame.samples if frame.samples else 1
                    rate = frame.sample_rate
                    caps_str = (
                        f"audio/x-raw,format={gst_fmt},layout={layout},"
                        f"channels={channels},rate={rate}"
                    )
                    self._audio_appsrc.set_property("caps", Gst.Caps.from_string(caps_str))
                    self._audio_caps_set = True
                    _log.debug(
                        "Audio stream: %s %s %dch %dHz",
                        gst_fmt,
                        layout,
                        channels,
                        rate,
                    )

                self._audio_appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(raw))

            except MediaStreamError:
                _log.debug("Audio track ended")
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.debug("Audio frame error: %s", exc)
                break

    # ------------------------------------------------------------------
    # GTK-thread status helpers
    # ------------------------------------------------------------------

    def _on_connected(self) -> bool:
        self._status_label.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _set_status(self, message: str) -> bool:
        self._status_label.set_label(message)
        self._status_label.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _on_stream_ended(self) -> bool:
        self._set_status("Stream ended")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Cleanup on dialog close
    # ------------------------------------------------------------------

    def _on_closed(self, *_) -> None:
        from ring_gtk.ring_client import get_client

        client = get_client()
        if client is not None and (self._session_id or self._pc):
            asyncio.run_coroutine_threadsafe(
                self._async_cleanup(),
                client._ensure_loop(),
            )
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

    async def _async_cleanup(self) -> None:
        for task in (self._video_task, self._audio_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._video_task = None
        self._audio_task = None

        if self._session_id:
            try:
                await self._device.close_webrtc_stream(self._session_id)
            except Exception as exc:
                _log.debug("Error closing WebRTC stream: %s", exc)
            self._session_id = None

        if self._pc:
            try:
                await self._pc.close()
            except Exception as exc:
                _log.debug("Error closing RTCPeerConnection: %s", exc)
            self._pc = None
