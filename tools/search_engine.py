from __future__ import annotations
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


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Free, no-API-key web search via DuckDuckGo HTML endpoint."""
    resp = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for a in soup.select(".result__a")[:max_results]:
        url = _resolve_ddg_redirect(a.get("href", ""))
        results.append({"title": a.get_text(strip=True), "url": url})
    return results


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
