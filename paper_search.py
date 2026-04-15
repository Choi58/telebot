from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    from tqdm import tqdm
except ImportError:
    class _NoopPbar:
        def __init__(self, total: int | None = None, desc: str = "") -> None:
            self.total = total
            self.desc = desc

        def update(self, n: int = 1) -> None:
            _ = n

        def close(self) -> None:
            return

    def tqdm(iterable=None, total: int | None = None, desc: str = "", unit: str = "it", leave: bool = False):
        _ = (unit, leave)
        if iterable is not None:
            return iterable
        return _NoopPbar(total=total, desc=desc)

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
S2_GRAPH_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,year,venue,citationCount,influentialCitationCount,fieldsOfStudy,externalIds,url"

# -----------------------
# Fixed Configuration
# -----------------------
CATEGORIES = ["q-fin.ST", "q-fin.PM", "q-fin.TR"]
ARXIV_PAGE_SIZE = 100
ARXIV_MAX_PAGES_PER_QUERY = 8
ARXIV_SORT_BY = "submittedDate"
ARXIV_SORT_ORDER = "descending"
ARXIV_REQUEST_PAUSE_SEC = 5

S2_API_KEY = ""  # Optional: put your Semantic Scholar API key here.
S2_CACHE_PATH = Path("cache/s2/s2_cache.json")
S2_PAUSE_SEC = 0.25
S2_BATCH_SIZE = 100
S2_FALLBACK_MAX = 300

# Relevance terms for 2020~2025 selection
KEYWORDS = [
    "portfolio",
    "trading",
    "market",
    "microstructure",
    "statistical",
    "factor",
    "risk",
    "volatility",
    "liquidity",
    "alpha",
    "returns",
    "optimization",
    "forecast",
    "reinforcement learning",
    "agent",
]

FIELDS_BOOST = {"Finance", "Economics", "Computer Science", "Mathematics"}
RECENT_RELEVANCE_THRESHOLD = 5

RECENT_MIN_CITATIONS_BY_YEAR = {
    2020: 80,
    2021: 60,
    2022: 45,
    2023: 30,
    2024: 20,
    2025: 10,
}

PAST_YEAR_MIN = 2005
PAST_YEAR_MAX = 2019
PAST_MIN_CITATIONS = 100
RECENT_YEAR_MIN = 2020
RECENT_YEAR_MAX = 2025

