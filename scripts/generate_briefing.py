import os
import html
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
import unicodedata
from difflib import SequenceMatcher
import xml.etree.ElementTree as ET
from urllib.request import urlopen
import feedparser
import markdown
from openai import OpenAI

if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY is missing!")

FEEDS = {
    "Z domova": [
        "https://www.irozhlas.cz/rss/irozhlas/section/zpravy-domov",
        "https://ct24.ceskatelevize.cz/rss",
        "https://www.seznamzpravy.cz/rss",
        "https://www.aktualne.cz/rss",
    ],

    "Finance": [
        "https://www.irozhlas.cz/rss/irozhlas/section/ekonomika",
        "https://www.aktualne.cz/rss/ekonomika",
        "https://www.seznamzpravy.cz/rss",
    ],

    "Hospodářství a ekonomie": [
        "https://www.irozhlas.cz/rss/irozhlas/section/ekonomika",
        "https://www.aktualne.cz/rss/ekonomika",
        "https://www.seznamzpravy.cz/rss",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ],

    "Svět": [
        "https://www.irozhlas.cz/rss/irozhlas/section/zpravy-svet",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://rss.dw.com/rdf/rss-en-all",
    ],

    "Kultura": [
        "https://www.irozhlas.cz/rss/irozhlas/section/kultura",
        "https://www.aktualne.cz/rss/kultura",
        "https://www.theguardian.com/culture/rss",
    ],

    "Sport": [
        "https://www.irozhlas.cz/rss/irozhlas/section/sport",
        "https://www.irozhlas.cz/rss/irozhlas/sportovni-zpravy",
        "https://sport.ceskatelevize.cz/rss",
        "https://isport.blesk.cz/rss",
    ],
}

CNB_FX_URL = "https://www.cnb.cz/cs/financni_trhy/devizovy_trh/kurzy_devizoveho_trhu/denni_kurz.xml"
MAX_ARTICLES = 120
MAX_ARTICLES_PER_CATEGORY = 20
MAX_SUMMARY_CHARS = 700
SOURCE_WEIGHTS = {
    "Reuters": 4,
    "BBC": 3,
    "The Guardian": 3,
    "DW": 2,
    "iROZHLAS": 3,
    "ČT24": 3,
    "ČT sport": 3,
    "Seznam Zprávy": 2,
    "Aktuálně": 2,
    "iSport": 2,
}

BRIEFING_CATEGORIES = [
    "Z domova",
    "Finance",
    "Hospodářství a ekonomie",
    "Svět",
    "Kultura",
    "Sport",
]
CATEGORY_KEYWORDS = {
    "Z domova": [
        "vláda", "premiér", "sněmovna", "senát", "ministerstvo", "policie",
        "soud", "zákon", "rozpočet", "nato", "obrana", "nemocnice",
        "česká televize", "český rozhlas",
    ],
    "Finance": [
        "koruna", "euro", "dolar", "ropa", "brent", "wti", "akcie",
        "burza", "index", "inflace", "sazby", "čnb", "fed", "ecb",
        "zlato", "dluhopisy",
    ],
    "Hospodářství a ekonomie": [
        "hdp", "hospodářství", "průmysl", "nezaměstnanost", "mzdy",
        "inflace", "rozpočet", "deficit", "export", "import", "cla",
        "centrální banka",
    ],
    "Svět": [
        "usa", "čína", "rusko", "ukrajina", "írán", "izrael", "gaza",
        "eu", "nato", "osn", "sankce", "válka", "prezident",
        "premiér", "volby", "útok",
    ],
    "Kultura": [
        "festival", "film", "divadlo", "výstava", "koncert", "album",
        "cena", "literatura", "muzeum", "galerie", "opera",
    ],
    "Sport": [
        "vyhrál", "porazil", "postoupil", "finále", "semifinále",
        "liga", "turnaj", "mistrovství", "skóre", "gól", "závod",
        "formule", "moto", "tenis", "hokej", "fotbal",
    ],
}

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
    
def get_cnb_fx_rates():
    try:
        with urlopen(CNB_FX_URL, timeout=20) as response:
            xml_data = response.read()

        root = ET.fromstring(xml_data)

        rates = {}

        for radek in root.findall(".//radek"):
            kod = radek.attrib.get("kod")
            kurz = radek.attrib.get("kurz")
            mnozstvi = radek.attrib.get("mnozstvi", "1")

            if kod in {"EUR", "USD"} and kurz:
                rates[kod] = {
                    "rate": kurz.replace(",", "."),
                    "amount": mnozstvi,
                }

        return {
            "EUR": rates.get("EUR"),
            "USD": rates.get("USD"),
            "source": "Česká národní banka",
            "url": CNB_FX_URL,
        }

    except Exception as e:
        print(f"Warning: could not fetch CNB FX rates: {e}")
        return {
            "EUR": None,
            "USD": None,
            "source": "Česká národní banka",
            "url": CNB_FX_URL,
        }
