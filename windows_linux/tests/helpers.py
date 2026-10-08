"""Shared test fixtures: a throw-away MUSICDL_HOME, synthetic audio made with ffmpeg, a local file server."""
import http.server
import os
import random
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HOME = tempfile.mkdtemp(prefix="musicdl_test_home_")
os.environ["MUSICDL_HOME"] = HOME                 # settings / secrets / cache never touch the real profile
os.environ["MUSICDL_NO_PREWARM"] = "1"            # a job never opens connections to the real services ahead of time

from musicdl import platform_  # noqa: E402

FFMPEG = platform_.find_tool("ffmpeg")
NO_WINDOW = platform_.NO_WINDOW


def make_audio(path, seconds, *args):
    """Synthesise a sine tone with ffmpeg (extra encoder args select format/bitrate)."""
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", *args, path],
                   check=True, creationflags=NO_WINDOW)


def make_noise(path, seconds, *args, color="pink"):
    """Full-band music-like material (noise reaches 22 kHz, like a real lossless recording) as a stereo file."""
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"anoisesrc=color={color}:amplitude=0.5:duration={seconds}:sample_rate=44100", "-ac", "2", *args, path],
                   check=True, creationflags=NO_WINDOW)


def make_lossy_origin(path, seconds, kbps=128, encoder="libmp3lame"):
    """A *lossless* file (the extension decides) holding sound that went through a lossy encoder first — the 'FLAC
    made from an MP3' that a lossless claim cannot be trusted for."""
    mid = os.path.join(os.path.dirname(path), f"_origin_{os.path.basename(path)}.{'mp3' if 'mp3' in encoder else 'm4a'}")
    make_noise(mid, seconds, "-c:a", encoder, "-b:a", f"{kbps}k")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", mid, "-c:a", "flac" if path.endswith(".flac") else "pcm_s16le", path],
                   check=True, creationflags=NO_WINDOW)
    os.remove(mid)


class FileServer:
    """Serves a folder on 127.0.0.1 with an optional per-request delay."""

    def __init__(self, directory):
        self.delay = 0.0
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=directory, **k)

            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(outer.delay)
                try:
                    super().do_GET()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class RangeServer:
    """A file server that behaves like the ones downloads meet: byte ranges, a speed limit per connection, and faults on
    demand. Keep-alive (HTTP/1.1), so connection reuse is real.

    rate         bytes/second one connection may use (None = unlimited)
    ranges       honour Range requests (and say so)
    cuts         one entry per response, in order: None = send it all, N = hang up after N body bytes
    fail         path -> [status, times, retry_after]: answer that many requests with the status instead of the file
    log          (path, Range header) of every request;  peak: most connections served at once
    """

    def __init__(self, directory, rate=None, ranges=True):
        self.rate, self.ranges, self.cuts, self.fail, self.log = rate, ranges, [], {}, []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle(self):
                try:
                    super().handle()
                except OSError:                                    # the client hung up (tests do that on purpose)
                    pass

            def do_GET(self):
                path = self.path.split("?")[0]
                with outer.lock:
                    outer.log.append((path, self.headers.get("Range", "")))
                    outer.active += 1
                    outer.peak = max(outer.peak, outer.active)
                try:
                    self.serve(path)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True
                finally:
                    with outer.lock:
                        outer.active -= 1

            def reply(self, code, body=b"", **headers):
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k.replace("_", "-"), str(v))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def serve(self, path):
                with outer.lock:
                    spec = outer.fail.get(path)
                    firing = bool(spec and spec[1] > 0)
                    if firing:
                        spec[1] -= 1
                if firing:
                    extra = {"Retry-After": spec[2]} if len(spec) > 2 and spec[2] is not None else {}
                    return self.reply(spec[0], b"no", **extra)
                f = os.path.join(directory, path.lstrip("/"))
                if not os.path.isfile(f):
                    return self.reply(404, b"missing")
                with open(f, "rb") as fh:
                    data = fh.read()
                total, a, b, code = len(data), 0, len(data) - 1, 200
                m = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range", "")) if outer.ranges else None
                if m:
                    a = int(m.group(1))
                    b = min(total - 1, int(m.group(2))) if m.group(2) else total - 1
                    if a >= total:
                        return self.reply(416, b"", Content_Range=f"bytes */{total}")
                    code = 206
                body = data[a:b + 1]
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"v1"')
                if outer.ranges:
                    self.send_header("Accept-Ranges", "bytes")
                if code == 206:
                    self.send_header("Content-Range", f"bytes {a}-{b}/{total}")
                self.end_headers()
                with outer.lock:
                    cut = outer.cuts.pop(0) if outer.cuts else None
                sent = 0
                for i in range(0, len(body), 16384):
                    chunk = body[i:i + 16384]
                    if cut is not None and sent + len(chunk) > cut:
                        self.wfile.write(chunk[:max(0, cut - sent)])
                        self.wfile.flush()
                        self.close_connection = True              # hang up mid-body
                        return
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if outer.rate:
                        time.sleep(len(chunk) / outer.rate)

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def requests_for(self, path):
        with self.lock:
            return [r for p, r in self.log if p == "/" + path.lstrip("/")]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def dead_url():
    """A URL on a port nothing listens on (connections are refused at once)."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/"


def noise_cover(path, size=600):
    from PIL import Image
    random.seed(1)
    Image.effect_noise((size, size), 80).convert("RGB").save(path)


def fresh_dir(name):
    d = os.path.join(HOME, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d
