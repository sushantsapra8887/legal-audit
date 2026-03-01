#!/usr/bin/env python3
"""
GoLegal - Smart Legal Compliance Audit Crawler v2
Crawler + AI working together for accurate results
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re
import time
import os
import json
import anthropic

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
TIMEOUT = 15
MAX_PAGES = 20  # max pages to crawl per site

# ─── FETCHING ────────────────────────────────────────────────────────────────

def fetch_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "img"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return soup, text, resp.status_code, None
    except Exception as e:
        return None, "", 0, str(e)

def fetch_sitemap(base_url):
    """Try to find and parse sitemap for all URLs"""
    urls = []
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.php", "/sitemap/"]:
        try:
            resp = requests.get(base_url.rstrip("/") + path, headers=HEADERS, timeout=8)
            if resp.status_code == 200 and "xml" in resp.headers.get("content-type", ""):
                soup = BeautifulSoup(resp.text, "xml")
                locs = soup.find_all("loc")
                urls.extend([l.get_text(strip=True) for l in locs[:50]])
                if urls:
                    break
        except:
            pass
    return urls

def fetch_robots(base_url):
    """Fetch robots.txt for any useful info"""
    try:
        resp = requests.get(base_url.rstrip("/") + "/robots.txt", headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            return resp.text
    except:
        pass
    return ""

# ─── LINK DISCOVERY ───────────────────────────────────────────────────────────

def get_all_internal_links(soup, base_url):
    """Get all internal links from a page"""
    links = set()
    if not soup:
        return links
    base_domain = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        try:
            full = urljoin(base_url, a["href"])
            parsed = urlparse(full)
            if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
                # Clean URL - remove fragments
                clean = parsed._replace(fragment="").geturl()
                links.add(clean)
        except:
            pass
    return links

def score_url_for_policy(url, text):
    """Score how likely a URL/link text is to be a policy page (0-100)"""
    url_lower = url.lower()
    text_lower = text.lower()
    combined = url_lower + " " + text_lower
    score = 0

    policy_keywords = {
        "privacy": 90, "privacy-policy": 95, "privacypolicy": 95,
        "terms": 70, "terms-of-service": 95, "terms-and-conditions": 95,
        "tos": 80, "terms-of-use": 90,
        "cookie": 85, "cookies": 80,
        "refund": 90, "cancellation": 85, "return-policy": 90,
        "disclaimer": 85, "legal": 70, "legal-notice": 90,
        "about": 20, "contact": 50, "grievance": 90,
        "gdpr": 90, "dpdp": 90, "data-protection": 85,
        "shipping": 70, "delivery": 60,
    }
    for kw, sc in policy_keywords.items():
        if kw in combined:
            score = max(score, sc)
    return score

def discover_all_pages(base_url):
    """
    Smart multi-source page discovery:
    1. Homepage links (nav + footer + body)
    2. Sitemap.xml
    3. robots.txt hints
    4. Second-level crawl of important pages
    """
    discovered = {}  # url -> {"text": anchor_text, "score": policy_score, "source": where_found}
    base_domain = urlparse(base_url).netloc

    # 1. Fetch homepage
    soup, homepage_text, _, err = fetch_page(base_url)
    if err or not soup:
        return {}, homepage_text, soup

    # Collect all links from homepage
    for a in soup.find_all("a", href=True):
        try:
            full = urljoin(base_url, a["href"])
            parsed = urlparse(full)
            if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
                clean = parsed._replace(fragment="").geturl()
                anchor = a.get_text(strip=True)
                score = score_url_for_policy(clean, anchor)
                if clean not in discovered or score > discovered[clean]["score"]:
                    discovered[clean] = {"anchor": anchor, "score": score, "source": "homepage"}
        except:
            pass

    # 2. Sitemap
    sitemap_urls = fetch_sitemap(base_url)
    for u in sitemap_urls:
        try:
            parsed = urlparse(u)
            if parsed.netloc == base_domain:
                score = score_url_for_policy(u, "")
                if u not in discovered:
                    discovered[u] = {"anchor": "", "score": score, "source": "sitemap"}
        except:
            pass

    # 3. robots.txt
    robots = fetch_robots(base_url)
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                # fetch this extra sitemap
                try:
                    resp = requests.get(sitemap_url, headers=HEADERS, timeout=8)
                    if resp.status_code == 200:
                        s = BeautifulSoup(resp.text, "xml")
                        for loc in s.find_all("loc")[:30]:
                            u = loc.get_text(strip=True)
                            if urlparse(u).netloc == base_domain:
                                score = score_url_for_policy(u, "")
                                if u not in discovered:
                                    discovered[u] = {"anchor": "", "score": score, "source": "robots_sitemap"}
                except:
                    pass

    # 4. Deep crawl high-scoring pages and nav/footer specifically
    nav_footer_links = set()
    for section in soup.find_all(["nav", "footer", "header"]):
        for a in section.find_all("a", href=True):
            try:
                full = urljoin(base_url, a["href"])
                if urlparse(full).netloc == base_domain:
                    nav_footer_links.add(full)
            except:
                pass

    # Visit nav/footer pages to find more policy links
    visited_secondary = set()
    for url in list(nav_footer_links)[:10]:
        if url == base_url or url in visited_secondary:
            continue
        visited_secondary.add(url)
        s2, _, _, e2 = fetch_page(url)
        if s2:
            for a in s2.find_all("a", href=True):
                try:
                    full = urljoin(base_url, a["href"])
                    parsed = urlparse(full)
                    if parsed.netloc == base_domain:
                        clean = parsed._replace(fragment="").geturl()
                        anchor = a.get_text(strip=True)
                        score = score_url_for_policy(clean, anchor)
                        if score >= 60 and clean not in discovered:
                            discovered[clean] = {"anchor": anchor, "score": score, "source": "secondary_crawl"}
                except:
                    pass

    return discovered, homepage_text, soup

# ─── PAGE CONTENT COLLECTION ─────────────────────────────────────────────────

def collect_page_contents(base_url, discovered_pages):
    """
    Visit high-scoring policy pages and collect their content.
    Returns dict of url -> content
    """
    contents = {}
    
    # Sort by policy score, visit top candidates
    sorted_pages = sorted(discovered_pages.items(), key=lambda x: x[1]["score"], reverse=True)
    
    visited = 0
    for url, info in sorted_pages:
        if visited >= MAX_PAGES:
            break
        if info["score"] < 30:  # skip very low-score pages
            break
        _, text, status, err = fetch_page(url)
        if not err and text and len(text) > 100:
            contents[url] = {
                "text": text[:8000],  # cap per page
                "score": info["score"],
                "anchor": info["anchor"],
                "source": info["source"]
            }
        visited += 1

    return contents

# ─── AI ANALYSIS ─────────────────────────────────────────────────────────────

def ai_analyze(base_url, homepage_text, page_contents, ssl_ok):
    """
    Send all crawled evidence to Claude AI for accurate analysis.
    AI sees everything before making any judgement.
    """
    client = anthropic.Anthropic()

    # Build evidence summary
    evidence_parts = []
    evidence_parts.append(f"WEBSITE: {base_url}")
    evidence_parts.append(f"SSL/HTTPS: {'Yes' if ssl_ok else 'No'}")
    evidence_parts.append(f"\n--- HOMEPAGE CONTENT (first 3000 chars) ---\n{homepage_text[:3000]}")

    for url, data in page_contents.items():
        evidence_parts.append(f"\n--- PAGE: {url} (score: {data['score']}, anchor: '{data['anchor']}') ---\n{data['text'][:3000]}")

    evidence = "\n".join(evidence_parts)

    prompt = f"""You are an expert Indian legal compliance auditor with deep knowledge of:
