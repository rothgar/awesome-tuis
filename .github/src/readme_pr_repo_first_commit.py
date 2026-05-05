#!/usr/bin/env python3
import json
import os
import re
import sys
from urllib import error, parse, request


MARKER = "<!-- readme-pr-repo-first-commit -->"
URL_PATTERN = re.compile(r"https?://[^\s)\]>\"`]+")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def parse_link_header(link_header: str) -> dict:
    links = {}
    if not link_header:
        return links
    for part in link_header.split(","):
        sections = [s.strip() for s in part.split(";")]
        if not sections:
            continue
        url_part = sections[0]
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        url = url_part[1:-1]
        rel = None
        for section in sections[1:]:
            if section.startswith("rel="):
                rel = section.split("=", 1)[1].strip("\"")
                break
        if rel:
            links[rel] = url
    return links


class GithubClient:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = repository

    def api(self, path: str, method: str = "GET", payload=None):
        url = f"https://api.github.com/{path.lstrip('/')}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        req = request.Request(url=url, method=method, data=body)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with request.urlopen(req) as response:
                content = response.read().decode("utf-8")
                headers = dict(response.headers.items())
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            fail(f"GitHub API error {exc.code} for {path}: {detail}")
        except error.URLError as exc:
            fail(f"Network error for {path}: {exc}")

        parsed = None
        if content:
            parsed = json.loads(content)
        return parsed, headers


def find_readme_urls(changed_files) -> list[str]:
    found_urls = []
    seen = set()
    for changed in changed_files:
        if changed.get("filename") != "README.md":
            continue
        patch = changed.get("patch") or ""
        for line in patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            matches = URL_PATTERN.findall(line)
            for match in matches:
                if match in seen:
                    continue
                seen.add(match)
                found_urls.append(match)
    return found_urls


def normalize_github_repo_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed_url = parse.urlparse(raw_url)
    host = parsed_url.netloc.lower()
    if host not in {"github.com", "www.github.com"}:
        return ""
    segments = [seg for seg in parsed_url.path.split("/") if seg]
    if len(segments) < 2:
        return ""
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return ""
    return f"https://github.com/{owner}/{repo}"


def get_first_commit_date(client: GithubClient, repo_url: str) -> str:
    owner_repo = repo_url.removeprefix("https://github.com/")
    _, headers = client.api(f"repos/{owner_repo}/commits?per_page=1")
    links = parse_link_header(headers.get("Link", ""))
    last_page = 1
    if "last" in links:
        last_page_str = parse.parse_qs(parse.urlparse(links["last"]).query).get("page", ["1"])[0]
        if last_page_str.isdigit():
            last_page = int(last_page_str)

    commits, _ = client.api(f"repos/{owner_repo}/commits?per_page=1&page={last_page}")
    if not isinstance(commits, list) or not commits:
        return ""

    commit = commits[0].get("commit", {})
    author = (commit.get("author") or {}).get("date", "")
    committer = (commit.get("committer") or {}).get("date", "")
    return author or committer or ""


def upsert_comment(client: GithubClient, pr_number: str, body: str) -> None:
    comments, _ = client.api(f"repos/{client.repository}/issues/{pr_number}/comments?per_page=100")
    existing_id = ""
    if isinstance(comments, list):
        for comment in comments:
            comment_body = (comment or {}).get("body", "")
            user_login = ((comment or {}).get("user") or {}).get("login", "")
            if MARKER in comment_body and user_login == "github-actions[bot]":
                existing_id = str(comment.get("id", ""))

    if existing_id:
        client.api(
            f"repos/{client.repository}/issues/comments/{existing_id}",
            method="PATCH",
            payload={"body": body},
        )
        return

    client.api(
        f"repos/{client.repository}/issues/{pr_number}/comments",
        method="POST",
        payload={"body": body},
    )


def main() -> None:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    pr_number = os.getenv("PR_NUMBER")
    repository = os.getenv("GITHUB_REPOSITORY")

    if not token:
        fail("GITHUB_TOKEN or GH_TOKEN is required.")
    if not pr_number or not repository:
        fail("PR_NUMBER and GITHUB_REPOSITORY are required.")

    client = GithubClient(token=token, repository=repository)
    changed_files, _ = client.api(f"repos/{repository}/pulls/{pr_number}/files?per_page=100")

    readme_urls = []
    if isinstance(changed_files, list):
        readme_urls = find_readme_urls(changed_files)

    if not readme_urls:
        comment_body = (
            f"{MARKER}\n"
            "README.md was changed, but no new or modified URL was found in added lines."
        )
    else:
        lines = [MARKER, "Detected URLs from added/modified README.md lines:"]
        first_commit_cache = {}

        for raw_url in readme_urls:
            repo_url = normalize_github_repo_url(raw_url)
            if not repo_url:
                lines.append(f"- {raw_url} -> not a GitHub repository URL")
                continue

            if repo_url not in first_commit_cache:
                first_commit_cache[repo_url] = get_first_commit_date(client, repo_url)

            first_commit_date = first_commit_cache[repo_url]
            date_value = first_commit_date or "Unable to extract first commit date."
            lines.append(f"- {repo_url} -> first commit date: {date_value}")

        comment_body = "\n".join(lines)

    upsert_comment(client, pr_number=pr_number, body=comment_body)


if __name__ == "__main__":
    main()
