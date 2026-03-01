#!/usr/bin/env python3
"""
GoLegal - Smart Legal Compliance Audit Crawler v3
Powered by Google Gemini AI + Deep Crawler
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

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 15
MAX_PAGES = 20
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# ─── FETCH ────────────────────────────────────────────────────────────────────

def fetch_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return soup, text, resp.status_code, None
    except Exception as e:
        return None, "", 0, str(e)

def fetch_sitemap(base_url):
    urls = []
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.php"]:
        try:
            resp = requests.get(base_url.rstrip("/") + path, headers=HEADERS, timeout=8)
            if resp.status_code == 200 and "xml" in resp.headers.get("content-type", ""):
                soup = BeautifulSoup(resp.text, "xml")
                urls.extend([l.get_text(strip=True) for l in soup.find_all("loc")[:50]])
                if urls:
                    break
        except:
            pass
    return urls

def fetch_robots(base_url):
    try:
        resp = requests.get(base_url.rstrip("/") + "/robots.txt", headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            return resp.text
    except:
        pass
    return ""

# ─── DISCOVERY ────────────────────────────────────────────────────────────────

POLICY_KEYWORDS = {
    "privacy": 90, "privacy-policy": 95, "privacypolicy": 95,
    "terms": 70, "terms-of-service": 95, "terms-and-conditions": 95,
    "tos": 80, "terms-of-use": 90, "termsofuse": 90,
    "cookie": 85, "cookies": 80, "cookie-policy": 95,
    "refund": 90, "cancellation": 85, "return-policy": 90,
    "disclaimer": 85, "legal": 70, "legal-notice": 90,
    "contact": 50, "grievance": 90, "gdpr": 90, "dpdp": 95,
    "data-protection": 85, "shipping": 70, "about": 20,
    "data-policy": 90, "user-agreement": 90,
}

def score_url(url, anchor=""):
    combined = url.lower() + " " + anchor.lower()
    score = 0
    for kw, sc in POLICY_KEYWORDS.items():
        if kw in combined:
            score = max(score, sc)
    return score

def discover_pages(base_url):
    discovered = {}
    base_domain = urlparse(base_url).netloc

    def add_link(url, anchor, source):
        try:
            parsed = urlparse(url)
            if parsed.netloc != base_domain or parsed.scheme not in ("http", "https"):
                return
            clean = parsed._replace(fragment="").geturl()
            sc = score_url(clean, anchor)
            if clean not in discovered or sc > discovered[clean]["score"]:
                discovered[clean] = {"anchor": anchor, "score": sc, "source": source}
        except:
            pass

    # Fetch homepage
    soup, homepage_text, _, err = fetch_page(base_url)
    if err or not soup:
        return {}, "", None

    # All homepage links
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        add_link(full, a.get_text(strip=True), "homepage")

    # Sitemap
    for u in fetch_sitemap(base_url):
        add_link(u, "", "sitemap")

    # robots.txt sitemaps
    robots = fetch_robots(base_url)
    for line in robots.splitlines():
        if line.lower().startswith("sitemap:"):
            sm_url = line.split(":", 1)[1].strip()
            try:
                r = requests.get(sm_url, headers=HEADERS, timeout=8)
                if r.status_code == 200:
                    s = BeautifulSoup(r.text, "xml")
                    for loc in s.find_all("loc")[:30]:
                        add_link(loc.get_text(strip=True), "", "robots_sitemap")
            except:
                pass

    # Deep crawl nav + footer
    secondary = set()
    for section in soup.find_all(["nav", "footer", "header"]):
        for a in section.find_all("a", href=True):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == base_domain:
                secondary.add(full)

    for sec_url in list(secondary)[:10]:
        if sec_url == base_url:
            continue
        s2, _, _, e2 = fetch_page(sec_url)
        if s2:
            for a in s2.find_all("a", href=True):
                full = urljoin(base_url, a["href"])
                anchor = a.get_text(strip=True)
                sc = score_url(full, anchor)
                if sc >= 50:
                    add_link(full, anchor, "secondary_crawl")

    return discovered, homepage_text, soup

def collect_contents(base_url, discovered):
    contents = {}
    sorted_pages = sorted(discovered.items(), key=lambda x: x[1]["score"], reverse=True)
    visited = 0
    for url, info in sorted_pages:
        if visited >= MAX_PAGES or info["score"] < 20:
            break
        _, text, _, err = fetch_page(url)
        if not err and text and len(text) > 100:
            contents[url] = {
                "text": text[:6000],
                "score": info["score"],
                "anchor": info["anchor"],
                "source": info["source"]
            }
        visited += 1
    return contents

# ─── GEMINI AI ────────────────────────────────────────────────────────────────

def gemini_analyze(base_url, homepage_text, page_contents, ssl_ok):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in environment variables")

    # Build evidence
    evidence = [f"WEBSITE: {base_url}", f"SSL/HTTPS: {'Yes' if ssl_ok else 'No'}"]
    evidence.append(f"\n--- HOMEPAGE (first 3000 chars) ---\n{homepage_text[:3000]}")

    for url, data in list(page_contents.items())[:15]:
        evidence.append(f"\n--- PAGE: {url}\nAnchor text: '{data['anchor']}'\n{data['text'][:2500]}")

    full_evidence = "\n".join(evidence)[:14000]

    prompt = f"""You are an expert Indian legal compliance auditor with deep knowledge of:
