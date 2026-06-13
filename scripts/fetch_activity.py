#!/usr/bin/env python3
"""Fetch GitHub activity for tracked repos and generate site data.

APIs used:
  - GitHub REST API    (repo info, commits, PRs, issues, contributors, diffs)
  - Groq API           (per-repo commit summaries, weekly digest intro)
  - Gemini API         (cross-repo narrative digest)
  - Scopus API         (citation counts for authors by ORCID or name)
  - GitHub raw files   (R DESCRIPTION, CITATION, README parsing)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Credentials ──────────────────────────────────────────────────────────────
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
SCOPUS_API_KEY  = os.environ.get("SCOPUS_API_KEY", "")

GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    GITHUB_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


# ── GitHub helpers ────────────────────────────────────────────────────────────
def gh_get(url, params=None, accept=None):
    headers = dict(GITHUB_HEADERS)
    if accept:
        headers["Accept"] = accept
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 429 or r.status_code == 403:
                retry_after = int(r.headers.get("Retry-After", 10))
                print(f"    Rate limited, sleeping {retry_after}s...", file=sys.stderr)
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def gh_get_raw(url):
    r = requests.get(url, timeout=15)
    if r.status_code == 200:
        return r.text
    return ""


def fetch_repo_info(owner, repo):
    data = gh_get(f"https://api.github.com/repos/{owner}/{repo}")
    # Also fetch languages breakdown
    try:
        langs = gh_get(f"https://api.github.com/repos/{owner}/{repo}/languages")
        data["languages_breakdown"] = langs
    except Exception:
        data["languages_breakdown"] = {}
    return data


def fetch_branches(owner, repo):
    """Return list of branch dicts with name and last commit date."""
    try:
        branches = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/branches",
            params={"per_page": 100},
        ) or []
        result = []
        for b in branches:
            name = b.get("name", "")
            sha  = (b.get("commit") or {}).get("sha", "")
            # Get last commit date from the commit object
            commit_date = ""
            if sha:
                try:
                    c = gh_get(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}")
                    commit_date = (c.get("commit", {}).get("author", {}).get("date", "") or "")[:10]
                except Exception:
                    pass
            result.append({"name": name, "sha": sha, "last_commit": commit_date})
        return result
    except Exception:
        return []


def fetch_commits(owner, repo, since_days=30, sha=None):
    """Fetch commits since N days ago, optionally from a specific branch SHA."""
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    params = {"since": since, "per_page": 100}
    if sha:
        params["sha"] = sha
    commits, page = [], 1
    while True:
        params["page"] = page
        page_data = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            params=params,
        )
        if not page_data:
            break
        commits.extend(page_data)
        if len(page_data) < 100:
            break
        page += 1
    return commits


def fetch_commits_all_branches(owner, repo, branches, since_days=30):
    """Fetch commits across all active branches, deduplicated by SHA."""
    seen_shas = set()
    all_commits = []
    branch_activity = []
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=since_days)).strftime("%Y-%m-%d")

    for b in branches:
        # Skip branches with no recent activity
        if b.get("last_commit", "") < cutoff:
            continue
        branch_commits = fetch_commits(owner, repo, since_days=since_days, sha=b["sha"])
        new_commits = []
        for c in branch_commits:
            sha = c.get("sha", "")
            if sha not in seen_shas:
                seen_shas.add(sha)
                c["_branch"] = b["name"]
                new_commits.append(c)
                all_commits.append(c)
        if new_commits:
            branch_activity.append({
                "name": b["name"],
                "last_commit": b.get("last_commit", ""),
                "commits_30d": len(new_commits),
            })
        time.sleep(0.2)

    return all_commits, branch_activity


def fetch_recent_commits(owner, repo, n=20):
    return gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/commits",
        params={"per_page": n},
    ) or []


def fetch_commit_diff(owner, repo, sha):
    """Return stats and changed files for a single commit."""
    try:
        data = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
            accept="application/vnd.github+json",
        )
        stats = data.get("stats", {})
        files = [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": (f.get("patch", "") or "")[:500],
            }
            for f in (data.get("files") or [])[:8]
        ]
        return {"additions": stats.get("additions", 0), "deletions": stats.get("deletions", 0), "files": files}
    except Exception:
        return {}


def fetch_pull_requests(owner, repo, state="all"):
    try:
        prs = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": 20, "sort": "updated", "direction": "desc"},
        ) or []
        return [
            {
                "number": p.get("number"),
                "title": p.get("title", ""),
                "state": p.get("state", ""),
                "author": (p.get("user") or {}).get("login", ""),
                "author_url": (p.get("user") or {}).get("html_url", ""),
                "created_at": (p.get("created_at") or "")[:10],
                "updated_at": (p.get("updated_at") or "")[:10],
                "merged_at": (p.get("merged_at") or "")[:10],
                "url": p.get("html_url", ""),
                "draft": p.get("draft", False),
                "labels": [l.get("name","") for l in (p.get("labels") or [])],
                "comments": p.get("comments", 0),
                "review_comments": p.get("review_comments", 0),
            }
            for p in prs
        ]
    except Exception:
        return []


def fetch_releases(owner, repo):
    try:
        rels = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/releases",
            params={"per_page": 10},
        ) or []
        return [
            {
                "tag": r.get("tag_name", ""),
                "name": r.get("name", ""),
                "date": (r.get("published_at") or "")[:10],
                "url": r.get("html_url", ""),
                "prerelease": r.get("prerelease", False),
                "body": (r.get("body") or "")[:300],
            }
            for r in rels
        ]
    except Exception:
        return []


def fetch_issues(owner, repo, state="open"):
    try:
        issues = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": 20, "sort": "updated"},
        ) or []
        return [
            {
                "number": i.get("number"),
                "title": i.get("title", ""),
                "url": i.get("html_url", ""),
                "state": i.get("state", ""),
                "updated_at": (i.get("updated_at") or "")[:10],
                "created_at": (i.get("created_at") or "")[:10],
                "labels": [l.get("name","") for l in (i.get("labels") or [])],
                "comments": i.get("comments", 0),
                "author": (i.get("user") or {}).get("login", ""),
            }
            for i in issues
            if "pull_request" not in i
        ]
    except Exception:
        return []


def fetch_contributors(owner, repo):
    try:
        contribs = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/contributors",
            params={"per_page": 30},
        ) or []
        return contribs
    except Exception:
        return []


def fetch_contributor_profile(login):
    """Fetch full GitHub user profile."""
    try:
        u = gh_get(f"https://api.github.com/users/{login}") or {}
        return {
            "login": login,
            "name": u.get("name", ""),
            "bio": u.get("bio", ""),
            "company": u.get("company", ""),
            "location": u.get("location", ""),
            "blog": u.get("blog", ""),
            "twitter": u.get("twitter_username", ""),
            "avatar_url": u.get("avatar_url", ""),
            "html_url": u.get("html_url", ""),
            "public_repos": u.get("public_repos", 0),
            "followers": u.get("followers", 0),
            "following": u.get("following", 0),
            "created_at": (u.get("created_at") or "")[:10],
        }
    except Exception:
        return {"login": login}


def fetch_contributor_activity(login, repos_list):
    """Summarise this contributor's activity across all tracked repos."""
    activity = {}
    for (owner, repo) in repos_list:
        try:
            commits = gh_get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                params={"author": login, "per_page": 10},
            ) or []
            if commits:
                activity[f"{owner}/{repo}"] = len(commits)
        except Exception:
            pass
    return activity


