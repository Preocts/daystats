from __future__ import annotations

import argparse
import dataclasses
import datetime
import http.client
import json
import logging
import os
import time
from typing import Any

# This couldn't possibily be a bad way to get the UTC offset :3c
OFFSET = time.altzone if time.daylight else time.timezone
UTC_OFFSET = datetime.timedelta(hours=(OFFSET // 60 // 60))

BASE_URL = "https://api.github.com/graphql"
TOKEN_KEY = "DAYSTATS_TOKEN"
HTTPS_TIMEOUT = 10  # seconds

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class CLIArgs:
    loginname: str
    year: int | None = None
    month: int | None = None
    day: int | None = None
    url: str = BASE_URL
    token: str | None = None
    markdown: bool = False


@dataclasses.dataclass(frozen=True)
class Repo:
    owner: str
    name: str


@dataclasses.dataclass(frozen=True)
class Contributions:
    commits: int
    issues: int
    pullrequests: int
    reviews: int
    pr_repos: set[Repo]


@dataclasses.dataclass(frozen=True)
class PullRequest:
    reponame: str
    additions: int
    deletions: int
    files: int
    created_at: str
    number: int
    url: str


class _HTTPClient:
    def __init__(self, token: str | None, url: str = BASE_URL) -> None:
        """Define an HTTPClient with token and target GitHub GraphQL API url."""
        self._token = token or ""
        url = url.lower().replace("https://", "").replace("http://", "")
        url_split = url.split("/", 1)
        self._host = url_split[0]
        self._path = url_split[1] if len(url_split) > 1 else ""

    @property
    def _headers(self) -> dict[str, str]:
        return {"User-Agent": "egg-daystats", "Authorization": f"bearer {self._token}"}

    def post(self, data: dict[str, Any]) -> dict[str, Any]:
        """Post JSON serializable data to GitHub GraphQL API, return reponse."""
        connection = http.client.HTTPSConnection(self._host, timeout=HTTPS_TIMEOUT)
        connection.request("POST", f"/{self._path}", json.dumps(data), self._headers)
        resp = connection.getresponse()

        try:
            resp_json = json.loads(resp.read().decode())
        except json.JSONDecodeError as err:
            logger.error("HTTPS error code: %d", resp.status)
            return {"error": str(err)}

        return resp_json


def _create_contrib_query(loginname: str, from_: str, to_: str) -> dict[str, Any]:
    """Return the query."""
    query = """
query($loginname: String!, $from_time:DateTime, $to_time:DateTime) {
    user(login:$loginname) {
        contributionsCollection(from:$from_time, to:$to_time) {
            totalCommitContributions
            totalIssueContributions
            totalPullRequestContributions
            totalPullRequestReviewContributions
            pullRequestContributionsByRepository {
                repository {
                    owner {
                        login
                    }
                    name
                }
            }
        }
    }
}"""
    variables = {
        "loginname": loginname,
        "from_time": from_,
        "to_time": to_,
    }
    return {"query": query, "variables": variables}


def _fetch_contributions(
    client: _HTTPClient,
    loginname: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> Contributions:
    """
    Fetch contribution information from GitHub GraphQL API.

    start_dt and end_dt are the local time `.now()` without a UTC offset
    """
    # Odd that we are giving GitHub our local time but labeling it as zulu
    # yet GitHub will return the correct contribution activity with the
    # incorrectly set timezone.
    logger.debug("Start time: %s", start_dt)
    logger.debug("End time: %s", end_dt)
    from_ = start_dt.isoformat() + "Z"
    to_ = end_dt.isoformat() + "Z"
    query = _create_contrib_query(loginname, from_, to_)

    resp_json = client.post(query)
    if "data" not in resp_json:
        logger.error("Fetch contributions failed: %s", json.dumps(resp_json))
        return Contributions(0, 0, 0, 0, set())

    pr_repos = set()
    contribs = resp_json["data"]["user"]["contributionsCollection"]

    for pr in contribs["pullRequestContributionsByRepository"]:
        repo = Repo(
            owner=pr["repository"]["owner"]["login"],
            name=pr["repository"]["name"],
        )
        pr_repos.add(repo)

    logger.debug("contribution result: %s ", json.dumps(resp_json, indent=4))

    return Contributions(
        commits=contribs["totalCommitContributions"],
        issues=contribs["totalIssueContributions"],
        pullrequests=contribs["totalPullRequestContributions"],
        reviews=contribs["totalPullRequestReviewContributions"],
        pr_repos=pr_repos,
    )


def _create_pull_request_query(
    repoowner: str,
    reponame: str,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return the query."""
    query = """
query($repoowner: String!, $reponame: String!, $cursor: String) {
    repository(name:$reponame, owner:$repoowner) {
        pullRequests(orderBy: {field:CREATED_AT, direction:DESC}, first:25, after:$cursor) {
            totalCount
            pageInfo {
                endCursor
                hasNextPage
                hasPreviousPage
                startCursor
            }
            nodes {
                author {
                    login
                }
                createdAt
                updatedAt
                additions
                deletions
                changedFiles
                url
                number
            }
        }
    }
}"""
    variables = {
        "cursor": cursor,
        "repoowner": repoowner,
        "reponame": reponame,
    }
    return {"query": query, "variables": variables}


def _fetch_pull_requests(
    client: _HTTPClient,
    author: str,
    repoowner: str,
    reponame: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> list[PullRequest]:
    """
    Fetch list of pull request details from GitHub GraphQL API.

    Unlike fetch contributions, the start_dt and end_dt must be offset to UTC
    in order to correctly filter the activity.

    Args:
        client: HTTPClient object
        author: Results filtered to only include Author of pull request
        repoowner: Owner of repo (or org)
        reponame: Name of repo
        start_dt: Earliest created at time for pull request in UTC
        end_dt: Latest created at time for pull request in UTC
    """
    logger.debug("Start time: %s", start_dt)
    logger.debug("End time: %s", end_dt)
    cursor = None
    more = True
    prs = []

    while more:
        query = _create_pull_request_query(repoowner, reponame, cursor)
        resp_json = client.post(query)
        if "data" not in resp_json:
            logger.error("Fetch pull request failed: %s", json.dumps(resp_json))
            return []

        rjson = resp_json["data"]["repository"]["pullRequests"]

        cursor = rjson["pageInfo"]["endCursor"]
        more = rjson["pageInfo"]["hasNextPage"]

        logger.debug("Pull request result: %s", json.dumps(resp_json, indent=4))

        for node in rjson["nodes"]:
            created_at = datetime.datetime.fromisoformat(node["createdAt"].rstrip("Z"))

            if node["author"]["login"].lower() != author.lower():
                logger.debug("Author does not match, skipping PR")
                continue

            if not end_dt > created_at > start_dt:
                logger.debug("Create date not in range - %s", created_at)
                more = created_at > start_dt
                continue

            prs.append(
                PullRequest(
                    reponame=reponame,
                    additions=node["additions"],
                    deletions=node["deletions"],
                    files=node["changedFiles"],
                    created_at=node["createdAt"],
                    number=node["number"],
                    url=node["url"],
                )
            )

    return prs


def _build_bookend_times(
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """
    Build start/end datetime ranges from 00:00 to 23:59.

    Raises:
        ValueError: Raised if year/month/day values are not valid
    """
    now = datetime.datetime.now()

    if year:
        now = now.replace(year=year)

    if month:
        now = now.replace(month=month)

    if day:
        now = now.replace(day=day)

    start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)

    return start_dt, end_dt


def get_stats(
    loginname: str,
    *,
    token: str | None = None,
    url: str | None = None,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> tuple[Contributions, list[PullRequest]]:
    """
    Pull contribution and related pull request details from GitHub.

    Keyword Args:
        token: GitHub personal access token. Default looks for TOKEN_KEY in environ
        url: GitHub graphQL api url. Default uses BASE_URL
        year: Uses today as the default date.
        month: Uses today as the default date.
        day: Uses today as the default date.
    """
    client = _HTTPClient(token, url if url else BASE_URL)
    start_dt, end_dt = _build_bookend_times(year, month, day)
    logger.debug("Start time: %s", start_dt)
    logger.debug("End time: %s", end_dt)
    logger.debug("UTC Offset: %s", UTC_OFFSET)

    contribs = _fetch_contributions(client, loginname, start_dt, end_dt)
    pull_requests = []
    for repo in contribs.pr_repos:
        pull_requests.extend(
            _fetch_pull_requests(
                client=client,
                author=loginname,
                repoowner=repo.owner,
                reponame=repo.name,
                start_dt=start_dt + UTC_OFFSET,
                end_dt=end_dt + UTC_OFFSET,
            )
        )

    return contribs, pull_requests


def _parse_args(cli_args: list[str] | None = None) -> CLIArgs:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="daystats",
        description="Pull daily stats from GitHub.",
    )
    parser.add_argument(
        "loginname",
        type=str,
        help="Login name to GitHub (author name).",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Changes the text output to Markdown table for copy/paste.",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Year to query. (default: today)",
    )
    parser.add_argument(
        "--month",
        type=int,
        help="Month of the year to query. (default: today)",
    )
    parser.add_argument(
        "--day",
        type=int,
        help="Day of the month to query. (default: today)",
    )
    parser.add_argument(
        "--url",
        type=str,
        help=f"Override default GitHub GraphQL API url. (default: {BASE_URL})",
        default=BASE_URL,
    )
    parser.add_argument(
        "--token",
        type=str,
        help=f"GitHub Personal Access Token with read-only access for public repos. Defaults to ${TOKEN_KEY} environ variable.",
        default=os.getenv(TOKEN_KEY),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Turn debug logging output on. Use with care, will expose token!",
    )

    args = parser.parse_args(cli_args)

    if args.debug:
        logging.basicConfig(level="DEBUG")

    logger.debug("CLI Input Override: %s", cli_args)
    logger.debug("CLI Input: %s:", args)

    return CLIArgs(
        loginname=args.loginname,
        year=args.year,
        month=args.month,
        day=args.day,
        url=args.url,
        token=args.token,
        markdown=args.markdown,
    )


def cli_runner(cli_args: list[str] | None = None) -> int:
    """Run the program."""
    args = _parse_args(cli_args)

    contribs, pull_requests = get_stats(
        loginname=args.loginname,
        token=args.token,
        url=args.url,
        year=args.year,
        month=args.month,
        day=args.day,
    )

    logger.debug("Contributions: %s", contribs)
    logger.debug("Pull Requests:\n%s", "\n".join([str(pr) for pr in pull_requests]))

    print(generate_output(contribs, pull_requests, markdown=args.markdown))

    return 0


def generate_output(
    contribs: Contributions,
    pull_requests: list[PullRequest],
    *,
    markdown: bool = False,
) -> str:
    """Check CLI flags for various outputs."""
    if markdown:
        return _stats_to_markdown(contribs, pull_requests)

    return _stats_to_text(contribs, pull_requests)


def _stats_to_markdown(
    contribs: Contributions, pull_requests: list[PullRequest]
) -> str:
    """Generate markdown report of stats."""
    total_adds = sum(pr.additions for pr in pull_requests)
    total_dels = sum(pr.deletions for pr in pull_requests)
    total_files = sum(pr.files for pr in pull_requests)

    summary_table = [
        "\n**Daily GitHub Summary**:\n",
        "| Contribution | Count | Metric | Total |",
        "| -- | -- | -- | -- |",
        f"| Reviews | {contribs.reviews} | Files Changed | {total_files} |",
        f"| Issues | {contribs.issues} | Additions | {total_adds} |",
        f"| Commits | {contribs.commits} | Deletions | {total_dels} |",
        f"| Pull Requests | {contribs.pullrequests} | | |",
        "\n**Pull Request Breakdown**:\n",
        "| Repo | Addition | Deletion | Files | Number |",
        "| -- | -- | -- | -- | -- |",
    ]
    for pr in pull_requests:
        summary_table.append(
            f"| {pr.reponame} | {pr.additions} | {pr.deletions} | {pr.files} | [see: #{pr.number}]({pr.url}) |"
        )

    return "\n".join(summary_table)


def _stats_to_text(contribs: Contributions, pull_requests: list[PullRequest]) -> str:
    """Generate plain-text of stats."""
    total_adds = sum(pr.additions for pr in pull_requests)
    total_dels = sum(pr.deletions for pr in pull_requests)
    total_files = sum(pr.files for pr in pull_requests)
    summary = [
        "\nDaily GitHub Summary:\n"
        f'|{"Contribution":^20}|{"Count":^7}|{"Metric":^15}|{"Total":^7}|',
        "-" * (20 + 7 + 15 + 7 + 5),
        f'|{" Reviews":20}|{contribs.reviews:^7}|{" Files Changed":15}|{total_files:^7}|',
        f'|{" Issue":20}|{contribs.issues:^7}|{" Additions":15}|{total_adds:^7}|',
        f'|{" Commits":20}|{contribs.commits:^7}|{" Deletions":15}|{total_dels:^7}|',
        f'|{" Pull Requests":20}|{contribs.pullrequests:^7}|{"":15}|{"":^7}|',
        "\nPull Request Breakdown:\n",
        f'|{"Addition":^10}|{"Deletion":^10}|{"Files":^7}|{"Number":^8}| Url',
        "-" * (10 + 10 + 7 + 8 + 5),
    ]
    for pr in pull_requests:
        summary.append(
            f"|{pr.additions:^10}|{pr.deletions:^10}|{pr.files:^7}|{pr.number:^8}| {pr.url}"
        )

    return "\n".join(summary)


if __name__ == "__main__":
    raise SystemExit(cli_runner())
