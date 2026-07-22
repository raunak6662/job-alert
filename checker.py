import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
import os
import re
import json
import html
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

# ═══════════════════════════════════════════════════════════════
#  FILTERS — the only block you need to edit
# ═══════════════════════════════════════════════════════════════
FILTERS = {
    # Title must contain at least one of these
    "title_include": [
        "engineer", "sde", "developer", "member of technical staff",
    ],

    # Title must NOT contain these — different JOB FAMILIES only,
    # never IC-level words (senior/staff kept, levels vary by company)
    "title_exclude": [
        "manager", "director", "head of", "vp ", "vice president",
        "chief", "principal", "distinguished", "intern", "new grad",
        "phd", "recruiter", "analyst",
        "staff",                                    # 2 levels above SDE-2
        "support", "solutions", "sales", "designated",
        "customer", "field", "success", "consultant",
        "technical account", "developer advocate", "evangelist",
        "architect", "fellow", "lead ", " lead", "sr.", "senior staff",
    ],

    # Location must contain at least one of these
    # Specific wanted locations. Kept precise on purpose: broad terms like
    # "california" would wrongly match Sunnyvale/Santa Clara/etc.
    "location_include": [
        "india", "bangalore", "bengaluru",
    ],

    # JD must contain at least one backend signal
    "backend_signals": [
        "backend", "back-end", "back end", "distributed",
        "microservice", "server-side", "infrastructure",
        "platform", "data pipeline", "storage", "api",
        "scalab", "low latency", "high throughput",
    ],

    # JD is rejected if it REQUIRES a frontend stack
    "frontend_reject_patterns": [
        r"\breact\b", r"\bredux\b", r"\bvue\.?js\b", r"\bangular\b",
        r"\bnext\.js\b", r"html/css",
    ],

    # Experience: a person with up to MAX_MIN_YEARS of experience can apply if the
    # job's MINIMUM stated requirement is <= this. You have ~3 years, so 3.
    # (This replaces brittle ok/reject pattern lists — see extract_min_years().)
    "max_min_years": 3,
}

# ═══════════════════════════════════════════════════════════════
#  TARGETS
# ═══════════════════════════════════════════════════════════════
TARGETS = [
    {
        "company": "Databricks",
        "strategy": "greenhouse",
        "board_token": "databricks",
        "source_url": "https://www.databricks.com/company/careers/open-positions?department=Engineering&location=all",
    },
    {
        "company": "Rubrik",
        "strategy": "greenhouse",
        "board_token": "rubrik",
        "source_url": "https://www.rubrik.com/company/careers/departments/engineering",
    },
    {
        "company": "Airbnb",
        "strategy": "greenhouse",
        "board_token": "airbnb",
        "source_url": "https://careers.airbnb.com/positions/?_departments=engineering&_where_you_work=india%2Cbangalore-india",
    },
    {
        "company": "Google",
        "strategy": "google",
        "source_url": "https://www.google.com/about/careers/applications/jobs/results?location=Bangalore%2C%20India",
        "base_url": "https://www.google.com/about/careers/applications/",
        "job_link_pattern": r"jobs/results/\d+",
    },
    {
        "company": "Rippling",
        "strategy": "browser",
        "source_url": "https://www.rippling.com/en-IN/careers/open-roles?department=Engineering&location=Bangalore%2C%20India",
        "base_url": "https://ats.rippling.com",
        "job_link_pattern": r"ats\.rippling\.com/rippling/jobs/[0-9a-f-]{8,}",
    },
    {
        "company": "Amazon",
        "strategy": "amazon",
        "source_url": "https://www.amazon.jobs/en/search?base_query=software+development+engineer&loc_query=Bengaluru%2C+India",
        "search_query": "software development engineer",
        "location_query": "Bengaluru, India",
    },
]

SENDER_EMAIL   = os.environ["GMAIL_ADDRESS"]
RECEIVER_EMAIL = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
SEEN_FILE = "seen.json"
# Entries older than this are dropped from seen.json on each run, so the file
# stays small. NOTE: a job still open after this many days will be re-alerted
# once -- treat that as a useful "still open" nudge rather than a bug.
SEEN_RETENTION_DAYS = 7
MAX_JOBS_PER_COMPANY = 60

