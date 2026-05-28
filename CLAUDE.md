# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Daily news aggregator that fetches RSS articles from multiple sources, summarizes them in Japanese using Gemini AI, and publishes a static HTML page to GitHub Pages. It runs automatically via GitHub Actions twice a day (JST 09:30 and 15:00).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the news generator (requires GEMINI_API_KEY env var)
GEMINI_API_KEY=your_key python scripts/generate_news.py

# Trigger the GitHub Actions workflow manually (via GitHub UI or gh CLI)
gh workflow run daily_news.yml
```

There are no tests or linters configured.

## Architecture

All logic lives in `scripts/generate_news.py`. The pipeline is:

1. **`fetch_articles(category, sources)`** — Parses each RSS feed with `feedparser`, caps at 3 articles per source and 15 per category.
2. **`summarize_category(category, articles, client)`** — Sends a Japanese-language prompt to `gemini-2.5-flash` via `google-genai`. Retries once on 503 (after 30s), retries on 429 rate limits with exponential backoff (4s, 8s…); aborts immediately on daily quota exhaustion (`PerDay` in error string).
3. **`build_html(summaries)`** — Produces a self-contained HTML file with all CSS inlined.
4. **`main()`** — Orchestrates the above with a 5-second pause between categories to avoid rate limiting, then writes output to `docs/index.html`.

## Key conventions

- **`docs/index.html` is auto-generated** — never edit it manually; it is overwritten on every run.
- The `GEMINI_API_KEY` must be set as a GitHub Actions secret (`secrets.GEMINI_API_KEY`) for the workflow and as a local env var for manual runs.
- RSS sources and per-category limits are defined in the `RSS_SOURCES` dict at the top of `generate_news.py`. Categories are keyed by Japanese strings (`世界のニュース`, `日本のニュース`, `テクノロジー`, `エネルギー`); the matching emoji icons are in `CATEGORY_ICONS`.
- The workflow commits directly to `master` under the `github-actions[bot]` identity with the message format `chore: update news digest YYYY-MM-DD HH:MM JST`.
- Python version is 3.12 (pinned in the workflow).
