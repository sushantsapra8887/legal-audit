# GoLegal Legal Audit Tool
## Setup & Deployment Guide

---

## How It Works

```
User enters URL
       ↓
Frontend (index.html) sends request to backend
       ↓
Backend (server.py) CRAWLS the actual website:
  - Fetches homepage
  - Finds all policy links in navigation + footer
  - Visits each policy page individually
  - Checks content of each page for legal signals
  - Checks DPDP Act, IT Act, GDPR signals
  - Checks SSL, cookie banner, contact info, etc.
       ↓
Returns real compliance report
       ↓
Free basic report shown → Paid full report upsell
```

---

## Local Setup (Test on your computer)

### Step 1 — Install Python dependencies
```bash
pip install flask flask-cors requests beautifulsoup4
```

### Step 2 — Start the backend server
```bash
python3 server.py
```
You should see: `🚀 GoLegal Audit Crawler running on http://localhost:5000`

### Step 3 — Open the frontend
Open `index.html` in your browser (double-click it or use Live Server in VS Code).

Enter any URL and click "Scan My Website" — it will actually crawl the site!

---

## Production Deployment (Put it live on your website)

### Option A — Hostinger / VPS (Recommended)

1. Upload `server.py` and `requirements.txt` to your server
2. SSH into your server:
```bash
pip install -r requirements.txt
# Run permanently with:
nohup python3 server.py &
# Or better, use gunicorn:
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5000 server:app
```
3. Point a subdomain like `audit-api.thegolegal.com` to your server IP
4. In `index.html`, change this line:
```javascript
const API_BASE = 'http://localhost:5000';
// Change to:
const API_BASE = 'https://audit-api.thegolegal.com';
```
5. Add index.html as a Custom HTML block in WordPress

### Option B — Railway.app (Easiest, free tier available)

1. Go to railway.app → New Project → Deploy from GitHub
2. Upload server.py + requirements.txt
3. Add a `Procfile`:
```
web: gunicorn -w 2 -b 0.0.0.0:$PORT server:app
```
4. Railway gives you a URL like `your-app.railway.app`
5. Update `API_BASE` in index.html to that URL

### Option C — Render.com (Also free tier)

Same as Railway — upload files, it auto-detects Python.

---

## Adding to WordPress

1. In WordPress dashboard → Pages → Add New
2. Add a **Custom HTML** block
3. Paste the entire contents of `index.html`
4. Set slug to `/legal-audit`
5. Publish

Then on your Services page, make the "Legal Audit" button link to `/legal-audit`

---

## What Gets Checked (11 Compliance Points)

| Check | How It's Verified |
|-------|------------------|
| SSL/HTTPS | URL check |
| Privacy Policy | Link found + page content verified |
| Terms of Service | Link found + page content verified |
| Cookie Policy | Link found + page content verified |
| Refund Policy | Link found + page content verified |
| Legal Disclaimer | Link found + page content verified |
| Cookie Banner | HTML element detection on homepage |
| DPDP Act 2023 | Keyword signals in privacy/homepage |
| Contact Information | Text + link detection |
| Copyright Notice | © symbol + text detection |
| Grievance Officer | Required under India's IT Act |

---

## Revenue Flow

```
Free Basic Scan (11 checks)
         ↓
    ₹999 Full Report (25+ checks, PDF, 48hr delivery)
         ↓
    ₹X,XXX Fix It For You (Remediation Service)
```