# Daily heartbeat: send one "still alive" email per day at/after 8:00 AM IST
# (= 02:30 UTC). GitHub cron is imprecise (runs can drift 5-30+ min), so we do
# NOT match an exact hour. Instead: the FIRST run on a given calendar day whose
# time is >= the target hour fires the heartbeat; we record the date so it only
# happens once per day. State is kept in a tiny file committed alongside seen.json.
HEARTBEAT_HOUR_UTC = 2          # 02:xx UTC = 08:xx IST
HEARTBEAT_STATE_FILE = "heartbeat.json"


# ───────────────────────── filter logic ─────────────────────────

def title_ok(title):
    t = title.lower()
    if not any(kw in t for kw in FILTERS["title_include"]):
        return False
    if any(kw in t for kw in FILTERS["title_exclude"]):
        return False
    return True

def location_ok(text):
    t = text.lower()
    return any(kw in t for kw in FILTERS["location_include"])

def backend_ok(text):
    t = text.lower()
    return any(kw in t for kw in FILTERS["backend_signals"])

def frontend_rejected(text):
    t = text.lower()
    return any(re.search(p, t) for p in FILTERS["frontend_reject_patterns"])

OVERALL_CONTEXT = (
    r"(?:software|engineering|professional|industry|overall|total|relevant|work|"
    r"full[- ]time|hands[- ]on|development|technical|building|programming|product|"
    r"coding|backend|experience)"
)

# A years-mention followed by a specific technology is a SUB-SKILL, not the
# overall bar:  "3 years of experience with Kafka"
SUBSKILL_TAIL = re.compile(
    r'\s*(?:with|in|using|of)\s+'
    r'(python|java|golang|go\b|kafka|spark|scala|react|aws|gcp|azure|sql|c\+\+|'
    r'kubernetes|docker|rust|typescript|javascript|ml|ai\b|llm|genai|data structures)',
    re.I
)

# A years-mention near these words is optional/bonus and never gates eligibility:
#   "2 years of experience with LLMs is a plus"
BONUS_NEAR = re.compile(r'(is a plus|preferred|nice to have|bonus|desirable|a plus)', re.I)


def extract_min_years(text):
    """Return the HARD overall years-of-experience requirement for this role.

    Why MAX and not MIN: a JD that says "8+ years of software engineering
    experience" often ALSO contains smaller numbers -- a bonus item ("2 years
    with LLMs is a plus") or a degree-equivalence clause. Taking the minimum
    let those 8+ roles slip through as if they were entry-level. The hard bar
    is the HIGHEST overall requirement stated, so that is what gates.

    Mentions tied to a specific technology (sub-skills) and mentions marked as
    a plus/preferred are excluded before taking the max.
    Returns None if no experience requirement is stated at all.
    """
    t = re.sub(r'\s+', ' ', text).lower()
    overall = []
    for m in re.finditer(
        r'(?:minimum(?:\s+of)?|at\s+least|min\.?)?\s*'
        r'(\d{1,2})\s*(?:\+|\-|–|—|to)?\s*\d{0,2}\s*\+?\s*'
        r'years?\s+(?:of\s+)?'
        r'(?:[a-z\-]+\s+){0,3}'          # filler: "non-internship", "progressive", etc.
        + OVERALL_CONTEXT,
        t
    ):
        tail = t[m.end():m.end() + 50]
        if SUBSKILL_TAIL.match(tail):
            continue
        window = t[max(0, m.start() - 30): m.end() + 60]
        if BONUS_NEAR.search(window):
            continue
        overall.append(int(m.group(1)))

    return max(overall) if overall else None


def experience_check(text):
    """'ok' if a person with FILTERS['max_min_years'] years clears the job's
    hard requirement, 'reject' if not, 'unclear' if the JD never states one."""
    y = extract_min_years(text)
    if y is None:
        return "unclear"
    return "ok" if y <= FILTERS["max_min_years"] else "reject"


