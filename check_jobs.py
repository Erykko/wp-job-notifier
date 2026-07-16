#!/usr/bin/env python3
"""
Checks https://jobs.wordpress.net/ for new job postings, drafts a tailored
application email for each new job using the Gemini API, and emails the
result to you.

State (which jobs we've already seen) is kept in seen_jobs.json, which this
script updates. The GitHub Actions workflow commits that file back to the
repo after each run so state persists between scheduled runs.
"""

import json
import io
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

SITE = "https://jobs.wordpress.net"
ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen_jobs.json"
RESUME_FILE = ROOT / "resume.txt"
RESUMES_DIR = ROOT / "resumes"
RESUME_MAP_FILE = RESUMES_DIR / "by-job-id.json"
STATUS_FILE = ROOT / "docs" / "status.json"
GEMINI_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_HEADERS = {"User-Agent": "wp-job-notifier/1.1 (+https://github.com/Erykko/wp-job-notifier)"}


def get_candidate_profile():
    """Contact details for cover letters / PDF metadata. Prefer env secrets."""
    return {
        "name": os.environ.get("CANDIDATE_NAME", "Your Name").strip() or "Your Name",
        "email": os.environ.get("CANDIDATE_EMAIL", "you@example.com").strip()
        or "you@example.com",
        "phone": os.environ.get("CANDIDATE_PHONE", "").strip(),
        "website": os.environ.get("CANDIDATE_WEBSITE", "").strip(),
    }


def format_candidate_signature(profile=None):
    profile = profile or get_candidate_profile()
    lines = [profile["name"]]
    for key in ("email", "phone", "website"):
        value = profile.get(key)
        if value:
            lines.append(value)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 1: Fetch current job listings
# ---------------------------------------------------------------------------

def fetch_jobs():
    """Try RSS first, then HTML scraping. Returns job dicts with id, title,
    link, and (when available) company/date/description."""

    jobs = fetch_jobs_rss()
    if jobs:
        print(f"Fetched {len(jobs)} job(s) via RSS.")
        return jobs

    jobs = fetch_jobs_html()
    if jobs:
        print(f"Fetched {len(jobs)} job(s) via HTML scraping.")
        return jobs

    return []


def fetch_jobs_rss():
    feed_urls = (
        f"{SITE}/feed/",
        f"{SITE}/job-feed/",
        f"{SITE}/?feed=job_feed",
    )
    for feed_url in feed_urls:
        try:
            response = requests.get(
                feed_url, timeout=20, headers=REQUEST_HEADERS
            )
            if response.status_code != 200:
                continue
            if "<rss" not in response.text.lower():
                continue
            soup = BeautifulSoup(response.text, "xml")
            items = soup.find_all("item")
            if not items:
                continue
            jobs = []
            for item in items:
                link = item.link.text.strip() if item.link else ""
                title = item.title.text.strip() if item.title else ""
                job_id = extract_job_id(link)
                description = (
                    strip_html(item.description.text) if item.description else ""
                )
                pub_date = item.pubDate.text.strip() if item.pubDate else ""
                jobs.append({
                    "id": job_id or link,
                    "title": title,
                    "link": link,
                    "date": pub_date,
                    "description": description,
                    "company": "",
                })
            if jobs:
                return jobs
        except requests.RequestException:
            continue
    return []


