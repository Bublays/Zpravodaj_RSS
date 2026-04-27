import os
import html
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import markdown
from openai import OpenAI

if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY is missing!")

FEEDS = {
    "Z domova": [
        "https://www.seznamzpravy.cz/rss",
        "https://www.irozhlas.cz/rss/irozhlas",
        "https://ct24.ceskatelevize.cz/rss",
        "https://www.aktualne.cz/rss",
    ],
    "Sport": [
        "https://sport.ceskatelevize.cz/rss",
        "https://isport.blesk.cz/rss",
    ],
    "Svět": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://rss.dw.com/rdf/rss-en-all",
    ],
}


MAX_ARTICLES = 80
MAX_SUMMARY_CHARS = 700
OUTPUT_DIR = Path("public")
OUTPUT_HTML = OUTPUT_DIR / "index.html"
PROMPT_PATH = Path("prompts/system_prompt.txt")


def parse_date(entry):
    if getattr(entry, "published", None):
        try:
            return parsedate_to_datetime(entry.published)
        except Exception:
            pass

    if getattr(entry, "updated", None):
        try:
            return parsedate_to_datetime(entry.updated)
        except Exception:
            pass

    return None


def clean_text(value):
    if not value:
        return ""
    return " ".join(html.unescape(value).split())


def collect_articles():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    articles = []

    for category, urls in FEEDS.items():
        for feed_url in urls:
            feed = feedparser.parse(feed_url)
            source = clean_text(feed.feed.get("title", feed_url))

            for entry in feed.entries:
                published = parse_date(entry)
                if not published:
                    continue

                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)

                published_utc = published.astimezone(timezone.utc)

                if published_utc < cutoff:
                    continue

                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", entry.get("description", "")))
                link = clean_text(entry.get("link", ""))

                if not title:
                    continue

                articles.append({
                    "title": title,
                    "summary": summary[:MAX_SUMMARY_CHARS],
                    "source": source,
                    "published": published_utc.isoformat(),
                    "category_hint": category,
                    "url": link,  # interně pro audit, prompt zakazuje vypisovat URL
                })

    articles.sort(key=lambda x: x["published"], reverse=True)
    return articles[:MAX_ARTICLES]


def build_user_prompt(articles):
    lines = [
        "Použij VÝHRADNĚ následující články jako zdroj.",
        "Nevytvářej žádné zprávy mimo tento seznam.",
        "Nevkládej do výstupu žádné URL odkazy.",
        "Neuváděj řádek začínající „Odkaz:“.",
        "Pokud pro některou rubriku není dost ověřených aktuálních článků, napiš to stručně místo vymýšlení.",
        "",
        "ČLÁNKY:",
    ]

    for i, article in enumerate(articles, start=1):
        lines.append(
            f"{i}. [{article['category_hint']}] {article['title']}\n"
            f"   Zdroj: {article['source']}\n"
            f"   Publikováno: {article['published']}\n"
            f"   Shrnutí: {article['summary']}\n"
            f"   Interní URL: {article['url']}"
        )

    return "\n".join(lines)


def generate_briefing(articles):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_prompt = build_user_prompt(articles)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )

    return response.output_text


def render_html(briefing_md, articles_count):
    body = markdown.markdown(
        briefing_md,
        extensions=["extra", "sane_lists"]
    )

    generated_at = datetime.now().strftime("%d.%m.%Y %H:%M")

    return f"""<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ranní briefing</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 900px;
      margin: 40px auto;
      padding: 0 20px;
      line-height: 1.6;
      color: #1f2937;
      background: #f9fafb;
    }}
    main {{
      background: white;
      padding: 32px;
      border-radius: 16px;
      box-shadow: 0 10px 30px rgba(0,0,0,.06);
    }}
    h1, h2, h3 {{
      line-height: 1.25;
    }}
    h1 {{
      margin-top: 0;
    }}
    .meta {{
      color: #6b7280;
      font-size: 14px;
      margin-bottom: 32px;
    }}
    p {{
      margin-bottom: 14px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Ranní briefing</h1>
    <div class="meta">
      Vygenerováno: {generated_at}<br>
      Zpracováno článků: {articles_count}
    </div>
    {body}
  </main>
</body>
</html>
"""


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    articles = collect_articles()

    if not articles:
        briefing = "RANNÍ BRIEFING\n\nNepodařilo se najít žádné aktuální články za posledních 24 hodin."
    else:
        briefing = generate_briefing(articles)

    OUTPUT_HTML.write_text(
        render_html(briefing, len(articles)),
        encoding="utf-8"
    )

    print(f"Generated {OUTPUT_HTML} from {len(articles)} articles.")


if __name__ == "__main__":
    main()
