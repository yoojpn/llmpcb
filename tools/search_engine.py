from __future__ import annotations
import os
from urllib.parse import unquote, parse_qs, urlparse
import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _resolve_ddg_redirect(href: str) -> str:
    # DuckDuckGo HTML results wrap real URLs as //duckduckgo.com/l/?uddg=<encoded>
    if "uddg=" in href:
        qs = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _web_search_duckduckgo(query: str, max_results: int) -> list[dict]:
    resp = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=3,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for a in soup.select(".result__a")[:max_results]:
        url = _resolve_ddg_redirect(a.get("href", ""))
        results.append({"title": a.get_text(strip=True), "url": url})
    return results


def _web_search_bing(query: str, max_results: int) -> list[dict]:
    """Fallback engine when DuckDuckGo is slow/blocked (confirmed on a
    DigitalOcean Droplet: DDG's HTML endpoint didn't respond within a
    bare curl's 20s timeout from that specific cloud IP range). Bing's
    HTML results page needs no API key either, and is a genuinely
    different network path/provider, giving a real chance of succeeding
    when DDG specifically is blocked -- not a guarantee (Bing could be
    blocked too from some other network), but a meaningfully different
    single point of failure than retrying the same blocked endpoint.
    """
    resp = requests.get(
        "https://www.bing.com/search",
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=3,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for li in soup.select("li.b_algo")[:max_results]:
        a = li.find("a")
        if a and a.get("href"):
            results.append({"title": a.get_text(strip=True), "url": a["href"]})
    return results


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Free, no-API-key web search, DuckDuckGo primary with a Bing
    fallback if DuckDuckGo fails/times out.

    Found via direct testing from a DigitalOcean Droplet: DuckDuckGo's
    HTML endpoint hung for the FULL 15s timeout (confirmed with a bare
    curl call, not an llmpcb bug) -- likely blocking/rate-limiting that
    specific cloud IP range, causing search_footprint_library to take
    ~30s per not-found component (2 retries x 15s) instead of the ~5s
    typical elsewhere, making a 24,291-component batch run take an
    estimated 243 hours instead of ~36. Mitigations: (1) a MUCH shorter
    timeout (3s per engine) so a blocked/slow connection fails fast
    instead of hanging the full 15s, (2) a Bing fallback -- a genuinely
    different network path/provider, so a block affecting DDG's endpoint
    specifically doesn't necessarily affect Bing too, and (3) an env var
    to skip web search entirely (LLMPCB_SKIP_WEB_SEARCH=1) for bulk
    data-generation runs where LCSC/KiCad-library results alone are an
    acceptable trade-off for reliably finishing in reasonable time.
    """
    if os.environ.get("LLMPCB_SKIP_WEB_SEARCH") == "1":
        return []
    try:
        results = _web_search_duckduckgo(query, max_results)
        if results:
            return results
    except requests.exceptions.RequestException:
        pass
    try:
        return _web_search_bing(query, max_results)
    except requests.exceptions.RequestException:
        return []


def fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Fetch and strip a page (or PDF) to plain text, truncated."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return _extract_pdf_text(resp.content, max_chars)
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars]


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int) -> str:
    import io
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[pypdf not installed, cannot extract PDF text]"
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            chunks.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return " ".join(chunks)[:max_chars]
    except Exception as e:
        return f"[PDF parse error: {e}]"


if __name__ == "__main__":
    results = web_search("RP2040 official datasheet")
    for r in results:
        print(r)
