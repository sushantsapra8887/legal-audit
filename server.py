#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re, os, json, traceback
import concurrent.futures

app = Flask(__name__)
CORS(app)

HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"}
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "[https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent](https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent)"

def clean_json_response(raw_text):
    """Safely strips markdown backticks from AI responses before parsing."""
    clean = raw_text.strip()
    if clean.startswith("```json"):
        clean = clean[7:]
    elif clean.startswith("```"):
        clean = clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    return clean.strip()

def fetch_page(url):
    try:
        # Reduced timeout to 4 seconds to prevent Gunicorn worker timeout
        r = requests.get(url, headers=HEADERS, timeout=4, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script","style","noscript"]): t.decompose()
        # Only take first 1500 chars — enough to detect policies
        return soup, soup.get_text(separator=" ", strip=True)[:3000], None
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
            if urlparse(full).netloc != base_domain: continue
            anchor = a.get_text(strip=True).lower()
            for kw in keywords:
                if kw in full.lower() or kw in anchor:
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
                "temperature": 0.0,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json" # Forces Gemini to return strict JSON
            }
        },
        timeout=55
    )
    if resp.status_code != 200:
        raise Exception(f"Gemini API Error {resp.status_code}: {resp.text[:200]}")
    
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/audit", methods=["POST"])
def audit():
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No JSON body"}), 400
        url = data.get("url", "").strip()
        if not url: return jsonify({"error": "URL required"}), 400
        if not url.startswith(("http://","https://")): url = "https://" + url
        if not GEMINI_API_KEY: return jsonify({"error": "GEMINI_API_KEY not set"}), 500

        # Step 1: Fetch homepage
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"error": f"Cannot reach website: {err}"}), 400

        # Step 2: Find policy links
        links = find_links(soup, url)

        # Step 3: Fetch policy pages IN PARALLEL (Massive Speed Boost)
        pages = {"Homepage": homepage_text}
        links_to_fetch = list(links.items())[:8]
        
        def fetch_policy_task(item):
            kw, link_url = item
            _, text, e = fetch_page(link_url)
            if not e and text:
                return kw.title(), text
            return None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(fetch_policy_task, links_to_fetch)
            for title, text in results:
                if title and text:
                    pages[title] = text

        # Step 4: Build SHORT evidence summary
        ssl = "YES" if url.startswith("https") else "NO"
        evidence_lines = [f"URL: {url} | SSL: {ssl}"]
        for name, text in pages.items():
            evidence_lines.append(f"[{name}]: {text[:800]}")
        evidence = "\n".join(evidence_lines)

        # Step 5: SHORT focused prompt
        prompt = f"""Indian legal compliance audit. Check this website evidence and respond with ONLY a JSON object.

{evidence}

JSON response (fill in actual findings, start with {{, end with }}):
{{"score":0,"checks":{{"ssl":{{"status":"pass","title":"SSL","description":"finding","found_at":null}},"privacy_policy":{{"status":"fail","title":"Privacy Policy","description":"finding","found_at":null}},"terms_of_service":{{"status":"fail","title":"Terms of Service","description":"finding","found_at":null}},"cookie_policy":{{"status":"fail","title":"Cookie Policy","description":"finding","found_at":null}},"refund_policy":{{"status":"fail","title":"Refund Policy","description":"finding","found_at":null}},"dpdp_compliance":{{"status":"fail","title":"DPDP Act 2023","description":"finding","found_at":null}},"grievance_officer":{{"status":"fail","title":"Grievance Officer","description":"finding","found_at":null}},"contact_info":{{"status":"fail","title":"Contact Info","description":"finding","found_at":null}},"disclaimer":{{"status":"fail","title":"Disclaimer","description":"finding","found_at":null}},"copyright":{{"status":"fail","title":"Copyright","description":"finding","found_at":null}}}},"ai_summary":"summary","top_risks":["r1","r2","r3"]}}"""

        raw_json = call_gemini(prompt)
        clean_json = clean_json_response(raw_json)

        # Parse Native JSON cleanly
        try:
            result = json.loads(clean_json)
        except json.JSONDecodeError:
            return jsonify({"error": "Failed to parse AI response as JSON", "raw": clean_json[:200]}), 500

        checks = result.get("checks", {})
        vals = list(checks.values())
        passed = sum(1 for c in vals if c.get("status") == "pass")
        warned = sum(1 for c in vals if c.get("status") == "warn")
        failed = sum(1 for c in vals if c.get("status") == "fail")
        total = len(vals)

        return jsonify({
            "url": url,
            "score": result.get("score", round((passed*10+warned*5)/max(total*10,1)*100)),
            "checks": checks,
            "summary": {"passed": passed, "warnings": warned, "failed": failed, "total": total},
            "pages_crawled": list(pages.keys()),
            "pages_checked": len(pages),
            "ai_summary": result.get("ai_summary", ""),
            "top_risks": result.get("top_risks", []),
            "policy_links": links
        })

    except Exception as e:
        error_msg = str(e)
        # Better error handling for API timeouts and limits
        if "Gemini API Error" in error_msg:
            return jsonify({"error": "AI Service unavailable or rate limited.", "details": error_msg}), 502
        return jsonify({"error": error_msg, "trace": traceback.format_exc()[-600:]}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "GoLegal Audit v5 - Parallel", "gemini_key_set": bool(GEMINI_API_KEY)})

