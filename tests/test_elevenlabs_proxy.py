from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tts.elevenlabs import request_elevenlabs_audio


class RotatorStubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        self.server.calls.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        status, payload, content_type = self.server.responses.pop(0)  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class RotatorStub:
    def __init__(self, responses: list[tuple[int, bytes, str]]) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), RotatorStubHandler)
        self.server.responses = list(responses)  # type: ignore[attr-defined]
        self.server.calls = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RotatorStub":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/proxy/elevenlabs"

    @property
    def calls(self) -> list[dict]:
        return self.server.calls  # type: ignore[attr-defined]


class ElevenLabsProxyTests(unittest.TestCase):
    def test_proxy_request_injects_proxy_key_not_elevenlabs_key(self) -> None:
        with RotatorStub([(200, b"audio-bytes", "audio/mpeg")]) as stub, tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "line_0.mp3"
            request_elevenlabs_audio(
                "Xin chao",
                output,
                api_key=None,
                voice_id="voice_1",
                model_id="eleven_v3",
                output_format="mp3_44100_128",
                config={
                    "proxy_base_url": stub.base_url,
                    "proxy_key": "rotator-secret",
                    "voice_settings": {
                        "stability": 0.45,
                        "similarity_boost": 0.8,
                    },
                },
                previous_text="Cau truoc",
                next_text="Cau sau",
            )

            self.assertEqual(output.read_bytes(), b"audio-bytes")
            self.assertEqual(len(stub.calls), 1)
            call = stub.calls[0]
            self.assertEqual(
                call["path"],
                "/proxy/elevenlabs/v1/text-to-speech/voice_1?output_format=mp3_44100_128",
            )
            self.assertEqual(call["headers"].get("X-Proxy-Key"), "rotator-secret")
            self.assertNotIn("xi-api-key", {key.lower() for key in call["headers"]})
            payload = json.loads(call["body"].decode("utf-8"))
            self.assertEqual(payload["text"], "Xin chao")
            self.assertEqual(payload["model_id"], "eleven_v3")
            self.assertEqual(payload["previous_text"], "Cau truoc")
            self.assertEqual(payload["next_text"], "Cau sau")
            self.assertEqual(payload["voice_settings"]["stability"], 0.45)

    def test_proxy_retries_transient_statuses(self) -> None:
        responses = [
            (429, b'{"error":"rate limit"}', "application/json"),
            (200, b"audio-after-retry", "audio/mpeg"),
        ]
        with RotatorStub(responses) as stub, tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "line_0.mp3"
            request_elevenlabs_audio(
                "Retry me",
                output,
                api_key=None,
                voice_id="voice_1",
                model_id="eleven_v3",
                output_format="mp3_44100_128",
                config={
                    "proxy_base_url": stub.base_url,
                    "proxy_key": "rotator-secret",
                    "proxy_retries": 2,
                },
            )

            self.assertEqual(output.read_bytes(), b"audio-after-retry")
            self.assertEqual(len(stub.calls), 2)


if __name__ == "__main__":
    unittest.main()
