#!/usr/bin/env python3
"""Telegram News Bot - Polityka / Ukraina"""

import os
import re
import json
import html
import logging
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), "config.env")
if os.path.exists(env_path):
    load_dotenv(env_path)

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID      = os.getenv("ADMIN_CHAT_ID")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
PIXABAY_API_KEY    = os.getenv("PIXABAY_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")

HOURS_BACK      = 6
TOP_N           = 5
PUBLISHED_LOG   = os.path.join(os.path.dirname(__file__), "published.json")
POSTS_LOG       = os.path.join(os.path.dirname(__file__), "posts_log.json")
STATE_FILE      = os.path.join(os.path.dirname(__file__), "state.json")
LOG_KEEP_DAYS   = 2

# Режими: active = 90хв інтервал / 30хв таймаут; slow = 3год / 90хв
MODE_ACTIVE_INTERVAL = 90 * 60
MODE_ACTIVE_TIMEOUT  = 30 * 60
MODE_SLOW_INTERVAL   = 3 * 60 * 60
MODE_SLOW_TIMEOUT    = 90 * 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_TG = "https://api.telegram.org/bot" + (TELEGRAM_TOKEN or "")

RSS_FEEDS = [
    ("Ukrinform",        "https://www.ukrinform.ua/rss/block-lastnews"),
    ("Radio Svoboda",    "https://www.radiosvoboda.org/api/zymqdmspry"),
    ("Suspilne",         "https://suspilne.media/rss/ukraine.rss"),
    ("LB.ua",            "https://lb.ua/rss/ukraine.xml"),
    ("Babel",            "https://babel.ua/rss"),
    ("Ukrainska Pravda", "https://www.pravda.com.ua/rss/view_news/"),
    ("UNIAN",            "https://rss.unian.net/site/news_ukr.rss"),
    ("Dzerkalo Tyzhnia", "https://zn.ua/rss/politics.html"),
    ("NV",               "https://nv.ua/rss/ukraine.xml"),
    ("Hromadske",        "https://hromadske.ua/rss"),
    ("24 Kanal",         "https://24tv.ua/rss/all.xml"),
]