def find_experience_snippet(text, max_len=110):
    """Return the raw sentence/phrase from the JD that states the experience
    requirement, so the email can show what the filter actually read."""
    t = re.sub(r"\s+", " ", text)
    # find the first mention of "<N> year(s)" and grab surrounding context
    m = re.search(r'[^.\n]{0,80}?\b\d{1,2}\s*(?:\+|\-|–|—|to)?\s*\d{0,2}\s*\+?\s*years?[^.\n]{0,80}', t, re.I)
    if not m:
        return ""
    snip = m.group(0).strip(" •-–—\t")
    return (snip[:max_len] + "…") if len(snip) > max_len else snip


def evaluate_job(title, location, jd_text):
    if not title_ok(title):
        return False, "title"
    # Structured-location gate: if the job has an explicit location field and
    # it does NOT match any wanted location, reject immediately (don't let JD
    # boilerplate mentioning a wanted city sneak a wrong-location role through).
    if location.strip() and not location_ok(location):
        return False, "location (structured field not in wanted list)"
    if not location_ok(location + " " + jd_text):
        return False, "location"
    if not backend_ok(jd_text):
        return False, "not backend"
    if frontend_rejected(jd_text):
        return False, "frontend stack required"
    exp = experience_check(jd_text)
    if exp == "reject":
        return False, "too senior"
    note = "experience unclear — verify manually" if exp == "unclear" else ""
    return True, note


# ───────────────────────── fetch strategies ─────────────────────────

def extract_jsonld_jobposting(html_text):
    """Look for schema.org JobPosting structured data embedded as JSON-LD.
    Many career sites (including Google's) embed this for SEO / Google for Jobs.
    Returns (title, description_text) or (None, None) if not found.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        # Handle @graph wrapping
        expanded = []
        for c in candidates:
            if isinstance(c, dict) and "@graph" in c:
                expanded.extend(c["@graph"])
            else:
                expanded.append(c)
        for item in expanded:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                title = item.get("title", "") or ""
                desc_html = item.get("description", "") or ""
                desc_text = BeautifulSoup(html.unescape(desc_html), "html.parser").get_text(" ")
                return title, desc_text
    return None, None


def check_greenhouse(target):
    url = f"https://boards-api.greenhouse.io/v1/boards/{target['board_token']}/jobs?content=true"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    if not jobs:
        raise RuntimeError("Greenhouse API returned zero jobs")

    matches = []
    for job in jobs:
        title    = job.get("title", "")
        location = (job.get("location") or {}).get("name", "")
        job_url  = job.get("absolute_url", "")
        content  = BeautifulSoup(html.unescape(job.get("content", "")), "html.parser").get_text(" ")
        passed, note = evaluate_job(title, location, content)
        if passed:
            matches.append({
                "title": title, "location": location, "url": job_url, "note": note,
                "years": extract_min_years(content),
                "exp_snippet": find_experience_snippet(content),
            })
    return matches


def check_amazon(target):
    url = "https://www.amazon.jobs/en/search.json"
    params = {
        "base_query": target["search_query"],
        "loc_query": target["location_query"],
        "result_limit": 100, "offset": 0, "sort": "recent",
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    if not jobs:
        raise RuntimeError("Amazon API returned zero jobs")

    matches = []
    for job in jobs:
        title    = job.get("title", "")
        location = job.get("normalized_location", "") or job.get("location", "")
        job_url  = "https://www.amazon.jobs" + job.get("job_path", "")
        jd_text  = " ".join(filter(None, [
            job.get("description", ""), job.get("basic_qualifications", ""),
            job.get("preferred_qualifications", ""),
        ]))
        jd_text = BeautifulSoup(html.unescape(jd_text), "html.parser").get_text(" ")

        # Cheap pre-filter so we only fetch detail pages for plausible roles
        if not title_ok(title):
            continue
        if location.strip() and not location_ok(location):
            continue

        # Amazon's search API often returns EMPTY qualification fields, which
        # made experience come back "unclear" -> nothing was filtered. Fetch the
        # detail page to get the real "N+ years of ... experience" text.
        if extract_min_years(jd_text) is None:
            try:
                r = requests.get(job_url, headers=HEADERS, timeout=20)
                r.raise_for_status()
                detail = BeautifulSoup(r.text, "html.parser").get_text(" ")
                if extract_min_years(detail) is not None:
                    jd_text = detail
            except Exception as e:
                print(f"    (detail fetch failed for {title[:40]}: {e})")

        passed, note = evaluate_job(title, location, jd_text)
        if passed:
            matches.append({
                "title": title, "location": location, "url": job_url, "note": note,
                "years": extract_min_years(jd_text),
                "exp_snippet": find_experience_snippet(jd_text),
            })
        else:
            print(f"    filtered ({note}): {title[:55]}")
    return matches


def check_uber(target):
    """Uber's internal search API (same one their careers page calls)."""
    url = "https://www.uber.com/api/loadSearchJobsResults?localeCode=en"
    headers = dict(HEADERS)
    headers.update({"Content-Type": "application/json", "x-csrf-token": "x"})
    payload = {
        "params": {
            "location": [{"country": "IND", "region": "Karnataka", "city": "Bengaluru"}],
            "department": ["Engineering"],
        },
        "limit": 100,
        "page": 0,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("data") or {}).get("results") or []
    if not results:
        raise RuntimeError("Uber API returned zero results — payload/endpoint may have changed")

    matches = []
    for job in results:
        title = job.get("title", "")
        locs = job.get("allLocations") or ([job.get("location")] if job.get("location") else [])
        location = "; ".join(
            f"{l.get('city','')}, {l.get('countryName', l.get('country',''))}" for l in locs if l
        )
        job_id  = job.get("id", "")
        job_url = f"https://www.uber.com/global/en/careers/list/{job_id}/"
        jd_text = BeautifulSoup(html.unescape(job.get("description", "") or ""), "html.parser").get_text(" ")
        passed, note = evaluate_job(title, location, jd_text)
        if passed:
            matches.append({"title": title, "location": location, "url": job_url, "note": note})
    return matches