def fetch_stargazers_timeseries(owner, repo):
    """Approximate star growth: fetch star events (last 2 pages max)."""
    try:
        stars = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/stargazers",
            params={"per_page": 30},
            accept="application/vnd.github.star+json",
        ) or []
        return [
            {"date": (s.get("starred_at") or "")[:10], "user": (s.get("user") or {}).get("login", "")}
            for s in stars
        ]
    except Exception:
        return []


def build_commit_activity_from_commits(commits, n_weeks=12):
    """Compute weekly commit counts from a list of commit objects (all branches)."""
    import math
    now = datetime.now(timezone.utc)
    # Start of current week (Monday)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    buckets = {}
    for w in range(n_weeks):
        ws = week_start - timedelta(weeks=w)
        buckets[ws] = 0

    for c in commits:
        date_str = c.get("commit", {}).get("author", {}).get("date", "")
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            # Find which week bucket this falls in
            days_ago = (now - dt).days
            weeks_ago = days_ago // 7
            if weeks_ago < n_weeks:
                ws = week_start - timedelta(weeks=weeks_ago)
                if ws in buckets:
                    buckets[ws] += 1
        except Exception:
            continue

    return [
        {"week": int(ws.timestamp()), "total": count}
        for ws, count in sorted(buckets.items())
    ]


