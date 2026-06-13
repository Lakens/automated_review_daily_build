#!/usr/bin/env python3
"""
Backfill feed.xml with one item per repo per day for the last N days.
Reads commit data directly from the GitHub API (authenticated).
Run once locally: python scripts/backfill_rss.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
SITE_URL     = "https://lakens.github.io/automated_review_daily_build"
LOOKBACK_DAYS = 10

GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    GITHUB_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


# ── Helpers ───────────────────────────────────────────────────────────────────
def gh_get(url, params=None):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=GITHUB_HEADERS, params=params, timeout=30)
            if r.status_code in (403, 429):
                wait = int(r.headers.get("Retry-After", 10))
                print(f"  Rate limited, sleeping {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  ERROR: {e}", file=sys.stderr)
                return []
            time.sleep(2 ** attempt)
    return []


def xml_escape(s):
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def rfc822(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def groq_summarize(repo_name, commit_lines):
    if not GROQ_API_KEY or not commit_lines:
        return ""
    text = "\n".join(commit_lines[:20])
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content":
                    f"In 2-3 sentences, summarise what was worked on in '{repo_name}' "
                    f"based on these commits. Be specific.\n\nCommits:\n{text}"
                }],
                "max_tokens": 200,
                "temperature": 0.3,
            },
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  Groq error: {e}", file=sys.stderr)
        return ""


def classify_commit_type(message):
    import re
    msg = message.lower()
    if re.search(r"\bfix\b|bug|crash|error|wrong|broken|revert", msg): return "fix"
    if re.search(r"\badd\b|\bnew\b|feat|implement|support|introduce", msg): return "feat"
    if re.search(r"\bdoc\b|readme|vignette|news|changelog", msg): return "docs"
    if re.search(r"\btest\b|testthat|spec|coverage", msg): return "test"
    if re.search(r"\brefactor\b|clean|rename|simplif", msg): return "refactor"
    if re.search(r"\bchore\b|bump|version|release|ci|workflow", msg): return "chore"
    return "other"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    repos_path = Path(__file__).parent.parent / "repos.json"
    feed_path  = Path(__file__).parent.parent / "feed.xml"

    with open(repos_path) as f:
        repos = json.load(f)

    now   = datetime.now(timezone.utc)
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()

    # Collect all items: (date, item_dict)
    all_items = []

    for r in repos:
        owner, repo = r["owner"], r["repo"]
        print(f"Fetching {owner}/{repo}...")

        # Get all commits for the lookback window
        commits_raw = []
        page = 1
        while True:
            page_data = gh_get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                params={"since": since, "per_page": 100, "page": page},
            )
            if not page_data or not isinstance(page_data, list):
                break
            commits_raw.extend(page_data)
            if len(page_data) < 100:
                break
            page += 1
        time.sleep(0.5)

        if not commits_raw:
            print(f"  No commits in last {LOOKBACK_DAYS} days, skipping.")
            continue

        # Group commits by day
        by_day = {}
        for c in commits_raw:
            date_str = (c.get("commit", {}).get("author", {}).get("date", "") or "")[:10]
            if not date_str:
                continue
            by_day.setdefault(date_str, []).append(c)

        repo_url = f"https://github.com/{owner}/{repo}"

        # Build one RSS item per day that had commits
        for date_str, day_commits in sorted(by_day.items(), reverse=True):
            commit_lines = []
            commits_html_list = []
            for c in day_commits:
                msg   = (c.get("commit", {}).get("message", "") or "").split("\n")[0]
                sha   = (c.get("sha", "") or "")[:7]
                author = (c.get("commit", {}).get("author", {}).get("name", "") or "")
                url   = c.get("html_url", repo_url)
                ctype = classify_commit_type(msg)
                commit_lines.append(f"[{date_str}] {author}: {msg}")
                commits_html_list.append(
                    f"<li>[{ctype}] <a href='{xml_escape(url)}'>"
                    f"<code>{xml_escape(sha)}</code></a> "
                    f"{xml_escape(msg)} <em>— {xml_escape(author)}</em></li>"
                )

            summary = groq_summarize(f"{owner}/{repo}", commit_lines)
            time.sleep(0.3)

            n = len(day_commits)
            commits_html = "<ul>" + "".join(commits_html_list) + "</ul>"
            summary_html = f"<p><em>{xml_escape(summary)}</em></p>" if summary else ""

            desc = (
                f"<h3><a href='{xml_escape(repo_url)}'>{xml_escape(owner)}/{xml_escape(repo)}</a>"
                f" — {date_str}</h3>"
                + summary_html
                + f"<p><strong>{n} commit{'s' if n != 1 else ''}</strong> on {date_str}:</p>"
                + commits_html
            )

            # pub date = end of that day UTC
            pub_iso = f"{date_str}T23:59:59+00:00"

            all_items.append((date_str, {
                "title": f"{repo} — {n} commit{'s' if n != 1 else ''} on {date_str}",
                "link": repo_url,
                "guid": f"{SITE_URL}/repo/{owner}/{repo}/{date_str}",
                "pubDate": rfc822(pub_iso),
                "description": desc,
            }))

        print(f"  {len(by_day)} day(s) with commits")

    # Sort all items newest first
    all_items.sort(key=lambda x: x[0], reverse=True)

    # Build daily digest items (one per day across all repos)
    # Group by date
    days_seen = {}
    for date_str, item in all_items:
        days_seen.setdefault(date_str, []).append(item)

    digest_items = []
    for date_str in sorted(days_seen.keys(), reverse=True):
        day_items = days_seen[date_str]
        repos_active = len(day_items)
        total_commits = sum(
            int(i["title"].split(" — ")[1].split(" commit")[0])
            for i in day_items
            if " — " in i["title"]
        )
        repo_list = "".join(
            f"<li>{xml_escape(i['title'].split(' — ')[0])}: "
            f"{xml_escape(i['title'].split(' — ')[1])}</li>"
            for i in day_items
        )
        desc = (
            f"<h3>Daily digest — {date_str}</h3>"
            f"<p>{repos_active} repo{'s' if repos_active != 1 else ''} active, "
            f"{total_commits} total commit{'s' if total_commits != 1 else ''}.</p>"
            f"<ul>{repo_list}</ul>"
            f"<p><a href='{SITE_URL}'>View full dashboard</a></p>"
        )
        digest_items.append((date_str, {
            "title": f"Daily digest — {date_str}: {total_commits} commits across {repos_active} repos",
            "link": SITE_URL,
            "guid": f"{SITE_URL}/digest/{date_str}",
            "pubDate": rfc822(f"{date_str}T23:59:59+00:00"),
            "description": desc,
        }))

    # Merge: digest first for each day, then per-repo items
    final_items = []
    for date_str in sorted(days_seen.keys(), reverse=True):
        digest = next((d for d in digest_items if d[0] == date_str), None)
        if digest:
            final_items.append(digest[1])
        for _, item in all_items:
            if item["guid"].endswith(f"/{date_str}"):
                final_items.append(item)

    # Write feed
    items_xml = "\n".join(f"""    <item>
      <title>{xml_escape(i['title'])}</title>
      <link>{i['link']}</link>
      <guid isPermaLink="false">{i['guid']}</guid>
      <pubDate>{i['pubDate']}</pubDate>
      <description><![CDATA[{i['description']}]]></description>
    </item>""" for i in final_items)

    build_date = rfc822(now.isoformat())
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>What Is Being Built — Daily Activity Feed</title>
    <link>{SITE_URL}</link>
    <description>Daily updates on open-source automated research tools: commits, releases, and community activity.</description>
    <language>en-us</language>
    <lastBuildDate>{build_date}</lastBuildDate>
    <atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>
"""
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed)

    print(f"\nWrote {len(final_items)} RSS items to {feed_path}")
    print(f"Days covered: {', '.join(sorted(days_seen.keys(), reverse=True))}")


if __name__ == "__main__":
    main()
