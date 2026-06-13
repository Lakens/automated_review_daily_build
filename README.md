# What Is Being Built

A daily-updated dashboard tracking activity across open-source automated research tools.

**Live site:** https://lakens.github.io/automated_review_daily_build

**RSS feed:** https://lakens.github.io/automated_review_daily_build/feed.xml

## What it does

The site fetches data from GitHub every day at 06:00 UTC and generates an overview of community activity across a curated list of repositories. It uses AI to summarise what has been worked on and surfaces things like dormant repos coming back to life, new releases, and open pull requests.

### Dashboard pages

- **Overview** — stat bar, highlight chips, AI-generated cross-repo narrative, this week's commits, 12-week activity heatmap
- **Repositories** — per-repo cards with commit summaries, PRs, releases, issues, star history, R dependency lists
- **People** — contributor profiles with GitHub stats and Scopus publication metrics
- **Digest** — most active repos, open PRs, recent releases, dependency overlap, code frequency charts
- **Timeline** — all commits across all repos, filterable by type (feature, fix, docs, test, refactor, chore)

### APIs used

| API | Purpose |
|-----|---------|
| GitHub REST API | Commits, PRs, issues, contributors, releases, code stats |
| [Groq](https://groq.com) (llama-3.3-70b) | Per-repo commit summaries and diff descriptions |
| [Gemini](https://deepmind.google/technologies/gemini/) (1.5 Flash) | Cross-repo community narrative |
| [Scopus](https://www.scopus.com) | Author h-index and publication counts |

## Tracked repositories

<!-- keep in sync with repos.json -->
| Repository | Description |
|-----------|-------------|
| [scienceverse/metacheck](https://github.com/scienceverse/metacheck) | Check research outputs for best practices |
| [scienceverse/regcheck](https://github.com/scienceverse/regcheck) | Check registered reports and pre-registrations |
| [MicheleNuijten/statcheck](https://github.com/MicheleNuijten/statcheck) | Spellchecker for statistics |
| [marton-balazs-kovacs/tenzing](https://github.com/marton-balazs-kovacs/tenzing) | Automated contributorship documentation (CRediT) |
| [lhdjung/scrutiny](https://github.com/lhdjung/scrutiny) | Error detection via consistency tests for summary statistics |
| [quest-bih/oddpub](https://github.com/quest-bih/oddpub) | Detect open data and open code statements in publications |
| [cjvanlissa/worcs](https://github.com/cjvanlissa/worcs) | Workflow for Open Reproducible Code in Science |
| [giladfeldman/docpluck](https://github.com/giladfeldman/docpluck) | Extract statistical information from Word documents |
| [quest-bih/ContriBOT](https://github.com/quest-bih/ContriBOT) | Automated extraction of author contributions |
| [quest-bih/rtransparent](https://github.com/quest-bih/rtransparent) | Screen publications for transparency indicators |
| [Davidvandijcke/coarse](https://github.com/Davidvandijcke/coarse) | Coarsened exact matching for causal inference |

To add a repository, edit [repos.json](repos.json) and open a pull request.

## Stay updated

Subscribe to the [RSS feed](https://lakens.github.io/automated_review_daily_build/feed.xml) in any RSS reader (Feedly, NetNewsWire, etc.) to receive daily digests without visiting the site.

## How it works

```
repos.json
    │
    ▼
scripts/fetch_activity.py   (runs daily via GitHub Actions)
    │   ├── GitHub API  → commits, PRs, issues, releases, contributors
    │   ├── Groq API    → commit summaries, diff descriptions
    │   ├── Gemini API  → cross-repo narrative
    │   └── Scopus API  → author citation metrics
    │
    ├── data/activity.json  (consumed by index.html)
    └── feed.xml            (RSS feed)
```

GitHub Actions workflows:
- **update.yml** — runs daily, fetches data, commits `data/activity.json` and `feed.xml`
- **pages.yml** — deploys the site to GitHub Pages on every push to `main`

## Setup (for forks)

1. Add repository secrets: `GROQ_API_KEY`, `GEMINI_API_KEY`, `SCOPUS_API_KEY`
2. Enable GitHub Pages: Settings → Pages → Source: GitHub Actions
3. Trigger the first run: Actions → "Update Activity Data" → Run workflow
