"""Wyoming STT interceptor server.

Multi-client asyncio server that:
  * accepts Wyoming ASR clients (e.g. ESPHome voice_assistant on Atom Echo),
  * feeds each client audio stream to a dedicated YAMNet AudioDetector (clap
    detection, same pipeline as the other sources),
  * forwards every Wyoming event to the real upstream Whisper Wyoming server
    and streams the responses back to the originating client.

Designed to run inside the main ClapTrap process on its own asyncio loop in a
background thread. Does not block the Flask/SocketIO server nor the existing
VBAN / RTSP / microphone sources.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Optional

import numpy as np
import requests

try:
    from wyoming.asr import Transcribe, Transcript  # noqa: F401 (imported for availability check)
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.event import Event, async_read_event, async_write_event
    from wyoming.info import (
        AsrModel,
        AsrProgram,
        Attribution,
        Describe,
        Info,
    )
    from wyoming.server import AsyncEventHandler, AsyncServer
    _WYOMING_AVAILABLE = True
    _WYOMING_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - runtime guard
    _WYOMING_AVAILABLE = False
    _WYOMING_IMPORT_ERROR = str(exc)

from audio_detector import AudioDetector
from settings_manager import load_settings

logger = logging.getLogger("wyoming_server")

_MODEL_PATH = "yamnet.tflite"
_TARGET_RATE = 16000

# Worker pool dedicated to blocking audio work (MediaPipe inference, resampling).
# Kept modest since each client already owns its AudioDetector and most of the
# cost is already spent inside MediaPipe's own threads.
_audio_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wyoming-audio")
_webhook_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wyoming-webhook")


# --- Module-level state -----------------------------------------------------

_server_thread: Optional[threading.Thread] = None
_server_loop: Optional[asyncio.AbstractEventLoop] = None
_server_task: Optional[asyncio.Task] = None
_server_started = threading.Event()
_stop_requested = threading.Event()
_socketio_ref: Any = None
_discovery_uuid: Optional[str] = None


def _resolve_hostname(peer_ip: str) -> str:
    """Best-effort reverse DNS to get a friendly short hostname (e.g. atom-chambre)."""
    if not peer_ip:
        return "unknown"
    try:
        name, _, _ = socket.gethostbyaddr(peer_ip)
        short = name.split(".", 1)[0]
        return short or peer_ip
    except Exception:
        return peer_ip


def _post_webhook(url: str, payload: dict) -> None:
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as exc:
        logger.warning("Wyoming webhook POST failed (%s): %s", url, exc)


def _notify_clap(source: str, score: float, clap_count: int) -> None:
    """Fan out a clap detection event: webhook, HA event, HA entities, socketio."""
    settings = load_settings()
    wy = settings.get("wyoming", {}) or {}
    webhook_url = wy.get("webhook_url", "")

    payload = {
        "event": "clap_detected",
        "source": source,
        "count": int(clap_count),
        "score": float(score),
        "timestamp": time.time(),
    }

    if webhook_url:
        _webhook_executor.submit(_post_webhook, webhook_url, payload)

    if _socketio_ref is not None:
        try:
            _socketio_ref.emit(
                "clap",
                {
                    "source_id": f"wyoming_{source}",
                    "timestamp": payload["timestamp"],
                    "score": payload["score"],
                    "clap_count": payload["count"],
                },
            )
        except Exception as exc:
            logger.debug("socketio emit failed: %s", exc)

    try:
        from ha_entities import on_clap_detected
        on_clap_detected(f"wyoming_{source}", float(score), int(clap_count))
    except Exception:
        pass

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        try:
            requests.post(
                "http://supervisor/core/api/events/claptrap_clap",
                headers={
                    "Authorization": f"Bearer {supervisor_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "source_id": f"wyoming_{source}",
                    "source": source,
                    "score": float(score),
                    "clap_count": int(clap_count),
                },
                timeout=3,
            )
        except Exception:
            pass


def _build_detection_callback(source: str) -> Callable[[dict], None]:
    def _cb(detection_data: dict) -> None:
        try:
            clap_count = int(detection_data.get("clap_count", 1))
            score = float(detection_data.get("score", 0.0))
            logger.info(
                "Wyoming clap from %s: %d clap(s), score=%.2f", source, clap_count, score
            )
            _notify_clap(source, score, clap_count)
        except Exception as exc:
            logger.error("Wyoming detection callback error for %s: %s", source, exc)
    return _cb


# --- Event handler (one instance per Wyoming client connection) --------------


class ClapTrapWyomingHandler(AsyncEventHandler):  # type: ignore[misc]
    """Per-connection handler: feeds YAMNet + forwards to upstream Whisper."""

    def __init__(self, reader, writer) -> None:
        super().__init__(reader, writer)
        peer = writer.get_extra_info("peername") or ("unknown", 0)
        self._peer_ip: str = peer[0] if peer else "unknown"
        self._source: str = self._peer_ip  # refined after reverse-DNS
        self._source_id: str = f"wyoming_{self._peer_ip}"
        self._detector: Optional[AudioDetector] = None
        self._detector_lock = asyncio.Lock()
        self._upstream_reader: Optional[asyncio.StreamReader] = None
        self._upstream_writer: Optional[asyncio.StreamWriter] = None
        self._upstream_pump: Optional[asyncio.Task] = None
        self._upstream_attempted: bool = False
        self._chunk_rate: int = _TARGET_RATE
        self._chunk_width: int = 2
        self._chunk_channels: int = 1
        logger.info("Wyoming client connected from %s", self._peer_ip)

    # ------------------------------------------------------------------ helpers

    async def _resolve_source_once(self) -> None:
        if self._source != self._peer_ip:
            return
        loop = asyncio.get_running_loop()
        hostname = await loop.run_in_executor(None, _resolve_hostname, self._peer_ip)
        self._source = hostname
        self._source_id = f"wyoming_{hostname}"
        logger.info("Wyoming client %s resolved as '%s'", self._peer_ip, hostname)

    async def _ensure_detector(self) -> None:
        if self._detector is not None:
            return
        async with self._detector_lock:
            if self._detector is not None:
                return
            await self._resolve_source_once()
            settings = load_settings()
            wy = settings.get("wyoming", {}) or {}
            global_s = settings.get("global", {}) or {}
            threshold = float(wy.get("threshold", global_s.get("threshold", 0.5)))
            clap_window = float(global_s.get("delay", 1.5))
            peak_cooldown = float(global_s.get("peak_cooldown", 0.08))
            peak_ratio = float(global_s.get("peak_ratio", 3.0))
            source = self._source
            source_id = self._source_id

            def _build() -> AudioDetector:
                det = AudioDetector(_MODEL_PATH, sample_rate=_TARGET_RATE, buffer_duration=1.0)
                det.initialize(
                    max_results=10,
                    score_threshold=threshold,
                    clap_window=clap_window,
                    peak_cooldown=peak_cooldown,
                    peak_ratio=peak_ratio,
                )
                det.add_source(
                    source_id=source_id,
                    detection_callback=_build_detection_callback(source),
                    labels_callback=None,
                    label=f"Wyoming: {source}",
                )
                det.start()
                return det

            loop = asyncio.get_running_loop()
            self._detector = await loop.run_in_executor(_audio_executor, _build)

            try:
                from ha_entities import register_source
                register_source(
                    self._source_id,
                    label=f"Wyoming: {source}",
                    technical_id=self._source_id,
                    clap_counts=wy.get("ha_entities", [1, 2]),
                )
            except Exception:
                pass

    async def _ensure_upstream(self) -> bool:
        if self._upstream_writer is not None:
            return True
        if self._upstream_attempted:
            # Already failed once for this session — don't spam reconnects.
            return False
        self._upstream_attempted = True
        settings = load_settings()
        wy = settings.get("wyoming", {}) or {}
        host = (wy.get("forward_host") or "").strip()
        port = int(wy.get("forward_port", 10300))
        if not host:
            logger.warning(
                "Wyoming: forward_host not configured — clap detection runs but STT responses will NOT be returned"
            )
            return False
        try:
            self._upstream_reader, self._upstream_writer = await asyncio.open_connection(host, port)
            self._upstream_pump = asyncio.create_task(self._pump_upstream())
            logger.info(
                "Wyoming %s: upstream Whisper connected at %s:%d", self._source, host, port
            )
            return True
        except Exception as exc:
            logger.error(
                "Wyoming: could not connect to upstream Whisper %s:%d — %s", host, port, exc
            )
            self._upstream_reader = None
            self._upstream_writer = None
            return False

    async def _pump_upstream(self) -> None:
        """Forward every event coming back from Whisper to the Wyoming client."""
        assert self._upstream_reader is not None
        try:
            while True:
                event = await async_read_event(self._upstream_reader)
                if event is None:
                    break
                try:
                    await self.write_event(event)
                except Exception as exc:
                    logger.debug("Wyoming %s: write back failed: %s", self._source, exc)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Wyoming %s: upstream pump ended: %s", self._source, exc)

    # ------------------------------------------------------------------ feeding

    async def _feed_yamnet(self, chunk: "AudioChunk") -> None:
        det = self._detector
        if det is None:
            return
        rate = chunk.rate or self._chunk_rate
        width = chunk.width or self._chunk_width
        channels = chunk.channels or self._chunk_channels
        audio_bytes = chunk.audio

        if not audio_bytes:
            return

        try:
            if width == 2:
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            elif width == 4:
                samples = np.frombuffer(audio_bytes, dtype=np.float32).copy()
            elif width == 1:
                samples = (
                    np.frombuffer(audio_bytes, dtype=np.uint8).astype(np.float32) - 128.0
                ) / 128.0
            else:
                return
        except Exception as exc:
            logger.debug("PCM decode error (%s): %s", self._source, exc)
            return

        if channels > 1:
            try:
                samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32)
            except ValueError:
                return

        if rate != _TARGET_RATE:
            # AudioDetector.process_audio expects 16 kHz; drop mismatched streams
            # rather than silently mis-classifying. ESPHome voice_assistant uses
            # 16 kHz already, so this path is defensive only.
            if rate and rate > _TARGET_RATE and rate % _TARGET_RATE == 0:
                factor = rate // _TARGET_RATE
                samples = samples[::factor]
            else:
                return

        source_id = self._source_id
        loop = asyncio.get_running_loop()
        loop.run_in_executor(_audio_executor, det.process_audio, samples, source_id)

    # --------------------------------------------------------------- dispatch

    async def handle_event(self, event: "Event") -> bool:
        try:
            if Describe.is_type(event.type):
                await self._send_info()
                return True

            # Forward to upstream whenever possible.
            if await self._ensure_upstream():
                assert self._upstream_writer is not None
                try:
                    await async_write_event(event, self._upstream_writer)
                except Exception as exc:
                    logger.error("Wyoming %s: forward write failed: %s", self._source, exc)

            # Feed YAMNet pipeline based on the audio events.
            if AudioStart.is_type(event.type):
                start = AudioStart.from_event(event)
                self._chunk_rate = start.rate or _TARGET_RATE
                self._chunk_width = start.width or 2
                self._chunk_channels = start.channels or 1
                await self._ensure_detector()
            elif AudioChunk.is_type(event.type):
                if self._detector is None:
                    # AudioChunk before AudioStart — still try to build.
                    await self._ensure_detector()
                chunk = AudioChunk.from_event(event)
                await self._feed_yamnet(chunk)
            elif AudioStop.is_type(event.type):
                # End of one utterance. Keep the connection & detector alive
                # for the next pipeline from the same Atom.
                pass

            return True
        except Exception as exc:
            logger.error("Wyoming %s: handle_event error: %s", self._source, exc)
            return True  # keep the connection alive

    async def _send_info(self) -> None:
        info = Info(
            asr=[
                AsrProgram(
                    name="claptrap",
                    description="ClapTrap YAMNet interceptor — forwards to upstream Whisper",
                    attribution=Attribution(
                        name="ClapTrap",
                        url="https://github.com/powange/ClapTrap-HA-Addon",
                    ),
                    installed=True,
                    version="1.0",
                    models=[
                        AsrModel(
                            name="claptrap-passthrough",
                            description="Passthrough to upstream Whisper Wyoming server",
                            attribution=Attribution(name="ClapTrap", url=""),
                            installed=True,
                            languages=["fr", "en"],
                            version="1.0",
                        )
                    ],
                )
            ]
        )
        await self.write_event(info.event())

    async def disconnect(self) -> None:
        logger.info("Wyoming client %s disconnecting", self._source)
        if self._upstream_pump is not None:
            self._upstream_pump.cancel()
            try:
                await self._upstream_pump
            except Exception:
                pass
        if self._upstream_writer is not None:
            try:
                self._upstream_writer.close()
                await self._upstream_writer.wait_closed()
            except Exception:
                pass
        if self._detector is not None:
            det = self._detector
            self._detector = None
            try:
                await asyncio.get_running_loop().run_in_executor(_audio_executor, det.stop)
            except Exception:
                pass


# --- Supervisor discovery (HA auto-add as Wyoming STT) ----------------------


def _get_addon_hostname() -> str:
    """Resolve the addon's hostname as seen from Home Assistant containers.

    Falls back to localhost if the supervisor API is unreachable.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return "localhost"
    try:
        r = requests.get(
            "http://supervisor/addons/self/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if r.ok:
            return r.json().get("data", {}).get("hostname") or "localhost"
    except Exception as exc:
        logger.debug("supervisor addons/self/info failed: %s", exc)
    return "localhost"


def _register_discovery(port: int) -> None:
    """Publish the Wyoming service to Home Assistant via supervisor discovery."""
    global _discovery_uuid
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        logger.info("Wyoming: SUPERVISOR_TOKEN missing — skipping HA discovery registration")
        return
    headers = {"Authorization": f"Bearer {token}"}

    # If we already have a uuid, drop it first (port may have changed).
    if _discovery_uuid:
        _unregister_discovery()

    hostname = _get_addon_hostname()
    payload = {
        "service": "wyoming",
        "config": {"uri": f"tcp://{hostname}:{port}"},
    }
    try:
        r = requests.post(
            "http://supervisor/discovery",
            headers=headers,
            json=payload,
            timeout=5,
        )
        if r.ok:
            uuid = r.json().get("data", {}).get("uuid")
            _discovery_uuid = uuid
            logger.info(
                "Wyoming: HA discovery registered (uuid=%s, uri=tcp://%s:%d)",
                uuid, hostname, port,
            )
        else:
            logger.warning(
                "Wyoming: HA discovery POST failed (%s): %s", r.status_code, r.text[:200]
            )
    except Exception as exc:
        logger.warning("Wyoming: HA discovery registration error: %s", exc)


def _unregister_discovery() -> None:
    global _discovery_uuid
    if not _discovery_uuid:
        return
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        _discovery_uuid = None
        return
    try:
        requests.delete(
            f"http://supervisor/discovery/{_discovery_uuid}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        logger.info("Wyoming: HA discovery unregistered (uuid=%s)", _discovery_uuid)
    except Exception as exc:
        logger.debug("Wyoming: HA discovery delete error: %s", exc)
    _discovery_uuid = None


# --- Public lifecycle API ---------------------------------------------------


async def _run_server() -> None:
    settings = load_settings()
    wy = settings.get("wyoming", {}) or {}
    port = int(wy.get("port", 10700))
    uri = f"tcp://0.0.0.0:{port}"
    server = AsyncServer.from_uri(uri)
    logger.info("Wyoming server listening on %s (forward -> %s:%s)",
                uri, wy.get("forward_host") or "<not configured>", wy.get("forward_port", 10300))
    try:
        await server.run(ClapTrapWyomingHandler)
    except asyncio.CancelledError:
        pass


def _thread_main() -> None:
    global _server_loop, _server_task
    loop = asyncio.new_event_loop()
    _server_loop = loop
    asyncio.set_event_loop(loop)
    try:
        _server_task = loop.create_task(_run_server())
        _server_started.set()
        loop.run_until_complete(_server_task)
    except Exception as exc:
        logger.error("Wyoming server loop stopped: %s", exc)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        _server_started.set()  # unblock any waiter even on failure


def start_wyoming_if_enabled(socketio=None) -> bool:
    """Start the Wyoming server in a background thread if enabled in settings.

    Safe to call multiple times; subsequent calls are no-ops while running.
    Returns True if the server is (now) running.
    """
    global _server_thread, _socketio_ref
    if socketio is not None:
        _socketio_ref = socketio

    settings = load_settings()
    wy = settings.get("wyoming", {}) or {}
    if not wy.get("enabled", False):
        logger.info("Wyoming server disabled in settings")
        return False

    if not _WYOMING_AVAILABLE:
        logger.error(
            "Wyoming server enabled but the 'wyoming' python package is not installed (%s)",
            _WYOMING_IMPORT_ERROR,
        )
        return False

    if _server_thread is not None and _server_thread.is_alive():
        logger.debug("Wyoming server already running")
        return True

    _stop_requested.clear()
    _server_started.clear()
    _server_thread = threading.Thread(
        target=_thread_main, name="wyoming-server", daemon=True
    )
    _server_thread.start()
    _server_started.wait(timeout=5)
    logger.info("Wyoming server thread started")

    # Register the service with HA so the Wyoming integration auto-detects it
    # as an STT/ASR provider (no manual integration setup needed).
    try:
        port = int((wy or {}).get("port", 10700))
        _register_discovery(port)
    except Exception as exc:
        logger.warning(f"Wyoming: discovery registration failed: {exc}")
    return True


def stop_wyoming() -> None:
    """Stop the Wyoming server (best effort; used on shutdown or hot-reload)."""
    global _server_thread, _server_loop, _server_task
    _unregister_discovery()
    if _server_thread is None or not _server_thread.is_alive():
        _server_thread = None
        _server_loop = None
        _server_task = None
        return
    loop = _server_loop
    task = _server_task
    if loop is not None and task is not None:
        try:
            loop.call_soon_threadsafe(task.cancel)
        except Exception:
            pass
    if loop is not None:
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
    _server_thread.join(timeout=5)
    _server_thread = None
    _server_loop = None
    _server_task = None
    logger.info("Wyoming server stopped")


def restart_wyoming(socketio=None) -> bool:
    """Stop (if running) then start according to current settings.

    Used to apply UI changes (enabled / port / forward host / forward port)
    without requiring a full addon restart.
    """
    stop_wyoming()
    return start_wyoming_if_enabled(socketio)