DOWNLOAD_DIR = Path("Papers")
DOWNLOAD_PAUSE_SEC = 0.7
REQUEST_RETRIES = 5
INITIAL_BACKOFF_SEC = 1.0


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _normalize_title(s: str) -> str:
    base = _clean_text(s).lower()
    base = re.sub(r"[^a-z0-9가-힣 ]+", " ", base)
    return re.sub(r"\s+", " ", base).strip()


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_json_save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _http_get_bytes(url: str, *, retries: int = REQUEST_RETRIES, initial_sleep_sec: float = INITIAL_BACKOFF_SEC, timeout_sec: int = 45) -> bytes:
    sleep_sec = max(0.1, initial_sleep_sec)
    for i in range(retries + 1):
        req = urllib.request.Request(url=url, headers={"User-Agent": "telebot-paper-search/1.0"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and i < retries:
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_sec = max(sleep_sec, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(sleep_sec)
                sleep_sec *= 2
                continue
            raise
        except urllib.error.URLError:
            if i < retries:
                time.sleep(sleep_sec)
                sleep_sec *= 2
                continue
            raise
    raise RuntimeError("GET retries exhausted")


def _http_json(
    *,
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    body: dict[str, Any] | None = None,
    retries: int = REQUEST_RETRIES,
    initial_sleep_sec: float = INITIAL_BACKOFF_SEC,
) -> dict[str, Any] | list[Any]:
    data_bytes = None
    req_headers = dict(headers)
    if body is not None:
        req_headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(body).encode("utf-8")

    sleep_sec = max(0.1, initial_sleep_sec)
    for i in range(retries + 1):
        req = urllib.request.Request(url=url, data=data_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and i < retries:
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_sec = max(sleep_sec, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(sleep_sec)
                sleep_sec *= 2
                continue
            raise
        except urllib.error.URLError:
            if i < retries:
                time.sleep(sleep_sec)
                sleep_sec *= 2
                continue
            raise
    return {}


def _extract_year_from_published(published: str) -> int:
    m = re.match(r"^(\d{4})-", (published or "").strip())
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _parse_arxiv_feed(xml_data: bytes, category: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_data)
    entries: list[dict[str, str]] = []
    for entry in root.findall("a:entry", ATOM_NS):
        title = _clean_text(entry.findtext("a:title", default="", namespaces=ATOM_NS) or "")
        published = entry.findtext("a:published", default="", namespaces=ATOM_NS) or ""
        arxiv_url = entry.findtext("a:id", default="", namespaces=ATOM_NS) or ""
        summary = _clean_text(entry.findtext("a:summary", default="", namespaces=ATOM_NS) or "")
        m = re.search(r"/abs/([^v/]+)", arxiv_url)
        arxiv_id = m.group(1) if m else ""
        entries.append(
            {
                "category": category,
                "title": title,
                "published": published,
                "id": arxiv_url,
                "arxiv_id": arxiv_id,
                "summary": summary,
            }
        )
    return entries


def fetch_arxiv_entries_for_category(category: str) -> list[dict[str, str]]:
    # Split collection by year windows to reduce deep pagination and improve stability under arXiv rate limits.
    past_query = (
        f"cat:{category} AND "
        f"submittedDate:[{PAST_YEAR_MIN}01010000 TO {PAST_YEAR_MAX}12312359]"
    )
    recent_query = (
        f"cat:{category} AND "
        f"submittedDate:[{RECENT_YEAR_MIN}01010000 TO {RECENT_YEAR_MAX}12312359]"
    )
    queries = [("past", past_query), ("recent", recent_query)]

    entries: list[dict[str, str]] = []
    total_pages = ARXIV_MAX_PAGES_PER_QUERY * len(queries)
    pbar = tqdm(total=total_pages, desc=f"arXiv {category}", unit="page", leave=False)
    for _, query in queries:
        for page in range(ARXIV_MAX_PAGES_PER_QUERY):
            start = page * ARXIV_PAGE_SIZE
            params = {
                "search_query": query,
                "start": max(0, int(start)),
                "max_results": max(1, int(ARXIV_PAGE_SIZE)),
                "sortBy": ARXIV_SORT_BY,
                "sortOrder": ARXIV_SORT_ORDER,
            }
            url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
            try:
                xml_data = _http_get_bytes(url)
            except urllib.error.HTTPError as exc:
                # Fail-soft on persistent throttling for this query window.
                if exc.code == 429:
                    pbar.update(1)
                    break
                raise
            batch = _parse_arxiv_feed(xml_data, category)
            pbar.update(1)
            if not batch:
                break
            entries.extend(batch)
            time.sleep(max(0.0, ARXIV_REQUEST_PAUSE_SEC))
    pbar.close()
    return entries


def _s2_headers(api_key: str) -> dict[str, str]:
    headers = {
        "User-Agent": "telebot-paper-search/1.0",
        "Accept": "application/json",
    }
    if api_key.strip():
        headers["x-api-key"] = api_key.strip()
    return headers


def _extract_fields_of_study(s2_paper: dict[str, Any]) -> list[str]:
    fields = s2_paper.get("fieldsOfStudy", [])
    if not isinstance(fields, list):
        return []
    out: list[str] = []
    for f in fields:
        if isinstance(f, str):
            val = f.strip()
            if val:
                out.append(val)
            continue
        if isinstance(f, dict):
            val = str(f.get("category", "")).strip() or str(f.get("name", "")).strip()
            if val:
                out.append(val)
    return out


def _search_s2_by_title(*, title: str, headers: dict[str, str]) -> dict[str, Any] | None:
    q = urllib.parse.urlencode({"query": title, "limit": 5, "fields": S2_FIELDS})
    url = f"{S2_GRAPH_BASE}/paper/search?{q}"
    obj = _http_json(url=url, headers=headers)
    time.sleep(max(0.0, S2_PAUSE_SEC))

    candidates = obj.get("data", []) if isinstance(obj, dict) else []
    if not isinstance(candidates, list) or not candidates:
        return None

    title_norm = _normalize_title(title)
    best: tuple[int, dict[str, Any]] | None = None
    for c in candidates:
        if not isinstance(c, dict):
            continue
        ct = _normalize_title(str(c.get("title", "")))
        score = 0
        if ct == title_norm:
            score += 100
        if ct and title_norm and ct in title_norm:
            score += 25
        if ct and title_norm and title_norm in ct:
            score += 25
        if best is None or score > best[0]:
            best = (score, c)
    return best[1] if best else candidates[0]


def enrich_with_semantic_scholar(entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    cache = _safe_json_load(S2_CACHE_PATH)
    out: list[dict[str, Any]] = []
    headers = _s2_headers(S2_API_KEY)

    pending_ids: list[str] = []
    for e in entries:
        aid = str(e.get("arxiv_id", "")).strip()
        if not aid:
            continue
        if f"arxiv:{aid}" in cache:
            continue
        pending_ids.append(f"ARXIV:{aid}")

    if pending_ids:
        url = f"{S2_GRAPH_BASE}/paper/batch?fields={urllib.parse.quote(S2_FIELDS, safe=',')}"
        for i in tqdm(range(0, len(pending_ids), S2_BATCH_SIZE), desc="S2 batch", unit="batch"):
            chunk = pending_ids[i : i + S2_BATCH_SIZE]
            payload = {"ids": chunk}
            try:
                batch_obj = _http_json(url=url, headers=headers, method="POST", body=payload)
                time.sleep(max(0.0, S2_PAUSE_SEC))
                if isinstance(batch_obj, list):
                    for item in batch_obj:
                        if not isinstance(item, dict):
                            continue
                        ext = item.get("externalIds", {})
                        arxiv = ""
                        if isinstance(ext, dict):
                            arxiv = str(ext.get("ArXiv", "")).strip()
                        if arxiv:
                            cache[f"arxiv:{arxiv}"] = item
            except Exception:
                # keep going; unmatched entries can still use cache or fallback
                continue

    fallback_used = 0
    for e in tqdm(entries, desc="S2 match", unit="paper"):
        aid = str(e.get("arxiv_id", "")).strip()
        title = str(e.get("title", "")).strip()
        matched = cache.get(f"arxiv:{aid}") if aid else None

        match_source = "arxiv_id"
        if not isinstance(matched, dict):
            match_source = "title"
            cache_key = f"title:{_normalize_title(title)}"
            matched = cache.get(cache_key)
            if not isinstance(matched, dict) and fallback_used < S2_FALLBACK_MAX:
                try:
                    matched = _search_s2_by_title(title=title, headers=headers)
                    fallback_used += 1
                except Exception:
                    matched = None
                if isinstance(matched, dict):
                    cache[cache_key] = matched

        row: dict[str, Any] = dict(e)
        if isinstance(matched, dict):
            row["s2_match"] = True
            row["s2_match_source"] = match_source
            row["s2_title"] = str(matched.get("title", "")).strip()
            row["year"] = matched.get("year")
            row["venue"] = str(matched.get("venue", "")).strip()
            row["citationCount"] = int(matched.get("citationCount") or 0)
            row["influentialCitationCount"] = int(matched.get("influentialCitationCount") or 0)
            row["fieldsOfStudy"] = _extract_fields_of_study(matched)
            row["s2_url"] = str(matched.get("url", "")).strip()
        else:
            row["s2_match"] = False
            row["s2_match_source"] = ""
            row["s2_title"] = ""
            row["year"] = None
            row["venue"] = ""
            row["citationCount"] = 0
            row["influentialCitationCount"] = 0
            row["fieldsOfStudy"] = []
            row["s2_url"] = ""
        out.append(row)

    _safe_json_save(S2_CACHE_PATH, cache)
    return out


def relevance_score(paper: dict[str, Any]) -> int:
    title = str(paper.get("title", "")).lower()
    summary = str(paper.get("summary", "")).lower()
    fos = {str(x).strip() for x in paper.get("fieldsOfStudy", []) if str(x).strip()}

    score = 0
    for kw in KEYWORDS:
        kw_lower = kw.lower()
        if kw_lower in title:
            score += 2
        if kw_lower in summary:
            score += 1

    if fos & FIELDS_BOOST:
        score += 2

    if int(paper.get("influentialCitationCount") or 0) >= 10:
        score += 2

    return score


def is_past_core(paper: dict[str, Any]) -> bool:
    year = int(paper.get("year") or 0)
    citations = int(paper.get("citationCount") or 0)
    return PAST_YEAR_MIN <= year <= PAST_YEAR_MAX and citations >= PAST_MIN_CITATIONS


def is_recent_selected(paper: dict[str, Any]) -> bool:
    year = int(paper.get("year") or 0)
    if year < RECENT_YEAR_MIN or year > RECENT_YEAR_MAX:
        return False
    min_citations = RECENT_MIN_CITATIONS_BY_YEAR.get(year, 999999)
    citations = int(paper.get("citationCount") or 0)
    rel = relevance_score(paper)
    return citations >= min_citations and rel >= RECENT_RELEVANCE_THRESHOLD


def dedupe_by_arxiv(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in entries:
        aid = str(e.get("arxiv_id", "")).strip()
        if not aid or aid in seen:
            continue
        seen.add(aid)
        out.append(e)
    return out


def download_pdf(arxiv_id: str, target_dir: Path) -> tuple[bool, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{arxiv_id}.pdf"
    if out_path.exists() and out_path.stat().st_size > 0:
        return True, f"skip (exists): {out_path}"

    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    try:
        data = _http_get_bytes(pdf_url, retries=REQUEST_RETRIES, initial_sleep_sec=INITIAL_BACKOFF_SEC, timeout_sec=90)
        out_path.write_bytes(data)
        return True, f"downloaded: {out_path}"
    except Exception as exc:
        return False, f"download failed ({arxiv_id}): {type(exc).__name__}: {exc}"


def print_group(title: str, papers: list[dict[str, Any]]) -> None:
    print(f"\n[{title}] count={len(papers)}")
    for i, p in enumerate(papers, 1):
        print(f"{i}. {p.get('year', '')} | {p.get('title', '')}")
        print(f"   arxiv: {p.get('id', '')}")
        print(f"   category: {p.get('category', '')}")
        print(f"   citationCount: {p.get('citationCount', 0)}")
        print(f"   influentialCitationCount: {p.get('influentialCitationCount', 0)}")
        print(f"   venue: {p.get('venue', '')}")
        fos = p.get("fieldsOfStudy", []) or []
        print(f"   fieldsOfStudy: {', '.join(str(x) for x in fos) if fos else ''}")
        print(f"   relevanceScore: {relevance_score(p)}")


def main() -> int:
    print("Fetching arXiv lists...")
    all_entries: list[dict[str, str]] = []
    for cat in tqdm(CATEGORIES, desc="Categories", unit="cat"):
        try:
            rows = fetch_arxiv_entries_for_category(cat)
        except Exception as exc:
            print(f"arXiv fetch failed for {cat}: {type(exc).__name__}: {exc}")
            continue
        print(f"  - {cat}: {len(rows)}")
        all_entries.extend(rows)

    all_entries = dedupe_by_arxiv(all_entries)
    print(f"Total unique arXiv entries: {len(all_entries)}")

    print("Enriching with Semantic Scholar...")
    enriched = enrich_with_semantic_scholar(all_entries)

    past_core = [p for p in enriched if is_past_core(p)]
    recent_selected = [p for p in enriched if is_recent_selected(p)]

    past_core.sort(key=lambda x: int(x.get("citationCount") or 0), reverse=True)
    recent_selected.sort(
        key=lambda x: (relevance_score(x), int(x.get("citationCount") or 0), int(x.get("influentialCitationCount") or 0)),
        reverse=True,
    )

    print_group("Past Core (2005~2019, citation>=100)", past_core)
    print_group("Recent Selected (2020~2025)", recent_selected)

    selected = dedupe_by_arxiv(past_core + recent_selected)
    print(f"\n[Download] selected_total={len(selected)}")

    ok_count = 0
    fail_count = 0
    for p in tqdm(selected, desc="PDF download", unit="paper"):
        arxiv_id = str(p.get("arxiv_id", "")).strip()
        if not arxiv_id:
            continue
        ok, msg = download_pdf(arxiv_id, DOWNLOAD_DIR)
        print(f" - {msg}")
        if ok:
            ok_count += 1
        else:
            fail_count += 1
        time.sleep(max(0.0, DOWNLOAD_PAUSE_SEC))

    print(f"Done. downloaded_or_skipped={ok_count}, failed={fail_count}, output_dir={DOWNLOAD_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
