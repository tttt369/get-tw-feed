import argparse
import asyncio
import csv
import html
import json
import logging
import sys
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import markdown
from dateutil import parser as dateutil_parser
from twscrape import API
from twscrape.models import Tweet

try:
    from _config import Config
except:
    from config import Config

@dataclass
class RunOptions:
    url: str = Config.DEFAULT_BASE_URL
    api_key: str = Config.DEFAULT_API_KEY
    model: str = Config.DEFAULT_MODEL

    fetch: bool = True
    merge: bool = True
    include_old_posts: bool = False
    include_seen: bool = False

    feed: str | None = None
    output: str | None = None
    output_format: str = "html"
    prompt_only: bool = False
    result_file: str | None = None


class Post:
    @staticmethod
    def is_recent(moment: datetime, now: datetime) -> bool:
        """Return True when ``moment`` falls inside the last 24 hours."""

        return now - timedelta(hours=24) <= moment <= now

    @staticmethod
    def parse_date(raw_date: Any) -> datetime | None:
        """Parse a post's ISO date string into a JST datetime (None if absent)."""

        if not raw_date:
            return None

        return dateutil_parser.isoparse(str(raw_date)).astimezone(Config.JST)

    @staticmethod
    def format_hashtags(raw_hashtags: Any) -> str:
        """Render a post's hashtags as a comma-separated string."""

        if not raw_hashtags:
            return ""
        if isinstance(raw_hashtags, str):
            return raw_hashtags

        return ", ".join(str(tag) for tag in raw_hashtags)


