import re
from datetime import timedelta, timezone
from pathlib import Path
import logging

class Config:
    """All tunables in one namespace (see the module docstring for layers)."""

    JST = timezone(timedelta(hours=9))

    ACCOUNT_ID = 1544873887363903489  # asdf62608926

    FOLLOWING_LIMIT = 100     # Maximum followed accounts to enumerate.
    SEARCH_LIMIT = 50         # Maximum tweets per search query.
    TIMELINE_LIMIT = 50       # Maximum tweets read per user timeline.
    OLD_TWEETS_LIMIT = 10     # Maximum older tweets kept per inactive user.
    SEARCH_CHUNK_SIZE = 10    # Accounts combined into a single search query.

    SEEN_DIR = Path("seen")
    FEED_DIR = Path("feed")
    REPORT_DIR = Path("report")
    PROMPT_DIR = Path("prompt")

    TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
    FEED_FILENAME_RE = re.compile(r"^(\d{8}_\d{6})-(\d{8}_\d{6})\.json$")

    DEFAULT_BASE_URL = "http://localhost:2048/v1"
    DEFAULT_MODEL = "gemini-3.8-flash"
    DEFAULT_API_KEY = "123456"
    REQUEST_TIMEOUT = 300.0

    REPORT_FORMATS = ("md", "html")

    LOGGER = logging.getLogger("twitter_feed")

    PROMPT = """いままでのTwitter/Xの情報を読み、ツイートを以下の分類に分けて、分類ごとに「誰が何を言っているか」をマークダウン形式でまとめてください。

【分類】
- イベント
- 告知
- コミュニティー
- 日常
- その他
- 不明

【要件】
1. 上記の分類ごとにセクション(##)を設け、誰がどのような発言・投稿を行っているかをまとめてください。
2. 各ユーザーの投稿内容は、ユーザー名(###)の下に箇条書き（* ）の形式で記載してください。
3. 発言内容の要約の末尾に、必ず [ソース](URL) という形式で該当するTwitter/XのURLをテキストリンクとして挿入してください。リンクの文言は「ソース」の2文字のみとし、余計な日本語の補足説明（アーティスト名、曲名、動画タイトルなど）は絶対に追加しないでください。

【出力フォーマット(厳密遵守)】
必ず以下の構造と文字列のみを使用し、完全に固定した形で出力してください。

## [分類名]

### [ユーザー名]
* [発言内容の要約]。[ソース]([URLをそのまま記述])
"""

    HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d1117;
    --text: #e6edf3;
    --heading: #f0f6fc;
    --border: #30363d;
    --link: #58a6ff;
  }

  body {
    margin: 0 auto;
    padding: 2rem 1.25rem 4rem;
    max-width: 50rem;
    line-height: 1.75;
    font-family: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic",
      Meiryo, system-ui, -apple-system, sans-serif;
    color: var(--text);
    background: var(--bg);
  }

  h1 {
    margin: 0 0 1.5rem;
    padding-bottom: 0.6rem;
    font-size: 1.6rem;
    color: var(--heading);
    border-bottom: 2px solid var(--border);
  }

  h2 {
    margin-top: 2.2rem;
    padding-bottom: 0.3rem;
    font-size: 1.3rem;
    color: var(--heading);
    border-bottom: 1px solid var(--border);
  }

  h3 {
    margin-top: 1.6rem;
    font-size: 1.05rem;
    color: var(--heading);
  }

  a {
    color: var(--link);
  }

  a:hover {
    color: #79c0ff;
  }

  ul {
    padding-left: 1.4rem;
  }

  li {
    margin: 0.3rem 0;
  }

  hr {
    border: 0;
    border-top: 1px solid var(--border);
  }

  code, pre {
    color: var(--text);
    background: #161b22;
  }

  blockquote {
    margin-left: 0;
    padding: 0.5rem 1rem;
    border-left: 3px solid #484f58;
    background: #161b22;
    color: #b1bac4;
  }

  ::selection {
    color: #ffffff;
    background: #264f78;
  }
</style>
</head>
<body>
<h1>__TITLE__</h1>
__HTML_BODY__
</body>
</html>
"""