def load_state():
    default = {"mode": "active", "last_sent": None, "last_approved": None}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except Exception:
        return default


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_published():
    if not os.path.exists(PUBLISHED_LOG):
        return set()
    try:
        with open(PUBLISHED_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = datetime.now(timezone.utc).timestamp() - LOG_KEEP_DAYS * 86400
        return {k for k, ts in data.items() if ts > cutoff}
    except Exception:
        return set()


def save_published(published, keys):
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


def save_post_log(status: str, title: str, text: str, image_url: str | None):
    """Зберігає запис у журнал постів (posts_log.json)."""
    try:
        log_data = []
        if os.path.exists(POSTS_LOG):
            with open(POSTS_LOG, "r", encoding="utf-8") as f:
                log_data = json.load(f)
        log_data.insert(0, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": status,   # "published" / "skipped" / "timeout"
            "title": title,
            "text": text,
            "image": image_url or "",
        })
        log_data = log_data[:100]  # зберігаємо останні 100
        with open(POSTS_LOG, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Помилка запису журналу: {e}")


def article_key(article):
    return article.get("link") or article.get("title", "")


def extract_image(entry):
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
        if field == "content":
            cl = entry.get("content", [])
            text = cl[0].get("value", "") if cl else ""
        else:
            text = entry.get("summary", "")
        m = re.search(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', text, re.I)
        if m:
            return m.group(1)
    return None


def fetch_og_image(url):
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
        log.warning("og:image error [%s]: %s", url[:60], e)
    return None


def fetch_recent_news(hours_back=HOURS_BACK):
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
            log.warning("RSS error [%s]: %s", source_name, e)
    articles.sort(key=lambda x: x["pub"], reverse=True)
    log.info(f"Знайдено {len(articles)} новин за {hours_back} год.")
    return articles


def build_prompt(news_text, skip_topics):
    lines = [
        "Ty redaktor ukrayinskoho Telegram-kanalu pro polityku ta Ukrayinu.",
        "",
        "Ось нові статті за останні " + str(HOURS_BACK) + " годин:",
        "",
        news_text,
    ]
    if skip_topics:
        lines.append("Ці теми вже були опубліковані — НЕ повторюй їх:")
        for t in skip_topics[:10]:
            lines.append("- " + t)
        lines.append("")
    lines += [
        "Завдання:",
        "1. Проаналізуй ВСІ наведені новини і визнач ОДНУ найважливішу НОВУ подію.",
        "2. Напиши авторський пост для Telegram українською мовою:",
        "   - Заголовок: влучний, інтригуючий (до 10 слів), з емодзі на початку, обгорни його в *зірочки*",
        "   - Основний текст: 2 абзаци по 2-3 речення, розділені порожнім рядком",
        "   - Перший абзац: суть події та контекст",
        "   - Другий абзац: наслідки та оцінка",
        "   - НЕ копіюй формулювання з джерел, пиши як журналіст-аналітик",
        "   - БЕЗ будь-яких посилань, згадок джерел чи URL у тексті",
        '   - Жодних "за даними ЗМІ", "як повідомляє" тощо',
        "",
        "Відповідь СТРОГО у форматі JSON:",
        '{"post_text": "повний текст поста з емодзі, без посилань", "chosen_index": 1, "chosen_title": "заголовок обраної новини"}',
    ]
    return "\n".join(lines)


def analyze_with_claude(articles, skip_topics=None):
    if not articles:
        return None
    if skip_topics is None:
        skip_topics = []

    candidates = articles[:TOP_N * 4]
    news_text = ""
    for i, a in enumerate(candidates, 1):
        news_text += str(i) + ". [" + a["source"] + "] " + a["title"] + "\n   " + a["summary"] + "\n\n"

    prompt = build_prompt(news_text, skip_topics)

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
            log.error("Claude ne povernuv JSON: %s", raw[:300])
            return None
        return json.loads(match.group())
    except Exception as e:
        log.error("Claude API error: %s", e)
        return None


def get_fallback_image(keywords):
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
            log.warning("Pixabay error: %s", e)
    if UNSPLASH_ACCESS_KEY:
        try:
            resp = requests.get(
                "https://api.unsplash.com/photos/random",
                params={"query": query, "orientation": "landscape"},
                headers={"Authorization": "Client-ID " + UNSPLASH_ACCESS_KEY},
                timeout=10,
            )
            return resp.json().get("urls", {}).get("regular")
        except Exception as e:
            log.warning("Unsplash error: %s", e)
    return None


def send_preview(text, image_url, callback_data):
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Опублікувати", "callback_data": "publish|" + callback_data},
            {"text": "❌ Пропустити",  "callback_data": "skip|" + callback_data},
        ]]
    }
    header = "PREVIEW - ochikuye pohodzhennya\n\n"
    if image_url:
        resp = requests.post(
            BASE_TG + "/sendPhoto",
            json={"chat_id": ADMIN_CHAT_ID, "photo": image_url,
                  "caption": header + text, "parse_mode": "Markdown",
                  "reply_markup": keyboard},
            timeout=20,
        )
    else:
        resp = requests.post(
            BASE_TG + "/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": header + text,
                  "parse_mode": "Markdown", "reply_markup": keyboard},
            timeout=20,
        )
    if resp.status_code == 200:
        msg_id = resp.json()["result"]["message_id"]
        log.info(f"Preview надіслано адміну (msg_id={msg_id})")
        return msg_id
    log.error("Pomylka nadislannya preview: %s", resp.text[:200])
    return None


def delete_message(msg_id):
    try:
        requests.post(BASE_TG + "/deleteMessage",
                      json={"chat_id": ADMIN_CHAT_ID, "message_id": msg_id}, timeout=10)
    except Exception:
        pass