class Seen:
    """Namespace for cross-run tweet deduplication."""

    @staticmethod
    def load(directory: Path | None = None) -> set[str]:
        """Read every seen-IDs CSV file into a set of tweet IDs."""

        directory = Config.SEEN_DIR if directory is None else directory

        ids: set[str] = set()
        if not directory.is_dir():
            return ids

        for path in sorted(directory.glob("*.csv")):
            try:
                with path.open("r", newline="", encoding="utf-8") as file:
                    ids.update(row[0] for row in csv.reader(file) if row)
            except OSError as exc:
                Config.LOGGER.warning("Could not read seen-IDs file %s: %s", path, exc)

        return ids

    @staticmethod
    def start_run(now: datetime, directory: Path | None = None) -> Path:
        """Create the CSV file that this run appends new IDs to."""

        directory = Config.SEEN_DIR if directory is None else directory
        directory.mkdir(parents=True, exist_ok=True)
        run_file = directory / f"{now.strftime(Config.TIMESTAMP_FORMAT)}.csv"
        run_file.touch()

        return run_file

    @staticmethod
    def record(ids: set[str], run_file: Path, tweet_id: str) -> None:
        """Record an ID in memory and append it to the run's CSV file."""

        ids.add(tweet_id)
        with run_file.open("a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow([tweet_id])


class Feed:
    @staticmethod
    def build(tweets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Group raw tweet dicts into ``{user_id: {profile, posts}}``."""

        feed: dict[str, dict[str, Any]] = {}
        for tweet in tweets:
            user = tweet.get("user") or {}
            user_id = str(user.get("id_str") or "")
            if not user_id:
                Config.LOGGER.warning(
                    "Skipping tweet %s without a user ID.", tweet.get("id_str")
                )
                continue

            entry = feed.setdefault(
                user_id,
                {
                    "displayname": user.get("displayname"),
                    "rawDescription": user.get("rawDescription"),
                    "posts": {},
                },
            )
            entry["posts"][str(tweet["id_str"])] = {
                key: value for key, value in tweet.items() if key != "user"
            }

        return feed

    @staticmethod
    def find_active(now: datetime) -> Path | None:
        """Return the feed file whose validity window contains ``now``."""

        if not Config.FEED_DIR.is_dir():
            return None

        for path in sorted(Config.FEED_DIR.glob("*.json")):
            match = Config.FEED_FILENAME_RE.match(path.name)
            if not match:
                continue
            try:
                valid_until = datetime.strptime(
                    match.group(1), Config.TIMESTAMP_FORMAT
                ).replace(tzinfo=Config.JST)

                valid_from = datetime.strptime(
                    match.group(2), Config.TIMESTAMP_FORMAT
                ).replace(tzinfo=Config.JST)
            except ValueError:
                continue

            if valid_from <= now <= valid_until:
                return path

        return None

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        """Load a feed JSON file; missing or malformed files yield ``{}``."""

        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            Config.LOGGER.warning("Could not load feed file %s: %s", path, exc)
            return {}

        return data if isinstance(data, dict) else {}

    @staticmethod
    def merge(base: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
        """Merge ``addition`` into ``base``; newer posts and profiles win."""

        for user_id, user_data in addition.items():
            if user_id not in base:
                base[user_id] = user_data
                continue

            entry = base[user_id]
            if user_data.get("displayname"):
                entry["displayname"] = user_data["displayname"]

            if user_data.get("rawDescription"):
                entry["rawDescription"] = user_data["rawDescription"]

            entry.setdefault("posts", {})
            entry["posts"].update(user_data.get("posts", {}))

        return base

    @staticmethod
    def resolve_output(now: datetime, merge: bool) -> tuple[Path, dict[str, Any]]:
        """Choose the feed file to write and any existing feed to merge in."""
        if not merge:
            path = Config.FEED_DIR / (
                f"latest-{now.strftime(Config.TIMESTAMP_FORMAT)}.json"
            )
            Config.LOGGER.info("Creating standalone feed: %s", path)
            return path, {}

        active_path = Feed.find_active(now)
        if active_path is not None:
            Config.LOGGER.info("Appending to active feed: %s", active_path)
            return active_path, Feed.load(active_path)

        valid_until = now + timedelta(hours=24)
        path = Config.FEED_DIR / (
            f"{valid_until.strftime(Config.TIMESTAMP_FORMAT)}-"
            f"{now.strftime(Config.TIMESTAMP_FORMAT)}.json"
        )

        Config.LOGGER.info("Creating new active feed: %s", path)

        return path, {}

    @staticmethod
    def simplify(
        raw_feed: dict[str, Any],
        only_recent: bool = True,
        today: str | None = None,
        yesterday: str | None = None,
    ) -> dict[str, Any]:
        """Reduce raw feed data to the fields the model needs."""

        if today is None or yesterday is None:
            now = datetime.now(Config.JST)
            today = today or now.strftime("%Y-%m-%d")
            yesterday = yesterday or (now - timedelta(days=1)).strftime("%Y-%m-%d")

        recent_dates = {today, yesterday}

        simplified: dict[str, Any] = {}
        for user_id, user_info in raw_feed.items():
            posts: dict[str, dict[str, str]] = {}

            for post_id, post_info in user_info.get("posts", {}).items():
                tweet_id = str(post_info.get("id_str") or post_id)
                posted_at = Post.parse_date(post_info.get("date"))

                if only_recent and (
                    posted_at is None
                    or posted_at.strftime("%Y-%m-%d") not in recent_dates
                ):
                    continue

                posts[tweet_id] = {
                    "rawContent": str(post_info.get("rawContent") or ""),
                    "date": (
                        ""
                        if posted_at is None
                        else posted_at.strftime("%Y-%m-%d %H:%M:%S %Z")
                    ),
                    "hashtags": Post.format_hashtags(post_info.get("hashtags")),
                    "url": f"https://x.com/i/status/{tweet_id}",
                }

            simplified[user_id] = {
                "displayname": user_info.get("displayname"),
                "rawDescription": user_info.get("rawDescription"),
                "posts": posts,
            }

        return simplified

    @staticmethod
    def find_newest() -> Path | None:
        """Return the most recently modified feed file, if any."""

        if not Config.FEED_DIR.is_dir():
            return None

        feed_files = list(Config.FEED_DIR.glob("*.json"))
        if not feed_files:
            return None

        return max(feed_files, key=lambda path: path.stat().st_mtime)

    @staticmethod
    def load_for_summary(feed_path: str | Path | None) -> tuple[Path, dict[str, Any]]:
        """Load the requested feed file, or the newest one when unspecified."""

        target = Path(feed_path) if feed_path is not None else Feed.find_newest()
        if target is None or not target.is_file():
            raise FileNotFoundError(
                "Feed file not found. Pass --feed PATH or check the feed/ directory."
            )

        Config.LOGGER.info("Reading raw data from: %s", target)

        try:
            with target.open("r", encoding="utf-8") as file:
                raw_feed = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Feed file contains invalid JSON: {target}") from exc

        if not isinstance(raw_feed, dict):
            raise ValueError(f"Feed file must contain a JSON object: {target}")

        return target, raw_feed


class Fetch:
    @staticmethod
    async def following(api: API) -> list[str]:
        """Return the usernames followed by ``Config.ACCOUNT_ID``."""

        return [
            user.username
            async for user in api.following(
                Config.ACCOUNT_ID, limit=Config.FOLLOWING_LIMIT
            )
        ]

    @staticmethod
    async def search_recent(
        api: API,
        usernames: Sequence[str],
        seen_ids: set[str],
        run_file: Path,
        now: datetime,
        include_seen: bool = False,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Collect the last 24 hours of tweets from ``usernames`` via search."""

        since_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        until_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        chunks = [
            usernames[start : start + Config.SEARCH_CHUNK_SIZE]
            for start in range(0, len(usernames), Config.SEARCH_CHUNK_SIZE)
        ]

        tweets: list[dict[str, Any]] = []
        active_usernames: set[str] = set()

        for index, chunk in enumerate(chunks, start=1):
            accounts_clause = " OR ".join(f"from:{name}" for name in chunk)
            query = f"({accounts_clause}) since:{since_date} until:{until_date}"
            Config.LOGGER.info("[%d/%d] Searching: %s", index, len(chunks), query)

            try:
                async with aclosing(
                    api.search(query, limit=Config.SEARCH_LIMIT)
                ) as stream:
                    async for tweet in stream:
                        if not Post.is_recent(tweet.date, now):
                            continue

                        if tweet.user and tweet.user.username:
                            active_usernames.add(tweet.user.username)

                        is_new = tweet.id_str not in seen_ids
                        if not is_new and not include_seen:
                            continue
                        if is_new:
                            Seen.record(seen_ids, run_file, tweet.id_str)

                        tweets.append(json.loads(tweet.json()))
            except Exception as exc:  # Keep one failing chunk from aborting the run.
                Config.LOGGER.warning("Search failed for chunk %d: %s", index, exc)

        tweets.sort(key=lambda tweet: tweet.get("date", ""), reverse=True)

        return tweets, active_usernames

    @staticmethod
    async def user_timeline(
        api: API,
        username: str,
        seen_ids: set[str],
        run_file: Path,
        now: datetime,
        include_seen: bool = False,
    ) -> list[dict[str, Any]]:
        """Collect not-yet-seen tweets from one user's timeline."""

        user = await api.user_by_login(username)
        if not user:
            Config.LOGGER.warning("User not found: %s", username)
            return []

        recent: list[tuple[Tweet, bool]] = []
        older: list[tuple[Tweet, bool]] = []

        async with aclosing(
            api.user_tweets(user.id, limit=Config.TIMELINE_LIMIT)
        ) as stream:
            async for tweet in stream:
                if (
                    len(recent) + len(older) >= Config.TIMELINE_LIMIT
                    or len(older) >= Config.OLD_TWEETS_LIMIT
                ):
                    break

                is_new = tweet.id_str not in seen_ids
                if is_new:
                    Seen.record(seen_ids, run_file, tweet.id_str)

                if Post.is_recent(tweet.date, now):
                    recent.append((tweet, is_new))
                elif recent:
                    # Timelines run newest-first: once we leave the 24-hour
                    # window after having seen recent tweets, only older
                    # tweets follow, so stop here.
                    break
                else:
                    older.append((tweet, is_new))

        selected = recent or older
        return [
            json.loads(tweet.json())
            for tweet, is_new in selected
            if is_new or include_seen
        ]

    @staticmethod
    async def run(
        include_old_posts: bool,
        merge: bool,
        include_seen: bool = False,
    ) -> None:
        """Collect tweets and write them to the feed (see module docstring)."""

        now = datetime.now(timezone.utc)
        now_jst = now.astimezone(Config.JST)

        seen_ids = Seen.load()
        run_file = Seen.start_run(now_jst)

        api = API()
        usernames = await Fetch.following(api)
        Config.LOGGER.info("Following %d users", len(usernames))

        Config.FEED_DIR.mkdir(parents=True, exist_ok=True)
        output_path, existing_feed = Feed.resolve_output(now_jst, merge)

        recent_tweets, active_usernames = await Fetch.search_recent(
            api, usernames, seen_ids, run_file, now, include_seen=include_seen
        )
        Config.LOGGER.info(
            "Search collected %d new tweets from %d active users",
            len(recent_tweets),
            len(active_usernames),
        )

        old_tweets: list[dict[str, Any]] = []
        if include_old_posts:
            inactive_usernames = [
                name for name in usernames if name not in active_usernames
            ]
            Config.LOGGER.info(
                "Fetching timelines for %d inactive users", len(inactive_usernames)
            )
            for name in inactive_usernames:
                tweets = await Fetch.user_timeline(
                    api, name, seen_ids, run_file, now, include_seen=include_seen
                )
                old_tweets.extend(tweets)
                Config.LOGGER.info("Collected %d new tweets from %s", len(tweets), name)
        else:
            Config.LOGGER.info("Skipping old tweets from inactive users")

        feed = Feed.build(recent_tweets + old_tweets)
        if merge:
            feed = Feed.merge(existing_feed, feed)

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(feed, file, indent=2, ensure_ascii=False)

        Config.LOGGER.info(
            "Wrote %d tweets to %s", len(recent_tweets) + len(old_tweets), output_path
        )


class LLM:
    @staticmethod
    def endpoint(base_url: str) -> str:
        """Normalize a base URL into a chat completions endpoint."""

        url = base_url.strip().rstrip("/")
        if not url:
            raise ValueError("The API base URL must not be empty.")

        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"

        if url.endswith("/chat/completions"):
            return url

        if url.endswith("/v1"):
            return f"{url}/chat/completions"

        return f"{url}/v1/chat/completions"

    @staticmethod
    def chat(
        url: str,
        api_key: str,
        model: str,
        prompt: str,
        history: Sequence[dict[str, str]] = (),
    ) -> str:
        """Send a prompt to the chat API and return the reply text."""

        endpoint = LLM.endpoint(url)
        messages = [*history, {"role": "user", "content": prompt}]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        Config.LOGGER.info(
            "Sending request to %s (model: %s, history: %d messages)",
            endpoint,
            model,
            len(messages),
        )

        try:
            with httpx.Client(timeout=Config.REQUEST_TIMEOUT) as client:
                response = client.post(
                    endpoint,
                    headers=headers,
                    json={"model": model, "messages": messages},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"API returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:1000]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Could not connect to the API at {endpoint}: {exc}"
            ) from exc

        try:
            reply = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "The API returned an unexpected response format."
            ) from exc
        if not isinstance(reply, str):
            raise RuntimeError(
                "The API response did not contain a text message."
            )

        return reply


class Report:
    @staticmethod
    def default_path(feed_path: Path) -> Path:
        """Return (and create the directory for) the default report path."""

        Config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        return Config.REPORT_DIR / f"summary_{feed_path.stem}.md"

    @staticmethod
    def save(output_file: str | Path, content: str, output_format: str) -> Path:
        """Save the summary as Markdown or HTML and return the written path."""

        output_format = output_format.lower()
        if output_format not in Config.REPORT_FORMATS:
            raise ValueError(
                f"Unsupported output format: {output_format!r}. "
                f"Choose from {Config.REPORT_FORMATS}."
            )

        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_format == "md":
            output_path.write_text(content, encoding="utf-8")
            Config.LOGGER.info("Saved Markdown report to: %s", output_path)
            return output_path

        html_body = markdown.markdown(
            content,
            extensions=["extra", "sane_lists", "nl2br"],
        )

        html_document = (
            Config.HTML_TEMPLATE
            .replace("__TITLE__", html.escape(output_path.stem))
            .replace("__HTML_BODY__", html_body)
        )

        Config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        html_path = Config.REPORT_DIR / f"{output_path.stem}.html"
        html_path.write_text(html_document, encoding="utf-8")
        Config.LOGGER.info("Saved HTML report to: %s", html_path)

        if output_path != html_path and output_path.exists():
            try:
                output_path.unlink()
                Config.LOGGER.info("Removed intermediate Markdown file: %s", output_path)
            except OSError as exc:
                Config.LOGGER.warning("Could not remove %s: %s", output_path, exc)

        return html_path


class Pipeline:
    @staticmethod
    def build_prompt(simplified: dict[str, Any]) -> str:
        """Assemble the full prompt: instructions followed by JSON data."""
        return "\n\n".join(
            [
                Config.PROMPT,
                json.dumps(simplified, ensure_ascii=False, indent=2),
            ]
        )

    @staticmethod
    def build_static_report(simplified: dict[str, Any]) -> str:
        """Generate a fallback report from feed data when the LLM fails."""

        lines: list[str] = ["## 不明", ""]

        for user_info in simplified.values():
            posts = user_info.get("posts") or {}
            if not posts:
                continue

            displayname = str(user_info.get("displayname") or "Unknown")
            lines.append(f"### {displayname}")

            for post in posts.values():
                content = str(post.get("rawContent") or "").replace("\r\n", "\n")
                content = content.replace("\n", " ").strip()
                if not content.endswith(("。", "！", "？", "!", "?")):
                    content += "。"
                url = str(post.get("url") or "")

                lines.append(f"* {content}[ソース]({url})")

            lines.append("")

        if len(lines) == 2:
            return "## 不明\n\n* 対象期間内に取得できた投稿はありません。\n"

        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def save_prompt(feed_path: Path, prompt: str) -> Path:
        """Save the prompt text to the prompt/ directory."""

        Config.PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        prompt_path = Config.PROMPT_DIR / f"prompt_{feed_path.stem}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        Config.LOGGER.info("Saved prompt to: %s", prompt_path)
        return prompt_path

    @staticmethod
    def run(options: RunOptions) -> Path:
        """Run the requested stages and return the path to the written file."""

        if options.fetch:
            Config.LOGGER.info("Collecting tweets...")
            asyncio.run(
                Fetch.run(
                    include_old_posts=options.include_old_posts,
                    merge=options.merge,
                    include_seen=options.include_seen,
                )
            )
        else:
            Config.LOGGER.info("Skipping tweet collection; using an existing feed.")

        feed_path, raw_feed = Feed.load_for_summary(options.feed)

        Config.LOGGER.info("Preparing tweet data...")
        simplified = Feed.simplify(raw_feed, only_recent=not options.include_old_posts)

        post_count = sum(len(user["posts"]) for user in simplified.values())
        Config.LOGGER.info("Users in feed: %d", len(simplified))
        Config.LOGGER.info("Tweets selected for summarization: %d", post_count)

        if post_count == 0:
            raise ValueError(
                "No tweets matched the selected date filter. "
                "Try --include-old-posts or check the feed contents."
            )

        prompt = Pipeline.build_prompt(simplified)
        prompt_path = Pipeline.save_prompt(feed_path, prompt)

        if options.prompt_only:
            Config.LOGGER.info(
                "Skipping LLM request and report generation (--prompt-only)."
            )
            return prompt_path

        try:
            Config.LOGGER.info("Sending tweets to the LLM for summarization...")
            summary = LLM.chat(
                url=options.url,
                api_key=options.api_key,
                model=options.model,
                prompt=prompt,
            )
            Config.LOGGER.info("AI summary (final output):\n%s", summary)
        except Exception as exc:  # noqa: BLE001 - fall back on any LLM failure
            Config.LOGGER.warning("LLM request failed: %s", exc)
            Config.LOGGER.info(
                "Falling back to static report generated from feed data."
            )
            summary = Pipeline.build_static_report(simplified)

        if options.output is not None:
            output = Path(options.output)
        else:
            output = Report.default_path(feed_path)

        return Report.save(output, summary, output_format=options.output_format)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect Twitter/X tweets from followed accounts and "
            "summarize them with an OpenAI-compatible LLM."
        ),
    )

    collecting = parser.add_argument_group("tweet collection")
    collecting.add_argument(
        "--include-old-posts",
        action="store_true",
        help=(
            "Also fetch older tweets from inactive users and include "
            "posts of any age in the summary (default: only the last "
            "two days)."
        ),
    )
    collecting.add_argument(
        "--include-seen",
        action="store_true",
        help=(
            "Include tweets whose IDs were already recorded in seen/ "
            "(disables cross-run deduplication for this run; newly seen "
            "IDs are still recorded)."
        ),
    )
    collecting.add_argument(
        "--skip-merge",
        dest="merge",
        action="store_false",
        help=(
            "Write this run's tweets to a standalone feed file instead "
            "of merging them into the active feed."
        ),
    )
    collecting.add_argument(
        "--skip-fetch",
        dest="fetch",
        action="store_false",
        help="Skip tweet collection and summarize an existing feed.",
    )

    reporting = parser.add_argument_group("input / output")
    reporting.add_argument(
        "--feed",
        default=None,
        help="Path to an existing feed JSON file (default: newest in feed/).",
    )
    reporting.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output report file path (default: report/summary_<feed>.md).",
    )
    reporting.add_argument(
        "--format",
        "-f",
        dest="output_format",
        choices=Config.REPORT_FORMATS,
        default="html",
        help=(
            "Report format: 'html' converts to HTML under report/ "
            "(default); 'md' writes the Markdown file directly."
        ),
    )
    reporting.add_argument(
        "--prompt-only",
        action="store_true",
        help=(
            "Save the prompt to prompt/ but skip the LLM request "
            "and report generation."
        ),
    )
    reporting.add_argument(
        "--result-file",
        default=None,
        help=(
            "Write the path of the file produced by the run "
            "(feed, prompt, or report) to this file."
        ),
    )

    llm = parser.add_argument_group("LLM connection")
    llm.add_argument(
        "--url",
        default=Config.DEFAULT_BASE_URL,
        help="OpenAI-compatible API base URL (env: OPENAI_BASE_URL; default: %(default)s).",
    )
    llm.add_argument(
        "--api-key",
        default=Config.DEFAULT_API_KEY,
        help="API key (env: OPENAI_API_KEY; default: %(default)s).",
    )
    llm.add_argument(
        "--model",
        default=Config.DEFAULT_MODEL,
        help="Model name (env: OPENAI_MODEL; default: %(default)s).",
    )

    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress logs (errors are still printed).",
    )

    return parser


def setup_logger(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def build_options(args: argparse.Namespace) -> RunOptions:
    """Populate ``RunOptions`` straight from parsed arguments."""
    known = {field.name for field in fields(RunOptions)}
    return RunOptions(
        **{name: value for name, value in vars(args).items() if name in known}
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logger(logging.ERROR if args.quiet else logging.INFO)
    options = build_options(args)

    try:
        result = Pipeline.run(options)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        Config.LOGGER.error("%s", exc)
        sys.exit(1)

    if options.result_file is not None:
        result_path = Path(options.result_file)
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(f"{result}\n", encoding="utf-8")
            Config.LOGGER.info("Wrote result path to: %s", result_path)
        except OSError as exc:
            Config.LOGGER.error(
                "Could not write result path to %s: %s", result_path, exc
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