def fetch_jobs_html():
    try:
        response = requests.get(SITE, timeout=20, headers=REQUEST_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    jobs = []

    # Current layout (2026): each listing is <a class="job-card" href="...">
    for card in soup.select("a.job-card[href]"):
        link = card["href"].strip()
        title_el = card.select_one(".job-card__title")
        title = title_el.get_text(strip=True) if title_el else card.get_text(strip=True)
        company_el = card.select_one(".job-card__company")
        company = company_el.get_text(strip=True) if company_el else ""
        date_el = card.select_one(".job-card__date")
        date = date_el.get_text(strip=True) if date_el else ""
        jobs.append({
            "id": extract_job_id(link) or link,
            "title": title,
            "link": link,
            "date": date,
            "description": "",
            "company": company,
        })

    if jobs:
        return jobs

    # Legacy WP Job Manager layout fallback
    for listing in soup.select("li.job_listing") or soup.select("ul.job_listings li"):
        anchor = listing.find("a", href=True)
        if not anchor:
            continue
        link = anchor["href"]
        title_el = listing.select_one(".position h3, h3")
        title = title_el.get_text(strip=True) if title_el else anchor.get_text(strip=True)
        company_el = listing.select_one(".company, strong.company")
        company = company_el.get_text(strip=True) if company_el else ""
        date_el = listing.select_one(".date, li.date")
        date = date_el.get_text(strip=True) if date_el else ""
        jobs.append({
            "id": extract_job_id(link) or link,
            "title": title,
            "link": link,
            "date": date,
            "description": "",
            "company": company,
        })

    return jobs


def extract_job_id(link):
    if not link:
        return None
    match = re.search(r"/job/([^/]+)/?", link)
    return match.group(1) if match else None


def strip_html(html):
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_application_method(soup, job_link=""):
    """Pull apply-by-email or apply URL from the job page sidebar/body."""
    candidates = []
    for anchor in soup.select(".job-sidebar__apply a[href], .application a[href], a.application_button[href], a[href^='mailto:']"):
        href = (anchor.get("href") or "").strip()
        if href:
            candidates.append(href)

    for href in candidates:
        if href.lower().startswith("mailto:"):
            email = href.split(":", 1)[1].split("?", 1)[0].strip()
            if email:
                return {
                    "type": "email",
                    "value": email,
                    "label": f"Apply via email: {email}",
                }

    for href in candidates:
        if href.startswith("http") and href.rstrip("/") != job_link.rstrip("/"):
            return {
                "type": "url",
                "value": href,
                "label": f"Apply via link: {href}",
            }

    if job_link:
        return {
            "type": "url",
            "value": job_link,
            "label": f"Apply via job page: {job_link}",
        }
    return {
        "type": "unknown",
        "value": "",
        "label": "Application method not listed on the job page",
    }


def format_application_method(method):
    if not method:
        return "Application method not listed on the job page"
    return method.get("label") or "Application method not listed on the job page"


def fetch_job_page(link):
    """Fetch description text and application method from a job page."""
    empty = {
        "description": "",
        "application_method": {
            "type": "unknown",
            "value": "",
            "label": "Application method not listed on the job page",
        },
    }
    try:
        response = requests.get(link, timeout=20, headers=REQUEST_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return empty

    soup = BeautifulSoup(response.text, "html.parser")
    description = ""
    for selector in (
        ".job-detail",
        ".job_description",
        ".single_job_listing",
        "article",
        "main",
    ):
        content = soup.select_one(selector)
        if content:
            description = strip_html(str(content))
            break
    if not description:
        body = soup.find("body")
        description = strip_html(str(body)) if body else ""

    return {
        "description": description,
        "application_method": extract_application_method(soup, link),
    }


def fetch_job_description(link):
    """Get the full job description from the job page when the feed only
    provided a summary."""
    return fetch_job_page(link)["description"]


def append_application_method(draft, method):
    label = format_application_method(method)
    return (
        f"{draft.rstrip()}\n\n"
        f"{'-' * 60}\n"
        f"HOW TO APPLY\n"
        f"{'-' * 60}\n"
        f"{label}\n"
    )


# ---------------------------------------------------------------------------
# Step 2: Draft a tailored cover letter
# ---------------------------------------------------------------------------

JOB_NEED_LABELS = (
    ("custom WordPress theme and plugin development", ("theme", "plugin")),
    ("responsive and mobile-friendly website delivery", ("responsive", "mobile")),
    (
        "speed, performance, and Core Web Vitals optimization",
        ("speed", "performance", "core web vitals"),
    ),
    (
        "website security and production troubleshooting",
        ("security", "troubleshoot", "bug", "error"),
    ),
    ("technical SEO and analytics-aware development", ("seo", "analytics", "search engine")),
    ("WooCommerce customization", ("woocommerce",)),
    ("REST API and third-party integrations", ("api", "integration", "third-party")),
    ("payment gateway integrations", ("payment", "gateway", "stripe", "paypal")),
    ("Git-based development workflows", ("git", "version control")),
    ("website migrations, hosting, and maintenance", ("migration", "hosting", "backup", "maintenance")),
)


RESUME_CAPABILITY_LABELS = (
    (
        "custom theme and plugin development",
        ("theme development", "plugin development", "custom themes", "custom plugins"),
    ),
    ("WooCommerce customization and workflow automation", ("woocommerce",)),
    (
        "Core Web Vitals, caching, and page-speed optimization",
        ("core web vitals", "caching", "page speed", "performance"),
    ),
    (
        "security hardening and production troubleshooting",
        ("security", "troubleshoot", "production issues"),
    ),
    (
        "technical SEO, redirects, schema, and indexing fixes",
        ("technical seo", "redirects", "schema", "indexing"),
    ),
    ("third-party API integrations", ("api integrations", "third-party api")),
    (
        "site migrations, DNS, SSL, Cloudflare, and hosting support",
        ("migrations", "dns", "ssl", "cloudflare", "hosting"),
    ),
    (
        "Git, WP-CLI, PHP, JavaScript, HTML, CSS, SQL, and MySQL",
        ("git", "wp-cli", "php", "javascript", "html", "css", "mysql"),
    ),
)

def build_cover_letter_prompt(job, resume_text, description, application_method=None):
    company = job.get("company") or "the company"
    apply_label = format_application_method(application_method)
    profile = get_candidate_profile()
    first_name = profile["name"].split()[0]
    signature = format_candidate_signature(profile)
    return f"""You are an expert career writer helping {profile['name']} apply for a WordPress-related job.

Your task is to write a polished, professional cover letter that is tightly tailored to this specific posting and has a strong chance of getting a positive response.

Rules:
- Use only facts supported by the resume. Do not invent employers, tools, dates, or achievements.
- Mirror important keywords and requirements from the job description naturally.
- Sound confident, direct, and human. Avoid cliches such as "I am excited to apply", "passionate", "perfect fit", or "hit the ground running".
- Do not use bullet points. Write in clear paragraphs.
- Keep the letter concise but substantive: about 250 to 350 words.
- Make the letter feel written for this exact role at {company}, not a generic template.
- If the posting is support-heavy, emphasize troubleshooting, communication, and reliability.
- If the posting is development-heavy, emphasize delivery, performance, SEO, and technical execution.
- If the posting is sales or customer-facing, emphasize communication, product knowledge, and client trust.
- End with a simple, professional call to action.
- Do not invent an application email or apply URL. The application method will be appended separately.

Candidate resume:
---
{resume_text}
---

Job posting:
Title: {job['title']}
Company: {company}
Link: {job['link']}
Application method: {apply_label}
Description:
{description[:6000]}
---

Write the cover letter in this exact format:

Subject: [specific, professional subject line for this role]

Dear Hiring Manager,

[3 to 4 short paragraphs:
1. Open with the role title and a sharp reason {first_name} is a strong match.
2. Connect 2 to 3 relevant accomplishments or skills from the resume to the posting's core requirements.
3. Show understanding of what the company needs and how {first_name}'s background fits their workflow.
4. Close professionally with availability and a request for next steps.]

Best regards,
{signature}

Output only the cover letter. No commentary before or after it."""


def collect_matching_labels(text, labeled_keywords, limit=4):
    text_lower = text.lower()
    matches = []
    for label, keywords in labeled_keywords:
        if any(keyword in text_lower for keyword in keywords):
            matches.append(label)
        if len(matches) >= limit:
            break
    return matches


def format_list(items):
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def extract_years_experience(resume_text):
    match = re.search(
        r"\b(\d+)\s+years?\s+of\s+experience\b",
        resume_text,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def build_local_cover_letter(job, resume_text, description):
    """Create a deterministic draft when Gemini is unavailable or rate-limited."""
    title = job["title"]
    company = (job.get("company") or "").strip()
    company_phrase = company or "your team"
    subject = (
        f"Application for {title} at {company}"
        if company
        else f"Application for {title}"
    )
    job_text = f"{title}\n{description}"
    job_needs = collect_matching_labels(job_text, JOB_NEED_LABELS, limit=5)
    resume_capabilities = collect_matching_labels(
        resume_text,
        RESUME_CAPABILITY_LABELS,
        limit=5,
    )
    years = extract_years_experience(resume_text)

    experience_phrase = (
        f"with {years} years of experience"
        if years
        else "with hands-on experience"
    )
    needs_sentence = (
        f"Your posting emphasizes {format_list(job_needs)}."
        if job_needs
        else "Your posting calls for dependable WordPress development, maintenance, and problem-solving."
    )
    capabilities_sentence = (
        f"My background includes {format_list(resume_capabilities)}."
        if resume_capabilities
        else "My background includes WordPress development, ongoing site support, and production troubleshooting."
    )

    signature = format_candidate_signature()
    return f"""Subject: {subject}

Dear Hiring Manager,

I am applying for the {title} role at {company_phrase}. I am a WordPress Developer and full-stack engineer {experience_phrase} building, maintaining, and optimizing WordPress websites for agencies, publishers, and business clients.

{needs_sentence} {capabilities_sentence} Across recent roles I have built and maintained custom WordPress themes and plugins, resolved production issues across client sites, and improved performance, security, caching, and Core Web Vitals. I have also led WordPress website builds, WooCommerce customization, third-party integrations, and ongoing maintenance for agency clients.

I can support {company_phrase} with clean WordPress implementation, responsive front-end work, reliable troubleshooting, SEO-aware development, and practical collaboration with design, content, and marketing teams. I am comfortable working with PHP, JavaScript, HTML, CSS, SQL, MySQL, Git, WP-CLI, and common WordPress hosting workflows.

I would welcome the chance to discuss how my WordPress development and support background can help with your current website projects and ongoing maintenance needs.

Best regards,
{signature}"""


def get_gemini_api_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")


def get_gemini_models():
    override = os.environ.get("GEMINI_MODEL", "").strip()
    if override:
        return (override,)
    return GEMINI_MODELS


def extract_gemini_text(data):
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    text_blocks = [part.get("text", "") for part in parts if part.get("text")]
    return "\n".join(text_blocks).strip()


def draft_application_email(job, resume_text):
    page = fetch_job_page(job["link"])
    description = job.get("description") or page["description"]
    application_method = page["application_method"]
    job["application_method"] = application_method
    api_key = get_gemini_api_key()
    if not api_key:
        print("Gemini API key missing; using local cover letter fallback.", file=sys.stderr)
        draft = build_local_cover_letter(job, resume_text, description)
        return append_application_method(draft, application_method)

    prompt = build_cover_letter_prompt(
        job,
        resume_text,
        description,
        application_method=application_method,
    )

    last_error = None
    for model in get_gemini_models():
        try:
            response = requests.post(
                f"{GEMINI_API_BASE}/{model}:generateContent",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 1500,
                        "temperature": 0.5,
                    },
                },
                timeout=60,
            )
            response.raise_for_status()
            draft = extract_gemini_text(response.json())
            if draft:
                print(f"Drafted cover letter with {model}.")
                return append_application_method(draft, application_method)
            last_error = RuntimeError(f"{model} returned an empty response")
        except requests.RequestException as exc:
            last_error = exc
            print(f"Gemini model {model} failed: {exc}", file=sys.stderr)

    print(
        f"All Gemini models failed; using local cover letter fallback. Last error: {last_error}",
        file=sys.stderr,
    )
    draft = build_local_cover_letter(job, resume_text, description)
    return append_application_method(draft, application_method)


# ---------------------------------------------------------------------------
# Step 3: Email the result to you
# ---------------------------------------------------------------------------

def get_email_config():
    sender = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER", "")
    smtp_user = os.environ.get("SMTP_USER") or sender
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_ssl = os.environ.get("SMTP_USE_SSL", "").lower() in ("1", "true", "yes")
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": port,
        "user": smtp_user,
        "password": os.environ.get("SMTP_PASS", ""),
        "from_addr": sender or smtp_user,
        "to_addr": os.environ.get("EMAIL_TO", ""),
        "use_ssl": use_ssl or port == 465,
    }


