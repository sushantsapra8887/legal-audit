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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
}
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

def clean_json_response(raw_text):
    clean = raw_text.strip()
    if clean.startswith("```json"): clean = clean[7:]
    elif clean.startswith("```"): clean = clean[3:]
    if clean.endswith("```"): clean = clean[:-3]
    return clean.strip()

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=5, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Remove junk elements that waste AI tokens
        for t in soup(["script", "style", "noscript", "nav", "footer", "header"]): 
            t.decompose()
            
        # SMART EXTRACTION: Only grab actual readable paragraphs and headers
        text_elements = soup.find_all(['p', 'h1', 'h2', 'h3', 'li'])
        extracted_text = " ".join([el.get_text(strip=True) for el in text_elements if len(el.get_text(strip=True)) > 10])
        
        # Give the AI up to 4000 characters of high-quality content
        return soup, extracted_text[:4000], None
    except Exception as e:
        return None, "", str(e)

def find_links(soup, base_url):
    links = {}
    if not soup: return links
    
    parsed_base = urlparse(base_url)
    # Strip 'www.' to properly match subdomains like policies.google.com
    base_domain = parsed_base.netloc.replace("www.", "") 
    
    # Hosted policy platforms that are perfectly valid
    trusted_hosts = ["termly", "iubenda", "privacypolicies.com", "termsfeed", "notion"]
    keywords = ["privacy", "terms", "cookie", "refund", "disclaimer", "legal", "contact", "grievance", "dpdp", "policy"]
    
    for a in soup.find_all("a", href=True):
        try:
            full = urljoin(base_url, a["href"])
            parsed_full = urlparse(full)
            
            # Check if link is same domain, a subdomain, or a trusted legal host
            is_valid_domain = (base_domain in parsed_full.netloc) or any(th in parsed_full.netloc for th in trusted_hosts)
            
            if not is_valid_domain: continue
            
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
                "temperature": 0.0, # 0.0 forces strict, factual outputs
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json" 
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

        # Step 2: Find policy links (now supports subdomains)
        links = find_links(soup, url)

        # Step 3: Fetch policy pages IN PARALLEL
        pages = {"Homepage": homepage_text}
        links_to_fetch = list(links.items())[:8]
        
        def fetch_policy_task(item):
            kw, link_url = item
            _, text, e = fetch_page(link_url)
            if not e and text:
                return kw.title(), text, link_url
            return None, None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(fetch_policy_task, links_to_fetch)
            for title, text, link_url in results:
                if title and text:
                    pages[title] = text

        # Step 4: Build high-quality evidence
        ssl = "YES" if url.startswith("https") else "NO"
        evidence_lines = [f"TARGET URL: {url} | SSL: {ssl}"]
        evidence_lines.append(f"FOUND LEGAL LINKS: {json.dumps(links)}") # Give AI the exact URLs found
        
        for name, text in pages.items():
            # Give the AI up to 1000 characters of pure paragraph text per page
            evidence_lines.append(f"[{name} CONTENT]: {text[:1000]}")
        evidence = "\n".join(evidence_lines)

        # Step 5: Smarter Prompt Design
        prompt = f"""You are an expert Legal Compliance Auditor performing a technical audit on a website.
Review the following scraped evidence from the website. 

CRITICAL AUDIT RULES:
1. If a URL to a policy (like privacy, terms, refund) is found in the 'FOUND LEGAL LINKS' list, you MUST mark that policy's status as "pass", even if the text content isn't fully visible below. The existence of the URL is proof of the policy.
2. Only mark "fail" if the policy is completely missing from both the links list and the content.
3. Be realistic. If it's a massive site like Google, they have these policies.

EVIDENCE TO ANALYZE:
{evidence}

Respond with ONLY a strict JSON object mapping exactly to this structure:
{{"score":0,"checks":{{"ssl":{{"status":"pass","title":"SSL","description":"finding","found_at":null}},"privacy_policy":{{"status":"fail","title":"Privacy Policy","description":"finding","found_at":null}},"terms_of_service":{{"status":"fail","title":"Terms of Service","description":"finding","found_at":null}},"cookie_policy":{{"status":"fail","title":"Cookie Policy","description":"finding","found_at":null}},"refund_policy":{{"status":"fail","title":"Refund Policy","description":"finding","found_at":null}},"dpdp_compliance":{{"status":"fail","title":"DPDP Act 2023","description":"finding","found_at":null}},"grievance_officer":{{"status":"fail","title":"Grievance Officer","description":"finding","found_at":null}},"contact_info":{{"status":"fail","title":"Contact Info","description":"finding","found_at":null}},"disclaimer":{{"status":"fail","title":"Disclaimer","description":"finding","found_at":null}},"copyright":{{"status":"fail","title":"Copyright","description":"finding","found_at":null}}}},"ai_summary":"summary","top_risks":["r1","r2","r3"]}}"""

        raw_json = call_gemini(prompt)
        clean_json = clean_json_response(raw_json)

        try:
            result = json.loads(clean_json)
        except json.JSONDecodeError:
            return jsonify({"error": "Failed to parse AI response as JSON", "raw": clean_json[:200]}), 500

        checks = result.get("checks", {})
        vals = list(checks.values())
        passed = sum(1 for c in vals if c.get("status") == "pass")
        warned = sum(1 for c in vals if c.get("status") == "warn")
        total = len(vals)

        return jsonify({
            "url": url,
            "score": result.get("score", round((passed*10+warned*5)/max(total*10,1)*100)),
            "checks": checks,
            "summary": {"passed": passed, "warnings": warned, "failed": total - passed - warned, "total": total},
            "pages_crawled": list(pages.keys()),
            "pages_checked": len(pages),
            "ai_summary": result.get("ai_summary", ""),
            "top_risks": result.get("top_risks", []),
            "policy_links": links
        })

    except Exception as e:
        error_msg = str(e)
        if "Gemini API Error" in error_msg:
            return jsonify({"error": "AI Service unavailable.", "details": error_msg}), 502
        return jsonify({"error": error_msg, "trace": traceback.format_exc()[-600:]}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