def check_atlassian(target):
    """Atlassian's public careers listings endpoint."""
    url = "https://www.atlassian.com/endpoint/careers/listings"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    postings = resp.json()
    if not isinstance(postings, list) or not postings:
        raise RuntimeError("Atlassian endpoint returned no postings")

    matches = []
    for job in postings:
        title    = job.get("title", "")
        location = str(job.get("locations") or job.get("location") or "")
        job_id   = job.get("id") or job.get("portalId") or ""
        job_url  = job.get("applyUrl") or f"https://www.atlassian.com/company/careers/details/{job_id}"
        jd_text  = BeautifulSoup(html.unescape(str(job.get("overview", "")) + " " + str(job.get("responsibilities", "")) + " " + str(job.get("qualifications", ""))), "html.parser").get_text(" ")
        if not jd_text.strip():
            jd_text = json.dumps(job)
        passed, note = evaluate_job(title, location, jd_text)
        if passed:
            matches.append({"title": title, "location": location, "url": job_url, "note": note})
    return matches


def check_html(target):
    """Generic HTML strategy with a raw-HTML regex fallback for JS/Next.js sites."""
    resp = requests.get(target["source_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(target["job_link_pattern"], href):
            links.add(urljoin(target["base_url"], href).split("#")[0].split("?")[0])

    if not links:
        for m in re.finditer(r'["\'](https?://[^"\']*?' + target["job_link_pattern"] + r'[^"\']*?)["\']', resp.text):
            links.add(m.group(1).split("#")[0].split("?")[0])
        for m in re.finditer(r'["\'](' + target["job_link_pattern"] + r'[^"\']*?)["\']', resp.text):
            links.add(urljoin(target["base_url"], m.group(1)).split("#")[0].split("?")[0])

    if not links:
        raise RuntimeError("0 job links found — page is likely JS-rendered or layout changed")

    matches = []
    for url in list(links)[:MAX_JOBS_PER_COMPANY]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            jsoup = BeautifulSoup(r.text, "html.parser")
            title_tag = jsoup.find("h1") or jsoup.find("h2") or jsoup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""
            jd_text = jsoup.get_text(" ")
            loc_found = ""
            for kw in FILTERS["location_include"]:
                if kw in jd_text.lower():
                    loc_found = kw
                    break
            passed, note = evaluate_job(title, "", jd_text)
            if passed:
                matches.append({
                    "title": title, "location": loc_found.title(), "url": url, "note": note,
                    "years": extract_min_years(jd_text),
                    "exp_snippet": find_experience_snippet(jd_text),
                })
            else:
                print(f"    filtered ({note}): {title[:60]}")
        except Exception as e:
            print(f"    skip {url}: {e}")
    return matches


def check_browser(target):
    """Render a JS-heavy listing page with Playwright and extract job links.

    IMPORTANT: for sites like Rippling the *location* is shown on the LISTING
    page (next to each job link) but NOT on the ATS detail page. So we capture
    the location from the listing row here and carry it through, rather than
    trying (and failing) to find it in the detail page body text.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

    link_locs = {}          # url -> location string captured from the listing row
    all_job_hrefs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        try:
            page.goto(target["source_url"], timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            browser.close()
            raise RuntimeError(f"page load failed: {e}")

        page.wait_for_timeout(9000)
        for _ in range(5):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1200)

        anchors = page.query_selector_all("a[href]")
        hrefs = []
        for a in anchors:
            href = a.get_attribute("href") or ""
            hrefs.append(href)
            if not re.search(target["job_link_pattern"], href):
                continue
            full = urljoin(target["base_url"], href).split("#")[0].split("?")[0]

            # Capture the location text from the listing row containing this link.
            row_text = ""
            try:
                row_text = a.inner_text() or ""
                # walk up a couple of levels to catch sibling location cells
                parent = a.evaluate_handle("el => el.closest('li, tr, div')")
                if parent:
                    el = parent.as_element()
                    if el:
                        row_text = el.inner_text() or row_text
            except Exception:
                pass
            link_locs[full] = row_text.replace("\n", " ").strip()

        browser.close()

    all_job_hrefs = [h for h in hrefs if h and any(
        k in h.lower() for k in ("job", "career", "position", "opening", "role"))]

    print(f"    [diag] total <a> links: {len(hrefs)}, job-ish: {len(all_job_hrefs)}, pattern-matched: {len(link_locs)}")
    if not link_locs:
        if all_job_hrefs:
            print("    [diag] sample job-ish links:")
            for s in all_job_hrefs[:8]:
                print(f"        {s}")
        raise RuntimeError(f"0 links matched pattern (found {len(all_job_hrefs)} job-ish links)")

    matches = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        for url, listing_loc in list(link_locs.items())[:MAX_JOBS_PER_COMPANY]:
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                page_html = page.content()

                jsonld_title, jsonld_desc = extract_jsonld_jobposting(page_html)
                if jsonld_title:
                    title = jsonld_title
                    jd_text = jsonld_desc or page.inner_text("body")
                else:
                    title = ""
                    for sel in ("h1", "h2", "title"):
                        el = page.query_selector(sel)
                        if el:
                            title = el.inner_text().strip()
                            break
                    jd_text = page.inner_text("body")

                # Location: prefer the listing-row text (reliable), fall back to
                # the detail page body. Feed BOTH into the location check.
                location_text = listing_loc or ""
                combined_for_location = location_text + " " + jd_text

                if not title_ok(title):
                    print(f"    filtered (title): {title[:60]}")
                    continue
                if not location_ok(combined_for_location):
                    print(f"    filtered (location): {title[:60]}")
                    continue
                if not backend_ok(jd_text):
                    print(f"    filtered (not backend): {title[:60]}")
                    continue
                if frontend_rejected(jd_text):
                    print(f"    filtered (frontend stack required): {title[:60]}")
                    continue
                exp = experience_check(jd_text)
                if exp == "reject":
                    print(f"    filtered (too senior): {title[:60]}")
                    continue

                note = "experience unclear — verify manually" if exp == "unclear" else ""
                shown_loc = ""
                for kw in FILTERS["location_include"]:
                    if kw in combined_for_location.lower():
                        shown_loc = kw.title()
                        break
                matches.append({
                    "title": title, "location": shown_loc, "url": url, "note": note,
                    "years": extract_min_years(jd_text),
                    "exp_snippet": find_experience_snippet(jd_text),
                })

            except Exception as e:
                print(f"    skip {url}: {e}")
        browser.close()
    return matches


def check_google(target):
    """Google careers is an Angular SPA where the DOM title stays 'Job Details'.
    BUT the job title is embedded in the URL slug, e.g.
      .../jobs/results/12345-software-engineer-search  ->  'software engineer search'
    We extract the title from the slug (reliable), and pull JD text from the
    rendered page body for the backend/experience/frontend filters.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed.")

    # 1. Get job links from the listing page (rendered)
    links = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(target["source_url"], timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        for _ in range(5):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1200)
        hrefs = [a.get_attribute("href") or "" for a in page.query_selector_all("a[href]")]
        browser.close()

    for href in hrefs:
        if re.search(r"jobs/results/\d+", href):
            links.add(urljoin(target["base_url"], href).split("#")[0].split("?")[0])

    if not links:
        raise RuntimeError("0 Google job links found on listing page")

    def title_from_slug(url):
        # .../results/119202878869906118-software-engineer-iii-search  -> words after the id
        m = re.search(r"/results/\d+-([a-z0-9\-]+)", url)
        if not m:
            return ""
        return m.group(1).replace("-", " ").strip()

    matches = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        for url in list(links)[:MAX_JOBS_PER_COMPANY]:
            try:
                title = title_from_slug(url)
                if not title:
                    print(f"    skip (no slug title): {url}")
                    continue
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                jd_text = page.inner_text("body")
                loc_found = ""
                for kw in FILTERS["location_include"]:
                    if kw in jd_text.lower():
                        loc_found = kw
                        break
                # Pass empty location to skip strict gate; combined check still
                # requires a wanted location to appear in the JD text.
                passed, note = evaluate_job(title, "", jd_text)
                if passed:
                    matches.append({
                        "title": title.title(), "location": loc_found.title(), "url": url, "note": note,
                        "years": extract_min_years(jd_text),
                        "exp_snippet": find_experience_snippet(jd_text),
                    })
                else:
                    print(f"    filtered ({note}): {title[:60]}")
            except Exception as e:
                print(f"    skip {url}: {e}")
        browser.close()
    return matches


def check_skip(target):
    raise RuntimeError("handled via LinkedIn alert (site renders jobs as non-link elements)")


STRATEGIES = {
    "skip": check_skip,
    "greenhouse": check_greenhouse,
    "amazon": check_amazon,
    "uber": check_uber,
    "atlassian": check_atlassian,
    "html": check_html,
    "browser": check_browser,
    "google": check_google,
}


# ───────────────────────── state & email ─────────────────────────

def _heartbeat_already_sent_today(today_str):
    if os.path.exists(HEARTBEAT_STATE_FILE):
        try:
            with open(HEARTBEAT_STATE_FILE) as f:
                return json.load(f).get("last_date") == today_str
        except Exception:
            return False
    return False

def _record_heartbeat(today_str):
    with open(HEARTBEAT_STATE_FILE, "w") as f:
        json.dump({"last_date": today_str}, f)


def load_seen():
    """Load seen job URLs, dropping anything older than SEEN_RETENTION_DAYS.

    Storage format is {url: "YYYY-MM-DD"} (the date first seen). The old format
    was a plain list with no dates; those entries are migrated by stamping them
    with today's date, so the first run after upgrading keeps everything and
    pruning starts working from then on.
    Returns (active_urls_set, url_to_date_dict).
    """
    if not os.path.exists(SEEN_FILE):
        return set(), {}

    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
    except Exception:
        return set(), {}

    today = datetime.now(timezone.utc).date()

    # Migrate old list-of-urls format
    if isinstance(data, list):
        stamped = {url: today.isoformat() for url in data}
        print(f"  (migrated {len(stamped)} seen entries to timestamped format)")
        return set(stamped), stamped

    cutoff = today - timedelta(days=SEEN_RETENTION_DAYS)
    kept, dropped = {}, 0
    for url, date_str in data.items():
        try:
            seen_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            seen_date = today          # unparseable -> treat as fresh
        if seen_date >= cutoff:
            kept[url] = date_str
        else:
            dropped += 1

    if dropped:
        print(f"  (pruned {dropped} seen entries older than {SEEN_RETENTION_DAYS} days)")
    return set(kept), kept


def save_seen(seen_dates):
    with open(SEEN_FILE, "w") as f:
        json.dump(dict(sorted(seen_dates.items())), f, indent=1)

def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, GMAIL_APP_PASS)
        server.send_message(msg)
    print("Email sent.")


# ───────────────────────── main ─────────────────────────

def main():
    seen, seen_dates = load_seen()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    new_matches = {}
    broken_sources = []

    for target in TARGETS:
        company = target["company"]
        print(f"\nChecking {company} ({target['strategy']})...")
        try:
            matches = STRATEGIES[target["strategy"]](target)
            fresh = [m for m in matches if m["url"] not in seen]
            print(f"  {len(matches)} match(es), {len(fresh)} new")
            for m in matches:
                yrs = m.get("years")
                yrs_str = f"{yrs}+ yrs" if yrs is not None else "yrs N/A"
                loc = m.get("location") or "loc N/A"
                print(f"    - {m['title']}  [{loc}]  ({yrs_str})")
            if fresh:
                new_matches[company] = fresh
                for m in fresh:
                    seen.add(m["url"])
                    seen_dates[m["url"]] = today_iso
        except Exception as e:
            print(f"  SOURCE BROKEN: {e}")
            broken_sources.append(f"{company}: {target['source_url']}\n    error: {e}")

    save_seen(seen_dates)

    total_new = sum(len(v) for v in new_matches.values())
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    # Heartbeat fires on the first run at/after the target hour each day, and
    # only if it hasn't already fired today (survives GitHub cron drift).
    is_heartbeat_run = (
        now_utc.hour >= HEARTBEAT_HOUR_UTC
        and not _heartbeat_already_sent_today(today_str)
    )
    if is_heartbeat_run:
        _record_heartbeat(today_str)

    should_send = total_new > 0 or is_heartbeat_run

    print(f"\nTotal new: {total_new} | UTC hour: {now_utc.hour} | heartbeat run: {is_heartbeat_run} | will send: {should_send}")

    if not should_send:
        print("Not a heartbeat run and nothing new — staying silent, no email sent.")
        return

    sections = []
    if total_new:
        sections.append(f"{total_new} new matching job(s):\n")
        for company, jobs in new_matches.items():
            sections.append(f"=== {company} ===")
            for j in jobs:
                sections.append(f"  {j['title']}")

                # Location
                if j.get("location"):
                    sections.append(f"    Location   : {j['location']}")

                # Experience: show the detected minimum and the raw JD line
                yrs = j.get("years")
                if yrs is not None:
                    sections.append(f"    Experience : {yrs}+ yrs required  (you: {FILTERS['max_min_years']} yrs -> eligible)")
                else:
                    sections.append(f"    Experience : not stated in JD - VERIFY MANUALLY")

                snip = j.get("exp_snippet")
                if snip:
                    sections.append(f"    JD says    : \"{snip}\"")

                if j.get("note"):
                    sections.append(f"    Note       : {j['note']}")

                sections.append(f"    Apply      : {j['url']}")
                sections.append("")
            sections.append("")
    else:
        sections.append("Daily check-in: no new matching jobs since the last update.\n")

    if broken_sources:
        sections.append("SOURCES NEEDING ATTENTION (update URL or upgrade scraper):\n")
        sections.extend(broken_sources)
    elif is_heartbeat_run:
        sections.append("All active sources are healthy.")

    subject = f"[Job Alert] {total_new} new match(es)" if total_new else "[Job Alert] Daily check-in — nothing new"
    if broken_sources:
        subject += f", {len(broken_sources)} source(s) broken"
    send_email(subject, "\n".join(sections))


if __name__ == "__main__":
    main()