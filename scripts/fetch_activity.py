#!/usr/bin/env python3
"""Fetch GitHub activity for tracked repos and generate site data."""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    GITHUB_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


def gh_get(url, params=None):
    r = requests.get(url, headers=GITHUB_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_repo_info(owner, repo):
    return gh_get(f"https://api.github.com/repos/{owner}/{repo}")


def fetch_commits(owner, repo, since_days=30):
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    commits = []
    page = 1
    while True:
        page_data = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            params={"since": since, "per_page": 100, "page": page},
        )
        if not page_data:
            break
        commits.extend(page_data)
        if len(page_data) < 100:
            break
        page += 1
    return commits


def fetch_recent_commits(owner, repo, n=10):
    """Fetch the n most recent commits regardless of date."""
    data = gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/commits",
        params={"per_page": n},
    )
    return data


def fetch_releases(owner, repo):
    try:
        return gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/releases",
            params={"per_page": 5},
        )
    except Exception:
        return []


def fetch_issues(owner, repo, state="open"):
    try:
        return gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": 10, "sort": "updated"},
        )
    except Exception:
        return []


def fetch_contributors(owner, repo):
    try:
        return gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/contributors",
            params={"per_page": 10},
        )
    except Exception:
        return []


def commit_to_text(commit):
    msg = commit.get("commit", {}).get("message", "").split("\n")[0]
    date = commit.get("commit", {}).get("author", {}).get("date", "")[:10]
    author = commit.get("commit", {}).get("author", {}).get("name", "unknown")
    return f"[{date}] {author}: {msg}"


def summarize_with_groq(repo_name, commit_lines):
    if not GROQ_API_KEY:
        return "No Groq API key configured — summaries unavailable."
    if not commit_lines:
        return "No recent commits to summarize."

    text = "\n".join(commit_lines[:40])
    prompt = (
        f"Summarize the recent development activity for the GitHub repository '{repo_name}' "
        f"based on these commit messages. Write 2-4 sentences describing what has been worked on, "
        f"what changed, and the overall direction of development. Be concrete and specific.\n\n"
        f"Commits:\n{text}"
    )

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def is_dormant_recently_active(commits_30d, info):
    """True if repo had no commits for >60 days but has one in last 30."""
    pushed_at = info.get("pushed_at", "")
    created_at = info.get("created_at", "")
    if not pushed_at:
        return False
    # We check: has commits in 30d window AND repo was inactive before that
    if not commits_30d:
        return False
    # Use the repo's updated_at vs pushed_at gap heuristic:
    # If latest commit is <30d old AND the second commit in the list is >60d old, flag it
    if len(commits_30d) == 0:
        return False
    # Simpler: tag as dormant-revived if commits_30d has entries but total recent count is <=3
    # (meaning activity just restarted). The site generation can refine this.
    return len(commits_30d) <= 3


def process_repo(owner, repo, description):
    print(f"  Processing {owner}/{repo}...")
    try:
        info = fetch_repo_info(owner, repo)
    except Exception as e:
        print(f"    ERROR fetching info: {e}", file=sys.stderr)
        return None

    commits_30d = []
    commits_7d = []
    commits_1d = []
    try:
        commits_30d = fetch_commits(owner, repo, since_days=30)
        now = datetime.now(timezone.utc)
        commits_7d = [
            c for c in commits_30d
            if datetime.fromisoformat(
                c["commit"]["author"]["date"].replace("Z", "+00:00")
            ) > now - timedelta(days=7)
        ]
        commits_1d = [
            c for c in commits_30d
            if datetime.fromisoformat(
                c["commit"]["author"]["date"].replace("Z", "+00:00")
            ) > now - timedelta(days=1)
        ]
    except Exception as e:
        print(f"    WARNING: could not fetch commits: {e}", file=sys.stderr)

    recent_commits = fetch_recent_commits(owner, repo, n=15)
    releases = fetch_releases(owner, repo)
    open_issues = fetch_issues(owner, repo, state="open")
    contributors = fetch_contributors(owner, repo)

    commit_lines = [commit_to_text(c) for c in commits_30d]
    summary = summarize_with_groq(f"{owner}/{repo}", commit_lines)

    pushed_at = info.get("pushed_at", "")
    last_push_days = None
    if pushed_at:
        pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        last_push_days = (datetime.now(timezone.utc) - pushed_dt).days

    dormant_revived = is_dormant_recently_active(commits_30d, info)

    return {
        "owner": owner,
        "repo": repo,
        "description": description or info.get("description", ""),
        "url": info.get("html_url", f"https://github.com/{owner}/{repo}"),
        "stars": info.get("stargazers_count", 0),
        "forks": info.get("forks_count", 0),
        "open_issues_count": info.get("open_issues_count", 0),
        "language": info.get("language", ""),
        "topics": info.get("topics", []),
        "pushed_at": pushed_at,
        "last_push_days": last_push_days,
        "commits_30d": len(commits_30d),
        "commits_7d": len(commits_7d),
        "commits_1d": len(commits_1d),
        "dormant_revived": dormant_revived,
        "summary": summary,
        "recent_commits": [
            {
                "sha": c.get("sha", "")[:7],
                "message": c.get("commit", {}).get("message", "").split("\n")[0],
                "author": c.get("commit", {}).get("author", {}).get("name", ""),
                "date": c.get("commit", {}).get("author", {}).get("date", "")[:10],
                "url": c.get("html_url", ""),
            }
            for c in (recent_commits or [])[:10]
        ],
        "releases": [
            {
                "tag": r.get("tag_name", ""),
                "name": r.get("name", ""),
                "date": (r.get("published_at") or "")[:10],
                "url": r.get("html_url", ""),
            }
            for r in (releases or [])[:3]
        ],
        "open_issues": [
            {
                "number": i.get("number"),
                "title": i.get("title", ""),
                "url": i.get("html_url", ""),
                "updated_at": (i.get("updated_at") or "")[:10],
            }
            for i in (open_issues or [])[:5]
            if "pull_request" not in i
        ],
        "contributors": [
            {
                "login": c.get("login", ""),
                "contributions": c.get("contributions", 0),
                "avatar_url": c.get("avatar_url", ""),
                "html_url": c.get("html_url", ""),
            }
            for c in (contributors or [])[:5]
        ],
    }


def main():
    repos_path = Path(__file__).parent.parent / "repos.json"
    output_path = Path(__file__).parent.parent / "data" / "activity.json"
    output_path.parent.mkdir(exist_ok=True)

    with open(repos_path) as f:
        repos = json.load(f)

    results = []
    for r in repos:
        data = process_repo(r["owner"], r["repo"], r.get("description", ""))
        if data:
            results.append(data)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repos": results,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(results)} repos to {output_path}")


if __name__ == "__main__":
    main()
