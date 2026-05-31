"""
Проверка версий софта монеты через GitHub API.

fetch_latest(url, mode, token) -> dict | None: {version, url, kind, published}.
mode: "auto" (релиз -> тег -> коммит) либо явно "release"/"tag"/"commit".

Без токена GitHub режет анонимные запросы (≈60/час на IP) — для пары реп раз
в час хватает, но с GITHUB_TOKEN лимит сильно выше. Токен опционален.
"""
from __future__ import annotations

import re
import logging

import httpx

log = logging.getLogger("versions")

API = "https://api.github.com"
_RE = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$")


def parse_repo(url: str):
    """'https://github.com/Owner/Repo(.git)?' / 'git@github.com:Owner/Repo' -> (owner, repo)."""
    m = _RE.search((url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "dwarfus-minerbot"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def parse_release(d: dict) -> dict | None:
    if not isinstance(d, dict) or not d.get("tag_name"):
        return None
    return {"version": d["tag_name"], "url": d.get("html_url"),
            "kind": "release", "published": d.get("published_at")}


def parse_tags(d) -> dict | None:
    if isinstance(d, list) and d and d[0].get("name"):
        return {"version": d[0]["name"], "url": None, "kind": "tag", "published": None}
    return None


def parse_commits(d) -> dict | None:
    if isinstance(d, list) and d:
        c = d[0]
        sha = (c.get("sha") or "")[:7]
        if not sha:
            return None
        date = ((c.get("commit") or {}).get("committer") or {}).get("date")
        return {"version": sha, "url": c.get("html_url"), "kind": "commit", "published": date}
    return None


async def fetch_latest(url: str, mode: str = "auto", token: str | None = None) -> dict | None:
    pr = parse_repo(url)
    if not pr:
        log.warning("не распознал GitHub url: %s", url)
        return None
    owner, repo = pr
    base = f"/repos/{owner}/{repo}"
    async with httpx.AsyncClient(timeout=15.0, headers=_headers(token)) as c:
        try:
            if mode in ("auto", "release"):
                r = await c.get(API + base + "/releases/latest")
                if r.status_code == 200:
                    res = parse_release(r.json())
                    if res:
                        return res
                elif r.status_code == 403:
                    log.warning("GitHub rate limit/forbidden для %s/%s", owner, repo)
                    return None
                if mode == "release":
                    return None
            if mode in ("auto", "tag"):
                r = await c.get(API + base + "/tags?per_page=1")
                if r.status_code == 200:
                    res = parse_tags(r.json())
                    if res:
                        return res
                if mode == "tag":
                    return None
            if mode in ("auto", "commit"):
                r = await c.get(API + base + "/commits?per_page=1")
                if r.status_code == 200:
                    return parse_commits(r.json())
        except Exception as e:
            log.warning("version fetch error %s: %s", url, e)
    return None
