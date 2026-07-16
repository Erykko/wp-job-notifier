# WP Job Notifier

Checks [jobs.wordpress.net](https://jobs.wordpress.net/) on a schedule. For every new listing it drafts a tailored professional cover letter (via Gemini when available, with a local fallback when the API is rate-limited) and emails it to you, so you get "new job + ready-to-send application" in one message.

**Live dashboard:** https://erykko.github.io/wp-job-notifier/

## Setup

1. **Add repo secrets** under Settings → Secrets and variables → Actions:

   | Secret | Gmail value |
   |---|---|
   | `GEMINI_API_KEY` | Optional but recommended: your Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) |
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | Your Gmail address, e.g. `you@gmail.com` |
   | `SMTP_PASS` | A Gmail [App Password](https://myaccount.google.com/apppasswords) — not your normal Gmail password |
   | `EMAIL_FROM` | Same Gmail address as `SMTP_USER` |
   | `EMAIL_TO` | Where alerts should land — can be the same Gmail inbox |
   | `CANDIDATE_NAME` | Your name (used in cover-letter signatures) |
   | `CANDIDATE_EMAIL` | Public contact email for applications |
   | `CANDIDATE_PHONE` | Optional phone number |
   | `CANDIDATE_WEBSITE` | Optional portfolio URL |
   | `RESUME_TXT` | Full contents of your `resume.txt` (keeps it out of the public repo) |

   **Gmail setup notes:**
   - Turn on 2-Step Verification for your Google account first.
   - Create an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
   - Use that 16-character app password as `SMTP_PASS`.
   - `SMTP_HOST` and `SMTP_PORT` can be omitted if you use the Gmail defaults above.

2. **Keep your resume private.** Copy `resume.example.txt` to `resume.txt` locally (gitignored). For GitHub Actions, paste the same text into the `RESUME_TXT` secret. Do not commit real resumes.

3. **Run the workflow** from the Actions tab → "Check WordPress Jobs" → "Run workflow". Use the `dry_run` checkbox to test without sending email.

The workflow runs every 2 hours automatically and deploys a status page to GitHub Pages after each run.

## How it works

- `check_jobs.py` fetches listings from the WordPress RSS feed (`/feed/`), with HTML scraping as a fallback for the current `job-card` layout.
- New job IDs are tracked in `seen_jobs.json`, which the workflow commits back to the repo after each run.
- For each new job, it fetches the full posting text and calls the Gemini API with your resume to draft a professional cover letter. If Gemini is missing, rate-limited, or unavailable, it generates a deterministic local draft instead of sending a failure placeholder.
- The listing details + drafted cover letter are sent in one email, with the resume attached as a PDF.
- `docs/status.json` is updated each run and shown on the GitHub Pages dashboard.

## Local testing

```bash
pip install -r requirements.txt
cp resume.example.txt resume.txt   # then edit with your real resume

# Fetch jobs only (no API/email needed):
python -c "import check_jobs as c; print(c.fetch_jobs())"

# Full dry run (Gemini optional, skips email):
DRY_RUN=true GEMINI_API_KEY=your-key \
CANDIDATE_NAME="Your Name" CANDIDATE_EMAIL=you@example.com \
python check_jobs.py

# Test Gmail delivery locally:
SMTP_USER=you@gmail.com \
SMTP_PASS=your-gmail-app-password \
EMAIL_FROM=you@gmail.com \
EMAIL_TO=you@gmail.com \
GEMINI_API_KEY=your-key \
CANDIDATE_NAME="Your Name" \
CANDIDATE_EMAIL=you@example.com \
python check_jobs.py
```

## Notes

- Nothing here auto-sends the application — it lands in your inbox as a draft for you to review and send yourself.
- Change the cron schedule in `.github/workflows/check-jobs.yml` if 2 hours is too often or not often enough.
- If jobs.wordpress.net changes its HTML again, update `fetch_jobs_html()` in `check_jobs.py`.
