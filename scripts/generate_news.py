"""
Daily News Aggregator
- RSSフィードからニュースを取得
- Gemini APIで日本語要約
- GitHub Pages用HTMLを生成
"""

import html
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import feedparser
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────
# RSSフィード設定
# ─────────────────────────────────────────
RSS_SOURCES = {
    "世界のニュース": [
        ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-all"),
    ],
    "日本のニュース": [
        ("Yahoo!ニュース", "https://news.yahoo.co.jp/rss/topics/top-picks.xml"),
        ("livedoor NEWS", "https://news.livedoor.com/topics/rss/top.xml"),
    ],
    "テクノロジー": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Hacker News", "https://hnrss.org/frontpage"),
    ],
    "エネルギー": [
        ("Energy Monitor", "https://www.energymonitor.ai/feed/"),
        ("Utility Dive", "https://www.utilitydive.com/feeds/news/"),
    ],
}

MAX_ARTICLES_PER_SOURCE = 3
MAX_ARTICLES_PER_CATEGORY = 15
MAX_SUMMARY_CHARS = 300
JST = timezone(timedelta(hours=9))
GEMINI_MODEL = "gemini-2.5-flash"

ROOT_DIR = Path(__file__).parent.parent
DOCS_DIR = ROOT_DIR / "docs"
ARCHIVE_DIR = DOCS_DIR / "archive"


class QuotaExceededError(Exception):
    """Gemini の日次クォータ超過（以降のカテゴリも要約不可）"""


# ─────────────────────────────────────────
# テキスト整形ユーティリティ
# ─────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """RSSのsummaryに含まれるHTMLタグ・実体参照を除去してプレーンテキスト化"""
    text = _TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def safe_url(url: str) -> str:
    """http(s) 以外のスキーム（javascript: 等）は無効化する"""
    url = (url or "").strip()
    if urlparse(url).scheme.lower() in ("http", "https"):
        return url
    return "#"


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


