#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re, time, os, json, traceback

app = Flask(__name__)
CORS(app)

HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"}
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent"

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script","style","noscript"]): t.decompose()
        return soup, soup.get_text(separator=" ", strip=True)[:6000], None
    except Exception as e:
        return None, "", str(e)

def find_links(soup, base_url):
    links = {}
    if not soup: return links
    base_domain = urlparse(base_url).netloc
    keywords = ["privacy","terms","cookie","refund","disclaimer","legal","contact","grievance","about","dpdp"]
    for a in soup.find_all("a", href=True):
        try:
            full = urljoin(base_url, a["href"])
            p = urlparse(full)
            if p.netloc != base_domain: continue
            anchor = a.get_text(strip=True).lower()
            href_lower = full.lower()
            for kw in keywords:
                if kw in href_lower or kw in anchor:
                    if kw not in links:
                        links[kw] = full
        except: pass
    return links

def call_gemini(prompt):
    resp = requests.post(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000}
        },
        timeout=30
    )
    if resp.status_code != 200:
        raise Exception(f"Gemini error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return re.sub(r"```json|```", "", raw).strip()

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/audit", methods=["POST"])
def audit():
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No JSON body"}), 400
        url = data.get("url","").strip()
        if not url: return jsonify({"error": "URL required"}), 400
        if not url.startswith(("http://","https://")): url = "https://"+url

        if not GEMINI_API_KEY:
            return jsonify({"error": "GEMINI_API_KEY not set in Railway Variables"}), 500

        # Step 1: fetch homepage
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"error": f"Cannot reach website: {err}"}), 400

        # Step 2: find policy links
        links = find_links(soup, url)

        # Step 3: fetch each policy page
        pages_content = {"homepage": homepage_text}
        for kw, link_url in list(links.items())[:8]:
            _, text, e = fetch_page(link_url)
            if not e and text:
                pages_content[link_url] = text[:3000]

        # Step 4: build evidence
        evidence = f"WEBSITE: {url}\nSSL: {'Yes' if url.startswith('https') else 'No'}\n"
        for page_url, text in pages_content.items():
            evidence += f"\n=== {page_url} ===\n{text[:2000]}\n"

        # Step 5: call Gemini
        prompt = f"""You are an Indian legal compliance expert. Analyze this website evidence and return ONLY valid JSON.

{evidence[:10000]}

Return ONLY this JSON (no markdown, no explanation):
{{
  "score": <0-100>,
  "checks": {{
    "ssl": {{"status": "pass/fail/warn", "title": "SSL Security", "description": "finding", "found_at": null}},
    "privacy_policy": {{"status": "pass/fail/warn", "title": "Privacy Policy", "description": "finding", "found_at": "url or null"}},
    "terms_of_service": {{"status": "pass/fail/warn", "title": "Terms of Service", "description": "finding", "found_at": "url or null"}},
    "cookie_policy": {{"status": "pass/fail/warn", "title": "Cookie Policy", "description": "finding", "found_at": "url or null"}},
    "refund_policy": {{"status": "pass/fail/warn", "title": "Refund Policy", "description": "finding", "found_at": "url or null"}},
    "dpdp_compliance": {{"status": "pass/fail/warn", "title": "DPDP Act 2023", "description": "finding", "found_at": "url or null"}},
    "grievance_officer": {{"status": "pass/fail/warn", "title": "Grievance Officer", "description": "finding", "found_at": "url or null"}},
    "contact_info": {{"status": "pass/fail/warn", "title": "Contact Information", "description": "finding", "found_at": "url or null"}},
    "disclaimer": {{"status": "pass/fail/warn", "title": "Legal Disclaimer", "description": "finding", "found_at": "url or null"}},
    "copyright": {{"status": "pass/fail/warn", "title": "Copyright Notice", "description": "finding", "found_at": "url or null"}}
  }},
  "ai_summary": "2-3 sentence overall assessment",
  "top_risks": ["risk1", "risk2", "risk3"]
}}"""

        raw_json = call_gemini(prompt)
        result = json.loads(raw_json)

        checks = result.get("checks", {})
        vals = list(checks.values())
        passed = sum(1 for c in vals if c.get("status") == "pass")
        warned = sum(1 for c in vals if c.get("status") == "warn")
        failed = sum(1 for c in vals if c.get("status") == "fail")
        total = len(vals)

        return jsonify({
            "url": url,
            "score": result.get("score", round((passed*10 + warned*5) / max(total*10,1) * 100)),
            "checks": checks,
            "summary": {"passed": passed, "warnings": warned, "failed": failed, "total": total},
            "pages_crawled": list(pages_content.keys()),
            "pages_checked": len(pages_content),
            "ai_summary": result.get("ai_summary", ""),
            "top_risks": result.get("top_risks", []),
            "policy_links": links
        })

    except json.JSONDecodeError as e:
        return jsonify({"error": f"AI returned invalid JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-800:]}), 500

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "GoLegal Audit v3 - Gemini",
        "gemini_key_set": bool(GEMINI_API_KEY),
        "key_preview": GEMINI_API_KEY[:8]+"..." if GEMINI_API_KEY else "NOT SET"
    })

@app.route("/test-gemini")
def test_gemini():
    """Test Gemini connection directly"""
    try:
        if not GEMINI_API_KEY:
            return jsonify({"error": "GEMINI_API_KEY not set"}), 500
        result = call_gemini('Say hello in JSON: {"message": "hello"}')
        return jsonify({"success": True, "response": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()[-500:]})
@app.route("/list-models")
def list_models():
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}",
            timeout=10
        )
        data = resp.json()
        models = [m["name"] for m in data.get("models",[]) 
                  if "generateContent" in m.get("supportedGenerationMethods",[])]
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"error": str(e)})
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