def normalize_text(text):
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def build_event_hint(article):
    text = normalize_text(f"{article.get('title', '')} {article.get('summary', '')}")

    rules = [
        ("fotbal_chance_liga", ["sparta", "slavia", "plzen", "banik", "bohemians", "chance liga"]),
        ("hokej_extraliga_baraz", ["litvinov", "jihlava", "baraz", "extraliga"]),
        ("tenis_madrid", ["madrid", "wta", "atp", "pliskova", "lehecka", "noskova", "siniakova"]),
        ("ropa_trhy", ["ropa", "brent", "wti", "hormuz"]),
        ("koruna_kurzy", ["koruna", "eur czk", "usd czk", "cnb"]),
        ("obrana_nato", ["nato", "obrana", "vydaje na obranu", "hdp"]),
        ("verejnopravni_media", ["ceska televize", "cesky rozhlas", "poplatky"]),
    ]

    for hint, keywords in rules:
        if any(k in text for k in keywords):
            return hint

    words = text.split()
    important_words = [w for w in words if len(w) > 4]
    return "_".join(important_words[:4]) if important_words else "unknown"

def source_score(source):
    source_norm = normalize_text(source)

    for name, weight in SOURCE_WEIGHTS.items():
        if normalize_text(name) in source_norm:
            return weight

    return 1


def keyword_score(article):
    text = normalize_text(f"{article.get('title', '')} {article.get('summary', '')}")
    category = article.get("category_hint", "")
    keywords = CATEGORY_KEYWORDS.get(category, [])

    score = 0
    for keyword in keywords:
        if normalize_text(keyword) in text:
            score += 2

    return score


def freshness_score(article):
    try:
        published = datetime.fromisoformat(article["published"])
    except Exception:
        return 0

    now = datetime.now(timezone.utc)
    age_hours = (now - published).total_seconds() / 3600

    if age_hours <= 6:
        return 5
    if age_hours <= 12:
        return 4
    if age_hours <= 18:
        return 3
    if age_hours <= 24:
        return 2

    return 0


def detail_score(article):
    text = f"{article.get('title', '')} {article.get('summary', '')}"

    score = 0

    # čísla, procenta, skóre, ceny
    if re.search(r"\d", text):
        score += 2

    # sportovní skóre typu 3:1 nebo 27:23
    if re.search(r"\b\d+:\d+\b", text):
        score += 3

    # procenta
    if "%" in text or "procent" in text:
        score += 2

    # měny / komodity
    if any(x in text.lower() for x in ["korun", "dolar", "eur", "usd", "ropa", "brent", "zlato"]):
        score += 2

    # rozumná délka perexu = více kontextu
    summary_len = len(article.get("summary", ""))
    if summary_len > 120:
        score += 1
    if summary_len > 250:
        score += 1

    return score


def article_score(article):
    score = 0

    score += source_score(article.get("source", ""))
    score += keyword_score(article)
    score += freshness_score(article)
    score += detail_score(article)

    return score

def deduplicate_articles(articles, similarity_threshold=0.72):
    unique = []
    seen_event_hints = set()

    for article in articles:
        article["event_hint"] = build_event_hint(article)

        title_norm = normalize_text(article.get("title", ""))
        summary_norm = normalize_text(article.get("summary", ""))
        combined_norm = f"{title_norm} {summary_norm}"

        duplicate = False

        # Tvrdší deduplikace pro stejné event_hint v rámci stejné rubriky
        event_key = (article.get("category_hint"), article.get("event_hint"))
        if event_key in seen_event_hints and article["event_hint"] != "unknown":
            duplicate = True

        # Měkčí deduplikace podle podobnosti textu
        if not duplicate:
            for existing in unique:
                existing_norm = normalize_text(
                    f"{existing.get('title', '')} {existing.get('summary', '')}"
                )

                same_category = article.get("category_hint") == existing.get("category_hint")
                similar = similarity(combined_norm, existing_norm) >= similarity_threshold

                if same_category and similar:
                    duplicate = True
                    break

        if not duplicate:
            unique.append(article)
            seen_event_hints.add(event_key)

    return unique

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
                    "url": link,
                })

    # Přidání skóre
    for article in articles:
        article["score"] = article_score(article)

    # Nejprve seřadit podle skóre, aby deduplikace nechala silnější článek
    articles.sort(
        key=lambda x: (x.get("score", 0), x["published"]),
        reverse=True
    )

    # Odstranění duplicit
    articles = deduplicate_articles(articles)

    # Finální řazení
    articles.sort(
        key=lambda x: (x.get("score", 0), x["published"]),
        reverse=True
    )

    return articles[:MAX_ARTICLES]


