#!/usr/bin/env python3
"""
GoLegal - Legal Compliance Audit Crawler
Run: python3 server.py
Requires: pip install requests beautifulsoup4 flask flask-cors
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re
import json
import time
import anthropic

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GoLegalAuditBot/1.0; +https://thegolegal.com)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 10

# Keywords to identify policy pages
POLICY_PATTERNS = {
    "privacy_policy": [
        "privacy", "privacy-policy", "privacy_policy", "privacypolicy",
        "data-policy", "data-protection", "datapolicy"
    ],
    "terms_of_service": [
        "terms", "terms-of-service", "terms-and-conditions", "tos",
        "terms-of-use", "termsofservice", "terms_conditions", "toc"
    ],
    "cookie_policy": [
        "cookie", "cookies", "cookie-policy", "cookiepolicy"
    ],
    "refund_policy": [
        "refund", "refund-policy", "returns", "cancellation",
        "return-policy", "refundpolicy", "money-back"
    ],
    "disclaimer": [
        "disclaimer", "disclaimers", "legal-disclaimer", "legal-notice"
    ],
    "shipping_policy": [
        "shipping", "delivery", "shipping-policy"
    ],
}

# Text patterns to detect policies in content
CONTENT_SIGNALS = {
    "privacy_policy": [
        r"privacy policy", r"personal data", r"data we collect",
        r"information we collect", r"your privacy", r"data controller",
        r"gdpr", r"dpdp", r"data protection", r"third.party sharing"
    ],
    "terms_of_service": [
        r"terms of service", r"terms and conditions", r"terms of use",
        r"by using this", r"by accessing", r"you agree to",
        r"intellectual property", r"limitation of liability", r"governing law"
    ],
    "cookie_policy": [
        r"cookie", r"cookies", r"tracking", r"local storage",
        r"web beacon", r"pixel", r"analytics"
    ],
    "refund_policy": [
        r"refund", r"return policy", r"cancellation", r"money.back",
        r"no refund", r"eligible for refund", r"days of purchase"
    ],
    "dpdp_compliance": [
        r"digital personal data", r"dpdp", r"data fiduciary",
        r"data principal", r"data protection board", r"consent manager"
    ],
    "gdpr_compliance": [
        r"gdpr", r"general data protection", r"data subject rights",
        r"right to erasure", r"right to access", r"data processor"
    ],
    "contact_info": [
        r"contact us", r"email.*@", r"@.*\.com", r"phone", r"telephone",
        r"address", r"reach us", r"support@", r"help@", r"grievance"
    ],
    "copyright": [
        r"©", r"copyright", r"all rights reserved", r"proprietary"
    ],
    "disclaimer": [
        r"disclaimer", r"not legal advice", r"for informational",
        r"no attorney.client", r"professional advice"
    ],
    "ssl": [],  # checked separately
}

def fetch_page(url, timeout=TIMEOUT):
    """Fetch a page and return (soup, raw_text, status_code, error)"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove scripts/styles for cleaner text
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True).lower()
        return soup, text, resp.status_code, None
    except requests.exceptions.SSLError:
        return None, "", 0, "SSL certificate error"
    except requests.exceptions.ConnectionError:
        return None, "", 0, "Could not connect to website"
    except requests.exceptions.Timeout:
        return None, "", 0, "Website took too long to respond"
    except Exception as e:
        return None, "", 0, str(e)

def check_ssl(url):
    """Check if site uses HTTPS"""
    return url.startswith("https://")

def check_cookie_banner(soup, text):
    """Check for cookie consent mechanisms"""
    if not soup:
        return False
    # Common cookie banner IDs/classes
    cookie_selectors = [
        "cookie", "gdpr", "consent", "cookiebot", "cookieconsent",
        "cookie-notice", "cookie-banner", "cc-", "cookie-law"
    ]
    for sel in cookie_selectors:
        if soup.find(id=re.compile(sel, re.I)):
            return True
        if soup.find(class_=re.compile(sel, re.I)):
            return True
    # Check text for cookie consent
    return bool(re.search(r"accept.*cookie|cookie.*accept|we use cookies|cookie.*consent", text))

