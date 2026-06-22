#!/usr/bin/env python3
"""Ukraine News Bot — @UN_1_chanel"""

import os, re, json, html, time, logging, sys
import requests, feedparser
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# ─── Конфіг ───────────────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "config.env"))

TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL    = os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN      = os.getenv("ADMIN_CHAT_ID", "")
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")

TG = f"https://api.telegram.org/bot{TOKEN}"

DIR         = os.path.dirname(__file__)
STATE_FILE  = os.path.join(DIR, "state.json")
PUB_FILE    = os.path.join(DIR, "published.json")
LOG_FILE    = os.path.join(DIR, "posts_log.json")

HOURS_BACK      = 6
ACTIVE_INTERVAL = 90 * 60    # між постами в active режимі
SLOW_INTERVAL   = 3 * 60 * 60
ACTIVE_TIMEOUT  = 30 * 60    # скільки чекати рішення адміна
SLOW_TIMEOUT    = 90 * 60
POLL_MINUTES    = 20          # скільки хв чекати в поточному job
PUB_KEEP_DAYS   = 3           # скільки днів зберігати published URLs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout, force=True,
)
log = logging.getLogger(__name__)

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


# ─── Стан / файли ─────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def load_published():
    try:
        with open(PUB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = time.time() - PUB_KEEP_DAYS * 86400
        return {k: v for k, v in data.items() if v > cutoff}
    except Exception:
        return {}

def save_published(data):
    with open(PUB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def write_log(status, title, text="", image=""):
    try:
        entries = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
        entries.insert(0, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": status, "title": title,
            "text": text, "image": image,
        })
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries[:100], f, ensure_ascii=False, indent=2)
    except Exception as ex:
        log.warning(f"write_log: {ex}")


# ─── RSS / новини ─────────────────────────────────────────────────────────────

