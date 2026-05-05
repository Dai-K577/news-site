"""
Daily News Aggregator
- RSSフィードからニュースを取得
- Gemini APIで日本語要約
- GitHub Pages用HTMLを生成
"""

import os
import sys
import json
import time
import feedparser
from google import genai
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────
# RSSフィード設定
# ─────────────────────────────────────────
RSS_SOURCES = {
    "世界のニュース": [
        ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Reuters Top", "https://feeds.reuters.com/reuters/topNews"),
    ],
    "日本のニュース": [
        ("NHK国際放送", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
        ("朝日新聞", "https://www.asahi.com/rss/asahi/newsheadlines.rdf"),
    ],
    "テクノロジー": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Hacker News", "https://hnrss.org/frontpage"),
    ],
    "エネルギー": [
        ("Reuters Energy", "https://feeds.reuters.com/reuters/environment"),
        ("Energy Monitor", "https://www.energymonitor.ai/feed/"),
    ],
}

MAX_ARTICLES_PER_SOURCE = 5   # 1ソースあたりの最大記事数
MAX_ARTICLES_PER_CATEGORY = 8 # 1カテゴリあたりの最大記事数(要約用)
JST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────
# RSS取得
# ─────────────────────────────────────────
def fetch_articles(category: str, sources: list[tuple]) -> list[dict]:
    articles = []
    for source_name, url in sources:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:MAX_ARTICLES_PER_SOURCE]:
                articles.append({
                    "source": source_name,
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", entry.get("description", ""))[:300],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"[WARN] {source_name} の取得失敗: {e}", file=sys.stderr)
    return articles[:MAX_ARTICLES_PER_CATEGORY]


# ─────────────────────────────────────────
# Gemini API で要約（リトライあり）
# ─────────────────────────────────────────
def summarize_category(category: str, articles: list[dict], client=None) -> str:
    if not articles:
        return "本日は記事を取得できませんでした。"

    articles_text = "\n\n".join(
        f"【{a['source']}】{a['title']}\n{a['summary']}"
        for a in articles
    )

    prompt = f"""以下は「{category}」カテゴリの本日のニュース記事です。
日本語で3〜5つの重要なポイントを箇条書きでまとめてください。
各ポイントは2〜3文で、わかりやすく簡潔に説明してください。

--- 記事一覧 ---
{articles_text}
"""

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 30 * (attempt + 1)  # 30秒、60秒、90秒…と待機
                print(f"  [WARN] {category} 要約失敗（{attempt + 1}回目）: {e} → {wait}秒後リトライ")
                time.sleep(wait)
            else:
                print(f"  [ERROR] {category} 要約を{max_retries}回試みましたが失敗しました: {e}")
                return "一時的なエラーにより要約を取得できませんでした。"


# ─────────────────────────────────────────
# HTML生成
# ─────────────────────────────────────────
CATEGORY_ICONS = {
    "世界のニュース": "🌍",
    "日本のニュース": "🗾",
    "テクノロジー": "💻",
    "エネルギー": "⚡",
}

def build_html(summaries: dict[str, dict]) -> str:
    now = datetime.now(JST)
    date_str = now.strftime("%Y年%m月%d日")
    time_str = now.strftime("%H:%M JST")

    sections_html = ""
    for category, data in summaries.items():
        icon = CATEGORY_ICONS.get(category, "📰")
        summary_html = data["summary"].replace("\n", "<br>")

        articles_html = "".join(
            f'<li><a href="{a["link"]}" target="_blank" rel="noopener">'
            f'<span class="source-badge">{a["source"]}</span> {a["title"]}</a></li>'
            for a in data["articles"]
        )

        sections_html += f"""
        <section class="category-card">
            <h2>{icon} {category}</h2>
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
    </header>
    <main>
        {sections_html}
    </main>
    <footer>
        Powered by Gemini API &amp; GitHub Actions &nbsp;|&nbsp; 3時間おきに自動更新
    </footer>
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
    for i, (category, sources) in enumerate(RSS_SOURCES.items()):
        if i > 0:
            time.sleep(5)  # レート制限回避のため5秒待機
        print(f"  [{category}] 記事取得中...")
        articles = fetch_articles(category, sources)
        print(f"  [{category}] {len(articles)}件取得 → Gemini で要約中...")
        summary = summarize_category(category, articles, client)
        summaries[category] = {"summary": summary, "articles": articles}

    print("HTMLを生成中...")
    html = build_html(summaries)

    output_path = Path(__file__).parent.parent / "docs" / "index.html"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"完了: {output_path}")


if __name__ == "__main__":
    main()