def fetch_commit_activity(owner, repo):
    """Weekly commit count for last 52 weeks (GitHub stats, default branch only)."""
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/stats/commit_activity") or []
        if isinstance(data, list) and data:
            return [{"week": w.get("week"), "total": w.get("total", 0)} for w in data[-12:]]
        return []
    except Exception:
        return []


def fetch_code_frequency(owner, repo):
    """Weekly additions/deletions for last 52 weeks."""
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/stats/code_frequency") or []
        return [{"week": w[0], "additions": w[1], "deletions": w[2]} for w in (data[-12:] if data else [])]
    except Exception:
        return []


def parse_r_description(owner, repo):
    """Try to fetch and parse DESCRIPTION file from an R package."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/DESCRIPTION"
    text = gh_get_raw(url)
    if not text:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/master/DESCRIPTION"
        text = gh_get_raw(url)
    if not text:
        return {}

    fields = {}
    current_key = None
    for line in text.splitlines():
        if re.match(r"^\S.*:", line):
            parts = line.split(":", 1)
            current_key = parts[0].strip()
            fields[current_key] = parts[1].strip() if len(parts) > 1 else ""
        elif current_key and line.startswith(" "):
            fields[current_key] = (fields[current_key] + " " + line.strip()).strip()

    imports = []
    for key in ("Imports", "Depends", "Suggests"):
        raw = fields.get(key, "")
        if raw:
            pkgs = [re.sub(r"\s*\(.*\)", "", p).strip() for p in raw.split(",")]
            imports.extend([p for p in pkgs if p and p != "R"])

    return {
        "version": fields.get("Version", ""),
        "title": fields.get("Title", ""),
        "authors": fields.get("Authors@R", fields.get("Author", "")),
        "license": fields.get("License", ""),
        "r_version": fields.get("Depends", ""),
        "imports": imports[:20],
        "suggests": [],
        "description_text": fields.get("Description", "")[:400],
        "url_field": fields.get("URL", ""),
        "bug_reports": fields.get("BugReports", ""),
    }


def fetch_readme_summary(owner, repo):
    """Fetch first 600 chars of README."""
    for branch in ("main", "master"):
        for fname in ("README.md", "README.Rmd", "README.rst"):
            text = gh_get_raw(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}")
            if text:
                # Strip badges and html comments
                text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
                text = re.sub(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", "", text)
                text = re.sub(r"#.*\n", "", text, count=3)
                text = text.strip()
                return text[:600]
    return ""


# ── Scopus ────────────────────────────────────────────────────────────────────
def scopus_search_author(name):
    """Search Scopus for an author by name, return h-index and doc count."""
    if not SCOPUS_API_KEY:
        return {}
    try:
        r = requests.get(
            "https://api.elsevier.com/content/search/author",
            params={"query": f"AUTHNAME({name})", "count": 1},
            headers={"X-ELS-APIKey": SCOPUS_API_KEY, "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        entries = r.json().get("search-results", {}).get("entry", [])
        if not entries:
            return {}
        e = entries[0]
        return {
            "scopus_id": e.get("dc:identifier", "").replace("AUTHOR_ID:", ""),
            "document_count": e.get("document-count", ""),
            "h_index": e.get("h-index", ""),
            "affiliation": (e.get("affiliation-current") or {}).get("affiliation-name", ""),
        }
    except Exception:
        return {}


def scopus_cited_by_count(title):
    """Search Scopus for a paper by title, return citation count."""
    if not SCOPUS_API_KEY or not title:
        return None
    try:
        r = requests.get(
            "https://api.elsevier.com/content/search/scopus",
            params={"query": f"TITLE({title[:80]})", "count": 1, "field": "citedby-count,dc:title"},
            headers={"X-ELS-APIKey": SCOPUS_API_KEY, "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        entries = r.json().get("search-results", {}).get("entry", [])
        if not entries:
            return None
        return int(entries[0].get("citedby-count", 0) or 0)
    except Exception:
        return None


# ── Groq ──────────────────────────────────────────────────────────────────────
def groq_chat(prompt, max_tokens=400, model="llama-3.3-70b-versatile"):
    if not GROQ_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"    Groq error: {e}", file=sys.stderr)
        return ""


def summarize_commits_groq(repo_name, commit_lines):
    if not commit_lines:
        return "No recent commits to summarize."
    text = "\n".join(commit_lines[:40])
    return groq_chat(
        f"Summarize the recent development activity for the GitHub repository '{repo_name}' "
        f"based on these commit messages. Write 2-4 sentences describing what has been worked on, "
        f"what changed, and the overall direction of development. Be concrete and specific.\n\nCommits:\n{text}",
        max_tokens=300,
    ) or "Summary unavailable."


def summarize_diff_groq(repo_name, commit_msg, diff_files):
    if not diff_files:
        return ""
    file_list = "; ".join(f["filename"] for f in diff_files[:5])
    patch_snippets = "\n".join(
        f"--- {f['filename']} (+{f['additions']}/-{f['deletions']})\n{f['patch'][:200]}"
        for f in diff_files[:3]
        if f.get("patch")
    )
    return groq_chat(
        f"In one sentence, describe what this commit does in '{repo_name}'. "
        f"Commit: '{commit_msg}'. Files changed: {file_list}.\n"
        + (f"Patch excerpt:\n{patch_snippets}" if patch_snippets else ""),
        max_tokens=80,
    )


def classify_commit_type(message):
    """Heuristically classify commit as fix/feat/docs/refactor/test/chore."""
    msg = message.lower()
    if re.search(r"\bfix\b|bug|crash|error|wrong|broken|revert", msg):
        return "fix"
    if re.search(r"\badd\b|\bnew\b|feat|implement|support|introduce", msg):
        return "feat"
    if re.search(r"\bdoc\b|readme|vignette|cran|pkgdown|news|changelog|comment", msg):
        return "docs"
    if re.search(r"\btest\b|testthat|spec|coverage", msg):
        return "test"
    if re.search(r"\brefactor\b|clean|rename|reorgani|restructure|simplif", msg):
        return "refactor"
    if re.search(r"\bchore\b|bump|version|release|ci|github.action|workflow|depend", msg):
        return "chore"
    return "other"


# ── Gemini ────────────────────────────────────────────────────────────────────
def gemini_narrative(all_repo_summaries):
    """Generate a cross-repo narrative digest using Gemini."""
    if not GEMINI_API_KEY or not all_repo_summaries:
        return ""
    text = "\n\n".join(f"**{name}**: {summary}" for name, summary in all_repo_summaries)
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text":
                    f"You are a science communication assistant. Based on the following summaries of "
                    f"recent activity across multiple open-source research tools, write a short "
                    f"(4-6 sentence) community newsletter paragraph. Highlight connections between "
                    f"projects, celebrate progress, and note anything noteworthy.\n\n{text}"
                }]}],
                "generationConfig": {"maxOutputTokens": 400, "temperature": 0.5},
            },
            timeout=30,
        )
        r.raise_for_status()
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
        return parts[0].get("text", "").strip()
    except Exception as e:
        print(f"    Gemini error: {e}", file=sys.stderr)
        return ""


# ── Main processing ───────────────────────────────────────────────────────────
def commit_to_text(commit):
    msg = commit.get("commit", {}).get("message", "").split("\n")[0]
    date = commit.get("commit", {}).get("author", {}).get("date", "")[:10]
    author = commit.get("commit", {}).get("author", {}).get("name", "unknown")
    return f"[{date}] {author}: {msg}"


def is_dormant_revived(commits_30d, pushed_at):
    if not commits_30d or not pushed_at:
        return False
    now = datetime.now(timezone.utc)
    pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    # Revived = has recent commits but repo was quiet for a long time before
    # Heuristic: <=3 commits in last 30d, and the oldest of those is <15d old
    if len(commits_30d) > 5:
        return False
    dates = []
    for c in commits_30d:
        d = c.get("commit", {}).get("author", {}).get("date", "")
        if d:
            dates.append(datetime.fromisoformat(d.replace("Z", "+00:00")))
    if not dates:
        return False
    oldest_recent = min(dates)
    return (now - oldest_recent).days < 15


def process_repo(owner, repo, description, all_logins_seen, repos_list):
    print(f"  [{owner}/{repo}]")

    try:
        info = fetch_repo_info(owner, repo)
    except Exception as e:
        print(f"    ERROR: {e}", file=sys.stderr)
        return None

    now = datetime.now(timezone.utc)

    # Branches — fetch all, then gather commits across active ones
    print(f"    fetching branches...")
    branches = fetch_branches(owner, repo)

    commits_30d = []
    branch_activity = []
    try:
        commits_30d, branch_activity = fetch_commits_all_branches(owner, repo, branches, since_days=30)
    except Exception as e:
        print(f"    warn commits: {e}", file=sys.stderr)

    def parse_dt(c):
        return datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00"))

    commits_7d = [c for c in commits_30d if parse_dt(c) > now - timedelta(days=7)]
    commits_1d = [c for c in commits_30d if parse_dt(c) > now - timedelta(days=1)]

    commits_90d = []
    try:
        commits_90d, _ = fetch_commits_all_branches(owner, repo, branches, since_days=90)
    except Exception:
        pass

    # Recent commits sorted newest-first (across all branches)
    commits_30d_sorted = sorted(commits_30d, key=parse_dt, reverse=True)
    recent_commits_raw = commits_30d_sorted[:20]

    # Classify commits
    recent_commits = []
    for c in recent_commits_raw[:15]:
        msg = c.get("commit", {}).get("message", "").split("\n")[0]
        sha = c.get("sha", "")[:7]
        ctype = classify_commit_type(msg)
        entry = {
            "sha": sha,
            "message": msg,
            "author": c.get("commit", {}).get("author", {}).get("name", ""),
            "author_login": (c.get("author") or {}).get("login", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", "")[:10],
            "url": c.get("html_url", ""),
            "type": ctype,
            "branch": c.get("_branch", ""),
            "diff": {},
        }
        recent_commits.append(entry)

    # Diff for most recent commit only (API quota care)
    if recent_commits_raw:
        full_sha = recent_commits_raw[0].get("sha", "")
        if full_sha and recent_commits:
            recent_commits[0]["diff"] = fetch_commit_diff(owner, repo, full_sha)
            diff_summary = summarize_diff_groq(
                f"{owner}/{repo}",
                recent_commits[0]["message"],
                recent_commits[0]["diff"].get("files", []),
            )
            recent_commits[0]["diff_summary"] = diff_summary

    # Commit type breakdown (30d, all branches)
    type_breakdown = {}
    for c in commits_30d:
        msg = c.get("commit", {}).get("message", "").split("\n")[0]
        t = classify_commit_type(msg)
        type_breakdown[t] = type_breakdown.get(t, 0) + 1

    # PRs, releases, issues
    prs        = fetch_pull_requests(owner, repo, state="all")
    releases   = fetch_releases(owner, repo)
    open_issues  = fetch_issues(owner, repo, state="open")
    closed_issues_recent = fetch_issues(owner, repo, state="closed")

    # Contributors
    raw_contribs = fetch_contributors(owner, repo)
    contributors = []
    for c in (raw_contribs or [])[:10]:
        login = c.get("login", "")
        all_logins_seen.add(login)
        contributors.append({
            "login": login,
            "contributions": c.get("contributions", 0),
            "avatar_url": c.get("avatar_url", ""),
            "html_url": c.get("html_url", ""),
        })

    # Activity timeseries — compute from actual fetched commits (all branches),
    # fall back to GitHub stats API which only covers default branch
    commit_activity = build_commit_activity_from_commits(commits_90d or commits_30d, n_weeks=12)
    if not any(w["total"] > 0 for w in commit_activity):
        commit_activity = fetch_commit_activity(owner, repo)
    code_frequency  = fetch_code_frequency(owner, repo)
    star_history    = fetch_stargazers_timeseries(owner, repo)

    # R package metadata
    r_meta = parse_r_description(owner, repo)
    readme_excerpt = fetch_readme_summary(owner, repo)

    # Scopus: try to get citation count for the package title
    scopus_citations = None
    if r_meta.get("title"):
        scopus_citations = scopus_cited_by_count(r_meta["title"])

    # AI summary (Groq) — only for repos with recent commits
    commit_lines = [commit_to_text(c) for c in commits_30d]
    summary = summarize_commits_groq(f"{owner}/{repo}", commit_lines) if commit_lines else ""

    pushed_at = info.get("pushed_at", "")
    last_push_days = None
    if pushed_at:
        pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        last_push_days = (now - pushed_dt).days

    # Unique authors last 30d
    authors_30d = list({
        c.get("commit", {}).get("author", {}).get("name", "")
        for c in commits_30d
        if c.get("commit", {}).get("author", {}).get("name")
    })

    # PR stats
    open_prs   = [p for p in prs if p["state"] == "open"]
    merged_prs = [p for p in prs if p["merged_at"]]

    return {
        "owner": owner,
        "repo": repo,
        "description": description or info.get("description", ""),
        "url": info.get("html_url", f"https://github.com/{owner}/{repo}"),
        "stars": info.get("stargazers_count", 0),
        "watchers": info.get("watchers_count", 0),
        "forks": info.get("forks_count", 0),
        "open_issues_count": info.get("open_issues_count", 0),
        "network_count": info.get("network_count", 0),
        "subscribers_count": info.get("subscribers_count", 0),
        "language": info.get("language", ""),
        "languages_breakdown": info.get("languages_breakdown", {}),
        "topics": info.get("topics", []),
        "pushed_at": pushed_at,
        "created_at": (info.get("created_at") or "")[:10],
        "last_push_days": last_push_days,
        "default_branch": info.get("default_branch", "main"),
        "license": (info.get("license") or {}).get("spdx_id", ""),
        # Branches
        "branches": [{"name": b["name"], "last_commit": b.get("last_commit", "")} for b in branches],
        "branch_activity": branch_activity,
        # Commit counts (across all branches)
        "commits_30d": len(commits_30d),
        "commits_7d":  len(commits_7d),
        "commits_1d":  len(commits_1d),
        "commits_90d": len(commits_90d),
        "authors_30d": authors_30d,
        "type_breakdown": type_breakdown,
        # Status flags
        "dormant_revived": is_dormant_revived(commits_30d, pushed_at),
        "is_archived": info.get("archived", False),
        # AI summaries
        "summary": summary,
        # PRs
        "prs": prs[:15],
        "open_prs_count": len(open_prs),
        "merged_prs_count": len(merged_prs),
        # Releases
        "releases": releases[:5],
        # Issues
        "open_issues": open_issues[:10],
        "closed_issues_recent": closed_issues_recent[:5],
        # Contributors
        "contributors": contributors,
        # Timeseries
        "commit_activity": commit_activity,
        "code_frequency": code_frequency,
        "star_history": star_history[-10:],
        # R package
        "r_meta": r_meta,
        "readme_excerpt": readme_excerpt,
        # Scopus
        "scopus_citations": scopus_citations,
        # Commits
        "recent_commits": recent_commits,
    }


def build_people_index(repos_data, all_logins_seen, repos_list):
    """Build a cross-repo people index with enriched profiles."""
    print("  Building people index...")
    people = {}

    # Collect all contributors across all repos
    for repo_data in repos_data:
        for c in repo_data.get("contributors", []):
            login = c["login"]
            if login not in people:
                people[login] = {
                    "login": login,
                    "avatar_url": c["avatar_url"],
                    "html_url": c["html_url"],
                    "repos": {},
                    "total_contributions": 0,
                }
            people[login]["repos"][f"{repo_data['owner']}/{repo_data['repo']}"] = c["contributions"]
            people[login]["total_contributions"] += c["contributions"]

    # Enrich top contributors with full profiles
    sorted_people = sorted(people.values(), key=lambda x: x["total_contributions"], reverse=True)
    for person in sorted_people[:15]:
        login = person["login"]
        print(f"    Enriching profile: {login}")
        profile = fetch_contributor_profile(login)
        person.update(profile)
        time.sleep(0.3)  # gentle rate limiting

        # Scopus lookup for named contributors
        if profile.get("name"):
            scopus = scopus_search_author(profile["name"])
            person["scopus"] = scopus
        else:
            person["scopus"] = {}

    return sorted_people


def build_weekly_digest(repos_data, gemini_narrative_text):
    """Assemble structured weekly digest."""
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    active = [r for r in repos_data if r["commits_7d"] > 0]
    dormant_revived = [r for r in repos_data if r["dormant_revived"]]
    new_releases = [r for r in repos_data if r["releases"] and r["releases"][0]["date"] >= (now - timedelta(days=7)).strftime("%Y-%m-%d")]
    most_active = sorted(active, key=lambda x: x["commits_7d"], reverse=True)

    # Collect all commit messages this week across repos
    highlights = []
    for r in repos_data:
        for c in r.get("recent_commits", []):
            if c["date"] >= week_start and c["type"] in ("feat", "fix"):
                highlights.append({
                    "repo": r["repo"],
                    "owner": r["owner"],
                    "message": c["message"],
                    "type": c["type"],
                    "author": c["author"],
                    "date": c["date"],
                })
    highlights.sort(key=lambda x: x["date"], reverse=True)

    return {
        "week_start": week_start,
        "active_repos": len(active),
        "total_commits_7d": sum(r["commits_7d"] for r in repos_data),
        "dormant_revived": [{"owner": r["owner"], "repo": r["repo"]} for r in dormant_revived],
        "new_releases": [{"owner": r["owner"], "repo": r["repo"], "tag": r["releases"][0]["tag"]} for r in new_releases],
        "most_active": [{"owner": r["owner"], "repo": r["repo"], "commits": r["commits_7d"]} for r in most_active[:5]],
        "highlights": highlights[:20],
        "narrative": gemini_narrative_text,
    }


SITE_URL = "https://lakens.github.io/automated_review_daily_build"


def xml_escape(s):
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def rfc822(iso):
    """Convert ISO 8601 UTC string to RFC 822 format for RSS."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def generate_rss(results, digest, generated_at, output_path):
    """Generate an RSS 2.0 feed with one item per notable daily event."""
    items = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Item 1: daily digest summary
    active = [r for r in results if r["commits_7d"] > 0]
    narrative = digest.get("narrative", "")
    total_commits = digest.get("total_commits_7d", 0)
    digest_desc = []
    if narrative:
        digest_desc.append(f"<p>{xml_escape(narrative)}</p>")
    digest_desc.append(f"<p><strong>{total_commits}</strong> commits across {len(active)} active repositories this week.</p>")
    if digest.get("new_releases"):
        rels = ", ".join(f"{r['repo']} {r['tag']}" for r in digest["new_releases"])
        digest_desc.append(f"<p>New releases: {xml_escape(rels)}</p>")
    if digest.get("dormant_revived"):
        repos = ", ".join(r["repo"] for r in digest["dormant_revived"])
        digest_desc.append(f"<p>Dormant repos revived: {xml_escape(repos)}</p>")
    most_active = digest.get("most_active", [])
    if most_active:
        rows = "".join(
            f"<tr><td><a href='https://github.com/{xml_escape(r['owner'])}/{xml_escape(r['repo'])}'>"
            f"{xml_escape(r['repo'])}</a></td><td>{r['commits']} commits</td></tr>"
            for r in most_active
        )
        digest_desc.append(f"<p><strong>Most active:</strong></p><table>{rows}</table>")

    items.append({
        "title": f"Daily digest — {today}",
        "link": SITE_URL,
        "guid": f"{SITE_URL}/digest/{today}",
        "pubDate": rfc822(generated_at),
        "description": "\n".join(digest_desc),
    })

    # One item per repo that had commits today or a new release
    for r in results:
        events = []
        if r["commits_1d"] > 0:
            today_commits = [c for c in r.get("recent_commits", []) if c["date"] == today]
            commits_html = "".join(
                f"<li><code>{xml_escape(c['sha'])}</code> "
                f"[{xml_escape(c.get('branch', ''))}] "
                f"<a href='{xml_escape(c['url'])}'>{xml_escape(c['message'])}</a> "
                f"— {xml_escape(c['author'])}</li>"
                for c in (today_commits or r.get("recent_commits", []))[:20]
            )
            events.append(
                f"<p><strong>{r['commits_1d']} commit(s) today:</strong></p>"
                f"<ul>{commits_html}</ul>"
            )
        if r.get("releases") and r["releases"][0].get("date") == today:
            rel = r["releases"][0]
            events.append(
                f"<p>New release: <a href='{xml_escape(rel['url'])}'>"
                f"{xml_escape(rel['tag'])}</a>"
                + (f" — {xml_escape(rel['name'])}" if rel.get("name") else "")
                + "</p>"
            )
        if r.get("dormant_revived"):
            events.append("<p>&#9888; This repository was dormant and has recently become active again.</p>")

        if not events:
            continue

        summary_html = (
            f"<p><em>{xml_escape(r['summary'])}</em></p>" if r.get("summary") else ""
        )
        desc = (
            f"<h3><a href='{xml_escape(r['url'])}'>{xml_escape(r['owner'])}/{xml_escape(r['repo'])}</a></h3>"
            + summary_html
            + "\n".join(events)
            + (f"<p><a href='{xml_escape(r['url'])}/commits'>View all commits &rarr;</a></p>")
        )
        items.append({
            "title": f"{r['repo']}: {r['commits_1d']} commit{'s' if r['commits_1d'] != 1 else ''} today"
                     if r["commits_1d"] > 0 else f"{r['repo']}: new release {r['releases'][0]['tag']}",
            "link": r["url"],
            "guid": f"{SITE_URL}/repo/{r['owner']}/{r['repo']}/{today}",
            "pubDate": rfc822(r.get("pushed_at") or generated_at),
            "description": desc,
        })

    items_xml = "\n".join(f"""    <item>
      <title>{xml_escape(i['title'])}</title>
      <link>{i['link']}</link>
      <guid isPermaLink="false">{i['guid']}</guid>
      <pubDate>{i['pubDate']}</pubDate>
      <description><![CDATA[{i['description']}]]></description>
    </item>""" for i in items)

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>What Is Being Built — Daily Activity Feed</title>
    <link>{SITE_URL}</link>
    <description>Daily updates on open-source automated research tools: commits, releases, and community activity.</description>
    <language>en-us</language>
    <lastBuildDate>{rfc822(generated_at)}</lastBuildDate>
    <atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(feed)
    print(f"Wrote RSS feed to {output_path} ({len(items)} items)")