def find_policy_links(soup, base_url):
    """Crawl all links on the page and identify policy pages"""
    found = {}
    if not soup:
        return found
    
    all_links = soup.find_all("a", href=True)
    for link in all_links:
        href = link.get("href", "").lower().strip()
        text = link.get_text(strip=True).lower()
        full_url = urljoin(base_url, link["href"])
        
        # Skip external links, anchors, JS
        if not full_url.startswith(("http://", "https://")):
            continue
        parsed_base = urlparse(base_url)
        parsed_link = urlparse(full_url)
        if parsed_link.netloc != parsed_base.netloc:
            continue
        
        combined = href + " " + text
        for policy_type, patterns in POLICY_PATTERNS.items():
            if policy_type not in found:
                for pattern in patterns:
                    if pattern in combined:
                        found[policy_type] = full_url
                        break

    return found

def check_meta_tags(soup):
    """Check important meta tags for SEO/legal"""
    results = {}
    if not soup:
        return results
    
    results["has_meta_description"] = bool(soup.find("meta", attrs={"name": "description"}))
    results["has_robots"] = bool(soup.find("meta", attrs={"name": "robots"}))
    results["has_og_tags"] = bool(soup.find("meta", attrs={"property": re.compile("og:")}))
    results["has_canonical"] = bool(soup.find("link", attrs={"rel": "canonical"}))
    
    return results

def check_content_signals(text, policy_type):
    """Check if text contains signals for a specific policy type"""
    if not text or policy_type not in CONTENT_SIGNALS:
        return False, 0
    
    signals = CONTENT_SIGNALS[policy_type]
    if not signals:
        return False, 0
    
    matches = sum(1 for signal in signals if re.search(signal, text, re.I))
    return matches > 0, matches

