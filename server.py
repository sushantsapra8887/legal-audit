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
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script","style","noscript"]): t.decompose()
        return soup, soup.get_text(separator=" ", strip=True)[:5000], None
    except Exception as e:
        return None, "", str(e)

def find_links(soup, base_url):
    links = {}
    if not soup: return links
    base_domain = urlparse(base_url).netloc
    keywords = ["privacy","terms","cookie","refund","disclaimer","legal","contact","grievance","dpdp"]
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
           "generationConfig": {
    "temperature": 0.1,
    "maxOutputTokens": 3000
}
        },
        timeout=60
    )
    if resp.status_code != 200:
        raise Exception(f"Gemini error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    # Clean any markdown code blocks
    clean = re.sub(r"```json\s*|\s*```", "", raw).strip()
    return clean

def build_default_checks():
    """Return default fail checks if AI fails"""
    keys = ["ssl","privacy_policy","terms_of_service","cookie_policy","refund_policy",
            "dpdp_compliance","grievance_officer","contact_info","disclaimer","copyright"]
    titles = ["SSL Security","Privacy Policy","Terms of Service","Cookie Policy",
              "Refund Policy","DPDP Act 2023","Grievance Officer","Contact Information",
              "Legal Disclaimer","Copyright Notice"]
    return {k: {"status":"fail","title":t,"description":"Could not verify","found_at":None}
            for k, t in zip(keys, titles)}

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
            return jsonify({"error": "GEMINI_API_KEY not set"}), 500

        # Step 1: fetch homepage
        soup, homepage_text, err = fetch_page(url)
        if err:
            return jsonify({"error": f"Cannot reach website: {err}"}), 400

        # Step 2: find policy links
        links = find_links(soup, url)

        # Step 3: fetch each policy page
        pages_content = {"homepage": homepage_text}
        for kw, link_url in list(links.items())[:6]:
            _, text, e = fetch_page(link_url)
            if not e and text:
                pages_content[link_url] = text[:2000]

        # Step 4: build evidence string
        evidence = f"WEBSITE: {url}\nSSL: {'Yes' if url.startswith('https') else 'No'}\n"
        for page_url, text in pages_content.items():
            evidence += f"\n=== {page_url} ===\n{text[:1500]}\n"

        # Step 5: call Gemini with strict JSON instruction
        prompt = f"""You are an Indian legal compliance auditor. Analyze this website and return a JSON compliance report.

WEBSITE EVIDENCE:
{evidence[:8000]}

INSTRUCTIONS:
- Only mark FAIL if genuinely absent from ALL pages
- Mark WARN if present but incomplete  
- Be accurate and fair like a real auditor
- Return ONLY the JSON object below, filled in with your findings

JSON FORMAT TO RETURN:
{{
  "score": 75,
  "checks": {{
    "ssl": {{"status": "pass", "title": "SSL Security", "description": "your finding here", "found_at": null}},
    "privacy_policy": {{"status": "pass", "title": "Privacy Policy", "description": "your finding here", "found_at": "url or null"}},
    "terms_of_service": {{"status": "fail", "title": "Terms of Service", "description": "your finding here", "found_at": null}},
    "cookie_policy": {{"status": "warn", "title": "Cookie Policy", "description": "your finding here", "found_at": null}},
    "refund_policy": {{"status": "fail", "title": "Refund Policy", "description": "your finding here", "found_at": null}},
    "dpdp_compliance": {{"status": "warn", "title": "DPDP Act 2023", "description": "your finding here", "found_at": null}},
    "grievance_officer": {{"status": "fail", "title": "Grievance Officer", "description": "your finding here", "found_at": null}},
    "contact_info": {{"status": "pass", "title": "Contact Information", "description": "your finding here", "found_at": null}},
    "disclaimer": {{"status": "fail", "title": "Legal Disclaimer", "description": "your finding here", "found_at": null}},
    "copyright": {{"status": "pass", "title": "Copyright Notice", "description": "your finding here", "found_at": null}}
  }},
  "ai_summary": "your 2-3 sentence overall assessment here",
  "top_risks": ["risk 1", "risk 2", "risk 3"]
}}"""

        # Call Gemini
        raw_json = call_gemini(prompt)

        # Try to parse — with fallback
        try:
            result = json.loads(raw_json)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            match = re.search(r'\{.*\}', raw_json, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                raise Exception(f"Could not parse AI response: {raw_json[:200]}")

        checks = result.get("checks", build_default_checks())
        vals = list(checks.values())
        passed = sum(1 for c in vals if c.get("status") == "pass")
        warned = sum(1 for c in vals if c.get("status") == "warn")
        failed = sum(1 for c in vals if c.get("status") == "fail")
        total = len(vals)
        score = result.get("score", round((passed*10 + warned*5) / max(total*10,1) * 100))

        return jsonify({
            "url": url,
            "score": score,
            "checks": checks,
            "summary": {"passed": passed, "warnings": warned, "failed": failed, "total": total},
            "pages_crawled": list(pages_content.keys()),
            "pages_checked": len(pages_content),
            "ai_summary": result.get("ai_summary", ""),
            "top_risks": result.get("top_risks", []),
            "policy_links": links
        })

    except Exception as e:
        return jsonify({
            "error": str(e),
            "trace": traceback.format_exc()[-800:]
        }), 500

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "GoLegal Audit v3 - Gemini 2.5 Flash",
        "gemini_key_set": bool(GEMINI_API_KEY),
        "key_preview": GEMINI_API_KEY[:8]+"..." if GEMINI_API_KEY else "NOT SET"
    })

@app.route("/test-gemini")
def test_gemini():
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
@app.route("/debug-audit")
def debug_audit():
    try:
        url = request.args.get("url", "https://thegolegal.com")
        if not GEMINI_API_KEY:
            return jsonify({"error": "GEMINI_API_KEY not set"}), 500
        soup, homepage_text, err = fetch_page(url)
        if err:
            return jsonify({"step": "fetch_page", "error": err})
        links = find_links(soup, url)
        pages_content = {"homepage": homepage_text}
        for kw, link_url in list(links.items())[:3]:
            _, text, e = fetch_page(link_url)
            if not e and text:
                pages_content[link_url] = text[:1000]
        evidence = f"WEBSITE: {url}\n"
        for page_url, text in pages_content.items():
            evidence += f"\n=== {page_url} ===\n{text[:500]}\n"
        prompt = f"Analyze this website for legal compliance and return JSON with a 'test' key set to 'working':\n{evidence[:2000]}"
        raw = call_gemini(prompt)
        return jsonify({
            "step": "complete",
            "links_found": links,
            "pages_crawled": len(pages_content),
            "gemini_raw_response": raw[:500]
        })
    except Exception as e:
        return jsonify({
            "step": "error",
            "error": str(e),
            "trace": traceback.format_exc()[-600:]
        })
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