- India's Digital Personal Data Protection (DPDP) Act 2023
- India's IT Act 2000 and IT (Amendment) Act 2008
- Consumer Protection Act 2019
- Standard global web legal compliance

I have deep-crawled a website and collected content from {len(page_contents)} pages. Analyze ALL evidence carefully before making any judgement.

CRITICAL RULES:
- Only mark something FAIL if genuinely absent from ALL crawled pages
- If content exists but is thin or incomplete, mark WARN not FAIL
- Be accurate like a real legal auditor — not too strict, not too lenient
- A page about "legal" or "policies" might contain multiple policies
- Check every page before concluding something is missing

CRAWLED EVIDENCE:
{full_evidence}

Respond ONLY with a valid JSON object, no markdown or explanation:
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
      "description": "specific finding",
      "found_at": "url or null"
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
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "grievance_officer": {{
      "status": "pass|fail|warn",
      "title": "Grievance Officer (IT Act)",
      "description": "specific finding",
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
      "description": "specific finding",
      "found_at": "url or null"
    }},
    "third_party": {{
      "status": "pass|fail|warn",
      "title": "Third-Party Disclosures",
      "description": "specific finding",
      "found_at": "url or null"
    }}
  }},
  "ai_summary": "2-3 sentence overall compliance assessment",
  "top_risks": ["risk1", "risk2", "risk3"]
}}"""

    resp = requests.post(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000}
        },
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    clean = re.sub(r"```json|```", "", raw).strip()
    return json.loads(clean)

# ─── MAIN AUDIT ──────────────────────────────────────────────────────────────

def full_audit(url):
    ssl_ok = url.startswith("https://")

    # Phase 1: Discover all pages
    discovered, homepage_text, soup = discover_pages(url)

    # Phase 2: Collect page contents
    page_contents = collect_contents(url, discovered)

    # Phase 3: Gemini AI analysis
    ai_result = gemini_analyze(url, homepage_text, page_contents, ssl_ok)

    checks = ai_result.get("checks", {})
    vals = list(checks.values())
    passed = sum(1 for c in vals if c.get("status") == "pass")
    warned = sum(1 for c in vals if c.get("status") == "warn")
    failed = sum(1 for c in vals if c.get("status") == "fail")
    total = len(vals)
    score = round((passed * 10 + warned * 5) / max(total * 10, 1) * 100)

    return {
        "url": url,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks,
        "score": score,
        "summary": {"passed": passed, "warnings": warned, "failed": failed, "total": total},
        "pages_crawled": list(page_contents.keys()),
        "pages_checked": len(page_contents),
        "ai_summary": ai_result.get("ai_summary", ""),
        "top_risks": ai_result.get("top_risks", []),
        "policy_links": {u: d["anchor"] for u, d in page_contents.items() if d["score"] >= 60}
    }

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
        return jsonify(full_audit(url))
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except json.JSONDecodeError:
        return jsonify({"error": "AI response could not be parsed. Please try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "GoLegal Smart Audit v3 - Gemini Powered"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