def sanitize_filename(value):
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    return safe.strip("-") or "resume"


def build_resume_pdf_bytes(resume_text):
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)
    width, height = LETTER
    left_margin = 50
    top_margin = height - 50
    line_height = 14
    max_chars_per_line = 95

    def write_line(text):
        nonlocal top_margin
        if top_margin <= 50:
            pdf.showPage()
            pdf.setFont("Helvetica", 11)
            top_margin = height - 50
        pdf.drawString(left_margin, top_margin, text)
        top_margin -= line_height

    profile = get_candidate_profile()
    pdf.setTitle(f"{profile['name']} Resume")
    pdf.setAuthor(profile["name"])
    pdf.setFont("Helvetica", 11)

    for raw_line in resume_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            write_line("")
            continue

        # Wrap long lines so generated PDFs remain readable.
        while len(line) > max_chars_per_line:
            split_at = line.rfind(" ", 0, max_chars_per_line)
            if split_at <= 0:
                split_at = max_chars_per_line
            write_line(line[:split_at])
            line = line[split_at:].lstrip()
        write_line(line)

    pdf.save()
    data = buffer.getvalue()
    buffer.close()
    return data


def send_email(subject, body, attachment=None):
    if os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"):
        print(f"[DRY RUN] Would email: {subject}")
        print(body[:500] + ("..." if len(body) > 500 else ""))
        return

    config = get_email_config()
    if not config["password"]:
        raise RuntimeError("SMTP_PASS is required")
    if not config["to_addr"]:
        raise RuntimeError("EMAIL_TO is required")
    if not config["from_addr"] or not config["user"]:
        raise RuntimeError("Set SMTP_USER or EMAIL_FROM to your Gmail address")

    msg = MIMEMultipart()
    msg["From"] = config["from_addr"]
    msg["To"] = config["to_addr"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    if attachment:
        filename, content = attachment
        part = MIMEApplication(content, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    if config["use_ssl"]:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=30) as server:
            server.login(config["user"], config["password"])
            server.send_message(msg)
        return

    with smtplib.SMTP(config["host"], config["port"], timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(config["user"], config["password"])
        server.send_message(msg)


# ---------------------------------------------------------------------------
# State handling + status page
# ---------------------------------------------------------------------------

def load_seen():
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as file:
            return set(json.load(file))
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w") as file:
        json.dump(sorted(seen_ids), file, indent=2)
        file.write("\n")


def load_resume_map():
    if not RESUME_MAP_FILE.exists():
        return {}
    with open(RESUME_MAP_FILE) as file:
        return json.load(file)


def load_resume(job=None):
    if job:
        mapped_file = load_resume_map().get(job["id"])
        if mapped_file:
            tailored = RESUMES_DIR / mapped_file
            if tailored.exists():
                with open(tailored) as file:
                    resume = file.read().strip()
                if resume:
                    print(f"Using tailored resume: {mapped_file}")
                    return resume

    if RESUME_FILE.exists():
        with open(RESUME_FILE) as file:
            return file.read().strip()
    return ""


def write_status(*, jobs_checked, new_jobs, error=None):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "jobs_checked": jobs_checked,
        "new_jobs": [
            {
                "title": job["title"],
                "company": job.get("company", ""),
                "link": job["link"],
                "date": job.get("date", ""),
            }
            for job in new_jobs
        ],
        "error": error,
    }
    with open(STATUS_FILE, "w") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")


def validate_env(*, require_email):
    missing = []
    if require_email and os.environ.get("DRY_RUN", "").lower() not in ("1", "true", "yes"):
        if not os.environ.get("SMTP_PASS"):
            missing.append("SMTP_PASS")
        if not os.environ.get("EMAIL_TO"):
            missing.append("EMAIL_TO")
        if not (os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER")):
            missing.append("SMTP_USER or EMAIL_FROM")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


def enrich_jobs_with_company(jobs):
    """RSS entries often omit company; fill gaps from the homepage cards."""
    missing = [job for job in jobs if not job.get("company")]
    if not missing:
        return jobs

    html_jobs = fetch_jobs_html()
    company_by_id = {
        job["id"]: job.get("company", "")
        for job in html_jobs
        if job.get("company")
    }
    for job in missing:
        company = company_by_id.get(job["id"])
        if company:
            job["company"] = company
    return jobs


def main():
    default_resume = load_resume()
    if not default_resume:
        print("Warning: resume.txt is empty — drafts will be generic.", file=sys.stderr)

    seen = load_seen()
    jobs = enrich_jobs_with_company(fetch_jobs())
    new_jobs = []

    try:
        if not jobs:
            print("No jobs found (site may have changed structure, or is temporarily down).")
            write_status(jobs_checked=0, new_jobs=[])
            return

        new_jobs = [job for job in jobs if job["id"] not in seen]

        if not new_jobs:
            print(f"No new jobs. {len(jobs)} listings checked, none unseen.")
            write_status(jobs_checked=len(jobs), new_jobs=[])
            return

        # First run: record current listings without emailing about old jobs.
        if not seen and os.environ.get("FORCE_NOTIFY", "").lower() not in ("1", "true", "yes"):
            for job in jobs:
                seen.add(job["id"])
            save_seen(seen)
            print(
                f"Bootstrap: marked {len(jobs)} existing job(s) as seen. "
                "Only new postings after this run will trigger alerts."
            )
            write_status(jobs_checked=len(jobs), new_jobs=[])
            return

        validate_env(require_email=True)
        print(f"Found {len(new_jobs)} new job(s). Drafting cover letters...")

        for job in new_jobs:
            resume_text = load_resume(job) or default_resume
            try:
                draft = draft_application_email(job, resume_text)
            except Exception as exc:
                draft = f"(Could not draft email automatically: {exc})"

            body = (
                f"New WordPress job posted: {job['title']}\n"
                f"Company: {job.get('company', 'Unknown')}\n"
                f"Link: {job['link']}\n"
                f"Posted: {job.get('date', 'unknown')}\n"
                f"{format_application_method(job.get('application_method'))}\n\n"
                f"{'-' * 60}\n"
                f"DRAFTED COVER LETTER\n"
                f"{'-' * 60}\n\n"
                f"{draft}\n"
            )

            profile = get_candidate_profile()
            name_slug = sanitize_filename(profile["name"])
            attachment_name = f"{name_slug}-resume-{sanitize_filename(job['id'])}.pdf"
            resume_pdf = build_resume_pdf_bytes(resume_text)
            send_email(
                f"New WP job: {job['title']}",
                body,
                attachment=(attachment_name, resume_pdf),
            )
            print(f"Emailed draft for: {job['title']}")
            seen.add(job["id"])

        save_seen(seen)
        write_status(jobs_checked=len(jobs), new_jobs=new_jobs)
    except Exception as exc:
        write_status(jobs_checked=len(jobs), new_jobs=new_jobs, error=str(exc))
        raise


if __name__ == "__main__":
    main()