@app.route("/test-gemini")
def test_gemini():
    try:
        raw = call_gemini('Reply with only this JSON: {"message": "hello"}')
        clean = clean_json_response(raw)
        return jsonify({"success": True, "response": json.loads(clean)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/debug-audit")
def debug_audit():
    try:
        url = request.args.get("url", "https://thegolegal.com")
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"error": err})
        links = find_links(soup, url)
        return jsonify({"step": "complete", "links_found": links, "homepage_chars": len(homepage_text)})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/debug-full")
def debug_full():
    try:
        url = request.args.get("url", "https://thegolegal.com")
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"step": "fetch_failed", "error": err})
        
        links = find_links(soup, url)
        pages = {"Homepage": homepage_text}
        for kw, link_url in list(links.items())[:3]:
            _, text, e = fetch_page(link_url)
            if not e and text:
                pages[kw] = text

        ssl = "YES" if url.startswith("https") else "NO"
        evidence_lines = [f"URL: {url} | SSL: {ssl}"]
        for name, text in pages.items():
            evidence_lines.append(f"[{name}]: {text[:400]}")
        evidence = "\n".join(evidence_lines)

        prompt = f"""You are a legal compliance expert. Analyze this website and return a short JSON report.

{evidence}

Return ONLY this JSON (keep descriptions under 10 words each):
{{"score":0,"checks":{{"ssl":{{"status":"pass","title":"SSL","description":"brief finding","found_at":null}},"privacy_policy":{{"status":"fail","title":"Privacy Policy","description":"brief finding","found_at":null}},"terms_of_service":{{"status":"fail","title":"Terms","description":"brief finding","found_at":null}},"cookie_policy":{{"status":"fail","title":"Cookie Policy","description":"brief finding","found_at":null}},"refund_policy":{{"status":"fail","title":"Refund Policy","description":"brief finding","found_at":null}},"dpdp_compliance":{{"status":"fail","title":"DPDP Act","description":"brief finding","found_at":null}},"grievance_officer":{{"status":"fail","title":"Grievance Officer","description":"brief finding","found_at":null}},"contact_info":{{"status":"fail","title":"Contact Info","description":"brief finding","found_at":null}},"disclaimer":{{"status":"fail","title":"Disclaimer","description":"brief finding","found_at":null}},"copyright":{{"status":"fail","title":"Copyright","description":"brief finding","found_at":null}}}},"ai_summary":"one sentence summary","top_risks":["risk1","risk2","risk3"]}}"""

        raw = call_gemini(prompt)
        clean = clean_json_response(raw)
        
        # Try parse
        try:
            result = json.loads(clean)
            return jsonify({"step": "success", "result": result})
        except Exception as pe:
            return jsonify({"step": "json_parse_failed", "parse_error": str(pe), "raw_response": raw[:500]})

    except Exception as e:
        return jsonify({"step": "exception", "error": str(e), "trace": traceback.format_exc()[-600:]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")            if urlparse(full).netloc != base_domain: continue
            anchor = a.get_text(strip=True).lower()
            for kw in keywords:
                if kw in full.lower() or kw in anchor:
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
                "temperature": 0.0,
                "maxOutputTokens": 4096,
            }
        },
        timeout=55
    )
    if resp.status_code != 200:
        raise Exception(f"Gemini API Error {resp.status_code}: {resp.text[:200]}")
    
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/audit", methods=["POST"])
def audit():
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No JSON body"}), 400
        url = data.get("url", "").strip()
        if not url: return jsonify({"error": "URL required"}), 400
        if not url.startswith(("http://","https://")): url = "https://" + url
        if not GEMINI_API_KEY: return jsonify({"error": "GEMINI_API_KEY not set"}), 500

        # Step 1: Fetch homepage
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"error": f"Cannot reach website: {err}"}), 400

        # Step 2: Find policy links
        links = find_links(soup, url)

        # Step 3: Fetch policy pages IN PARALLEL (Massive Speed Boost)
        pages = {"Homepage": homepage_text}
        links_to_fetch = list(links.items())[:8]
        
        def fetch_policy_task(item):
            kw, link_url = item
            _, text, e = fetch_page(link_url)
            if not e and text:
                return kw.title(), text
            return None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(fetch_policy_task, links_to_fetch)
            for title, text in results:
                if title and text:
                    pages[title] = text

        # Step 4: Build SHORT evidence summary
        ssl = "YES" if url.startswith("https") else "NO"
        evidence_lines = [f"URL: {url} | SSL: {ssl}"]
        for name, text in pages.items():
            evidence_lines.append(f"[{name}]: {text[:800]}")
        evidence = "\n".join(evidence_lines)

        # Step 5: SHORT focused prompt
        prompt = f"""Indian legal compliance audit. Check this website evidence and respond with ONLY a JSON object.

{evidence}

JSON response (fill in actual findings, start with {{, end with }}):
{{"score":0,"checks":{{"ssl":{{"status":"pass","title":"SSL","description":"finding","found_at":null}},"privacy_policy":{{"status":"fail","title":"Privacy Policy","description":"finding","found_at":null}},"terms_of_service":{{"status":"fail","title":"Terms of Service","description":"finding","found_at":null}},"cookie_policy":{{"status":"fail","title":"Cookie Policy","description":"finding","found_at":null}},"refund_policy":{{"status":"fail","title":"Refund Policy","description":"finding","found_at":null}},"dpdp_compliance":{{"status":"fail","title":"DPDP Act 2023","description":"finding","found_at":null}},"grievance_officer":{{"status":"fail","title":"Grievance Officer","description":"finding","found_at":null}},"contact_info":{{"status":"fail","title":"Contact Info","description":"finding","found_at":null}},"disclaimer":{{"status":"fail","title":"Disclaimer","description":"finding","found_at":null}},"copyright":{{"status":"fail","title":"Copyright","description":"finding","found_at":null}}}},"ai_summary":"summary","top_risks":["r1","r2","r3"]}}"""

        raw_json = call_gemini(prompt)

        # Parse Native JSON cleanly
        try:
            result = json.loads(raw_json)
        except json.JSONDecodeError:
            return jsonify({"error": "Failed to parse AI response as JSON", "raw": raw_json[:200]}), 500

        checks = result.get("checks", {})
        vals = list(checks.values())
        passed = sum(1 for c in vals if c.get("status") == "pass")
        warned = sum(1 for c in vals if c.get("status") == "warn")
        failed = sum(1 for c in vals if c.get("status") == "fail")
        total = len(vals)

        return jsonify({
            "url": url,
            "score": result.get("score", round((passed*10+warned*5)/max(total*10,1)*100)),
            "checks": checks,
            "summary": {"passed": passed, "warnings": warned, "failed": failed, "total": total},
            "pages_crawled": list(pages.keys()),
            "pages_checked": len(pages),
            "ai_summary": result.get("ai_summary", ""),
            "top_risks": result.get("top_risks", []),
            "policy_links": links
        })

    except Exception as e:
        error_msg = str(e)
        # Better error handling for API timeouts and limits
        if "Gemini API Error" in error_msg:
            return jsonify({"error": "AI Service unavailable or rate limited.", "details": error_msg}), 502
        return jsonify({"error": error_msg, "trace": traceback.format_exc()[-600:]}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "GoLegal Audit v5 - Parallel", "gemini_key_set": bool(GEMINI_API_KEY)})

