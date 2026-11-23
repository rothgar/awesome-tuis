#!/usr/bin/env python3
"""
Audit every GitHub repository listed in README.md and keep one issue up to
date with what needs attention: archived, missing, renamed/moved, younger than
6 months on GitHub, or no push in over 3 years.

Uses the same age rule as the PR check (readme_pr_repo_first_commit.py): the
later of the first commit and the repository's creation date.
"""
import importlib.util
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib import error, request

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "readme_pr_repo_first_commit", os.path.join(HERE, "readme_pr_repo_first_commit.py")
)
pr_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_check)

ISSUE_TITLE = "Repository audit"
ISSUE_MARKER = "<!-- repo-audit -->"
STALE_DAYS = 3 * 365
ENTRY = re.compile(r"- \[([^\]]*)\]\((https?://[^)\s]+)\)")
HEADING = re.compile(r"<h2>(.*?)</h2>")


def readme_entries(path: str) -> list[tuple[str, str, str]]:
    section, entries, seen = "", [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            heading = HEADING.search(line)
            if heading:
                section = heading.group(1)
            match = ENTRY.match(line)
            if not match:
                continue
            url = pr_check.normalize_github_repo_url(match.group(2))
            if url and url.lower() not in seen:
                seen.add(url.lower())
                entries.append((section, match.group(1), url))
    return entries


def graphql(token: str, query: str) -> dict:
    req = request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req) as resp:
            return json.loads(resp.read()).get("data") or {}
    except error.HTTPError as exc:
        pr_check.fail(f"GraphQL error {exc.code}: {exc.read().decode(errors='ignore')}")


def repo_metadata(token: str, urls: list[str]) -> dict[str, dict | None]:
    meta: dict[str, dict | None] = {}
    for start in range(0, len(urls), 50):
        chunk = urls[start : start + 50]
        fields = []
        for i, url in enumerate(chunk):
            owner, name = url.split("/")[-2:]
            fields.append(
                f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                "{ url createdAt pushedAt isArchived isDisabled }"
            )
        data = graphql(token, "{" + " ".join(fields) + "}")
        for i, url in enumerate(chunk):
            meta[url] = data.get(f"r{i}")
    return meta


def audit(client, token: str, entries) -> dict[str, list[str]]:
    urls = [url for _, _, url in entries]
    meta = repo_metadata(token, urls)

    def first_commit(url: str) -> str:
        try:
            return pr_check.get_first_commit_date(client, url)
        except SystemExit:
            return ""

    alive = [u for u in urls if meta.get(u)]
    with ThreadPoolExecutor(8) as pool:
        first = dict(zip(alive, pool.map(first_commit, alive)))

    today = datetime.now(timezone.utc).date()
    found = {"missing": [], "archived": [], "young": [], "renamed": [], "stale": []}
    for section, name, url in entries:
        item = f"[{name}]({url}) ({section})"
        info = meta.get(url)
        if not info or info.get("isDisabled"):
            found["missing"].append(item)
            continue
        if info["isArchived"]:
            found["archived"].append(item)
            continue
        start = pr_check.effective_start(first.get(url, ""), info["createdAt"])
        _, _, status = pr_check.go_no_go_for_first_commit(start)
        if status == pr_check.STATUS_TOO_YOUNG:
            found["young"].append(
                f"{item}: repo created {info['createdAt'][:10]}, "
                f"first commit {(first.get(url) or '—')[:10]}"
            )
        if info["url"].lower() != url.lower():
            found["renamed"].append(f"{item} → {info['url']}")
        pushed = pr_check.parse_iso_date_utc(info["pushedAt"])
        if pushed and (today - pushed).days > STALE_DAYS:
            found["stale"].append(f"{item}: last push {info['pushedAt'][:10]}")
    return found


def issue_body(found: dict[str, list[str]], total: int) -> str:
    titles = {
        "missing": "Missing or disabled (remove)",
        "archived": "Archived (remove)",
        "young": "Younger than 6 months on GitHub (remove)",
        "renamed": "Renamed or moved (update the URL)",
        "stale": "No push in over 3 years (review)",
    }
    lines = [
        ISSUE_MARKER,
        f"Monthly audit of the {total} GitHub repositories in README.md, "
        f"run {datetime.now(timezone.utc).date().isoformat()}.",
        "",
    ]
    for key, title in titles.items():
        items = found[key]
        lines.append(f"<details{' open' if key != 'stale' and items else ''}>"
                     f"<summary><b>{title}: {len(items)}</b></summary>\n")
        lines.extend(f"- {item}" for item in items)
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def upsert_issue(client, body: str, needs_attention: bool) -> None:
    repo = client.repository
    issues, _ = client.api(f"repos/{repo}/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100")
    existing = next(
        (i for i in issues or [] if ISSUE_MARKER in (i.get("body") or "") and "pull_request" not in i),
        None,
    )
    if existing:
        payload = {"body": body}
        if not needs_attention:
            payload["state"] = "closed"
        client.api(f"repos/{repo}/issues/{existing['number']}", method="PATCH", payload=payload)
        print(f"Updated issue #{existing['number']}")
    elif needs_attention:
        created, _ = client.api(f"repos/{repo}/issues", method="POST",
                                payload={"title": ISSUE_TITLE, "body": body})
        print(f"Opened issue #{created['number']}")


def main() -> None:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repository = os.getenv("GITHUB_REPOSITORY", "rothgar/awesome-tuis")
    if not token:
        pr_check.fail("GITHUB_TOKEN or GH_TOKEN is required.")
    client = pr_check.GithubClient(token=token, repository=repository)
    entries = readme_entries(os.getenv("README_PATH", "README.md"))
    found = audit(client, token, entries)
    body = issue_body(found, len(entries))
    if os.getenv("DRY_RUN"):
        print(body)
        return
    actionable = any(found[k] for k in ("missing", "archived", "young", "renamed"))
    upsert_issue(client, body, actionable)


if __name__ == "__main__":
    sys.exit(main())
