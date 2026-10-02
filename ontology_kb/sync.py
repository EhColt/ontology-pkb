from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

from .common import api_query, atomic_write, catalog, digest, now, parse_id, read_json, search_url, write_json

NS = {"a": "http://www.w3.org/2005/Atom", "o": "http://a9.com/-/spec/opensearch/1.1/", "x": "http://arxiv.org/schemas/atom"}


def clean(text):
    return " ".join((text or "").split())


def parse_feed(raw: bytes):
    try:
        feed = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("arXiv returned invalid XML; no catalog changes were made") from exc
    if feed.tag != "{" + NS["a"] + "}feed":
        raise ValueError("Response is not an Atom feed")
    papers = []
    for entry in feed.findall("a:entry", NS):
        url = entry.findtext("a:id", "", NS)
        if "/api/errors" in url:
            raise ValueError("arXiv API error: " + entry.findtext("a:summary", "Unknown", NS))
        base, version = parse_id(url)
        if version is None:
            raise ValueError(f"API omitted version for {base}")
        record = {"id": base, "version": version,
                  "title": clean(entry.findtext("a:title", "", NS)),
                  "abstract": clean(entry.findtext("a:summary", "", NS)),
                  "published": entry.findtext("a:published", "", NS),
                  "updated": entry.findtext("a:updated", "", NS),
                  "authors": [clean(a.findtext("a:name", "", NS)) for a in entry.findall("a:author", NS)],
                  "categories": sorted({c.attrib["term"] for c in entry.findall("a:category", NS)}),
                  "primary_category": "", "comment": clean(entry.findtext("x:comment", "", NS)),
                  "doi": clean(entry.findtext("x:doi", "", NS)),
                  "journal_ref": clean(entry.findtext("x:journal_ref", "", NS)),
                  "abs_url": f"https://arxiv.org/abs/{base}v{version}",
                  "pdf_url": f"https://arxiv.org/pdf/{base}v{version}"}
        primary = entry.find("x:primary_category", NS)
        if primary is not None:
            record["primary_category"] = primary.attrib["term"]
        if not record["title"] or not record["abstract"] or not record["categories"]:
            raise ValueError(f"Incomplete metadata for {base}")
        for date in ("published", "updated"):
            dt.datetime.fromisoformat(record[date].replace("Z", "+00:00"))
        record["year_month"] = record["published"][:7]
        papers.append(record)
    try:
        total = int(feed.findtext("o:totalResults", "", NS))
        start = int(feed.findtext("o:startIndex", "", NS))
    except ValueError as exc:
        raise ValueError("Feed lacks valid totalResults/startIndex") from exc
    if total < 0 or start < 0:
        raise ValueError("Invalid pagination values")
    return total, start, papers


def merge(old, incoming, stamp):
    """Merge a complete scan; never remove old records or regress versions."""
    result = {k: dict(v) for k, v in old.items()}
    counts = {"added": 0, "updated": 0, "unchanged": 0, "not_in_latest_search": 0}
    ids = {p["id"] for p in incoming}
    for paper in incoming:
        previous = old.get(paper["id"])
        if previous and (paper["version"], paper["updated"]) < (previous["version"], previous["updated"]):
            result[paper["id"]].update(last_seen=stamp, in_latest_search=True)
            counts["unchanged"] += 1
            continue
        fields = {k: previous.get(k) for k in paper} if previous else None
        state = "added" if previous is None else "unchanged" if fields == paper else "updated"
        counts[state] += 1
        result[paper["id"]] = dict(paper, first_seen=previous["first_seen"] if previous else stamp,
                                   last_seen=stamp, in_latest_search=True)
    for key in result.keys() - ids:
        result[key]["in_latest_search"] = False
        counts["not_in_latest_search"] += 1
    return result, counts


def update(root: Path, cfg, client, restart=False, progress=print):
    query = api_query(cfg)
    identity = digest({"query": query, "page_size": cfg["page_size"]})
    old = catalog(root)
    if old.get("query") and old["query"] != query:
        raise ValueError("Search criteria changed. Use a separate library folder to keep provenance unambiguous.")
    pending_path = root / "data/sync-progress.json"
    pending = read_json(pending_path)
    if pending and not restart:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(pending["started_at"])
        if pending["identity"] != identity or age.total_seconds() > 6 * 3600:
            pending = None
    else:
        pending = None
    if not pending:
        pending = {"identity": identity, "started_at": now(), "next_start": 0, "total": None, "papers": []}
    elif pending["next_start"]:
        progress(f"Resuming at {pending['next_start']} / {pending['total']}")
    run_id = pending["started_at"].replace(":", "-")
    seen = {p["id"] for p in pending["papers"]}
    while pending["total"] is None or pending["next_start"] < pending["total"]:
        offset = pending["next_start"]
        url = "https://export.arxiv.org/api/query?" + urlencode({"search_query": query, "start": offset,
              "max_results": cfg["page_size"], "sortBy": "submittedDate", "sortOrder": "descending"})
        raw = client.get(url)
        total, start, papers = parse_feed(raw)
        if total > 30000:
            raise ValueError("Search exceeds the API 30,000-result window; split the query before syncing")
        if pending["total"] is not None and total != pending["total"]:
            raise ValueError("Search count changed during pagination. Run update --restart to restart safely.")
        if start != offset or (not papers and offset < total) or len(papers) > cfg["page_size"]:
            raise ValueError("Unexpected/empty page. Existing catalog is intact; retry update or use --restart.")
        for paper in papers:
            if paper["id"] in seen:
                raise ValueError("Duplicate result across pages. Run update --restart; catalog is intact.")
            if not any(c.split(".")[0] in cfg["subjects"] for c in paper["categories"]):
                raise ValueError(f"API returned out-of-scope categories for {paper['id']}")
            seen.add(paper["id"])
        atomic_write(root / f"data/raw/{run_id}/{offset:06d}.xml", raw)
        pending["papers"].extend(papers)
        pending["total"], pending["next_start"] = total, offset + len(papers)
        write_json(pending_path, pending)
        progress(f"Fetched {pending['next_start']} / {total}")
    if len(seen) != pending["total"]:
        raise ValueError("Unique result count differs from API total; catalog was not changed")
    stamp = now()
    merged, counts = merge(old["papers"], pending["papers"], stamp)
    report = dict(counts, started_at=pending["started_at"], completed_at=stamp,
                  total_results=pending["total"], total_stored=len(merged), query=query,
                  source="https://export.arxiv.org/api/query", web_search_url=search_url(cfg))
    new = {"schema_version": 1, "query": query, "last_sync": stamp, "last_report": report, "papers": merged}
    if old["papers"]:
        write_json(root / "data/catalog.previous.json", old)
    # Canonical catalog commits in one atomic replace; generated views can be rebuilt.
    write_json(root / "data/catalog.json", new)
    write_json(root / f"data/runs/{run_id}.json", report)
    pending_path.unlink(missing_ok=True)
    return new, report
