"""
GitHub Extractor — fetches candidate data from a public GitHub profile URL.

Uses the GitHub REST API v3 (no auth required for public data, but respects rate limits).
Extracts: name, bio, location, email (if public), blog/portfolio, repos, languages.

Design decisions:
  - Async-capable via httpx but called synchronously for simplicity
  - On 429 (rate limit) or any network error: log warning + return empty
  - GitHub username is parsed from the URL (e.g. https://github.com/torvalds → torvalds)
"""
from __future__ import annotations
import re
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT = 10.0  # seconds


def _parse_username(url: str) -> Optional[str]:
    """Extract GitHub username from a profile URL."""
    url = url.strip().rstrip("/")
    match = re.search(r"github\.com/([^/?#]+)$", url, re.IGNORECASE)
    if match:
        username = match.group(1)
        # Exclude common non-user paths
        if username.lower() not in ("login", "signup", "explore", "trending"):
            return username
    return None


def _get_top_languages(repos: list[dict], top_n: int = 8) -> list[str]:
    """Aggregate languages from repo list, sorted by frequency."""
    lang_count: dict[str, int] = {}
    for repo in repos:
        lang = repo.get("language")
        if lang:
            lang_count[lang] = lang_count.get(lang, 0) + 1
    return sorted(lang_count, key=lambda k: -lang_count[k])[:top_n]


def extract_github(url: str) -> list[dict]:
    """
    Fetch a GitHub profile and return a list with one candidate dict.
    Returns empty list on any failure (rate limit, 404, network error).
    """
    username = _parse_username(url)
    if not username:
        logger.warning("Could not parse GitHub username from URL: %s", url)
        return []

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=_DEFAULT_TIMEOUT, headers=headers) as client:
            # Fetch user profile
            user_resp = client.get(f"{GITHUB_API_BASE}/users/{username}")

            if user_resp.status_code == 404:
                logger.warning("GitHub user not found: %s", username)
                return []
            if user_resp.status_code == 429:
                logger.warning("GitHub API rate limit hit for user: %s", username)
                return []
            if user_resp.status_code != 200:
                logger.warning(
                    "GitHub API returned %d for user %s", user_resp.status_code, username
                )
                return []

            user_data = user_resp.json()

            # Fetch public repos (first page, sorted by stars)
            repos_resp = client.get(
                f"{GITHUB_API_BASE}/users/{username}/repos",
                params={"sort": "pushed", "per_page": 30, "type": "owner"},
            )
            repos = repos_resp.json() if repos_resp.status_code == 200 else []
            if not isinstance(repos, list):
                repos = []

    except httpx.TimeoutException:
        logger.warning("GitHub API request timed out for: %s", url)
        return []
    except httpx.RequestError as e:
        logger.warning("GitHub API network error for %s: %s", url, e)
        return []

    # Extract languages from repos as skills
    languages = _get_top_languages(repos)

    # Build portfolio / blog link
    blog = user_data.get("blog", "")
    if blog and not blog.startswith("http"):
        blog = f"https://{blog}"

    # Build experience: interpret each public repo as a mini-project
    # (Not real experience — but useful signal)
    projects = []
    for repo in repos[:5]:
        if repo.get("description"):
            projects.append({
                "company": f"GitHub/{username}",
                "title": repo.get("name", ""),
                "summary": repo.get("description", ""),
                "start": (repo.get("created_at") or "")[:7],  # YYYY-MM
                "end": (repo.get("pushed_at") or "")[:7],
            })

    candidate = {
        "_source": "github_url",
        "_source_url": url,
        "full_name": user_data.get("name") or username,
        "email": user_data.get("email"),
        "location": user_data.get("location"),
        "headline": user_data.get("bio"),
        "github": f"https://github.com/{username}",
        "portfolio": blog if blog else None,
        "skills": languages,
        "experience": projects,
    }

    # Remove None values
    candidate = {k: v for k, v in candidate.items() if v is not None and v != ""}

    return [candidate]
