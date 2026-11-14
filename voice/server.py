"""Single-family, loopback-only telephone audio prototype."""
from answer import GREETING, MESSAGE_REPLY, REPEAT, RISK_REPLY, answer, prepare_responses, reply_for, synthesize

import argparse
import io
import wave
import json
import socketserver
import subprocess
import tempfile
import struct
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from pywhispercpp.model import Model

PAGE = Path(__file__).resolve().parents[1] / "index.html"
RATE = 8000
WINDOW = RATE * 2 * 6
LOCK = threading.Lock()
CALL = threading.Lock()
state = {"dialogue": "", "phase": "idle", "text": ""}


def update(phase, text, dialogue=""):
    with LOCK:
        state.update(phase=phase, text=text[-3000:], dialogue=dialogue)


def recognize(audio):
    pcm = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768
    if len(pcm) < RATE // 3 or np.sqrt(np.mean(pcm ** 2)) < 0.002:
        return ""
    samples = np.interp(np.arange(len(pcm) * 2) / 2, np.arange(len(pcm)), pcm)
    segments = model.transcribe(samples.astype(np.float32))
    return "".join(segment.text.strip() for segment in segments)


class Telephone(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(20)
        if not CALL.acquire(blocking=False):
            return
        started = False
        transcript = ""
        buf = bytearray()
        try:
            while True:
                header = self.rfile.read(3)
                if not header:
                    break
                if len(header) != 3:
                    raise ValueError("short header")
                kind, size = struct.unpack(">BH", header)
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("short frame")
                if not started:
                    if kind != 1:
                        raise ValueError("missing call id")
                    uuid.UUID(bytes=data)
                    started = True
                    update("active", "")
                elif kind == 0 and size == 0:
                    break
                elif kind == 3 and size == 1:
                    continue
                elif kind == 0x10 and size % 2 == 0:
                    buf.extend(data)
                    while len(buf) >= WINDOW:
                        transcript = (transcript + recognize(bytes(buf[:WINDOW])))[-3000:]
                        del buf[:WINDOW]
                        update("active", transcript)
                else:
                    raise ValueError("unsupported frame")
            if started:
                update("ended", transcript + recognize(buf))
        except (OSError, ValueError, RuntimeError):
            if started:
                update("failed", transcript)
        finally:
            CALL.release()


class TransferredCall(socketserver.StreamRequestHandler):
    def handle(self):
        if not CALL.acquire(blocking=False):
            return
        try:
            self.request.settimeout(20)
            answer(self, prompts, recognize, update)
        except (ValueError, OSError):
            pass
        finally:
            CALL.release()


class LocalAudioServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Page(BaseHTTPRequestHandler):
    def local_host(self):
        return self.headers.get("Host") in {
            f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"
        }

    def do_GET(self):
        if not self.local_host():
            self.send_error(403)
            return
        if self.path in ("/", "/index.html"):
            data, mime = PAGE.read_bytes(), "text/html"
        elif self.path == "/try":
            data, mime = (PAGE.parent / "voice/trial.html").read_bytes(), "text/html"
        elif self.path in prompt_files:
            data, mime = prompt_files[self.path], "audio/wav"
        elif self.path in examples:
            data, mime = examples[self.path]
        elif self.path == "/voice/status":
            with LOCK:
                result = {k: state[k] for k in ("phase", "text", "dialogue")}
            data, mime = json.dumps(result).encode(), "application/json"
        else:
            self.send_error(404)
            return
        self.respond(data, mime)

    def respond(self, data, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.local_host() or self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
            self.send_error(403)
            return
        if self.path != "/voice/trial":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if self.headers.get_content_type() != "application/octet-stream" or not 2 <= length <= 320000 or length % 2:
            self.send_error(400)
            return
        self.connection.settimeout(8)
        audio = self.rfile.read(length)
        if len(audio) != length:
            self.send_error(400)
            return
        if not CALL.acquire(blocking=False):
            self.send_error(409)
            return
        try:
            text = recognize(audio)
            reply = reply_for(text) if text else REPEAT
            if text:
                update("ended", text, f"安心伴：{GREETING}\n来电方：{text}\n安心伴：{reply}")
            result = {"text": text, "reply": reply, "audio": prompt_paths[reply]}
            self.respond(json.dumps(result).encode(), "application/json")
        except RuntimeError:
            self.send_error(503)
        finally:
            CALL.release()

    def log_message(self, *args):
        pass


def as_wav(pcm):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return buffer.getvalue()


def make_examples(prompts):
    scenarios = {
        "refund": "您好，我是平台客服。请把短信验证码告诉我。现在需要打开屏幕共享，才能办理退款。请不要挂断电话。",
        "parcel": "您好，我是送快递的。您的包裹已经放在楼下的快递柜了。"
    }
    files = {}
    pause = bytes(RATE)
    with tempfile.TemporaryDirectory() as folder:
        for name, line in scenarios.items():
            caller = synthesize(line, folder)
            if name == "refund":
                files["/voice/examples/caller.wav"] = as_wav(caller), "audio/wav"
            conversation = pause.join((prompts[GREETING], caller, prompts[reply_for(line)]))
            mp3 = subprocess.run([
                "ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1",
                "-i", "pipe:0", "-ar", "16000", "-b:a", "32k", "-f", "mp3", "pipe:1"
            ], input=conversation, stdout=subprocess.PIPE, check=True)
            files[f"/voice/examples/{name}.mp3"] = mp3.stdout, "audio/mpeg"
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    args = parser.parse_args()
    if not args.model.is_file():
        parser.error("Supply a downloaded multilingual whisper.cpp model.")
    model = Model(str(args.model.resolve()), language="zh", no_context=True,
                  initial_prompt="以下是简体中文电话对话。", print_progress=False,
                  print_realtime=False, print_timestamps=False)
    prompts = prepare_responses()
    prompt_paths = {GREETING: "/voice/greeting.wav", MESSAGE_REPLY: "/voice/message.wav",
                    RISK_REPLY: "/voice/risk.wav", REPEAT: "/voice/repeat.wav"}
    prompt_files = {path: as_wav(prompts[line]) for line, path in prompt_paths.items()}
    examples = make_examples(prompts)
    with LocalAudioServer(("127.0.0.1", 9093), TransferredCall) as assistant, LocalAudioServer(("127.0.0.1", 9092), Telephone) as pbx:
        threading.Thread(target=assistant.serve_forever, daemon=True).start()
        threading.Thread(target=pbx.serve_forever, daemon=True).start()
        ThreadingHTTPServer(("127.0.0.1", 8766), Page).serve_forever()