# ─────────────────────────────────────────
# RSS取得
# ─────────────────────────────────────────
def fetch_articles(category: str, sources: list[tuple]) -> list[dict]:
    articles = []
    for source_name, url in sources:
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                reason = feed.get("bozo_exception", "記事0件")
                print(f"[WARN] {source_name}: 記事を取得できませんでした ({reason})", file=sys.stderr)
                continue
            for entry in feed.entries[:MAX_ARTICLES_PER_SOURCE]:
                raw_summary = entry.get("summary", entry.get("description", ""))
                articles.append({
                    "source": source_name,
                    "title": strip_html(entry.get("title", "")),
                    "summary": strip_html(raw_summary)[:MAX_SUMMARY_CHARS],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"[WARN] {source_name} の取得失敗: {e}", file=sys.stderr)
    return articles[:MAX_ARTICLES_PER_CATEGORY]


# ─────────────────────────────────────────
# Gemini API で要約
# ─────────────────────────────────────────
SUMMARY_SCHEMA = genai_types.Schema(
    type=genai_types.Type.ARRAY,
    items=genai_types.Schema(
        type=genai_types.Type.OBJECT,
        properties={
            "headline": genai_types.Schema(type=genai_types.Type.STRING),
            "body": genai_types.Schema(type=genai_types.Type.STRING),
        },
        required=["headline", "body"],
    ),
)

_MD_RE = re.compile(r"(\*\*|__|`|^#+\s*|^\s*[-*•]\s+)", re.MULTILINE)


def parse_points(text: str) -> list[dict]:
    """Gemini の応答を [{headline, body}] に変換する。
    JSON として解釈できない場合は Markdown 記号を取り除いた段落として扱う。"""
    try:
        data = json.loads(text)
        points = [
            {"headline": str(p.get("headline", "")).strip(),
             "body": str(p.get("body", "")).strip()}
            for p in data if isinstance(p, dict)
        ]
        points = [p for p in points if p["headline"] or p["body"]]
        if points:
            return points
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    paragraphs = [_MD_RE.sub("", para).strip() for para in re.split(r"\n\s*\n", text or "")]
    return [{"headline": "", "body": para} for para in paragraphs if para]


def summarize_category(category: str, articles: list[dict], client) -> list[dict]:
    """カテゴリの記事を要約し、[{headline, body}] を返す。失敗時は例外を送出。"""
    articles_text = "\n\n".join(
        f"【{a['source']}】{a['title']}\n{a['summary']}"
        for a in articles
    )

    prompt = f"""以下は「{category}」カテゴリの本日のニュース記事です。
重要なポイントを3〜5つ選び、日本語でまとめてください。

出力ルール:
- JSON 配列のみを出力する（前置き・後書きは不要）
- 各要素は {{"headline": 見出し(30字程度), "body": 説明(2〜3文)}}
- Markdown 記法（**, *, # など）は使わない

--- 記事一覧 ---
{articles_text}
"""

    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                    response_schema=SUMMARY_SCHEMA,
                ),
            )
            points = parse_points(response.text or "")
            if not points:
                raise ValueError("Gemini の応答が空でした")
            return points
        except genai_errors.ServerError:
            # 503: 1回のみリトライ（クォータ節約のため）
            if attempt < max_retries - 1:
                print("  [リトライ 1/1] サーバーエラー、30秒後に再試行...")
                time.sleep(30)
            else:
                raise
        except genai_errors.ClientError as e:
            err_str = str(e)
            # 日次クォータ超過はリトライしても無意味なので即失敗
            if "PerDay" in err_str or "per_day" in err_str.lower():
                raise QuotaExceededError(err_str) from e
            # 分単位のレート制限のみリトライ
            if "429" in err_str and attempt < max_retries - 1:
                wait = 2 ** (attempt + 2)
                print(f"  [リトライ {attempt+1}/{max_retries}] 429レート制限、{wait}秒後に再試行...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("要約に失敗しました")


# ─────────────────────────────────────────
# HTML生成
# ─────────────────────────────────────────
CATEGORY_ICONS = {
    "世界のニュース": "🌍",
    "日本のニュース": "🗾",
    "テクノロジー": "💻",
    "エネルギー": "⚡",
}

def render_summary(data: dict) -> str:
    if data.get("error"):
        return f'<p class="summary-error">⚠️ {esc(data["error"])}</p>'
    items = []
    for p in data["points"]:
        headline = f'<strong>{esc(p["headline"])}</strong>' if p["headline"] else ""
        body = f'<p>{esc(p["body"])}</p>' if p["body"] else ""
        items.append(f"<li>{headline}{body}</li>")
    return f'<ul class="points">{"".join(items)}</ul>'


def build_html(summaries: dict[str, dict], now: datetime, nav_html: str) -> str:
    date_str = now.strftime("%Y年%m月%d日")
    time_str = now.strftime("%H:%M JST")

    sections_html = ""
    for category, data in summaries.items():
        icon = CATEGORY_ICONS.get(category, "📰")
        summary_html = render_summary(data)

        articles_html = "".join(
            f'<li><a href="{esc(safe_url(a["link"]))}" target="_blank" rel="noopener noreferrer">'
            f'<span class="source-badge">{esc(a["source"])}</span> {esc(a["title"])}</a></li>'
            for a in data["articles"]
        )

        sections_html += f"""
        <section class="category-card">
            <h2>{icon} {esc(category)}</h2>
            <div class="summary">{summary_html}</div>
            <details>
                <summary>元記事一覧 ({len(data["articles"])}件)</summary>
                <ul class="article-list">{articles_html}</ul>
            </details>
        </section>
"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily News Digest — {date_str}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Helvetica Neue', Arial, 'Hiragino Kaku Gothic ProN', sans-serif;
            background: #f0f2f5;
            color: #1a1a2e;
            line-height: 1.7;
        }}
        header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: white;
            padding: 2rem 1.5rem;
            text-align: center;
        }}
        header h1 {{ font-size: 1.8rem; margin-bottom: 0.3rem; }}
        header p {{ opacity: 0.7; font-size: 0.9rem; }}
        main {{
            max-width: 900px;
            margin: 2rem auto;
            padding: 0 1rem;
            display: grid;
            gap: 1.5rem;
        }}
        .category-card {{
            background: white;
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08);
        }}
        .category-card h2 {{
            font-size: 1.25rem;
            margin-bottom: 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 2px solid #e8ecf0;
        }}
        .summary {{
            font-size: 0.95rem;
            margin-bottom: 1rem;
            color: #333;
        }}
        .points {{ list-style: none; display: grid; gap: 0.9rem; }}
        .points li {{ padding-left: 0.9rem; border-left: 3px solid #4a6fa5; }}
        .points strong {{ display: block; font-size: 1rem; color: #1a1a2e; }}
        .points p {{ margin-top: 0.2rem; }}
        .summary-error {{ color: #a15c00; background: #fff6e5; border-radius: 6px; padding: 0.6rem 0.8rem; }}
        header nav {{ margin-top: 0.8rem; font-size: 0.85rem; }}
        header nav a {{ color: #cfe0ff; }}
        details {{ margin-top: 0.75rem; }}
        details summary {{
            cursor: pointer;
            font-size: 0.85rem;
            color: #666;
            padding: 0.4rem 0;
        }}
        .article-list {{
            list-style: none;
            margin-top: 0.75rem;
        }}
        .article-list li {{
            padding: 0.4rem 0;
            border-bottom: 1px solid #f0f0f0;
            font-size: 0.88rem;
        }}
        .article-list a {{
            color: #0066cc;
            text-decoration: none;
            display: flex;
            align-items: baseline;
            gap: 0.4rem;
        }}
        .article-list a:hover {{ text-decoration: underline; }}
        .source-badge {{
            background: #e8ecf0;
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 0.75rem;
            color: #555;
            white-space: nowrap;
            flex-shrink: 0;
        }}
        footer {{
            text-align: center;
            padding: 2rem;
            font-size: 0.8rem;
            color: #999;
        }}
    </style>
</head>
<body>
    <header>
        <h1>📰 Daily News Digest</h1>
        <p>{date_str} &nbsp;|&nbsp; 更新 {time_str} &nbsp;|&nbsp; Gemini AI 要約</p>
        <nav>{nav_html}</nav>
    </header>
    <main>
        {sections_html}
    </main>
    <footer>
        Powered by Gemini API &amp; GitHub Actions &nbsp;|&nbsp; 毎日 09:30 / 15:00 JST に自動更新
    </footer>
</body>
</html>
"""


# ─────────────────────────────────────────
# アーカイブ
# ─────────────────────────────────────────
ARCHIVE_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})\.html$")


