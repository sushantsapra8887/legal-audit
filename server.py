#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re
import time
import os

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GoLegalAuditBot/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}
TIMEOUT = 10

POLICY_PATTERNS = {
    "privacy_policy": ["privacy", "privacy-policy", "privacy_policy", "data-policy"],
    "terms_of_service": ["terms", "terms-of-service", "terms-and-conditions", "tos"],
    "cookie_policy": ["cookie", "cookies", "cookie-policy"],
    "refund_policy": ["refund", "refund-policy", "returns", "cancellation"],
    "disclaimer": ["disclaimer", "legal-disclaimer", "legal-notice"],
}

CONTENT_SIGNALS = {
    "privacy_policy": [r"privacy policy", r"personal data", r"data we collect", r"data protection"],
    "terms_of_service": [r"terms of service", r"terms and conditions", r"you agree to", r"limitation of liability"],
    "cookie_policy": [r"cookie", r"tracking", r"analytics"],
    "refund_policy": [r"refund", r"return policy", r"cancellation"],
    "dpdp_compliance": [r"digital personal data", r"dpdp", r"data fiduciary", r"data principal"],
    "contact_info": [r"contact us", r"@.*\.com", r"phone", r"address", r"support@"],
    "copyright": [r"©", r"copyright", r"all rights reserved"],
    "disclaimer": [r"disclaimer", r"not legal advice", r"for informational"],
    "grievance": [r"grievance", r"nodal officer", r"grievance officer"],
}

def fetch_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True).lower()
        return soup, text, resp.status_code, None
    except Exception as e:
        return None, "", 0, str(e)

def find_policy_links(soup, base_url):
    found = {}
    if not soup:
        return found
    for link in soup.find_all("a", href=True):
        href = link.get("href", "").lower().strip()
        text = link.get_text(strip=True).lower()
        try:
            full_url = urljoin(base_url, link["href"])
        except Exception:
            continue
        if not full_url.startswith(("http://", "https://")):
            continue
        if urlparse(full_url).netloc != urlparse(base_url).netloc:
            continue
        combined = href + " " + text
        for policy_type, patterns in POLICY_PATTERNS.items():
            if policy_type not in found:
                for pattern in patterns:
                    if pattern in combined:
                        found[policy_type] = full_url
                        break
    return found

def check_signals(text, key):
    if not text or key not in CONTENT_SIGNALS:
        return False, 0
    matches = sum(1 for s in CONTENT_SIGNALS[key] if re.search(s, text, re.I))
    return matches > 0, matches

def check_cookie_banner(soup, text):
    if not soup:
        return False
    for sel in ["cookie", "gdpr", "consent", "cookieconsent"]:
        if soup.find(id=re.compile(sel, re.I)) or soup.find(class_=re.compile(sel, re.I)):
            return True
    return bool(re.search(r"accept.*cookie|cookie.*accept|we use cookies", text))

