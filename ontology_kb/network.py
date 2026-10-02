from __future__ import annotations

import datetime as dt
import email.utils
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .common import read_json, write_json


class FetchError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, cfg, root: Path, transport="auto"):
        self.cfg, self.root, self.transport = cfg, root, transport
        self.curl = shutil.which("curl.exe" if __import__("os").name == "nt" else "curl")

    def _throttle(self, content):
        stamp = self.root / "data/.last-request.json"
        last = read_json(stamp, {})
        delay = max(last.get("delay", 0), self.cfg["content_delay_seconds" if content else "api_delay_seconds"])
        time.sleep(max(0, last.get("time", 0) + delay - time.time()))
        write_json(stamp, {"time": time.time(), "delay": self.cfg["content_delay_seconds" if content else "api_delay_seconds"]})

    def _curl(self, url):
        if not self.curl:
            raise FetchError("curl not found; use --transport urllib or install curl")
        with tempfile.TemporaryDirectory() as directory:
            body, header = Path(directory) / "body", Path(directory) / "headers"
            result = subprocess.run([self.curl, "--silent", "--show-error", "--location", "--proto", "=https",
                                     "--proto-redir", "=https", "--max-time", str(self.cfg["timeout_seconds"]),
                                     "--user-agent", self.cfg["user_agent"], "--output", str(body),
                                     "--dump-header", str(header), "--write-out", "%{http_code}", url],
                                    capture_output=True, timeout=self.cfg["timeout_seconds"] + 10)
            if result.returncode:
                raise FetchError("curl: " + result.stderr.decode(errors="replace").strip())
            headers = {}
            for line in header.read_text(encoding="latin-1").splitlines():
                if line.startswith("HTTP/"):
                    headers = {}
                elif ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            return int(result.stdout), body.read_bytes(), headers

    def _request(self, url):
        if self.transport == "curl":
            return self._curl(url)
        try:
            with urlopen(Request(url, headers={"User-Agent": self.cfg["user_agent"]}),
                         timeout=self.cfg["timeout_seconds"]) as response:
                return response.status, response.read(), dict((k.lower(), v) for k, v in response.headers.items())
        except HTTPError as exc:
            return exc.code, exc.read(), dict((k.lower(), v) for k, v in exc.headers.items())
        except URLError as exc:
            # Preserve TLS verification; Windows curl uses the system certificate store.
            if self.transport == "auto" and isinstance(exc.reason, ssl.SSLCertVerificationError) and self.curl:
                self.transport = "curl"
                return self._curl(url)
            raise FetchError(str(exc)) from exc

    def get(self, url, *, content=False):
        if not url.startswith(("https://export.arxiv.org/api/query?", "https://arxiv.org/html/", "https://arxiv.org/pdf/")):
            raise FetchError("Only official arXiv API/content URLs are supported")
        for attempt in range(self.cfg["retries"] + 1):
            self._throttle(content)
            retry_after = None
            try:
                status, body, headers = self._request(url)
                if status == 200:
                    return body
                error = FetchError(f"HTTP {status}: {url}", status)
                if status not in (429, 500, 502, 503, 504):
                    raise error  # 403/404 must not trigger repeated requests.
                retry_after = headers.get("retry-after")
            except (URLError, TimeoutError, subprocess.TimeoutExpired) as exc:
                error = FetchError(str(exc))
            except FetchError as exc:
                if exc.status is not None:
                    raise
                error = exc
            if attempt == self.cfg["retries"]:
                raise error
            delay = 5 * (2 ** attempt)
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(delay, (email.utils.parsedate_to_datetime(retry_after) - dt.datetime.now(dt.timezone.utc)).total_seconds())
                    except (TypeError, ValueError):
                        pass
            if delay > 120:
                raise FetchError(f"Server requests a {delay:.0f}s pause. Retry later.", 429)
            print(f"Request failed; retry {attempt + 1}/{self.cfg['retries']} in {delay:.0f}s", flush=True)
            time.sleep(delay)
        raise FetchError("Request failed")
