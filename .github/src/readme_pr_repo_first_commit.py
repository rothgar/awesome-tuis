#!/usr/bin/env python3
import base64
import calendar
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from urllib import error, parse, request


ALPHA_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "readme_pr_alphabetical.py"
)
_alpha_spec = importlib.util.spec_from_file_location("readme_pr_alphabetical", ALPHA_MODULE_PATH)
_alpha_module = importlib.util.module_from_spec(_alpha_spec)
_alpha_spec.loader.exec_module(_alpha_module)
check_alphabetical = _alpha_module.check_alphabetical


MARKER = "<!-- readme-pr-repo-first-commit -->"
CLOSE_COMMENT_MARKER = "<!-- readme-pr-repo-first-commit:close -->"
URL_PATTERN = re.compile(r"https?://[^\s)\]>\"`]+")

STATUS_PASS = "pass"
STATUS_TOO_YOUNG = "too_young"
STATUS_UNKNOWN = "unknown"

ALPHA_MARKER = "<!-- readme-pr-repo-first-commit:alphabetical -->"


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


def date_months_before(ref: date, months: int) -> date:
    """Return ref shifted back by `months` calendar months (day clamped)."""
    year, month = ref.year, ref.month
    month -= months
    while month <= 0:
        month += 12
        year -= 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(ref.day, last_day)
    return date(year, month, day)


def parse_iso_date_utc(iso_str: str) -> date | None:
    if not iso_str or not iso_str.strip():
        return None
    text = iso_str.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date()


def go_no_go_for_first_commit(iso_date_str: str) -> tuple[str, str, str]:
    """
    Returns (display_date, emoji, status).
    status is one of:
      - STATUS_PASS:     first commit is at least 6 calendar months before today (UTC)
      - STATUS_TOO_YOUNG: date is parseable but the project is younger than 6 months
      - STATUS_UNKNOWN:   date is missing or could not be parsed (treated as
                          comment-only; does NOT trigger auto-close)
    """
    parsed = parse_iso_date_utc(iso_date_str)
    if not parsed:
        return ("—", "⛔", STATUS_UNKNOWN)
    today_utc = datetime.now(timezone.utc).date()
    cutoff = date_months_before(today_utc, 6)
    if parsed <= cutoff:
        return (parsed.isoformat(), "✅", STATUS_PASS)
    return (parsed.isoformat(), "⛔", STATUS_TOO_YOUNG)


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


def markdown_table_row(c1: str, c2: str, c3: str) -> str:
    def esc(s: str) -> str:
        return s.replace("|", "\\|")

    return f"| {esc(c1)} | {esc(c2)} | {esc(c3)} |"


def build_comment_table(readme_urls: list[str], client: GithubClient) -> tuple[str, list[tuple[str, str, str]]]:
    """
    Build the marker comment body and return a parallel list of
    (repo_url, status, iso_date) for every repo that was evaluated as a
    real GitHub repo. Non-GitHub URLs are excluded from the status list
    (they are treated as UNKNOWN and do not affect close decisions).
    """
    first_commit_cache: dict[str, str] = {}
    rows: list[tuple[date | None, str, str, str]] = []
    statuses: list[tuple[str, str, str]] = []

    for raw_url in readme_urls:
        repo_url = normalize_github_repo_url(raw_url)
        if not repo_url:
            rows.append((None, "—", "⛔", raw_url))
            continue

        if repo_url not in first_commit_cache:
            first_commit_cache[repo_url] = get_first_commit_date(client, repo_url)

        iso = first_commit_cache[repo_url]
        display_date, emoji, status = go_no_go_for_first_commit(iso)
        sort_key = parse_iso_date_utc(iso)
        rows.append((sort_key, display_date, emoji, repo_url))
        statuses.append((repo_url, status, iso))

    rows.sort(key=lambda r: (r[0] is None, r[0] or date.min))

    header = (
        "| Start (sorted) | 🚦 | Repository URL |\n"
        "| :------------: | :-: | ----------------------------------------------------------- |"
    )
    body_lines = [MARKER, "", header]
    body_lines.extend(markdown_table_row(c1, c2, c3) for _, c1, c2, c3 in rows)
    return "\n".join(body_lines), statuses