@app.route("/test-gemini")
def test_gemini():
    try:
        result = call_gemini('Reply with only this JSON: {"message": "hello"}')
        return jsonify({"success": True, "response": json.loads(result)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/debug-audit")
def debug_audit():
    try:
        url = request.args.get("url", "https://thegolegal.com")
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"error": err})
        links = find_links(soup, url)
        return jsonify({"step": "complete", "links_found": links, "homepage_chars": len(homepage_text)})
    except Exception as e:
        return jsonify({"error": str(e)})
@app.route("/debug-full")
def debug_full():
    try:
        url = request.args.get("url", "https://thegolegal.com")
        soup, homepage_text, err = fetch_page(url)
        if err: return jsonify({"step": "fetch_failed", "error": err})
        
        links = find_links(soup, url)
        pages = {"Homepage": homepage_text}
        for kw, link_url in list(links.items())[:3]:
            _, text, e = fetch_page(link_url)
            if not e and text:
                pages[kw] = text

        ssl = "YES" if url.startswith("https") else "NO"
        evidence_lines = [f"URL: {url} | SSL: {ssl}"]
        for name, text in pages.items():
            evidence_lines.append(f"[{name}]: {text[:400]}")
        evidence = "\n".join(evidence_lines)

        prompt = f"""You are a legal compliance expert. Analyze this website and return a short JSON report.

{evidence}

Return ONLY this JSON (keep descriptions under 10 words each):
{{"score":0,"checks":{{"ssl":{{"status":"pass","title":"SSL","description":"brief finding","found_at":null}},"privacy_policy":{{"status":"fail","title":"Privacy Policy","description":"brief finding","found_at":null}},"terms_of_service":{{"status":"fail","title":"Terms","description":"brief finding","found_at":null}},"cookie_policy":{{"status":"fail","title":"Cookie Policy","description":"brief finding","found_at":null}},"refund_policy":{{"status":"fail","title":"Refund Policy","description":"brief finding","found_at":null}},"dpdp_compliance":{{"status":"fail","title":"DPDP Act","description":"brief finding","found_at":null}},"grievance_officer":{{"status":"fail","title":"Grievance Officer","description":"brief finding","found_at":null}},"contact_info":{{"status":"fail","title":"Contact Info","description":"brief finding","found_at":null}},"disclaimer":{{"status":"fail","title":"Disclaimer","description":"brief finding","found_at":null}},"copyright":{{"status":"fail","title":"Copyright","description":"brief finding","found_at":null}}}},"ai_summary":"one sentence summary","top_risks":["risk1","risk2","risk3"]}}"""

        raw = call_gemini(prompt)
        
        # Try parse
        try:
            result = json.loads(raw)
            return jsonify({"step": "success", "result": result})
        except Exception as pe:
            return jsonify({"step": "json_parse_failed", "parse_error": str(pe), "raw_response": raw[:500]})

    except Exception as e:
        import traceback
        return jsonify({"step": "exception", "error": str(e), "trace": traceback.format_exc()[-600:]})
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