def main():
    repos_path  = Path(__file__).parent.parent / "repos.json"
    output_dir  = Path(__file__).parent.parent / "data"
    output_dir.mkdir(exist_ok=True)

    with open(repos_path) as f:
        repos = json.load(f)

    repos_list = [(r["owner"], r["repo"]) for r in repos]
    all_logins_seen = set()

    print("Fetching repo data...")
    results = []
    for r in repos:
        data = process_repo(r["owner"], r["repo"], r.get("description", ""), all_logins_seen, repos_list)
        if data:
            results.append(data)
        time.sleep(1)

    print("Building people index...")
    people = build_people_index(results, all_logins_seen, repos_list)

    print("Generating Gemini cross-repo narrative...")
    repo_summaries = [(f"{r['owner']}/{r['repo']}", r["summary"]) for r in results if r["summary"]]
    narrative = gemini_narrative(repo_summaries)

    print("Building weekly digest...")
    digest = build_weekly_digest(results, narrative)

    # Compute aggregate stats
    all_authors = set()
    for r in results:
        all_authors.update(r.get("authors_30d", []))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repos": results,
        "people": people,
        "digest": digest,
        "aggregate": {
            "total_repos": len(results),
            "total_stars": sum(r["stars"] for r in results),
            "total_forks": sum(r["forks"] for r in results),
            "total_commits_30d": sum(r["commits_30d"] for r in results),
            "total_commits_7d": sum(r["commits_7d"] for r in results),
            "total_commits_1d": sum(r["commits_1d"] for r in results),
            "active_repos_7d": sum(1 for r in results if r["commits_7d"] > 0),
            "dormant_revived": sum(1 for r in results if r["dormant_revived"]),
            "total_open_issues": sum(r["open_issues_count"] for r in results),
            "total_open_prs": sum(r["open_prs_count"] for r in results),
            "unique_authors_30d": len(all_authors),
            "total_contributors": len(people),
        },
    }

    out_path = output_dir / "activity.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    rss_path = Path(__file__).parent.parent / "feed.xml"
    generate_rss(results, digest, output["generated_at"], rss_path)

    print(f"Done. Wrote {len(results)} repos, {len(people)} people to {out_path}")


if __name__ == "__main__":
    main()