def check_pending_decision(cb_key: str, state: dict):
    """Перевіряє getUpdates ОДИН РАЗ. Зберігає offset в state. Повертає 'publish'/'skip'/None."""
    try:
        params = {"timeout": 0, "allowed_updates": ["callback_query"]}
        offset = state.get("tg_offset")
        if offset:
            params["offset"] = offset

        resp = requests.get(BASE_TG + "/getUpdates", params=params, timeout=10)
        updates = resp.json().get("result", [])

        result = None
        for upd in updates:
            # Зсуваємо offset вперед для кожного оновлення
            state["tg_offset"] = upd["update_id"] + 1

            cb = upd.get("callback_query")
            if not cb:
                continue
            data = cb.get("data", "")

            # Відповідаємо на callback щоб кнопка не "крутилась"
            requests.post(BASE_TG + "/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}, timeout=5)

            if cb_key in data and result is None:
                result = data.split("|")[0]

        return result
    except Exception as e:
        log.warning(f"Помилка перевірки callback: {e}")
    return None


def publish_to_channel(text, image_url=None):
    if image_url:
        resp = requests.post(
            BASE_TG + "/sendPhoto",
            json={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url,
                  "caption": text, "parse_mode": "Markdown"},
            timeout=20,
        )
    else:
        resp = requests.post(
            BASE_TG + "/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "Markdown"},
            timeout=20,
        )
    if resp.status_code == 200:
        log.info("Пост опубліковано в канал!")
        return True
    log.error("Telegram error: %s", resp.text[:200])
    return False


def notify_admin(text):
    requests.post(BASE_TG + "/sendMessage",
                  json={"chat_id": ADMIN_CHAT_ID, "text": text}, timeout=10)


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

    state   = load_state()
    now     = datetime.now(timezone.utc).timestamp()
    mode    = state.get("mode", "active")
    timeout = MODE_ACTIVE_TIMEOUT if mode == "active" else MODE_SLOW_TIMEOUT
    interval = MODE_ACTIVE_INTERVAL if mode == "active" else MODE_SLOW_INTERVAL

    # ── ФАЗА 1: є pending preview — перевіряємо чи є рішення ────────────────
    pending = state.get("pending")
    if pending:
        cb_key   = pending["cb_key"]
        decision = check_pending_decision(cb_key, state)
        sent_at  = pending.get("sent_at", 0)
        post_text    = pending["post_text"]
        image_url    = pending.get("image_url")
        chosen_title = pending.get("chosen_title", "")
        candidate_keys = pending.get("candidate_keys", [])
        published    = load_published()

        if decision == "publish":
            published_ok = publish_to_channel(post_text, image_url)
            if published_ok:
                save_published(published, candidate_keys)
                save_post_log("published", chosen_title, post_text, image_url)
                topics = state.get("published_topics", [])
                if chosen_title and chosen_title not in topics:
                    topics.insert(0, chosen_title)
                    state["published_topics"] = topics[:20]
                state["mode"] = "active"
                state["last_approved"] = now
                notify_admin("✅ Пост опубліковано в канал!")
            state.pop("pending", None)
            save_state(state)

        elif decision == "skip":
            save_published(published, candidate_keys)
            save_post_log("skipped", chosen_title, post_text, image_url)
            notify_admin("❌ Пост пропущено.")
            state.pop("pending", None)
            save_state(state)

        elif now - sent_at > timeout:
            # Таймаут — ніхто не відповів
            save_published(published, candidate_keys)
            save_post_log("timeout", chosen_title, post_text, image_url)
            state["mode"] = "slow"
            state.pop("pending", None)
            save_state(state)
            log.info("Таймаут. Режим → slow.")
        else:
            log.info(f"Очікуємо рішення адміна ({(timeout - (now - sent_at)) / 60:.0f} хв залишилось).")
            save_state(state)  # зберігаємо оновлений tg_offset

        return  # завжди виходимо після обробки pending

    # ── ФАЗА 2: немає pending — чи час надсилати нове preview? ──────────────
    last_sent = state.get("last_sent") or 0
    elapsed = now - last_sent
    if elapsed < interval:
        log.info(f"Ще не час. Режим={mode}, залишилось {(interval - elapsed) / 60:.0f} хв.")
        return

    articles = fetch_recent_news()
    if not articles:
        log.warning("Новин не знайдено.")
        return

    published = load_published()
    articles = [a for a in articles if article_key(a) not in published]
    log.info(f"Після фільтру дублів: {len(articles)} нових новин.")
    if not articles:
        log.info("Усі новини вже були опубліковані.")
        return

    skip_topics = state.get("published_topics", [])
    result = analyze_with_claude(articles, skip_topics)
    if not result:
        log.error("Claude не зміг підготувати пост.")
        return

    post_text = result.get("post_text", "")
    lines = post_text.split("\n", 1)
    title = lines[0].strip("* ")
    post_text = "*" + title + "*" + ("\n" + lines[1] if len(lines) > 1 else "")

    chosen_index = result.get("chosen_index", 1) - 1
    candidates   = articles[:TOP_N * 4]
    chosen_title = result.get("chosen_title", "")
    log.info(f"Обрана новина: {chosen_title}")

    image_url = None
    chosen_article = candidates[chosen_index] if 0 <= chosen_index < len(candidates) else None
    if chosen_article:
        image_url = chosen_article.get("image")
        if not image_url:
            image_url = fetch_og_image(chosen_article.get("link", ""))
    if not image_url:
        image_url = get_fallback_image(result.get("image_keywords", []))

    cb_key = str(int(now))
    msg_id = send_preview(post_text, image_url, cb_key)
    if not msg_id:
        return

    # Зберігаємо кандидатів одразу — щоб не повторювались
    save_published(published, [article_key(a) for a in candidates])

    # Зберігаємо pending стан і виходимо
    state["last_sent"] = now
    state["pending"] = {
        "cb_key":        cb_key,
        "msg_id":        msg_id,
        "post_text":     post_text,
        "image_url":     image_url,
        "chosen_title":  chosen_title,
        "sent_at":       now,
        "candidate_keys": [article_key(a) for a in candidates],
    }
    save_state(state)
    log.info("Preview надіслано. Чекаємо рішення на наступному запуску.")


if __name__ == "__main__":
    main()