def extract_image(entry):
    for m in entry.get("media_content", []):
        u = m.get("url", "")
        if u and re.search(r"\.(jpg|jpeg|png|webp)($|\?)", u, re.I):
            return u
    for m in entry.get("media_thumbnail", []):
        u = m.get("url", "")
        if u:
            return u
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url")
    for field in ("summary", "content"):
        text = (entry.get("content") or [{}])[0].get("value", "") if field == "content" \
               else entry.get("summary", "")
        m = re.search(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', text, re.I)
        if m:
            return m.group(1)
    return None

def fetch_og_image(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pat, r.text, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def fetch_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    articles = []
    for src, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:30]:
                pub = None
                for attr in ("published_parsed", "updated_parsed"):
                    t = getattr(e, attr, None)
                    if t:
                        pub = datetime(*t[:6], tzinfo=timezone.utc)
                        break
                if pub and pub >= cutoff:
                    articles.append({
                        "source":  src,
                        "title":   html.unescape(e.get("title", "")).strip(),
                        "summary": re.sub(r"<[^>]+>", "", html.unescape(e.get("summary", ""))).strip()[:400],
                        "link":    e.get("link", ""),
                        "image":   extract_image(e),
                    })
        except Exception as ex:
            log.warning(f"RSS [{src}]: {ex}")
    log.info(f"RSS: {len(articles)} новин за {HOURS_BACK} год")
    return articles


# ─── Claude ───────────────────────────────────────────────────────────────────

def call_claude(articles, skip_topics):
    news_text = "\n".join(
        f"{i}. [{a['source']}] {a['title']}\n   {a['summary']}"
        for i, a in enumerate(articles, 1)
    )
    skip_block = ""
    if skip_topics:
        skip_block = (
            "ВЖЕ ПОКАЗАНІ НОВИНИ (ці статті або подібні до них вже потрапляли — НЕ ОБИРАЙ нічого про ті самі події чи людей):\n" +
            "\n".join(f"- {t}" for t in skip_topics[-40:]) +
            "\n\n"
        )

    prompt = f"""Ти редактор українського Telegram-каналу про політику та війну.

Нові статті за останні {HOURS_BACK} годин:
{news_text}

{skip_block}Завдання:
1. Обери ОДНУ найважливішу статтю, яка НЕ стосується подій або осіб зі списку "ВЖЕ ПОКАЗАНІ НОВИНИ".
   ПРАВИЛО: якщо будь-яка стаття зі списку і нова стаття — про одну й ту саму подію або людину (навіть різними словами, від різних джерел) — це ПОВТОР, не обирай її.
2. Напиши авторський пост українською мовою:
   - Рядок 1: заголовок з емодзі, обгорни в *зірочки* (до 10 слів)
   - Порожній рядок
   - Абзац 1 (2–3 речення): суть події та контекст
   - Порожній рядок
   - Абзац 2 (2–3 речення): наслідки та оцінка
   - БЕЗ посилань, БЕЗ згадок джерел, БЕЗ "за даними ЗМІ"

Відповідь ТІЛЬКИ у форматі JSON (без зайвого тексту):
{{"post_text": "...", "chosen_index": 1, "chosen_title": "заголовок обраної новини"}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=40,
        )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            log.error(f"Claude: no JSON: {raw[:200]}")
            return None
        return json.loads(m.group())
    except Exception as ex:
        log.error(f"Claude error: {ex}")
        return None


# ─── Telegram helpers ─────────────────────────────────────────────────────────

def tg_post(method, **kwargs):
    try:
        r = requests.post(f"{TG}/{method}", json=kwargs, timeout=20)
        return r.json()
    except Exception as ex:
        log.error(f"TG {method}: {ex}")
        return {"ok": False}

def send_preview(text, image_url, cb_key):
    kb = {"inline_keyboard": [[
        {"text": "✅ Опублікувати", "callback_data": f"publish|{cb_key}"},
        {"text": "❌ Пропустити",  "callback_data": f"skip|{cb_key}"},
    ]]}
    header = "⏳ PREVIEW — очікує погодження\n\n"
    if image_url:
        r = tg_post("sendPhoto", chat_id=ADMIN, photo=image_url,
                    caption=header + text, parse_mode="Markdown", reply_markup=kb)
    else:
        r = tg_post("sendMessage", chat_id=ADMIN, text=header + text,
                    parse_mode="Markdown", reply_markup=kb)
    if r.get("ok"):
        msg_id = r["result"]["message_id"]
        log.info(f"Preview надіслано (msg_id={msg_id})")
        return msg_id
    log.error(f"send_preview failed: {r}")
    return None

def publish_to_channel(text, image_url):
    if image_url:
        r = tg_post("sendPhoto", chat_id=CHANNEL, photo=image_url,
                    caption=text, parse_mode="Markdown")
    else:
        r = tg_post("sendMessage", chat_id=CHANNEL, text=text, parse_mode="Markdown")
    ok = r.get("ok", False)
    log.info(f"publish_to_channel: ok={ok}")
    return ok

def notify_admin(msg):
    tg_post("sendMessage", chat_id=ADMIN, text=msg)

def get_updates(offset=None, long_poll_secs=0):
    params = {"timeout": long_poll_secs, "allowed_updates": ["callback_query"]}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{TG}/getUpdates", params=params,
                         timeout=long_poll_secs + 10)
        data = r.json()
        if not data.get("ok"):
            log.error(f"getUpdates not ok: {data.get('description', data)}")
            return []
        return data.get("result", [])
    except Exception as ex:
        log.warning(f"getUpdates: {ex}")
        return []

def answer_callback(cb_id):
    try:
        requests.post(f"{TG}/answerCallbackQuery",
                      json={"callback_query_id": cb_id}, timeout=5)
    except Exception:
        pass


# ─── Polling ──────────────────────────────────────────────────────────────────

def poll_for_decision(cb_key, minutes):
    """
    Polls Telegram getUpdates для cb_key протягом `minutes` хвилин.
    Повертає 'publish', 'skip', або None (таймаут).
    """
    offset = None
    deadline = time.time() + minutes * 60
    log.info(f"Polling {minutes} хв для cb_key={cb_key!r}")

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        wait = min(20, remaining)
        if wait <= 0:
            break

        updates = get_updates(offset=offset, long_poll_secs=wait)

        for upd in updates:
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if not cb:
                continue
            data = cb.get("data", "")
            answer_callback(cb["id"])
            log.info(f"Callback отримано: {data!r}")
            if cb_key in data:
                action = data.split("|")[0]
                log.info(f"Рішення: {action}")
                return action

    log.info("Polling закінчився без рішення")
    return None


# ─── Pending actions ──────────────────────────────────────────────────────────

def do_publish(state):
    text  = state.get("pending_post_text", "")
    image = state.get("pending_image_url")
    title = state.get("pending_title", "")
    ok = publish_to_channel(text, image)
    if ok:
        write_log("published", title, text, image or "")
        topics = state.get("published_topics", [])
        if title and title not in topics:
            topics.insert(0, title)
            state["published_topics"] = topics[:20]
        state["mode"] = "active"
        state["last_approved"] = time.time()
        notify_admin("✅ Пост опубліковано в канал!")
    else:
        notify_admin("⚠️ Помилка публікації — перевір логи")
    clear_pending(state)
    save_state(state)

def do_skip(state):
    title = state.get("pending_title", "")
    write_log("skipped", title)
    # Зберігаємо тему навіть при пропуску — щоб не повторювалась
    if title:
        topics = state.get("published_topics", [])
        if title not in topics:
            topics.insert(0, title)
            state["published_topics"] = topics[:20]
    notify_admin("❌ Пост пропущено.")
    clear_pending(state)
    save_state(state)

def clear_pending(state):
    for k in ("pending_cb_key", "pending_post_text", "pending_image_url",
              "pending_title", "pending_sent_at"):
        state.pop(k, None)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 50)
    log.info("Bot start")

    # Перевірка змінних
    for name, val in [("TELEGRAM_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHANNEL),
                      ("ADMIN_CHAT_ID", ADMIN), ("ANTHROPIC_API_KEY", CLAUDE_KEY)]:
        if not val:
            log.error(f"Відсутня змінна: {name}")
            return

    # ── 1. Видаляємо webhook ────────────────────────────────────────────────
    # Якщо webhook активний — getUpdates завжди повертає помилку 409
    # і callbacks від кнопок ніколи не доходять до бота
    try:
        wh = requests.get(f"{TG}/getWebhookInfo", timeout=10).json()
        wh_url = wh.get("result", {}).get("url", "")
        if wh_url:
            log.warning(f"Знайдено активний webhook: {wh_url} — видаляємо!")
            requests.post(f"{TG}/deleteWebhook",
                          json={"drop_pending_updates": False}, timeout=10)
            log.info("Webhook видалено")
        else:
            log.info("Webhook відсутній — OK")
    except Exception as ex:
        log.warning(f"Webhook check error: {ex}")

    state = load_state()
    now   = time.time()

    # ── 2. Є незавершений pending preview? ─────────────────────────────────
    pending_cb = state.get("pending_cb_key")
    if pending_cb:
        sent_at = state.get("pending_sent_at", 0)
        mode    = state.get("mode", "active")
        timeout = ACTIVE_TIMEOUT if mode == "active" else SLOW_TIMEOUT
        elapsed = now - sent_at

        log.info(f"Pending preview: {pending_cb} ({elapsed/60:.0f} хв тому)")

        # Швидка перевірка — можливо кнопку вже натиснули після попереднього run
        decision = poll_for_decision(pending_cb, minutes=1)

        if decision == "publish":
            do_publish(state)
        elif decision == "skip":
            do_skip(state)
        elif elapsed > timeout:
            log.info(f"Таймаут {timeout//60} хв — переходимо в slow режим")
            write_log("timeout", state.get("pending_title", ""))
            state["mode"] = "slow"
            clear_pending(state)
            save_state(state)
        else:
            log.info(f"Ще очікуємо рішення (залишилось {(timeout-elapsed)/60:.0f} хв)")
            save_state(state)

        return  # поки є pending — нову новину не генеруємо

    # ── 3. Перевіряємо інтервал ─────────────────────────────────────────────
    mode     = state.get("mode", "active")
    interval = ACTIVE_INTERVAL if mode == "active" else SLOW_INTERVAL
    last_sent = state.get("last_sent", 0)
    elapsed   = now - last_sent

    if elapsed < interval:
        log.info(f"Не час. Режим={mode}, залишилось {(interval-elapsed)/60:.0f} хв")
        return

    # ── 4. Отримуємо новини ─────────────────────────────────────────────────
    articles = fetch_news()
    if not articles:
        log.warning("Новин не знайдено")
        return

    pub_data    = load_published()
    new_articles = [a for a in articles if a.get("link") and a["link"] not in pub_data]
    log.info(f"Після URL-дедуп: {len(new_articles)} нових статей")

    if not new_articles:
        log.info("Всі свіжі новини вже опубліковані")
        return

    # ── 5. Claude: обирає і пише пост ───────────────────────────────────────
    skip_topics = state.get("published_topics", [])
    result = call_claude(new_articles[:40], skip_topics)
    if not result:
        log.error("Claude не повернув результат")
        return

    post_text    = result.get("post_text", "")
    chosen_index = result.get("chosen_index", 1) - 1
    chosen_title = result.get("chosen_title", "")
    log.info(f"Обрано: {chosen_title}")

    # Перший рядок — заголовок, завжди жирний
    lines = post_text.split("\n", 1)
    title_clean = lines[0].strip().strip("*").strip()
    post_text = f"*{title_clean}*" + ("\n" + lines[1] if len(lines) > 1 else "")

    # ── 6. Фото ─────────────────────────────────────────────────────────────
    image_url = None
    candidates = new_articles[:40]
    if 0 <= chosen_index < len(candidates):
        image_url = candidates[chosen_index].get("image") or \
                    fetch_og_image(candidates[chosen_index].get("link"))

    # ── 7. Надсилаємо preview адміну ────────────────────────────────────────
    cb_key = str(int(now))
    msg_id = send_preview(post_text, image_url, cb_key)
    if not msg_id:
        log.error("send_preview failed")
        return

    # ── 8. Зберігаємо стан ДО polling ───────────────────────────────────────
    # (щоб при kill/timeout новини не повторювались)
    for a in candidates:
        if a.get("link"):
            pub_data[a["link"]] = now
    save_published(pub_data)

    # Зберігаємо заголовки ВСІХ кандидатних статей у skip-список
    # щоб наступного разу Claude не вибрав ту саму подію від іншого джерела
    all_titles = [a["title"] for a in candidates if a.get("title")]
    topics = state.get("published_topics", [])
    for t in all_titles:
        if t not in topics:
            topics.append(t)
    state["published_topics"] = topics[-40:]  # зберігаємо останні 40

    state.update({
        "last_sent":          now,
        "pending_cb_key":     cb_key,
        "pending_post_text":  post_text,
        "pending_image_url":  image_url,
        "pending_title":      chosen_title,
        "pending_sent_at":    now,
    })
    save_state(state)

    # ── 9. Чекаємо рішення 20 хв ────────────────────────────────────────────
    decision = poll_for_decision(cb_key, minutes=POLL_MINUTES)

    if decision == "publish":
        do_publish(state)
    elif decision == "skip":
        do_skip(state)
    else:
        log.info(f"Рішення не прийнято за {POLL_MINUTES} хв. "
                 "State збережено — наступний run перевірить.")


if __name__ == "__main__":
    main()
