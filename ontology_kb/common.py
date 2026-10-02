from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = {"cs", "econ", "eess", "math", "stat"}
ID_PATTERN = re.compile(r"(?P<base>\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v(?P<version>[1-9]\d*))?", re.I)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, content: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_config(root: Path):
    cfg = read_json(root / "config.json")
    if not cfg:
        raise ValueError(f"Missing config.json in {root}")
    if not cfg["subjects"] or not set(cfg["subjects"]) <= SUBJECTS:
        raise ValueError("subjects must be a nonempty subset of cs/econ/eess/math/stat")
    if not cfg["terms"] or any(not re.fullmatch(r'[\w\s"-]+', t) for t in cfg["terms"]):
        raise ValueError("terms must contain plain words, spaces, hyphens or a quoted phrase")
    for key, low, high in [("page_size", 1, 2000), ("title_page_size", 1, 50), ("chunk_chars", 1000, 12000)]:
        if not isinstance(cfg[key], int) or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
    if cfg["api_delay_seconds"] < 3 or cfg["content_delay_seconds"] < 15:
        raise ValueError("Minimum delays: API 3 seconds, content 15 seconds")
    if not 0 <= cfg["retries"] <= 5 or not 1 <= cfg["timeout_seconds"] <= 300:
        raise ValueError("Invalid retries/timeout")
    return cfg


def api_query(cfg):
    words = []
    for term in cfg["terms"]:
        # Unquoted multi-word input uses AND, as in the advanced search form.
        if term.startswith('"') and term.endswith('"'):
            words.append("all:" + term)
        else:
            words.extend("all:" + w for w in term.split())
    categories = " OR ".join(f"cat:{s}.*" for s in cfg["subjects"])
    return " AND ".join(words) + f" AND ({categories})"


def search_url(cfg):
    params = {"advanced": "1"}
    for i, term in enumerate(cfg["terms"]):
        params.update({f"terms-{i}-operator": "AND", f"terms-{i}-term": term, f"terms-{i}-field": "all"})
    names = {"cs": "computer_science", "econ": "economics", "eess": "eess", "math": "mathematics", "stat": "statistics"}
    params.update({f"classification-{names[s]}": "y" for s in cfg["subjects"]})
    params.update({"classification-physics_archives": "all", "classification-include_cross_list": "include",
                   "date-filter_by": "all_dates", "date-year": "", "date-from_date": "", "date-to_date": "",
                   "date-date_type": "submitted_date", "abstracts": "show", "size": "50", "order": "-announced_date_first"})
    return "https://arxiv.org/search/advanced?" + urlencode(params)


def parse_id(value: str):
    value = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf|html)/", "", value.strip())
    value = value.removesuffix(".pdf")
    match = ID_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid arXiv ID: {value}")
    return match["base"], int(match["version"]) if match["version"] else None


def safe_id(value: str):
    base, version = parse_id(value)
    return base.replace("/", "_") + (f"v{version}" if version else "")


def catalog(root: Path):
    return read_json(root / "data/catalog.json", {"schema_version": 1, "papers": {}, "last_sync": None})


@contextlib.contextmanager
def writer_lock(root: Path):
    """OS advisory lock: automatically released on crash, on Windows and macOS."""
    path = root / "data/.writer.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another update/fetch/reindex is running in this library") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
