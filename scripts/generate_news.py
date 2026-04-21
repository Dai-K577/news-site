import time
from google import genai
from google.genai import errors as genai_errors


def summarize_category(client, category, articles):
    prompt = f"""以下の{category}カテゴリのニュース記事を要約してください。

{articles}

各記事の要点を簡潔にまとめてください。"""

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text
        except genai_errors.ServerError as e:
            if e.status_code == 503 and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s → 2s → 4s → 8s → 16s
                print(f"  [リトライ {attempt+1}/{max_retries}] 503エラー、{wait}秒後に再試行...")
                time.sleep(wait)
            else:
                raise