- India's Digital Personal Data Protection (DPDP) Act 2023
- India's IT Act 2000 and IT (Amendment) Act 2008  
- Consumer Protection Act 2019
- RBI guidelines for payment/fintech sites
- General GDPR principles
- Standard web legal compliance

I have crawled a website and collected content from ALL its pages. Your job is to analyze this evidence carefully and provide an ACCURATE compliance audit. 

CRITICAL RULES:
- Only mark something as MISSING if you genuinely cannot find it anywhere in the evidence
- If content exists but is thin/incomplete, mark as WARNING not FAIL
- Be like a real auditor - thorough and fair, not trigger-happy
- Consider that policy content might be embedded in pages with unusual names
- Check ALL provided page contents before making any judgement

HERE IS ALL THE CRAWLED EVIDENCE:
{evidence[:12000]}

Analyze and respond with ONLY a valid JSON object (no markdown, no explanation) in this exact format:
{{
  "checks": {{
    "ssl": {{
      "status": "pass|fail|warn",
      "title": "SSL / HTTPS Security",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "privacy_policy": {{
      "status": "pass|fail|warn",
      "title": "Privacy Policy",
      "description": "specific finding - mention what's present or missing",
      "found_at": "url where found or null"
    }},
    "terms_of_service": {{
      "status": "pass|fail|warn",
      "title": "Terms of Service",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "cookie_policy": {{
      "status": "pass|fail|warn",
      "title": "Cookie Policy & Consent",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "refund_policy": {{
      "status": "pass|fail|warn",
      "title": "Refund / Cancellation Policy",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "dpdp_compliance": {{
      "status": "pass|fail|warn",
      "title": "DPDP Act 2023 Compliance",
      "description": "specific finding about India's Digital Personal Data Protection Act",
      "found_at": "url or null"
    }},
    "grievance_officer": {{
      "status": "pass|fail|warn",
      "title": "Grievance Officer (IT Act)",
      "description": "specific finding - required under India's IT Act",
      "found_at": "url or null"
    }},
    "contact_info": {{
      "status": "pass|fail|warn",
      "title": "Contact Information",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "disclaimer": {{
      "status": "pass|fail|warn",
      "title": "Legal Disclaimer",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "copyright": {{
      "status": "pass|fail|warn",
      "title": "Copyright Notice",
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "data_collection": {{
      "status": "pass|fail|warn",
      "title": "Data Collection Transparency",
      "description": "does the site disclose what data it collects and why",
      "found_at": "url or null"
    }},
    "third_party": {{
      "status": "pass|fail|warn",
      "title": "Third-Party Disclosures",
      "description": "are third-party tools, payment processors, analytics disclosed",
      "found_at": "url or null"
    }}
  }},
  "pages_checked": {len(page_contents)},
  "ai_summary": "2-3 sentence overall assessment of the site's legal compliance posture",
  "top_risks": ["risk1", "risk2", "risk3"]
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text
    # Clean any markdown
    clean = re.sub(r"```json|```", "", raw).strip()
    return json.loads(clean)

# ─── MAIN AUDIT ──────────────────────────────────────────────────────────────

def full_audit(url):
    result = {"url": url, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}

    ssl_ok = url.startswith("https://")

    # Phase 1: Discover all pages
    discovered, homepage_text, soup = discover_all_pages(url)

    # Phase 2: Collect content from policy-likely pages
    page_contents = collect_page_contents(url, discovered)

    # Phase 3: AI analyzes everything together
    ai_result = ai_analyze(url, homepage_text, page_contents, ssl_ok)

    # Build final result
    checks = ai_result.get("checks", {})
    vals = list(checks.values())
    passed = sum(1 for c in vals if c.get("status") == "pass")
    warned = sum(1 for c in vals if c.get("status") == "warn")
    failed = sum(1 for c in vals if c.get("status") == "fail")
    total = len(vals)
    score = round((passed * 10 + warned * 5) / max(total * 10, 1) * 100)

    result["checks"] = checks
    result["score"] = score
    result["summary"] = {"passed": passed, "warnings": warned, "failed": failed, "total": total}
    result["pages_crawled"] = list(page_contents.keys())
    result["pages_checked"] = ai_result.get("pages_checked", len(page_contents))
    result["ai_summary"] = ai_result.get("ai_summary", "")
    result["top_risks"] = ai_result.get("top_risks", [])
    result["policy_links"] = {
        url: data["anchor"] for url, data in page_contents.items() if data["score"] >= 60
    }

    return result

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/audit", methods=["POST"])
def audit():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        result = full_audit(url)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "AI could not parse the site. Please try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "GoLegal Smart Audit Crawler v2"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
