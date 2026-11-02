from __future__ import annotations

import os
import re
import sys
from datetime import timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_FILENAMES = (".env",)

def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` file; missing or unreadable files yield ``{}``."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"config: ignoring unreadable {path.name}: {exc}", file=sys.stderr)
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        values[key.strip()] = value

    return values


def _load_env_files() -> dict[str, str]:
    """Collect every env-file value without touching ``os.environ``."""

    values: dict[str, str] = {}
    for filename in _ENV_FILENAMES:
        values.update(_parse_env_file(_PROJECT_ROOT / filename))
    return values


_FILE_ENV = _load_env_files()


def _lookup(name: str) -> str | None:
    """Return the effective raw value for ``name`` (``None`` when unset)."""

    for source in (os.environ, _FILE_ENV):
        value = source.get(name)
        if value is not None and value.strip():
            return value

    return None


def _env_str(name: str, default: str) -> str:
    """Resolve a string setting: environment > env file > default."""

    value = _lookup(name)
    return default if value is None else value


def _env_int(name: str, default: int) -> int:
    """Resolve an integer setting, falling back with a warning on bad input."""

    value = _lookup(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        print(
            f"config: {name}={value!r} is not an integer, using {default}",
            file=sys.stderr,
        )
        return default


def _env_float(name: str, default: float) -> float:
    """Resolve a float setting, falling back with a warning on bad input."""

    value = _lookup(name)
    if value is None:
        return default

    try:
        return float(value)
    except ValueError:
        print(
            f"config: {name}={value!r} is not a number, using {default}",
            file=sys.stderr,
        )
        return default


def _env_path(name: str, default: Path) -> Path:
    """Resolve a path setting (relative paths stay relative to the CWD)."""

    value = _lookup(name)
    return default if value is None else Path(value)


class Config:
    """All tunables in one namespace; every scalar can come from the env."""

    JST = timezone(timedelta(hours=9))

    ACCOUNT_ID = _env_int("ACCOUNT_ID", 1544873887363903489)  # asdf62608926

    FOLLOWING_LIMIT = _env_int("FOLLOWING_LIMIT", 100)     # Maximum followed accounts to enumerate.
    SEARCH_LIMIT = _env_int("SEARCH_LIMIT", 50)            # Maximum tweets per search query.
    TIMELINE_LIMIT = _env_int("TIMELINE_LIMIT", 50)        # Maximum tweets read per user timeline.
    OLD_TWEETS_LIMIT = _env_int("OLD_TWEETS_LIMIT", 10)    # Maximum older tweets kept per inactive user.
    SEARCH_CHUNK_SIZE = _env_int("SEARCH_CHUNK_SIZE", 10)  # Accounts combined into a single search query.

    SEEN_DIR = _env_path("SEEN_DIR", Path("seen"))
    FEED_DIR = _env_path("FEED_DIR", Path("feed"))
    REPORT_DIR = _env_path("REPORT_DIR", Path("report"))
    PROMPT_DIR = _env_path("PROMPT_DIR", Path("prompt"))

    TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
    FEED_FILENAME_RE = re.compile(r"^(\d{8}_\d{6})-(\d{8}_\d{6})\.json$")

    DEFAULT_BASE_URL = _env_str("OPENAI_BASE_URL", "http://localhost:2048/v1")
    DEFAULT_MODEL = _env_str("OPENAI_MODEL", "gemini-3.8-flash")
    DEFAULT_API_KEY = _env_str("OPENAI_API_KEY", "123456")
    REQUEST_TIMEOUT = _env_float("REQUEST_TIMEOUT", 300.0)

    REPORT_FORMATS = ("md", "html")

    PROMPT = """いままでのTwitter/Xの情報を読み、ツイートを以下の分類に分けて、分類ごとに「誰が何を言っているか」をマークダウン形式でまとめてください。

【分類】
- イベント
- 告知
- コミュニティー
- 日常
- その他
- 不明

【要件】
1. 上記の分類ごとにセクション（##）を設け、誰がどのような発言・投稿を行っているかをまとめてください。
2. ユーザーの投稿ごとに必ずインデント（字下げ）を空け、見出しや箇条書きの階層構造を明確にしてください。
3. ソース項目には、余計な日本語の補足説明（アーティスト名、曲名、動画タイトルなど）を絶対に追加しないでください。URLのテキストリンク自体も、説明文にせずURLをそのまま書き込んでください。

【出力フォーマット（厳密遵守）】
必ず以下の構造と文字列のみを使用し、完全に固定した形で出力してください。

## [分類名]

### [ユーザー名]
* **内容**: 
    * [発言内容の要約]
    * **ソース**:
        * 記載リンク: [URLをそのまま記述]
"""

    HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  body {
    margin: 0 auto;
    padding: 2rem 1.25rem 4rem;
    max-width: 50rem;
    line-height: 1.75;
    font-family: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic",
      Meiryo, system-ui, -apple-system, sans-serif;
    color: #1f2328;
    background: #ffffff;
  }
  h1 {
    margin: 0 0 1.5rem;
    padding-bottom: 0.6rem;
    font-size: 1.6rem;
    border-bottom: 2px solid #d0d7de;
  }
  h2 {
    margin-top: 2.2rem;
    padding-bottom: 0.3rem;
    font-size: 1.3rem;
    border-bottom: 1px solid #eaeef2;
  }
  h3 { margin-top: 1.6rem; font-size: 1.05rem; }
  a { color: #0969da; }
  ul { padding-left: 1.4rem; }
  li { margin: 0.3rem 0; }
  hr { border: 0; border-top: 1px solid #d0d7de; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
__HTML_BODY__
</body>
</html>
"""