def build_close_comment(too_young: list[tuple[str, str]]) -> str:
    """Compose the comment posted when a PR is auto-closed."""
    lines = [
        CLOSE_COMMENT_MARKER,
        "",
        "Closing this PR because every repository added by this change is "
        "younger than the 6-month minimum required by this list.",
        "",
        "| Repository | First commit |",
        "| ---------- | ------------ |",
    ]
    for repo_url, iso_date in too_young:
        date_display = iso_date[:10] if iso_date else "—"
        lines.append(f"| {repo_url} | {date_display} |")
    lines += [
        "",
        "What to do next: please reopen this PR once each of the above "
        "repositories has been active for at least 6 months (by first "
        "commit). The status table above will refresh automatically when "
        "you reopen or push changes.",
    ]
    return "\n".join(lines)


def should_close_pr(statuses: list[tuple[str, str, str]]) -> tuple[bool, list[tuple[str, str]]]:
    """
    Per repo policy:
      - Close only if ALL added GitHub repos fail the 6-month requirement.
      - UNKNOWN (missing/unparseable date) does NOT count as failing and
        therefore blocks auto-close to avoid false positives.
    Returns (should_close, list_of_(repo_url, iso_date)_for_too_young_repos).
    """
    if not statuses:
        return (False, [])
    if any(status != STATUS_TOO_YOUNG for _, status, _ in statuses):
        return (False, [])
    return (True, [(repo_url, iso) for repo_url, _, iso in statuses])


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


def upsert_close_comment(client: GithubClient, pr_number: str, body: str) -> None:
    """Upsert a close-explanation comment under its own marker."""
    comments, _ = client.api(f"repos/{client.repository}/issues/{pr_number}/comments?per_page=100")
    existing_id = ""
    if isinstance(comments, list):
        for comment in comments:
            comment_body = (comment or {}).get("body", "")
            user_login = ((comment or {}).get("user") or {}).get("login", "")
            if CLOSE_COMMENT_MARKER in comment_body and user_login == "github-actions[bot]":
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


def get_pr_state(client: GithubClient, pr_number: str) -> str:
    pr, _ = client.api(f"repos/{client.repository}/pulls/{pr_number}")
    return (pr or {}).get("state", "")


def close_pull_request(client: GithubClient, pr_number: str) -> None:
    client.api(
        f"repos/{client.repository}/pulls/{pr_number}",
        method="PATCH",
        payload={"state": "closed"},
    )


def fetch_base_readme(client: GithubClient, pr_number: str) -> str | None:
    """Return the README.md content from the PR's base ref, or None on failure.

    Used by the alphabetical-order check to reconstruct the post-PR
    README without requiring fork-head access.
    """
    pr, _ = client.api(f"repos/{client.repository}/pulls/{pr_number}")
    if not isinstance(pr, dict):
        return None
    base = pr.get("base", {}) or {}
    ref = base.get("sha") or ""
    if not ref:
        return None
    path = f"repos/{client.repository}/contents/README.md?ref={ref}"
    try:
        data, _ = client.api(path)
    except SystemExit:
        return None
    if not isinstance(data, dict):
        return None
    content_b64 = data.get("content", "")
    if not content_b64:
        return None
    try:
        return base64.b64decode(content_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


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
    readme_patch = ""
    if isinstance(changed_files, list):
        readme_urls = find_readme_urls(changed_files)
        for f in changed_files:
            if (f or {}).get("filename") == "README.md":
                readme_patch = (f or {}).get("patch") or ""
                break

    statuses: list[tuple[str, str, str]] = []
    if not readme_urls:
        comment_body = (
            f"{MARKER}\n"
            "README.md was changed, but no new or modified URL was found in added lines."
        )
    else:
        comment_body, statuses = build_comment_table(readme_urls, client)

    alphabetical_section = ""
    if readme_patch:
        base_content = fetch_base_readme(client, pr_number)
        if base_content is not None:
            alphabetical_section = check_alphabetical(base_content, readme_patch)
            if alphabetical_section:
                alphabetical_section = (
                    f"\n{ALPHA_MARKER}\n{alphabetical_section}"
                )

    upsert_comment(client, pr_number=pr_number, body=comment_body + alphabetical_section)

    should_close, too_young = should_close_pr(statuses)
    if not should_close:
        return

    state = get_pr_state(client, pr_number)
    if state == "closed":
        return

    upsert_close_comment(client, pr_number, build_close_comment(too_young))
    close_pull_request(client, pr_number)


if __name__ == "__main__":
    main()
