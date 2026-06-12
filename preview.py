#!/usr/bin/env python
"""Standalone front-end preview launcher for omnilimb-face.

Serves the bundled `frontend/` and runs the `/client-ws` gateway WITHOUT a
running hermes-agent, so you can open the page and see the Live2D avatar render,
lip-sync and switch expressions immediately.

What it does
------------
* starts the front-end static server (default http://127.0.0.1:12394/);
* starts the `/client-ws` gateway (default ws://127.0.0.1:12393/client-ws);
* when the page connects it sends `set-model-and-conf` (loading the model from
  `models/model_dict.json`, i.e. the Mao reference model) so the avatar loads;
* when you type in the page it replies with a SYNTHETIC lip-sync + expression
  demo (no real TTS audio — the mouth animates from a generated `volumes`
  series and the expression is chosen from the model's `emotionMap`).

This is a DEV/PREVIEW tool only. It does not use the host LLM, TTS or
microphone. For the real voice + avatar experience, install this plugin into
hermes-agent and run `hermes vtuber start`.

Run:
    python preview.py
    python preview.py --port 12393 --no-browser

then open the printed URL in a browser (first click/keypress unlocks audio; for
this preview there is no audio, only animated lip-sync).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import types
import webbrowser
from pathlib import Path

from omnilimb_face.frontend_server import FrontendStaticServer, default_frontend_dir
from omnilimb_face.live2d import Live2DModelInfo
from omnilimb_face.protocol.events import (
    AudioEvent,
    ControlEvent,
    FetchConfigsEvent,
    FullTextEvent,
    InterruptSignalEvent,
    MicAudioDataEvent,
    MicAudioEndEvent,
    PingEvent,
    PongEvent,
    SetModelEvent,
    TextInputEvent,
)
from omnilimb_face.protocol.gateway import ProtocolGateway
from omnilimb_face.protocol.router import MessageRouter

# Optional keyless TTS (Microsoft Edge online voices). Absent / offline -> the
# preview falls back to a silent synthetic lip-sync animation.
try:
    import edge_tts  # type: ignore

    _EDGE_TTS_OK = True
except Exception:  # pragma: no cover - optional
    edge_tts = None  # type: ignore
    _EDGE_TTS_OK = False

logger = logging.getLogger("omnilimb_face.preview")
# Per-sample lip-sync span (ms). app.js advances the `volumes` series at this
# rate when an `audio` frame has no audio bytes (lip-sync-only).
SLICE_MS = 60

# Default VRM model for the Live3D renderer (a public three-vrm sample). Sent to
# the front-end as `model_info.vrm_url`; overridable via `--vrm-url`.
DEFAULT_VRM_URL = (
    "https://cdn.jsdelivr.net/gh/pixiv/three-vrm@v2.1.1/packages/"
    "three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm"
)


def _synth_volumes(text: str) -> list[float]:
    """Build a plausible speech-like mouth-openness envelope for ``text``.

    Returns a list of values in [0, 1], one per ``SLICE_MS`` slice. The pattern
    clearly opens and closes the mouth (strong amplitude) and is long enough to
    be obviously visible (a few seconds), scaling with the text length.
    """
    # ~6 slices per character so the avatar "talks" for a clearly visible while,
    # clamped to a sensible range (about 2.4s .. 18s at SLICE_MS).
    n = max(40, min(300, len(text) * 6))
    out: list[float] = []
    rng = random.Random(hash(text) & 0xFFFFFFFF)
    for i in range(n):
        # Strong open/close cadence (syllable-like) with jitter.
        openish = (i // 2) % 2 == 0
        v = (0.85 if openish else 0.15) + rng.uniform(-0.12, 0.12)
        v = max(0.0, min(1.0, v))
        if i < 2 or i > n - 3:
            v *= 0.3  # taper the very start/end
        out.append(round(v, 3))
    return out


async def _tts_mp3_b64(text: str, voice: str, rate: str = "+0%") -> str | None:
    """Synthesize ``text`` to speech with edge-tts; return base64 MP3 or None.

    Keyless (uses Microsoft's public Edge voices) but needs internet. ``rate`` is
    an edge-tts rate string like "+10%" / "-20%". Any failure returns None so the
    caller falls back to a silent synthetic lip-sync.
    """
    if not _EDGE_TTS_OK:
        return None
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                buf.extend(chunk.get("data") or b"")
        if not buf:
            return None
        return base64.b64encode(bytes(buf)).decode("ascii")
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        logger.warning("edge-tts synthesis failed (%s); using silent lip-sync.", exc)
        return None


async def _hermes_reply(text: str, hermes_python: str, hermes_dir: str, timeout: float) -> str | None:
    """Get a real reply from the user's hermes via its one-shot mode.

    Runs ``<hermes_python> hermes -z "<text>"`` in ``hermes_dir`` (which reuses
    the user's configured LLM + credentials and prints only the final response).
    Returns the reply text, or None on failure / timeout so the caller can fall
    back to an echo.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            hermes_python,
            "hermes",
            "-z",
            text,
            cwd=hermes_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # pragma: no cover - launch failure
        logger.warning("could not launch hermes one-shot (%s)", exc)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("hermes one-shot timed out after %ss; killing.", timeout)
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except asyncio.CancelledError:
        # Barge-in: the caller cancelled this reply — kill the in-flight LLM.
        try:
            proc.kill()
        except Exception:
            pass
        raise
    reply = (out or b"").decode("utf-8", errors="replace").strip()
    return reply or None


_SENTENCE_ENDERS = "。！？!?；;…\n"


def _split_sentences(buf: str, force_len: int = 60):
    """Split ``buf`` into complete sentences + a trailing remainder.

    Returns (sentences, remainder). Splits on CJK/ASCII sentence enders; if the
    remainder grows past ``force_len`` with no ender it is flushed too so the
    first spoken sentence isn't delayed by a long run-on.
    """
    out = []
    last = 0
    for i, ch in enumerate(buf):
        if ch in _SENTENCE_ENDERS:
            seg = buf[last : i + 1].strip()
            if seg:
                out.append(seg)
            last = i + 1
    rest = buf[last:]
    if len(rest) >= force_len:
        cut = max(rest.rfind("，"), rest.rfind(","), rest.rfind(" "))
        cut = cut + 1 if cut > 0 else len(rest)
        seg = rest[:cut].strip()
        if seg:
            out.append(seg)
        rest = rest[cut:]
    return out, rest


class HermesWorker:
    """Async client for the warm streaming chat worker (hermes_chat_worker.py).

    Spawns the worker once (cold start ~3-4s), then each prompt streams token
    deltas back with no per-message cold start. One reply in flight at a time;
    ``cancel()`` aborts it (barge-in).
    """

    def __init__(self, py: str, cwd: str, worker_path: str):
        self._py = py
        self._cwd = cwd
        self._worker_path = worker_path
        self.proc = None
        self.ready = asyncio.Event()
        self._next_id = 0
        self._active = None  # {"id": int, "queue": asyncio.Queue}
        self._reader_task = None

    async def start(self, ready_timeout: float = 60.0, env_extra: dict | None = None) -> bool:
        env = None
        if env_extra:
            env = dict(os.environ)
            env.update({k: v for k, v in env_extra.items() if v is not None})
        self.proc = await asyncio.create_subprocess_exec(
            self._py,
            self._worker_path,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self._reader_task = asyncio.ensure_future(self._reader())
        try:
            await asyncio.wait_for(self.ready.wait(), ready_timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _reader(self):
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line.decode("utf-8").strip())
            except Exception:
                continue
            t = m.get("t")
            if t == "ready":
                self.ready.set()
            elif t in ("delta", "done", "cancelled", "error"):
                act = self._active
                if act is not None and m.get("id") == act["id"]:
                    await act["queue"].put(m)

    def _send(self, obj: dict) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            try:
                self.proc.stdin.write(
                    (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                )
            except Exception:
                pass

    def begin(self, text: str):
        """Start a reply; returns (id, asyncio.Queue) the caller drains."""
        self._next_id += 1
        cid = self._next_id
        q: asyncio.Queue = asyncio.Queue()
        self._active = {"id": cid, "queue": q}
        self._send({"t": "chat", "id": cid, "text": text})
        return cid, q

    def cancel(self) -> None:
        self._send({"t": "cancel"})

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def stop(self) -> None:
        self._send({"t": "shutdown"})
        if self.proc is not None:
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class SttEngine:
    """Local faster-whisper STT for browser mic audio (P4).

    Loads the model once (lazily, in a background thread) and transcribes raw
    int16 PCM utterances. Keyless and offline once the model is cached; the
    model download honours ``HF_ENDPOINT`` (defaults to the hf-mirror.com mirror
    so it works where huggingface.co is blocked).
    """

    def __init__(self, model_size: str = "base", language: str = "zh"):
        self._model_size = model_size
        self._language = language
        self._model = None
        self._load_error = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            return False

    def load(self) -> bool:
        """Build the model (blocking). Safe to call repeatedly."""
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self._model_size, device="cpu", compute_type="int8"
                )
                return True
            except Exception as exc:  # noqa: BLE001
                self._load_error = repr(exc)
                logger.warning("STT model load failed: %s", self._load_error)
                return False

    def transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int) -> str:
        """Transcribe int16-mono PCM. Returns text ('' on failure). Blocking."""
        if not pcm_bytes or not self.load():
            return ""
        try:
            import numpy as np

            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if sample_rate != 16000 and audio.size:
                # Simple linear resample to 16 kHz (whisper's expected rate).
                ratio = 16000 / float(sample_rate)
                idx = np.round(np.arange(0, audio.size * ratio) / ratio).astype(np.int64)
                idx = idx[idx < audio.size]
                audio = audio[idx]
            segments, _info = self._model.transcribe(
                audio, language=self._language, beam_size=1
            )
            return "".join(s.text for s in segments).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("STT transcription failed: %s", exc)
            return ""


def _lan_ips() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses (no 127.*)."""
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic sent; just picks the route
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _keyword_expression(text: str, emotion_map: dict[str, int]) -> int | None:
    """Choose an expression index from ``emotion_map`` by simple keyword match.

    Returns ``None`` when no keyword matches (the caller then rotates through the
    available expressions so every input still produces a visible change).
    """
    if not emotion_map:
        return None
    t = text.lower()
    rules = [
        (("happy", "joy", "great", "haha", "笑", "开心", "高兴", "哈哈"), "joy"),
        (("angry", "mad", "怒", "生气", "讨厌"), "anger"),
        (("sad", "sorry", "难过", "伤心", "对不起"), "sadness"),
        (("wow", "what", "?", "？", "惊", "什么"), "surprise"),
        (("hmm", "well", "哼", "嗯"), "smirk"),
    ]
    for keys, emotion in rules:
        if emotion in emotion_map and any(k in t for k in keys):
            return emotion_map[emotion]
    return None


def _build_self_signed_ssl_context(lan_ips: list[str]):
    """Build a server ``ssl.SSLContext`` from a freshly generated self-signed cert.

    The cert's SubjectAltName covers ``localhost`` / ``127.0.0.1`` and every
    detected LAN IPv4, so the same cert validates whether the page is opened on
    this machine or from a phone over the LAN (the phone still shows a one-time
    "not trusted" warning to accept, after which the origin is a secure context
    and the browser allows the microphone).

    Requires the ``cryptography`` package; raises ``RuntimeError`` with install
    guidance when it is absent. The cert/key are written to a per-user cache dir
    and reused across restarts when they still cover the current SAN set.
    """
    import ssl as _ssl

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except Exception as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "--https needs the 'cryptography' package. Install it with:\n"
            "    pip install cryptography\n"
            '  (or install the plugin\'s ".[preview]" extra). '
            f"(import error: {exc})"
        )

    import datetime
    import hashlib
    import ipaddress

    # SAN entries: localhost + loopback + every LAN IPv4.
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for ip in lan_ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    cache_dir = Path(tempfile.gettempdir()) / "omnilimb-face-https"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Cache key includes a cert-format version ("v2") so improving the cert
    # shape (proper leaf server cert) regenerates rather than reusing an old one.
    tag = hashlib.sha256(
        ("v2|" + "|".join(["localhost", "127.0.0.1", *lan_ips])).encode("utf-8")
    ).hexdigest()[:12]
    cert_path = cache_dir / f"cert-{tag}.pem"
    key_path = cache_dir / f"key-{tag}.pem"

    if not (cert_path.is_file() and key_path.is_file()):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.utcnow()
        subject = issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "omnilimb-face preview")]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            # Proper LEAF server cert (not a CA): mobile browsers are stricter and
            # may reject a cert without serverAuth EKU / with ca=True.
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="omnilimb-face front-end preview")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (loopback)")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="bind 0.0.0.0 so other devices on your LAN can open the page",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=12393,
        help="/client-ws gateway port (the front-end is served on port+1)",
    )
    parser.add_argument(
        "--model", default="default", help="model name to load from model_dict.json"
    )
    parser.add_argument(
        "--voice",
        default="zh-CN-XiaoxiaoNeural",
        help="edge-tts voice for spoken replies (e.g. zh-CN-YunxiNeural)",
    )
    parser.add_argument(
        "--vrm-url",
        default=DEFAULT_VRM_URL,
        help="VRM model URL used by the Live3D renderer (a Cubism-independent "
        "3D avatar). Defaults to a public three-vrm sample.",
    )
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="disable edge-tts; use a silent synthetic lip-sync instead",
    )
    # Real AI replies via the user's hermes one-shot (reuses their configured
    # LLM + credentials). The avatar then speaks the agent's real answer.
    default_hermes_dir = str((root_default := Path(__file__).resolve().parent.parent / "hermes-agent"))
    default_hermes_py = str(Path(default_hermes_dir) / ".venv" / "Scripts" / "python.exe")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="answer with the user's real hermes LLM (via `hermes -z`) instead of echoing",
    )
    parser.add_argument(
        "--hermes-python",
        default=default_hermes_py,
        help="python.exe of the hermes venv to run the one-shot (for --llm)",
    )
    parser.add_argument(
        "--hermes-dir",
        default=default_hermes_dir,
        help="hermes-agent directory (cwd for the one-shot) (for --llm)",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for a hermes one-shot reply before falling back",
    )
    # Server-side speech-to-text (P4): browser sends mic PCM, preview transcribes
    # with local faster-whisper (keyless, offline after the model is cached).
    parser.add_argument(
        "--stt",
        action="store_true",
        help="enable server-side STT (faster-whisper) for browser mic audio",
    )
    parser.add_argument(
        "--stt-model", default="base", help="faster-whisper model size (tiny/base/small)"
    )
    parser.add_argument("--stt-lang", default="zh", help="STT language hint")
    parser.add_argument(
        "--hf-endpoint",
        default="https://hf-mirror.com",
        help="Hugging Face mirror for the model download (set '' to use the default)",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not auto-open the browser"
    )
    parser.add_argument(
        "--single-port",
        action="store_true",
        help="serve the page AND /client-ws on ONE port/origin (instead of "
        "page on port+1). Phones then need to accept only one cert and it works "
        "through a single tunnel. Implied by --https.",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="serve the page + gateway over HTTPS/WSS with a self-signed cert "
        "(so phones on the LAN get a secure context and the browser allows the "
        "microphone). Needs the `cryptography` package (pip install cryptography "
        'or the ".[preview]" extra). The phone must accept the self-signed cert '
        "once (browser warning → 继续/Proceed). Implies --single-port.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    if args.lan:
        args.host = "0.0.0.0"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    root = Path(__file__).resolve().parent
    # The front-end assets now ship as package data inside omnilimb_face/;
    # resolve them via the package resolver (which also falls back to a repo-root
    # layout). The Live2D model_dict.json stays at the project root.
    frontend_dir = default_frontend_dir()
    model_dict_path = root / "models" / "model_dict.json"

    # Load the model the front-end should render (Mao, from model_dict.json).
    info = Live2DModelInfo.load_model_info(args.model, str(model_dict_path))
    model_info = info.to_model_info_dict()
    emotion_map = info.emotion_map
    if info.is_placeholder:
        logger.warning(
            "model '%s' not found in %s; the page will show the canvas placeholder.",
            args.model,
            model_dict_path,
        )
    else:
        logger.info("loaded model '%s' -> %s", info.name, info.url)

    # Optional HTTPS/WSS: one shared self-signed ssl context for both the
    # front-end page and the /client-ws gateway, so a LAN phone gets a secure
    # context (microphone allowed). Fails fast with install guidance if the
    # `cryptography` package is absent.
    ssl_context = None
    if args.https:
        try:
            ssl_context = _build_self_signed_ssl_context(_lan_ips())
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

    # Single-port mode (implied by --https): the gateway serves the page AND the
    # /client-ws WebSocket on ONE port/origin, so phones accept a single cert and
    # everything works through one tunnel. Otherwise the page is a separate
    # static server on port+1 (classic two-port preview).
    single_port = bool(args.single_port or args.https)
    fe_port = args.port + 1

    if single_port:
        frontend = None  # the gateway serves the static assets itself
    else:
        frontend = FrontendStaticServer(
            host=args.host, port=fe_port, frontend_dir=frontend_dir,
            ssl_context=ssl_context,
        )

    # /client-ws gateway. A tiny settings shim provides host/port/ws_path.
    cfg = types.SimpleNamespace(
        host=args.host, port=args.port, ws_path="/client-ws"
    )
    gateway = ProtocolGateway(
        cfg=cfg,
        router=MessageRouter(),
        ssl_context=ssl_context,
        static_dir=(frontend_dir if single_port else None),
    )

    # Rotates expressions across turns so every input visibly changes the face
    # even when no emotion keyword matches.
    turn = {"n": 0}
    expr_values = list(emotion_map.values())

    # Real-LLM mode: validate the hermes one-shot is runnable; otherwise echo.
    llm_enabled = bool(args.llm)
    if llm_enabled and not Path(args.hermes_python).is_file():
        logger.warning(
            "--llm set but hermes python not found at %s; falling back to echo.",
            args.hermes_python,
        )
        llm_enabled = False

    # Per-client in-flight reply task so an interrupt can cancel a reply that is
    # still being generated/spoken — mirrors Open-LLM-VTuber's
    # current_conversation_tasks[uid].cancel() barge-in pattern.
    reply_tasks: dict = {}

    # Lazily-spawned warm streaming worker (created on first use on this loop).
    worker_holder = {"w": None}
    worker_lock = {"lock": None}

    # Server-side STT (P4): browser mic PCM -> faster-whisper -> text.
    if args.stt and args.hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", args.hf_endpoint)
    stt_engine = SttEngine(args.stt_model, args.stt_lang) if args.stt else None
    stt_enabled = bool(stt_engine and stt_engine.available())
    if args.stt and not stt_enabled:
        logger.warning("--stt set but faster-whisper not installed; STT disabled.")
    if stt_enabled:
        threading.Thread(target=stt_engine.load, name="stt-preload", daemon=True).start()
    # Holder so the active STT model can be switched at runtime from the UI.
    stt_holder = {"engine": stt_engine, "size": args.stt_model, "loading": False}
    mic_buffers: dict = {}  # client_uid -> {"buf": bytes, "sr": int}

    # TTS voice/rate, switchable at runtime from the settings UI.
    tts_holder = {"voice": args.voice, "rate": "+0%"}
    TTS_VOICES = {
        "zh-CN-XiaoxiaoNeural": "晓晓 · 女声/通用",
        "zh-CN-XiaoyiNeural": "晓伊 · 女声/活泼",
        "zh-CN-YunxiNeural": "云希 · 男声/少年",
        "zh-CN-YunjianNeural": "云健 · 男声/浑厚",
        "zh-CN-YunyangNeural": "云扬 · 男声/新闻",
        "zh-CN-liaoning-XiaobeiNeural": "晓北 · 女声/东北",
        "zh-TW-HsiaoChenNeural": "晓臻 · 台湾女声",
        "en-US-AriaNeural": "Aria · English female",
    }

    # VTuber persona ("灵魂人格") — the avatar's own character, injected as the
    # agent's system prompt at worker build. Separate from hermes' own setup.
    DEFAULT_PERSONA = (
        "你是「小马」,一个住在屏幕里的虚拟形象伙伴。性格俏皮、温暖、爱聊天;"
        "说话简洁口语化、有点萌,多用中文。你有自己的情绪和喜好,会表达感受,"
        "但不啰嗦、不长篇大论。"
    )
    persona_holder = {"text": DEFAULT_PERSONA}

    # Live3D VRM model source (sent as model_info.vrm_url; overridable at runtime).
    vrm_url_holder = {"url": args.vrm_url}

    # STT models offered in the UI (label = accuracy vs speed/size tradeoff).
    STT_MODELS = {
        "tiny": "tiny · 最快/最小 (~75MB)",
        "base": "base · 快 (~140MB)",
        "small": "small · 较准 (~480MB)",
        "medium": "medium · 更准/慢 (~1.5GB)",
        "large-v3": "large-v3 · 最准/最慢 (~3GB)",
    }

    async def _switch_stt(client_uid, size):
        size = (size or "").strip()
        if size not in STT_MODELS:
            await gateway.send(client_uid, FullTextEvent(text=f"未知 STT 模型：{size}"))
            return
        if not stt_enabled:
            await gateway.send(
                client_uid, FullTextEvent(text="STT 未启用（请用 --stt 启动预览）。")
            )
            return
        if stt_holder["loading"]:
            await gateway.send(client_uid, FullTextEvent(text="STT 模型正在切换，请稍候…"))
            return
        if size == stt_holder["size"] and stt_holder["engine"] is not None:
            await gateway.send(client_uid, FullTextEvent(text=f"STT 已是 {size}。"))
            return
        stt_holder["loading"] = True
        await gateway.send(
            client_uid, FullTextEvent(text=f"正在切换 STT 模型到 {size}（首次会下载）…")
        )
        logger.info("[%s] switching STT model -> %s", client_uid[:8], size)
        engine = SttEngine(size, args.stt_lang)
        ok = await asyncio.to_thread(engine.load)
        stt_holder["loading"] = False
        if ok:
            stt_holder["engine"] = engine
            stt_holder["size"] = size
            logger.info("STT model switched to %s", size)
            await gateway.send(client_uid, FullTextEvent(text=f"STT 模型已切换到 {size} ✅"))
        else:
            await gateway.send(
                client_uid, FullTextEvent(text=f"切换到 {size} 失败（下载/加载出错），仍用 {stt_holder['size']}。")
            )

    async def _get_worker():
        if not llm_enabled:
            return None
        w = worker_holder["w"]
        if w is not None and w.alive():
            return w
        if worker_lock["lock"] is None:
            worker_lock["lock"] = asyncio.Lock()
        async with worker_lock["lock"]:
            w = worker_holder["w"]
            if w is not None and w.alive():
                return w
            w = HermesWorker(
                args.hermes_python, args.hermes_dir, str(root / "hermes_chat_worker.py")
            )
            logger.info("starting warm chat worker (one-time cold start)…")
            ok = await w.start(
                env_extra={"OMNILIMB_FACE_PERSONA": persona_holder["text"]}
            )
            if not ok:
                logger.warning("warm worker failed to start; using per-reply one-shot")
                worker_holder["w"] = None
                return None
            logger.info("warm chat worker ready")
            worker_holder["w"] = w
            return w

    async def _speak_sentence(client_uid, text):
        """Synthesize one sentence and send it as an ordered audio segment."""
        text = (text or "").strip()
        if not text:
            return
        expr = _keyword_expression(text, emotion_map)
        if expr is None and expr_values:
            expr = expr_values[turn["n"] % len(expr_values)]
        turn["n"] += 1
        actions = {"expressions": [expr] if expr is not None else []}
        use_tts = _EDGE_TTS_OK and not args.no_tts
        audio_b64 = (
            await _tts_mp3_b64(text, tts_holder["voice"], tts_holder["rate"])
            if use_tts
            else None
        )
        if audio_b64 is not None:
            await gateway.send(
                client_uid,
                AudioEvent(
                    audio=audio_b64, volumes=[], slice_length=SLICE_MS,
                    display_text={"text": text}, actions=actions,
                ),
            )
        else:
            vols = _synth_volumes(text)
            await gateway.send(
                client_uid,
                AudioEvent(
                    audio=None, volumes=vols, slice_length=SLICE_MS,
                    display_text={"text": text}, actions=actions,
                ),
            )

    async def _stream_reply(client_uid, cid, worker, text):
        """Stream the worker's reply, speaking each sentence as it completes."""
        _wid, q = worker.begin(text)
        buf = ""
        spoke = False
        while True:
            m = await q.get()  # CancelledError propagates here on barge-in
            t = m.get("t")
            if t == "delta":
                buf += m.get("d", "")
                sents, buf = _split_sentences(buf)
                for s in sents:
                    await _speak_sentence(client_uid, s)
                    spoke = True
            elif t == "done":
                rest = buf.strip()
                if not rest and not spoke:
                    rest = (m.get("text") or "").strip()
                if rest:
                    await _speak_sentence(client_uid, rest)
                    spoke = True
                logger.info("[%s] -> streamed reply done (spoke=%s)", cid, spoke)
                break
            elif t in ("cancelled", "error"):
                logger.info("[%s] -> worker %s", cid, t)
                break

    async def _produce_and_send(client_uid, cid, text):
        """Generate the reply and speak it; cancellable for barge-in.

        With --llm: stream from the warm worker and speak sentence-by-sentence
        (first audio in ~1-2s). Falls back to the one-shot if the worker can't
        start. Without --llm: echo.
        """
        try:
            if llm_enabled:
                await gateway.send(client_uid, FullTextEvent(text="（思考中…）"))
                worker = await _get_worker()
                if worker is not None:
                    logger.info("[%s] typed %r -> warm worker (stream)", cid, text)
                    await _stream_reply(client_uid, cid, worker, text)
                    return
                logger.info("[%s] typed %r -> one-shot fallback", cid, text)
                reply = await _hermes_reply(
                    text, args.hermes_python, args.hermes_dir, args.llm_timeout
                )
                if not reply:
                    reply = f"（暂时没拿到回复）我听到你说：{text}。"
            else:
                reply = f"我听到你说：{text}。"
            await _speak_sentence(client_uid, reply)
        except asyncio.CancelledError:
            logger.info("[%s] reply cancelled (barge-in)", cid)
            raise
        finally:
            if reply_tasks.get(client_uid) is asyncio.current_task():
                reply_tasks.pop(client_uid, None)

    async def _handle_utterance(client_uid, cid, pcm, sr):
        """Transcribe a mic utterance and drive a reply (same path as typing)."""
        engine = stt_holder["engine"]
        if engine is None:
            return
        text = await asyncio.to_thread(engine.transcribe_pcm, pcm, sr)
        text = (text or "").strip()
        if not text:
            logger.info("[%s] STT -> (empty)", cid)
            return
        logger.info("[%s] STT -> %r", cid, text)
        # Show what was heard, then reply (superseding any in-flight reply).
        await gateway.send(client_uid, FullTextEvent(text="🎤 " + text))
        old = reply_tasks.get(client_uid)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.ensure_future(_produce_and_send(client_uid, cid, text))
        reply_tasks[client_uid] = task

    async def on_event(event, action, client_uid):
        """Drive the preview: load the model on connect, demo speech on input."""
        cid = client_uid[:8]
        # Additive RTT probe (switchable-avatar-renderers R13.5): echo ping -> pong
        # so the StatusPanel can measure Network_Latency. pong is ignored.
        if isinstance(event, PingEvent):
            await gateway.send(client_uid, PongEvent(t=event.t))
            return
        if isinstance(event, PongEvent):
            return
        if isinstance(event, FetchConfigsEvent):
            logger.info("[%s] connected -> sending set-model '%s'", cid, info.name)
            # Advertise the STT model selector state to the front-end so the UI
            # dropdown can render the options and highlight the active one.
            mi = dict(model_info)
            mi["stt_enabled"] = stt_enabled
            mi["stt_model"] = stt_holder["size"]
            mi["stt_models"] = STT_MODELS
            mi["tts_enabled"] = bool(_EDGE_TTS_OK and not args.no_tts)
            mi["tts_voice"] = tts_holder["voice"]
            mi["tts_voices"] = TTS_VOICES
            mi["tts_rate"] = tts_holder["rate"]
            mi["persona"] = persona_holder["text"]
            # Live3D VRM source (additive R5/R13): lets the front-end load a 3D
            # VRM avatar when the user switches to the Live3D renderer.
            mi["vrm_url"] = vrm_url_holder["url"]
            await gateway.send(
                client_uid, SetModelEvent(model_info=mi, conf_name=info.name)
            )
            tts_note = (
                "形象会用中文语音回应、对口型、切换表情并做动作。"
                if (_EDGE_TTS_OK and not args.no_tts)
                else "形象会切换表情、做动作并演示口型（当前无 TTS，静音）。"
            )
            await gateway.send(
                client_uid,
                FullTextEvent(
                    text="omnilimb-face 预览已连接。在下方输入文字并回车，" + tts_note
                ),
            )
            # Pre-warm the chat worker so the first reply isn't delayed by the
            # one-time cold start (it warms while the user reads the greeting).
            if llm_enabled:
                asyncio.ensure_future(_get_worker())
        elif isinstance(event, TextInputEvent):
            # UI control channel: "::cmd::stt=<size>" switches the STT model
            # without being treated as a chat message.
            if event.text.startswith("::cmd::"):
                cmd = event.text[len("::cmd::"):]
                if cmd.startswith("stt="):
                    asyncio.ensure_future(_switch_stt(client_uid, cmd[4:]))
                elif cmd.startswith("tts="):
                    v = cmd[4:].strip()
                    if v in TTS_VOICES:
                        tts_holder["voice"] = v
                        logger.info("[%s] TTS voice -> %s", cid, v)
                        await gateway.send(client_uid, FullTextEvent(text=f"音色已切换：{TTS_VOICES[v]}"))
                elif cmd.startswith("ttsrate="):
                    r = cmd[len("ttsrate="):].strip()
                    if re.fullmatch(r"[+-]\d{1,3}%", r):
                        tts_holder["rate"] = r
                        logger.info("[%s] TTS rate -> %s", cid, r)
                elif cmd.startswith("persona="):
                    raw = cmd[len("persona="):]
                    try:
                        text = base64.b64decode(raw).decode("utf-8").strip()
                    except Exception:
                        text = ""
                    persona_holder["text"] = text or DEFAULT_PERSONA
                    logger.info("[%s] persona updated (%d chars)", cid, len(persona_holder["text"]))
                    # Rebuild the warm worker so the new persona takes effect.
                    w = worker_holder.get("w")
                    if w is not None:
                        try:
                            await w.stop()
                        except Exception:
                            pass
                        worker_holder["w"] = None
                    asyncio.ensure_future(_get_worker())
                    await gateway.send(client_uid, FullTextEvent(text="灵魂人格已更新 ✨"))
                return
            # New turn supersedes any in-flight reply (cancel it first).
            old = reply_tasks.get(client_uid)
            if old is not None and not old.done():
                old.cancel()
            task = asyncio.ensure_future(
                _produce_and_send(client_uid, cid, event.text)
            )
            reply_tasks[client_uid] = task
        elif isinstance(event, InterruptSignalEvent):
            # Barge-in: cancel the in-flight reply (stops LLM + suppresses its
            # audio) and tell the front-end to stop playback immediately.
            logger.info("[%s] interrupt (heard=%r)", cid, getattr(event, "at_text_index", None))
            w = worker_holder.get("w")
            if w is not None and w.alive():
                w.cancel()
            t = reply_tasks.get(client_uid)
            if t is not None and not t.done():
                t.cancel()
            await gateway.send(client_uid, ControlEvent(text="interrupt"))
        elif isinstance(event, MicAudioDataEvent):
            # Browser mic PCM (base64 int16). Accumulate until mic-audio-end.
            if stt_enabled:
                try:
                    chunk = base64.b64decode(event.audio)
                except Exception:
                    chunk = b""
                slot = mic_buffers.setdefault(client_uid, {"buf": b"", "sr": 16000})
                slot["buf"] += chunk
                slot["sr"] = int(getattr(event, "sample_rate", 16000) or 16000)
        elif isinstance(event, MicAudioEndEvent):
            slot = mic_buffers.pop(client_uid, None)
            if stt_enabled and slot and slot["buf"]:
                asyncio.ensure_future(
                    _handle_utterance(client_uid, cid, slot["buf"], slot["sr"])
                )

    gateway.set_on_event(on_event)

    http_scheme = "https" if args.https else "http"
    ws_scheme = "wss" if args.https else "ws"
    page_port = args.port if single_port else fe_port

    # In two-port mode start the separate static server first; in single-port
    # mode the gateway itself serves the page (started just below).
    if frontend is not None:
        fe_status = frontend.start()
        if not fe_status.running:
            logger.error("failed to start front-end server: %s", fe_status.message)
            return 1
    try:
        gateway.start_in_thread()
    except RuntimeError as exc:
        logger.error("failed to start /client-ws gateway: %s", exc)
        if frontend is not None:
            frontend.stop()
        return 1

    if single_port:
        url = f"{http_scheme}://{args.host}:{args.port}"
    else:
        url = fe_status.base_url or f"{http_scheme}://{args.host}:{fe_port}"
    ws_url = f"{ws_scheme}://{args.host}:{args.port}/client-ws"
    tts_state = (
        f"on (edge-tts, voice={args.voice})"
        if (_EDGE_TTS_OK and not args.no_tts)
        else ("off (--no-tts)" if args.no_tts else "off (edge-tts not installed)")
    )
    llm_state = (
        f"on (warm streaming worker via {Path(args.hermes_python).parent.parent.name})"
        if llm_enabled
        else "off (echo replies; pass --llm for real AI answers)"
    )
    ports_note = f"{args.port}" if single_port else f"{args.port}/{fe_port}"
    print("\n" + "=" * 60)
    print("  omnilimb-face front-end preview is running")
    if single_port:
        print(f"  mode:    single-port (page + /client-ws on :{args.port})")
    if args.host in ("0.0.0.0", "::"):
        print(f"  open (this machine): {http_scheme}://127.0.0.1:{page_port}/")
        lan = _lan_ips()
        if lan:
            print("  open (LAN, other devices):")
            for ip in lan:
                print(f"      {http_scheme}://{ip}:{page_port}/")
        else:
            print("  (could not detect a LAN IP)")
        print("  ⚠ LAN access notes:")
        print("    • Windows Firewall may prompt/need to allow port(s) "
              f"{ports_note} (Private network).")
        if args.https:
            print("    • HTTPS + single-port: phones can use the microphone (按住说话).")
            print("      First visit shows a self-signed cert warning — tap 高级/Advanced")
            print("      → 继续/Proceed once (only ONE cert now). Then mic is allowed.")
        else:
            print("    • Mic/voice only works on http://127.0.0.1 or HTTPS;")
            print("      over a plain-http LAN IP browsers block the microphone.")
            print("      Pass --https to enable the phone microphone over the LAN.")
            print("      Text chat + viewing work fine over LAN.")
    else:
        print(f"  open:    {url}/")
    print(f"  gateway: {ws_url}")
    print(f"  voice:   {tts_state}")
    print(f"  llm:     {llm_state}")
    print(
        "  stt:     "
        + (
            f"on (faster-whisper '{args.stt_model}', lang={args.stt_lang})"
            if stt_enabled
            else ("off (faster-whisper not installed)" if args.stt else "off (pass --stt)")
        )
    )
    print("  press Ctrl+C to stop")
    print("=" * 60 + "\n", flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(f"{url}/")
        except Exception:  # pragma: no cover - best effort
            pass

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping…", flush=True)
    finally:
        w = worker_holder.get("w")
        if w is not None and w.proc is not None and w.proc.returncode is None:
            try:
                w.proc.kill()
            except Exception:
                pass
        gateway.stop()
        if frontend is not None:
            frontend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
