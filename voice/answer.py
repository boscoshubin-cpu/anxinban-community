"""Answer one transferred AudioSocket call with locally synthesized Mandarin."""
import re
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import numpy as np


GREETING = "您好，我是安心伴，机主的智能电话助手。我会把来意转成文字给机主看。请问您是哪位，找机主有什么事？"
RISK_REPLY = "这类事情需要机主通过官方渠道核实。我不能提供验证码，也不能代办付款。再见。"
MESSAGE_REPLY = "谢谢，您的来意已记下，机主可以稍后查看。再见。"
REPEAT = "刚才没有听清，请简短说一下您找机主有什么事。"
GOODBYE = "暂时没有听清，本次通话到这里。再见。"
RISK_WORDS = re.compile(r"验[证證][码马]|驗證[碼馬]|转账|轉[賬帳]|汇款|匯款|安全[账帳][户戶]|屏幕共享|共享屏幕|共享螢幕|螢幕共享")


def reply_for(text):
    return RISK_REPLY if RISK_WORDS.search(text) else MESSAGE_REPLY


def synthesize(line, folder):
    path = Path(folder) / "prompt.aiff"
    subprocess.run(["say", "-v", "Tingting", "-o", str(path), line], check=True)
    return subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-ar", "8000",
        "-ac", "1", "-f", "s16le", "-"
    ])


def prepare_responses():
    with tempfile.TemporaryDirectory() as folder:
        return {line: synthesize(line, folder)
                for line in (GREETING, RISK_REPLY, MESSAGE_REPLY, REPEAT, GOODBYE)}


def read_frame(stream):
    header = stream.read(3)
    if not header:
        return 0, b""
    if len(header) != 3:
        raise ValueError("incomplete header")
    kind, length = struct.unpack(">BH", header)
    data = stream.read(length)
    if len(data) != length:
        raise ValueError("incomplete packet")
    return kind, data


def play(sock, pcm):
    deadline = time.monotonic()
    for start in range(0, len(pcm), 320):
        chunk = pcm[start:start + 320]
        sock.sendall(struct.pack(">BH", 0x10, len(chunk)) + chunk)
        deadline += len(chunk) / 16000
        time.sleep(max(0, deadline - time.monotonic()))


def listen(stream):
    """Collect one utterance, ending on a pause, hangup or the time limit."""
    buf = bytearray()
    quiet = 0
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline and len(buf) < 16 * 16000:
        kind, block = read_frame(stream)
        if kind in (0, 0xff):
            return buf, True
        if kind == 3:
            continue
        if kind != 0x10 or not block or len(block) % 2:
            raise ValueError("invalid audio frame")
        pcm = np.frombuffer(block, dtype="<i2").astype(np.float32)
        speaking = np.sqrt(np.mean(pcm ** 2)) > 110
        if speaking or buf:
            buf.extend(block)
            quiet = 0 if speaking else quiet + len(block)
            if quiet >= 16000:
                break
    return buf, False


def answer(handler, prompts, recognize, publish):
    messages = []
    heard = []

    def status(phase):
        publish(phase, " ".join(heard), "\n".join(messages))

    def speak(line):
        messages.append("安心伴：" + line)
        status("active")
        play(handler.request, prompts[line])

    kind, data = read_frame(handler.rfile)
    if kind != 1:
        return
    uuid.UUID(bytes=data)
    try:
        speak(GREETING)
        for attempt in range(2):
            audio, ended = listen(handler.rfile)
            text = recognize(audio) if audio else ""
            if text:
                heard.append(text)
                messages.append("来电方：" + text)
                status("active")
            if ended:
                break
            if text:
                speak(reply_for(text))
                break
            speak(GOODBYE if attempt else REPEAT)
        handler.request.sendall(b"\x00\x00\x00")
        status("ended")
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        status("ended")
    except (OSError, ValueError, RuntimeError):
        status("failed")
