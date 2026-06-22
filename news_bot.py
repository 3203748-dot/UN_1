#!/usr/bin/env python3
"""
Telegram News Bot — Політика / Україна
Збирає новини за останні 6 годин, аналізує через Claude API,
надсилає preview адміну на погодження, потім публікує в канал.
"""

import os
import re
import json
import html
import logging
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Завантажуємо config.env якщо є (локальний запуск), в GitHub Actions — змінні середовища
_env_path = os.path.join(os.path.dirname(__file__), "config.env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)

# ── Налаштування ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID     = os.getenv("ADMIN_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
PIXABAY_API_KEY   = os.getenv("PIXABAY_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")

HOURS_BACK       = 6
TOP_N            = 5
APPROVAL_TIMEOUT = 30 * 60
PUBLISHED_LOG    = os.path.join(os.path.dirname(__file__), "published.json")
LOG_KEEP_DAYS    = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── RSS-джерела ───────────────────────────────────────────────────────────────
RSS_FEEDS = [
    ("Укрінформ",         "https://www.ukrinform.ua/rss/block-lastnews"),
    ("Радіо Свобода",     "https://www.radiosvoboda.org/api/zymqdmspry"),
    ("Суспільне",         "https://suspilne.media/rss/ukraine.rss"),
    ("LB.ua",             "https://lb.ua/rss/ukraine.xml"),
    ("Babel",             "https://babel.ua/rss"),
    ("Українська правда", "https://www.pravda.com.ua/rss/view_news/"),
    ("УНІАН",             "https://rss.unian.net/site/news_ukr.rss"),
    ("Дзеркало тижня",   "https://zn.ua/rss/politics.html"),
    ("NV",                "https://nv.ua/rss/ukraine.xml"),
    ("Громадське",        "https://hromadske.ua/rss"),
    ("24 канал",          "https://24tv.ua/rss/all.xml"),
]

BASE_TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ── Журнал опублікованих новин ───────────────────────────────────────────────
def load_published() -> set:
    if not os.path.exists(PUBLISHED_LOG):
        return set()
    try:
        with open(PUBLISHED_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = datetime.now(timezone.utc).timestamp() - LOG_KEEP_DAYS * 86400
        return {k for k, ts in data.items() if ts > cutoff}
    except Exception:
        return set()

def save_published(published: set, keys: list):
    data = {}
    if os.path.exists(PUBLISHED_LOG):
        try:
            with open(PUBLISHED_LOG, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    cutoff = datetime.now(timezone.utc).timestamp() - LOG_KEEP_DAYS * 86400
    data = {k: ts for k, ts in data.items() if ts > cutoff}
    now = datetime.now(timezone.utc).timestamp()
    for key in keys:
        if key:
            data[key] = now
    with open(PUBLISHED_LOG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def article_key(article: dict) -> str:
    return article.get("link") or article.get("title", "")


# ── Витяг зображення з RSS-запису ────────────────────────────────────────────
def extract_image(entry) -> str | None:
    for m in entry.get("media_content", []):
        url = m.get("url", "")
        if url and any(url.lower().endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
            return url
    for m in entry.get("media_thumbnail", []):
        url = m.get("url", "")
        if url:
            return url
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url")
    for field in ("summary", "content"):
        text = ""
        if field == "content":
            cl = entry.get("content", [])
            text = cl[0].get("value", "") if cl else ""
        else:
            text = entry.get("summary", "")
        m = re.search(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', text, re.I)
        if m:
            return m.group(1)
    return None


# ── Витяг og:image зі сторінки статті ────────────────────────────────────────
def fetch_og_image(url: str) -> str | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            resp.text, re.I
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            resp.text, re.I
        )
        if match:
            return match.group(1)
    except Exception as e:
        log.warning(f"og:image error [{url[:60]}]: {e}")
    return None


# ── Збір новин ────────────────────────────────────────────────────────────────
def fetch_recent_news(hours_back: int = HOURS_BACK) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    articles = []
    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                pub = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                if pub and pub >= cutoff:
                    title   = html.unescape(entry.get("title", "")).strip()
                    summary = html.unescape(entry.get("summary", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", summary)[:400]
                    articles.append({
                        "source":  source_name,
                        "title":   title,
                        "summary": summary,
                        "link":    entry.get("link", ""),
                        "pub":     pub,
                        "image":   extract_image(entry),
                    })
        except Exception as e:
            log.warning(f"RSS error [{source_name}]: {e}")
    articles.sort(key=lambda x: x["pub"], reverse=True)
    log.info(f"Знайдено {len(articles)} новин за {hours_back} год.")
    return articles


# ── Claude API ────────────────────────────────────────────────────────────────
def analyze_with_claude(articles: list[dict], skip_topics: list[str] = []) -> dict | None:
    if not articles:
        return None

    candidates = articles[:TOP_N * 4]
    news_text = ""
    for i, a in enumerate(candidates, 1):
        news_text += f"{i}. [{a['source']}] {a['title']}\n   {a['summary']}\n\n"

    prompt = f"""Ти редактор українського Telegram-каналу про політику та Україну.

Ось нові статті за останні {HOURS_BACK} годин:

{news_text}

{"" if not skip_topics else f"Ці теми вже були опубліковані — НЕ повторюй їх:\\n" + "\\n".join(f"- {t}" for t in skip_topics[:10]) + "\\n\\n"}Завдання:
1. Проаналізуй ВСІ наведені новини і визнач ОДНУ найважливішу НОВУ подію.
2. Напиши авторський пост для Telegram українською мовою:
   - Заголовок: влучний, інтригуючий (до 10 слів), з емодзі на початку, обгорни його в *зірочки* для жирного тексту
   - Основний текст: 2 абзаци по 2-3 речення, розділені порожнім рядком (\\n\\n)
   - Перший абзац: суть події та контекст
   - Другий абзац: наслідки та оцінка
   - НЕ копіюй формулювання з джерел, пиши як журналіст-аналітик
   - БЕЗ будь-яких посилань, згадок джерел чи URL у тексті
   - Жодних "за даними ЗМІ", "як повідомляє" тощо

Відповідь СТРОГО у форматі JSON:
{{
  "post_text": "повний текст поста з емодзі, без посилань",
  "chosen_index": 1,
  "chosen_title": "заголовок обраної новини"
}}
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        match = re.search(r"\{[\s\S]+\}", raw)
        if not match:
            log.error(f"Claude не повернув JSON: {raw[:300]}")
            return None
        return json.loads(match.group())
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return None


# ── Пошук резервного зображення ──────────────────────────────────────────────
def get_fallback_image(keywords: list[str]) -> str | None:
    if not keywords:
        return None
    query = " ".join(keywords[:2])
    if PIXABAY_API_KEY:
        try:
            resp = requests.get(
                "https://pixabay.com/api/",
                params={"key": PIXABAY_API_KEY, "q": query, "image_type": "photo",
                        "orientation": "horizontal", "min_width": 1280,
                        "safesearch": "true", "per_page": 5},
                timeout=10,
            )
            hits = resp.json().get("hits", [])
            if hits:
                return hits[0].get("largeImageURL")
        except Exception as e:
            log.warning(f"Pixabay error: {e}")
    if UNSPLASH_ACCESS_KEY:
        try:
            resp = requests.get(
                "https://api.unsplash.com/photos/random",
                params={"query": query, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                timeout=10,
            )
            return resp.json().get("urls", {}).get("regular")
        except Exception as e:
            log.warning(f"Unsplash error: {e}")
    return None


# ── Надіслати preview адміну з кнопками ──────────────────────────────────────
def send_preview(text: str, image_url: str | None, callback_data: str) -> int | None:
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Опублікувати", "callback_data": f"publish|{callback_data}"},
            {"text": "❌ Пропустити",   "callback_data": f"skip|{callback_data}"},
        ]]
    }
    header = "👁 *PREVIEW — очікує погодження*\n\n"

    if image_url:
        resp = requests.post(
            f"{BASE_TG}/sendPhoto",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "photo": image_url,
                "caption": header + text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
            timeout=20,
        )
    else:
        resp = requests.post(
            f"{BASE_TG}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": header + text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
            timeout=20,
        )

    if resp.status_code == 200:
        msg_id = resp.json()["result"]["message_id"]
        log.info(f"Preview надіслано адміну (msg_id={msg_id})")
        return msg_id
    else:
        log.error(f"Помилка надсилання preview: {resp.text[:200]}")
        return None


# ── Очікування рішення адміна ─────────────────────────────────────────────────
def wait_for_decision(callback_data: str, timeout: int = APPROVAL_TIMEOUT) -> str:
    log.info(f"Очікуємо рішення адміна (до {timeout // 60} хв)...")
    offset = None
    deadline = datetime.now(timezone.utc).timestamp() + timeout

    while datetime.now(timezone.utc).timestamp() < deadline:
        try:
            params = {"timeout": 30, "allowed_updates": ["callback_query"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{BASE_TG}/getUpdates", params=params, timeout=40)
            updates = resp.json().get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                if not cb:
                    continue
                data = cb.get("data", "")
                if callback_data not in data:
                    continue
                requests.post(f"{BASE_TG}/answerCallbackQuery",
                              json={"callback_query_id": cb["id"]}, timeout=5)
                action = data.split("|")[0]
                log.info(f"Рішення адміна: {action}")
                return action

        except Exception as e:
            log.warning(f"Polling error: {e}")

    log.warning("Час очікування вийшов — пост пропущено.")
    return "timeout"


# ── Публікація в канал ────────────────────────────────────────────────────────
def publish_to_channel(text: str, image_url: str | None = None) -> bool:
    if image_url:
        resp = requests.post(
            f"{BASE_TG}/sendPhoto",
            json={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url,
                  "caption": text, "parse_mode": "Markdown"},
            timeout=20,
        )
    else:
        resp = requests.post(
            f"{BASE_TG}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "Markdown"},
            timeout=20,
        )
    if resp.status_code == 200:
        log.info("✅ Пост опубліковано в канал!")
        return True
    log.error(f"Telegram error: {resp.text[:200]}")
    return False


# ── Повідомити адміна про результат ──────────────────────────────────────────
def notify_admin(text: str):
    requests.post(f"{BASE_TG}/sendMessage",
                  json={"chat_id": ADMIN_CHAT_ID, "text": text}, timeout=10)


# ── Головна функція ───────────────────────────────────────────────────────────
def main():
    log.info("=== News Bot запущено ===")

    missing = [k for k, v in {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "ADMIN_CHAT_ID": ADMIN_CHAT_ID,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    }.items() if not v]
    if missing:
        log.error(f"Відсутні змінні: {', '.join(missing)}. Перевір config.env")
        return

    # 1. Збір новин
    articles = fetch_recent_news()
    if not articles:
        log.warning("Новин не знайдено.")
        return

    published = load_published()
    articles = [a for a in articles if article_key(a) not in published]
    log.info(f"Після фільтру дублів: {len(articles)} нових новин.")
    if not articles:
        log.info("Усі новини вже були опубліковані. Пропускаємо.")
        return

    # 2. Аналіз через Claude
    result = analyze_with_claude(articles)
    if not result:
        log.error("Claude не зміг підготувати пост.")
        return

    post_text    = result.get("post_text", "")
    lines = post_text.split("\n", 1)
    title = lines[0].strip("* ")
    post_text = f"*{title}*" + ("\n" + lines[1] if len(lines) > 1 else "")
    chosen_index = result.get("chosen_index", 1) - 1
    candidates   = articles[:TOP_N * 4]
    log.info(f"Обрана новина: {result.get('chosen_title', '?')}")

    # 3. Зображення
    image_url = None
    chosen_article = candidates[chosen_index] if 0 <= chosen_index < len(candidates) else None

    if chosen_article:
        image_url = chosen_article.get("image")
        if not image_url:
            log.info("Фото в RSS відсутнє — беремо og:image зі сторінки...")
            image_url = fetch_og_image(chosen_article.get("link", ""))
        if image_url:
            log.info(f"Фото знайдено: {image_url[:80]}")

    if not image_url:
        log.info("og:image не знайдено — шукаємо резервне...")
        image_url = get_fallback_image(result.get("image_keywords", []))

    # 4. Preview адміну
    cb_key = str(int(datetime.now(timezone.utc).timestamp()))
    msg_id = send_preview(post_text, image_url, cb_key)
    if not msg_id:
        return

    # 5. Чекати рішення
    decision = wait_for_decision(cb_key)

    if decision == "publish":
        published_ok = publish_to_channel(post_text, image_url)
        if published_ok:
            all_keys = [article_key(a) for a in candidates]
            save_published(published, all_keys)
            notify_admin("✅ Пост опубліковано в канал!")
    elif decision == "skip":
        notify_admin("⏭ Пост пропущено.")
    else:
        notify_admin("⏰ Час очікування вийшов — пост не опубліковано.")


if __name__ == "__main__":
    main()