def build_user_prompt(articles, fx_rates):
    lines = [
        "Použij VÝHRADNĚ následující články jako zdroj.",
        "Nevytvářej žádné zprávy mimo tento seznam.",
        "Pokud více článků sdílí stejný Event hint nebo popisuje stejnou událost, sluč je do jedné zprávy.",
        "Při výběru témat přihlížej ke Score: vyšší Score znamená vyšší pravděpodobnou redakční důležitost.",
        "Nevkládej do výstupu žádné URL odkazy.",
        "Neuváděj řádek začínající „Odkaz:“.",
        "Pokud pro některou rubriku není dost ověřených aktuálních článků, napiš to stručně místo vymýšlení.",
        "",
                "",
        "KURZOVÝ SERVIS — POVINNĚ POUŽIJ V RUBRICE FINANCE:",
        f"EUR/CZK: {fx_rates['EUR']['rate'] if fx_rates.get('EUR') else 'není dostupné'}",
        f"USD/CZK: {fx_rates['USD']['rate'] if fx_rates.get('USD') else 'není dostupné'}",
        "Zdroj: Česká národní banka",
        "",
        "ČLÁNKY:",
    ]

    for i, article in enumerate(articles, start=1):
        lines.append(
            f"{i}. [{article['category_hint']}] {article['title']}\n"
            f"   Zdroj: {article['source']}\n"
            f"   Publikováno: {article['published']}\n"
            f"   Score: {article.get('score', 0)}\n"
            f"   Event hint: {article.get('event_hint', 'unknown')}\n"
            f"   Shrnutí: {article['summary']}\n"
            f"   Interní URL: {article['url']}"
        )

    return "\n".join(lines)


def generate_category(category, articles, fx_rates):
    client = OpenAI()

    category_articles = [
        a for a in articles if a["category_hint"] == category
    ]

    if not category_articles:
        return f"{category}\nPro tuto rubriku není dostatek ověřených aktuálních zpráv.\n"

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # jednoduchý prompt jen pro jednu rubriku
    lines = [
        f"Vytvoř rubriku: {category}",
        "",
        "Použij pouze tyto články.",
        "Nevytvářej nic mimo ně.",
        "Maximálně 5 zpráv.",
        "Každá zpráva má přesně 2 věty.",
        "První věta začíná faktem.",
        "Druhá věta začíná formulací: 'Důležité je, že' nebo 'Hlavní highlight je, že'.",
        "Začni názvem rubriky (např. 'Z domova') na samostatném řádku.",
        "Mezi názvem a první zprávou vlož prázdný řádek.",
        "Každou zprávu odděl novým řádkem.",
        "Nevypisuj URL.",
        "",
    ]

    # kurzový servis jen pro finance
    if category == "Finance":
        lines.append("POVINNĚ ZAČNI BLOKEM:")
        lines.append("Kurzový servis")
        lines.append(f"EUR/CZK: {fx_rates['EUR']['rate']}")
        lines.append(f"USD/CZK: {fx_rates['USD']['rate']}")
        lines.append("Zdroj: Česká národní banka")
        lines.append("")

    lines.append("ČLÁNKY:")

    for i, article in enumerate(category_articles[:30], 1):
        lines.append(
            f"{i}. {article['title']} | {article['summary']} | Score:{article['score']}"
        )

    user_prompt = "\n".join(lines)

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
    fx_rates = get_cnb_fx_rates()

    if not articles:
        briefing = "RANNÍ BRIEFING\n\nNepodařilo se najít žádné aktuální články za posledních 24 hodin."
    else:
        sections = []

        for category in BRIEFING_CATEGORIES:
            section = generate_category(category, articles, fx_rates)
            sections.append(section)

        briefing = "RANNÍ BRIEFING\n\n" + "\n\n".join(sections)

    OUTPUT_HTML.write_text(
        render_html(briefing, len(articles)),
        encoding="utf-8"
    )

    print(f"Generated {OUTPUT_HTML} from {len(articles)} articles.")


if __name__ == "__main__":
    main()
