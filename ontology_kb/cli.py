from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .common import ROOT, catalog, load_config, parse_id, read_json, safe_id, search_url, writer_lock
from .index import TOPICS, build, card, format_results, search
from .network import Client
from .sync import update


def bounded(value):
    number = int(value)
    if not 500 <= number <= 100000:
        raise argparse.ArgumentTypeError("Use a character budget between 500 and 100000")
    return number


def positive(value):
    number = int(value)
    if not 1 <= number <= 50:
        raise argparse.ArgumentTypeError("Use a value between 1 and 50")
    return number


def get_paper(data, value):
    base, requested = parse_id(value)
    if base not in data["papers"]:
        raise ValueError(f"{base} is not in this catalog. Run update first.")
    paper = data["papers"][base]
    if requested and requested != paper["version"]:
        raise ValueError(f"Catalog describes v{paper['version']}; use the explicit old cache path to read another version.")
    return paper


def parser():
    p = argparse.ArgumentParser(description="Local arXiv ontology knowledge base (Python 3.10+)")
    p.add_argument("--root", type=Path, default=ROOT, help="Library folder; defaults to this script's folder")
    sub = p.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("update", help="Scan all matching metadata and merge new/revised papers")
    sync.add_argument("--restart", action="store_true", help="Discard interrupted scan position and fetch from page 1")
    sync.add_argument("--transport", choices=["auto", "urllib", "curl"], default="auto")
    sub.add_parser("reindex", help="Rebuild portable cards and indexes from the local catalog")
    sub.add_parser("status", help="Show counts and sync status")
    sub.add_parser("query", help="Print exact API query and original advanced-search URL")
    s = sub.add_parser("search", help="Offline BM25 search; only a few titles are returned")
    s.add_argument("query")
    s.add_argument("--limit", type=positive, default=12)
    s.add_argument("--abstracts", action="store_true")
    s.add_argument("--max-chars", type=bounded, default=12000)
    s.add_argument("--topic", choices=list(TOPICS))
    s.add_argument("--year", type=int)
    s.add_argument("--include-missing", action="store_true", help="Include historical records absent from latest scan")
    show = sub.add_parser("show", help="Read the original abstract for selected papers")
    show.add_argument("ids", nargs="+")
    show.add_argument("--max-chars", type=bounded, default=16000)
    fetch = sub.add_parser("fetch", help="Download selected full texts, cache and chunk them")
    fetch.add_argument("ids", nargs="*")
    fetch.add_argument("--all", action="store_true", help="Fetch every current paper serially; may take many hours")
    fetch.add_argument("--format", choices=["auto", "html", "pdf"], default="auto")
    fetch.add_argument("--transport", choices=["auto", "urllib", "curl"], default="auto")
    read = sub.add_parser("read", help="Read a cached contents page or one full-text chunk")
    read.add_argument("id")
    read.add_argument("--chunk", type=int)
    read.add_argument("--max-chars", type=bounded, default=8000)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    root = args.root.resolve()
    try:
        cfg = load_config(root)
        if args.command == "query":
            from .common import api_query
            print(api_query(cfg))
            print(search_url(cfg))
        elif args.command == "update":
            with writer_lock(root):
                data, report = update(root, cfg, Client(cfg, root, args.transport), args.restart,
                                      progress=lambda msg: print(msg, flush=True))
                build(root, cfg, data)
                print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "reindex":
            with writer_lock(root):
                print(f"Indexed {build(root, cfg)} papers")
        elif args.command == "status":
            data = catalog(root)
            current = [p for p in data["papers"].values() if p.get("in_latest_search", True)]
            cached = sum((root / f"papers/{safe_id(p['id'])}/{safe_id(p['id'])}v{p['version']}/manifest.json").exists() for p in current)
            print(json.dumps({"last_sync_utc": data["last_sync"], "papers_stored": len(data["papers"]),
                              "papers_in_latest_search": len(current), "current_fulltexts_cached": cached,
                              "interrupted_scan": (root / "data/sync-progress.json").exists(),
                              "index_state": read_json(root / "data/index-state.json"),
                              "last_report": data.get("last_report")}, ensure_ascii=False, indent=2))
        elif args.command == "search":
            results = search(catalog(root)["papers"], args.query, args.limit, args.topic, args.year, args.include_missing)
            print(format_results(results, args.abstracts, args.max_chars))
        elif args.command == "show":
            data = catalog(root)
            marker = "[输出预算不足，剩余卡片未显示；请单篇读取或增加 --max-chars。]"
            remaining = args.max_chars - len(marker) - 1
            for value in args.ids:
                text = card(get_paper(data, value))
                if len(text) > remaining:
                    print(marker)
                    break
                print(text)
                remaining -= len(text) + 1
        elif args.command == "fetch":
            from .fulltext import fetch_paper
            if bool(args.ids) == args.all:
                raise ValueError("Provide either one or more IDs, or --all")
            import importlib.util
            required = ["pypdf"] if args.format == "pdf" else ["bs4"] if args.format == "html" else ["bs4", "pypdf"]
            if any(importlib.util.find_spec(name) is None for name in required):
                raise RuntimeError("Install full-text support first: python -m pip install -r requirements.txt (or use the .venv Python)")
            with writer_lock(root):
                data = catalog(root)
                papers = [p for p in data["papers"].values() if p.get("in_latest_search", True)] if args.all else [get_paper(data, i) for i in args.ids]
                client = Client(cfg, root, args.transport)
                failures = []
                for i, paper in enumerate(papers, 1):
                    try:
                        path = fetch_paper(root, cfg, client, paper, args.format)
                        print(f"[{i}/{len(papers)}] {path}", flush=True)
                    except (RuntimeError, ValueError, OSError) as exc:
                        failures.append(paper["id"])
                        print(f"FAILED {paper['id']}: {exc}", file=sys.stderr, flush=True)
                        # Do not continue issuing requests following access denial/rate limiting.
                        if getattr(exc, "status", None) in (403, 429):
                            raise
                if failures:
                    print("Retry failed IDs: " + " ".join(failures), file=sys.stderr)
                    return 1
        elif args.command == "read":
            from .fulltext import read_chunk
            print(read_chunk(root, get_paper(catalog(root), args.id), args.chunk, args.max_chars))
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Completed downloads/pages are retained; rerun to resume.", file=sys.stderr)
        return 130