def crawl_and_audit(url):
    """Main crawling and auditing function"""
    results = {
        "url": url,
        "timestamp": time.strftime("%Y-%m-%d %Human:%M:%S"),
        "crawl_data": {},
        "checks": {},
    }

    # 1. Fetch homepage
    soup, homepage_text, status, error = fetch_page(url)
    if error:
        return {"error": f"Could not access website: {error}"}

    results["crawl_data"]["homepage_status"] = status
    results["crawl_data"]["homepage_length"] = len(homepage_text)

    # 2. SSL Check
    ssl_ok = check_ssl(url)
    results["checks"]["ssl"] = {
        "status": "pass" if ssl_ok else "fail",
        "found": ssl_ok,
        "url": url if ssl_ok else None,
        "content_preview": "HTTPS detected" if ssl_ok else "HTTP only — no SSL encryption"
    }

    # 3. Find policy links on homepage
    policy_links = find_policy_links(soup, url)
    results["crawl_data"]["found_policy_links"] = policy_links

    # 4. Check footer for policies (often where they live)
    footer = soup.find("footer") if soup else None
    footer_text = footer.get_text(separator=" ", strip=True).lower() if footer else ""
    footer_links = find_policy_links(footer, url) if footer else {}
    
    # Merge footer links
    for k, v in footer_links.items():
        if k not in policy_links:
            policy_links[k] = v

    # 5. Check each policy
    policy_checks = [
        ("privacy_policy", "Privacy Policy"),
        ("terms_of_service", "Terms of Service"),
        ("cookie_policy", "Cookie Policy"),
        ("refund_policy", "Refund / Cancellation Policy"),
        ("disclaimer", "Legal Disclaimer"),
    ]

    for policy_key, policy_name in policy_checks:
        check_result = {
            "status": "fail",
            "found": False,
            "url": None,
            "content_preview": "",
            "signals_found": 0
        }

        # Check if link was found
        policy_url = policy_links.get(policy_key)

        # Also check homepage text for signals
        has_signal, signal_count = check_content_signals(homepage_text, policy_key)

        if policy_url:
            # Fetch the policy page
            p_soup, p_text, p_status, p_error = fetch_page(policy_url)
            if not p_error and p_text:
                has_deep_signal, deep_count = check_content_signals(p_text, policy_key)
                check_result["found"] = True
                check_result["url"] = policy_url
                check_result["signals_found"] = deep_count
                
                if has_deep_signal and deep_count >= 2:
                    check_result["status"] = "pass"
                    check_result["content_preview"] = f"Page found and verified ({deep_count} compliance signals detected)"
                else:
                    check_result["status"] = "warn"
                    check_result["content_preview"] = f"Page found but content appears thin ({deep_count} signals)"
            else:
                check_result["status"] = "warn"
                check_result["found"] = True
                check_result["url"] = policy_url
                check_result["content_preview"] = f"Link found but page could not be verified: {p_error}"
        elif has_signal and signal_count >= 2:
            # Policy content found inline on homepage
            check_result["status"] = "warn"
            check_result["found"] = True
            check_result["content_preview"] = f"Policy content found inline on homepage (no separate page)"
            check_result["signals_found"] = signal_count
        else:
            check_result["status"] = "fail"
            check_result["content_preview"] = f"No {policy_name} page or content found on the website"

        results["checks"][policy_key] = check_result

    # 6. Cookie Banner Check
    has_cookie_banner = check_cookie_banner(soup, homepage_text)
    results["checks"]["cookie_banner"] = {
        "status": "pass" if has_cookie_banner else "warn",
        "found": has_cookie_banner,
        "content_preview": "Cookie consent mechanism detected" if has_cookie_banner else "No cookie banner/consent popup detected on homepage"
    }

    # 7. DPDP Compliance Check
    dpdp_signal, dpdp_count = check_content_signals(homepage_text, "dpdp_compliance")
    # Also check privacy policy page
    pp_url = policy_links.get("privacy_policy")
    if pp_url and not dpdp_signal:
        _, pp_text, _, _ = fetch_page(pp_url)
        dpdp_signal, dpdp_count = check_content_signals(pp_text, "dpdp_compliance")

    results["checks"]["dpdp_compliance"] = {
        "status": "pass" if (dpdp_signal and dpdp_count >= 2) else ("warn" if dpdp_signal else "fail"),
        "found": dpdp_signal,
        "signals_found": dpdp_count,
        "content_preview": f"DPDP Act 2023 references found ({dpdp_count} signals)" if dpdp_signal else "No DPDP Act 2023 compliance language found"
    }

    # 8. Contact Information
    contact_signal, contact_count = check_content_signals(homepage_text, "contact_info")
    # Check for dedicated contact page
    contact_links = [l for l in (soup.find_all("a", href=True) if soup else []) 
                     if "contact" in l.get("href","").lower() or "contact" in l.get_text("").lower()]
    
    results["checks"]["contact_info"] = {
        "status": "pass" if (contact_signal or contact_links) else "fail",
        "found": bool(contact_signal or contact_links),
        "content_preview": "Contact information or contact page detected" if (contact_signal or contact_links) else "No contact information found"
    }

    # 9. Copyright Notice
    copyright_signal, _ = check_content_signals(homepage_text, "copyright")
    results["checks"]["copyright"] = {
        "status": "pass" if copyright_signal else "warn",
        "found": copyright_signal,
        "content_preview": "Copyright notice found" if copyright_signal else "No copyright notice detected"
    }

    # 10. Grievance Officer (India-specific requirement)
    grievance_found = bool(re.search(r"grievance|nodal officer|grievance officer|grievance redress", homepage_text, re.I))
    if not grievance_found and pp_url:
        _, pp_text2, _, _ = fetch_page(pp_url) if "pp_text" not in dir() else ("", "","","")
        grievance_found = bool(re.search(r"grievance|nodal officer", pp_text2 if pp_url else "", re.I))
    
    results["checks"]["grievance_officer"] = {
        "status": "pass" if grievance_found else "fail",
        "found": grievance_found,
        "content_preview": "Grievance Officer details found (required under IT Act & DPDP)" if grievance_found else "No Grievance Officer found — required under India's IT Act and DPDP Act"
    }

    # 11. Meta Tags
    meta = check_meta_tags(soup)
    results["checks"]["meta_tags"] = {
        "status": "pass" if meta.get("has_meta_description") else "warn",
        "found": meta.get("has_meta_description", False),
        "content_preview": "Meta description found" if meta.get("has_meta_description") else "Missing meta description tag"
    }

    # Calculate score
    check_values = list(results["checks"].values())
    passed = sum(1 for c in check_values if c["status"] == "pass")
    warned = sum(1 for c in check_values if c["status"] == "warn")
    total = len(check_values)
    score = round((passed * 10 + warned * 5) / (total * 10) * 100)
    
    results["score"] = score
    results["summary"] = {
        "passed": passed,
        "warnings": warned,
        "failed": total - passed - warned,
        "total": total
    }
    results["policy_links"] = policy_links

    return results


@app.route("/audit", methods=["POST"])
def audit():
    data = request.get_json()
    url = data.get("url", "").strip()
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    try:
        urlparse(url)
    except:
        return jsonify({"error": "Invalid URL"}), 400

    result = crawl_and_audit(url)
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "GoLegal Audit Crawler"})


if __name__ == "__main__":
    print("🚀 GoLegal Audit Crawler running on http://localhost:5000")
    print("📡 POST /audit with {url: 'https://example.com'}")
    app.run(debug=False, port=5000, host="0.0.0.0")
