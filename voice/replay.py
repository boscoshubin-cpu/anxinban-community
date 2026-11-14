"""Feed a telephone-quality WAV into the local AudioSocket receiver in real time."""
import argparse
import socket
import struct
import time
import uuid
import wave


def send(stream, kind, payload=b""):
    stream.sendall(struct.pack(">BH", kind, len(payload)) + payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav")
    args = parser.parse_args()
    with wave.open(args.wav, "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (8000, 1, 2):
            parser.error("Use an 8 kHz, mono, signed 16-bit PCM WAV.")
        with socket.create_connection(("127.0.0.1", 9092), timeout=10) as stream:
            send(stream, 1, uuid.uuid4().bytes)
            while data := wav.readframes(160):
                send(stream, 0x10, data)
                time.sleep(len(data) / 16000)
            send(stream, 0)