def build_archive_index(now: datetime) -> str:
    entries = sorted(
        (p.name for p in ARCHIVE_DIR.glob("*.html") if ARCHIVE_NAME_RE.match(p.name)),
        reverse=True,
    )
    items = []
    for name in entries:
        y, mo, d, h, mi = ARCHIVE_NAME_RE.match(name).groups()
        items.append(f'<li><a href="./{esc(name)}">{y}年{mo}月{d}日 {h}:{mi} JST</a></li>')
    list_html = "".join(items) or "<li>まだアーカイブはありません。</li>"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily News Digest — アーカイブ</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: 'Helvetica Neue', Arial, 'Hiragino Kaku Gothic ProN', sans-serif;
                background: #f0f2f5; color: #1a1a2e; line-height: 1.7; }}
        header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white;
                  padding: 2rem 1.5rem; text-align: center; }}
        header a {{ color: #cfe0ff; font-size: 0.85rem; }}
        main {{ max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
        ul {{ list-style: none; background: white; border-radius: 12px; padding: 1rem 1.5rem;
              box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
        li {{ padding: 0.5rem 0; border-bottom: 1px solid #f0f0f0; }}
        li:last-child {{ border-bottom: none; }}
        li a {{ color: #0066cc; text-decoration: none; }}
        li a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <header>
        <h1>🗂 過去の記事</h1>
        <p><a href="../">← 最新のダイジェストへ</a> &nbsp;|&nbsp; 最終更新 {now.strftime("%Y-%m-%d %H:%M JST")}</p>
    </header>
    <main><ul>{list_html}</ul></main>
</body>
</html>
"""


# ─────────────────────────────────────────
# メイン
# ─────────────────────────────────────────
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("ニュースを取得中...")
    summaries = {}
    quota_exceeded = False
    succeeded = 0
    for i, (category, sources) in enumerate(RSS_SOURCES.items()):
        print(f"  [{category}] 記事取得中...")
        articles = fetch_articles(category, sources)
        entry = {"articles": articles, "points": [], "error": None}
        summaries[category] = entry

        if not articles:
            entry["error"] = "本日は記事を取得できませんでした。"
            continue
        if quota_exceeded:
            entry["error"] = "API の利用上限に達したため要約を生成できませんでした。元記事一覧をご覧ください。"
            continue

        if i > 0:
            time.sleep(5)
        print(f"  [{category}] {len(articles)}件取得 → Gemini で要約中...")
        try:
            entry["points"] = summarize_category(category, articles, client)
            succeeded += 1
        except QuotaExceededError:
            print("  [クォータ超過] 本日の上限に達しました。以降のカテゴリは要約をスキップします。",
                  file=sys.stderr)
            quota_exceeded = True
            entry["error"] = "API の利用上限に達したため要約を生成できませんでした。元記事一覧をご覧ください。"
        except Exception as e:  # 1カテゴリの失敗でページ全体を止めない
            print(f"  [ERROR] {category} の要約に失敗: {e}", file=sys.stderr)
            entry["error"] = "要約を生成できませんでした。元記事一覧をご覧ください。"

    if succeeded == 0:
        # 全カテゴリ失敗時は既存ページを壊さないよう上書きしない
        print("ERROR: すべてのカテゴリで要約に失敗したため、ページを更新しません", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(JST)
    print("HTMLを生成中...")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    index_html = build_html(summaries, now, nav_html='<a href="./archive/">🗂 過去の記事</a>')
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")

    archive_name = now.strftime("%Y-%m-%d_%H%M.html")
    archive_html = build_html(
        summaries, now,
        nav_html='<a href="../">← 最新のダイジェスト</a> &nbsp;|&nbsp; <a href="./">🗂 過去の記事</a>',
    )
    (ARCHIVE_DIR / archive_name).write_text(archive_html, encoding="utf-8")
    (ARCHIVE_DIR / "index.html").write_text(build_archive_index(now), encoding="utf-8")

    print(f"完了: {DOCS_DIR / 'index.html'} / アーカイブ: {archive_name}")


if __name__ == "__main__":
    main()
