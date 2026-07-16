# Tailored resumes

Private resume files are gitignored. Keep real resumes only on your machine (or in Actions secrets / a private store), not in this public repo.

## Setup

1. Copy `../resume.example.txt` to `../resume.txt` and fill in your real bio.
2. Optionally add tailored files here (for example `hosting-support.txt`) based on `example-role.txt`.
3. Map job IDs to those files in `by-job-id.json`:

```json
{
  "some-job-slug": "hosting-support.txt"
}
```

The notifier reads `by-job-id.json` and uses the matching tailored resume when drafting application emails.