def crawl_and_audit(url):
    results = {"url": url, "checks": {}, "policy_links": {}, "score": 0, "summary": {}}

    soup, homepage_text, status, error = fetch_page(url)
    if error or not soup:
        return {"error": f"Could not access website: {error}"}

    ssl_ok = url.startswith("https://")
    results["checks"]["ssl"] = {
        "status": "pass" if ssl_ok else "fail", "found": ssl_ok, "url": None,
        "content_preview": "HTTPS detected — connection is secure" if ssl_ok else "HTTP only — no SSL encryption"
    }

    policy_links = find_policy_links(soup, url)
    footer = soup.find("footer")
    if footer:
        for k, v in find_policy_links(footer, url).items():
            if k not in policy_links:
                policy_links[k] = v
    results["policy_links"] = policy_links

    pp_text = ""
    for policy_key, policy_name in [
        ("privacy_policy", "Privacy Policy"),
        ("terms_of_service", "Terms of Service"),
        ("cookie_policy", "Cookie Policy"),
        ("refund_policy", "Refund / Cancellation Policy"),
        ("disclaimer", "Legal Disclaimer"),
    ]:
        check = {"status": "fail", "found": False, "url": None, "content_preview": "", "signals_found": 0}
        policy_url = policy_links.get(policy_key)
        has_signal, sig_count = check_signals(homepage_text, policy_key)
        if policy_url:
            p_soup, p_text, _, p_err = fetch_page(policy_url)
            if policy_key == "privacy_policy":
                pp_text = p_text
            if not p_err and p_text:
                has_deep, deep_count = check_signals(p_text, policy_key)
                check.update({"found": True, "url": policy_url, "signals_found": deep_count,
                    "status": "pass" if (has_deep and deep_count >= 2) else "warn",
                    "content_preview": f"Page verified ({deep_count} compliance signals)" if (has_deep and deep_count >= 2) else f"Page found but content is thin ({deep_count} signals)"})
            else:
                check.update({"status": "warn", "found": True, "url": policy_url, "content_preview": "Link found but page could not be verified"})
        elif has_signal and sig_count >= 2:
            check.update({"status": "warn", "found": True, "signals_found": sig_count, "content_preview": "Content found inline on homepage (no separate page)"})
        else:
            check["content_preview"] = f"No {policy_name} found on the website"
        results["checks"][policy_key] = check

    has_banner = check_cookie_banner(soup, homepage_text)
    results["checks"]["cookie_banner"] = {
        "status": "pass" if has_banner else "warn", "found": has_banner, "url": None,
        "content_preview": "Cookie consent mechanism detected" if has_banner else "No cookie banner detected"
    }

    dpdp_sig, dpdp_count = check_signals(homepage_text, "dpdp_compliance")
    if not dpdp_sig and pp_text:
        dpdp_sig, dpdp_count = check_signals(pp_text, "dpdp_compliance")
    results["checks"]["dpdp_compliance"] = {
        "status": "pass" if (dpdp_sig and dpdp_count >= 2) else ("warn" if dpdp_sig else "fail"),
        "found": dpdp_sig, "url": policy_links.get("privacy_policy"), "signals_found": dpdp_count,
        "content_preview": f"DPDP Act 2023 references found ({dpdp_count} signals)" if dpdp_sig else "No DPDP Act 2023 compliance language found"
    }

    contact_sig, _ = check_signals(homepage_text, "contact_info")
    contact_links = [l for l in soup.find_all("a", href=True) if "contact" in l.get("href", "").lower()]
    results["checks"]["contact_info"] = {
        "status": "pass" if (contact_sig or contact_links) else "fail",
        "found": bool(contact_sig or contact_links), "url": None,
        "content_preview": "Contact information detected" if (contact_sig or contact_links) else "No contact information found"
    }

    copy_sig, _ = check_signals(homepage_text, "copyright")
    results["checks"]["copyright"] = {
        "status": "pass" if copy_sig else "warn", "found": copy_sig, "url": None,
        "content_preview": "Copyright notice found" if copy_sig else "No copyright notice detected"
    }

    griev_sig, _ = check_signals(homepage_text, "grievance")
    if not griev_sig and pp_text:
        griev_sig, _ = check_signals(pp_text, "grievance")
    results["checks"]["grievance_officer"] = {
        "status": "pass" if griev_sig else "fail", "found": griev_sig,
        "url": policy_links.get("privacy_policy"),
        "content_preview": "Grievance Officer details found" if griev_sig else "No Grievance Officer found — required under India's IT Act and DPDP Act"
    }

    vals = list(results["checks"].values())
    passed = sum(1 for c in vals if c["status"] == "pass")
    warned = sum(1 for c in vals if c["status"] == "warn")
    total = len(vals)
    results["score"] = round((passed * 10 + warned * 5) / (total * 10) * 100)
    results["summary"] = {"passed": passed, "warnings": warned, "failed": total - passed - warned, "total": total}
    return results

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
    return jsonify(crawl_and_audit(url))

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "GoLegal Audit Crawler"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
