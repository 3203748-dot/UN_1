#!/usr/bin/env python3
"""Ukraine News Bot — @UN_1_channel"""

import os, re, json, html, time, logging, sys
import requests, feedparser
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "config.env"))

# ─── Config ───────────────────────────────────────────────────────────────────
TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL    = os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN      = os.getenv("ADMIN_CHAT_ID", "")
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")

TG = f"https://api.telegram.org/bot{TOKEN}"

DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "state.json")
PUB_FILE   = os.path.join(DIR, "published.json")
LOG_FILE   = os.path.join(DIR, "posts_log.json")

ACTIVE_INTERVAL = 90 * 60      # 90 хв між постами (active)
SLOW_INTERVAL   = 3 * 60 * 60  # 3 год (slow — коли адмін не відповідає)
POLL_MINUTES    = 20            # скільки хв чекати рішення адміна
PUB_KEEP_DAYS   = 3             # зберігати URL у published.json N днів
HOURS_BACK      = 6             # брати новини за останні N год

# Manual run = workflow_dispatch (ігнорує інтервал та pending)
MANUAL_RUN = os.getenv("MANUAL_RUN", "").lower() == "true"

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


# ─── State / Files ────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"mode": "active", "last_sent": 0, "recent_events": []}

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def load_pub():
    """Завантажує published.json, видаляє старіші за PUB_KEEP_DAYS."""
    try:
        with open(PUB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = time.time() - PUB_KEEP_DAYS * 86400
        return {k: v for k, v in data.items() if v > cutoff}
    except Exception:
        return {}

def save_pub(data):
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


# ─── RSS / News ───────────────────────────────────────────────────────────────

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
    for field in ("content", "summary"):
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
                        "summary": re.sub(r"<[^>]+>", "", html.unescape(
                                   e.get("summary", ""))).strip()[:400],
                        "link":    e.get("link", ""),
                        "image":   extract_image(e),
                    })
        except Exception as ex:
            log.warning(f"RSS [{src}]: {ex}")
    log.info(f"RSS: {len(articles)} новин за {HOURS_BACK} год")
    return articles


# ─── Claude ───────────────────────────────────────────────────────────────────

def call_claude(articles, recent_events):
    news_text = "\n".join(
        f"{i}. [{a['source']}] {a['title']}\n   {a['summary']}"
        for i, a in enumerate(articles, 1)
    )
    skip_block = ""
    if recent_events:
        skip_block = (
            "ЦІ ТЕМИ ВЖЕ ВИСВІТЛЕНІ — обери ІНШУ подію:\n" +
            "\n".join(f"[{i+1}] {s}" for i, s in enumerate(recent_events)) +
            "\n\nВАЖЛИВО: якщо стаття про ту саму людину, але ІНША подія — можна взяти. "
            "Якщо та САМА подія іншими словами — це повтор, не бери.\n\n"
        )

    prompt = f"""Ти редактор українського Telegram-каналу про політику та війну.

Нові статті за останні {HOURS_BACK} годин:
{news_text}

{skip_block}Завдання:
1. Обери ОДНУ найважливішу статтю — нову подію, якої немає в переліку вже висвітлених.
2. Напиши авторський пост українською мовою:
   - Рядок 1: заголовок з емодзі, обгорни в *зірочки* (до 10 слів)
   - Порожній рядок
   - Абзац 1 (2–3 речення): суть події та контекст
   - Порожній рядок
   - Абзац 2 (2–3 речення): наслідки або оцінка
   - БЕЗ посилань, БЕЗ "за даними ЗМІ", БЕЗ згадок джерел

Відповідь ТІЛЬКИ JSON (без зайвого тексту):
{{"post_text": "...", "chosen_index": 1, "chosen_title": "заголовок обраної статті"}}"""

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
            log.error(f"Claude: no JSON in response: {raw[:300]}")
            return None
        return json.loads(m.group())
    except Exception as ex:
        log.error(f"Claude error: {ex}")
        return None


# ─── Telegram helpers ─────────────────────────────────────────────────────────

def tg(method, **kw):
    try:
        r = requests.post(f"{TG}/{method}", json=kw, timeout=20)
        return r.json()
    except Exception as ex:
        log.error(f"TG {method}: {ex}")
        return {"ok": False}

def notify(msg):
    tg("sendMessage", chat_id=ADMIN, text=msg)

def send_preview(text, image_url, cb_key):
    kb = {"inline_keyboard": [[
        {"text": "✅ Опублікувати", "callback_data": f"publish|{cb_key}"},
        {"text": "❌ Пропустити",  "callback_data": f"skip|{cb_key}"},
    ]]}
    header = "⏳ *PREVIEW* — очікує погодження\n\n"

    # Спроба з фото
    if image_url:
        r = tg("sendPhoto", chat_id=ADMIN, photo=image_url,
               caption=header + text, parse_mode="Markdown", reply_markup=kb)
        if r.get("ok"):
            log.info(f"Preview (фото) msg_id={r['result']['message_id']}")
            return r["result"]["message_id"]
        log.warning(f"sendPhoto failed: {r.get('description')} — fallback текст")

    # Fallback без фото
    r = tg("sendMessage", chat_id=ADMIN, text=header + text,
           parse_mode="Markdown", reply_markup=kb)
    if r.get("ok"):
        log.info(f"Preview (текст) msg_id={r['result']['message_id']}")
        return r["result"]["message_id"]

    log.error(f"send_preview failed: {r}")
    return None

def publish_to_channel(text, image_url):
    # З фото
    if image_url:
        r = tg("sendPhoto", chat_id=CHANNEL, photo=image_url,
               caption=text, parse_mode="Markdown")
        if r.get("ok"):
            log.info("Опубліковано з фото")
            return True
        log.warning(f"Photo publish failed: {r.get('description')} — fallback текст")

    # Fallback без Markdown і без фото
    plain = re.sub(r"[*_`]", "", text)
    r = tg("sendMessage", chat_id=CHANNEL, text=plain)
    ok = r.get("ok", False)
    if not ok:
        log.error(f"Text publish failed: {r.get('description')}")
    return ok

def get_updates(offset=None, long_poll=0):
    params = {"timeout": long_poll, "allowed_updates": ["callback_query"]}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{TG}/getUpdates", params=params, timeout=long_poll + 15)
        data = r.json()
        if not data.get("ok"):
            log.error(f"getUpdates error: {data.get('description')}")
            return []
        return data.get("result", [])
    except Exception as ex:
        log.warning(f"getUpdates: {ex}")
        return []

def answer_cb(cb_id):
    try:
        requests.post(f"{TG}/answerCallbackQuery",
                      json={"callback_query_id": cb_id}, timeout=5)
    except Exception:
        pass

def poll(cb_key, minutes):
    """Чекає натискання кнопки. Повертає 'publish', 'skip' або None."""
    offset = None
    deadline = time.time() + minutes * 60
    log.info(f"Polling {minutes} хв для cb_key={cb_key!r}")

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        wait = min(20, remaining)
        if wait <= 0:
            break
        for upd in get_updates(offset=offset, long_poll=wait):
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if not cb:
                continue
            data = cb.get("data", "")
            answer_cb(cb["id"])
            log.info(f"Callback: {data!r}")
            if cb_key in data:
                action = data.split("|")[0]
                log.info(f"Рішення: {action}")
                return action

    log.info("Polling timeout — рішення не прийнято")
    return None


# ─── Publish / Skip ───────────────────────────────────────────────────────────

def do_publish(state):
    text  = state.get("pending_text", "")
    image = state.get("pending_image")
    title = state.get("pending_title", "")

    ok = publish_to_channel(text, image)
    if ok:
        write_log("published", title, text, image or "")
        _add_recent_event(state, text)
        state["last_sent"] = time.time()
        state["mode"] = "active"
        notify("✅ Пост опубліковано в канал!")
    else:
        state["last_sent"] = 0   # скидаємо — наступний run спробує знову
        notify("⚠️ Помилка публікації — перевір логи")

    _clear_pending(state)
    save_state(state)

def do_skip(state):
    title = state.get("pending_title", "")
    text  = state.get("pending_text", "")
    write_log("skipped", title)
    _add_recent_event(state, text)
    notify("❌ Пост пропущено.")
    _clear_pending(state)
    save_state(state)

def _add_recent_event(state, text):
    """Додає короткий опис події в recent_events для семантичного dedup."""
    if not text:
        return
    summary = re.sub(r"[*_`]", "", text)[:200]
    recent = state.get("recent_events", [])
    # Не додаємо дублікати
    if recent and recent[0] == summary:
        return
    recent.insert(0, summary)
    state["recent_events"] = recent[:16]

def _clear_pending(state):
    for k in ("pending_cb_key", "pending_text", "pending_image",
              "pending_title", "pending_url", "pending_sent_at"):
        state.pop(k, None)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"Bot start | MANUAL_RUN={MANUAL_RUN}")

    # Перевірка змінних середовища
    for name, val in [("TELEGRAM_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHANNEL),
                      ("ADMIN_CHAT_ID", ADMIN), ("ANTHROPIC_API_KEY", CLAUDE_KEY)]:
        if not val:
            log.error(f"Відсутня змінна: {name}")
            return

    # ── 1. Видаляємо webhook ────────────────────────────────────────────────
    try:
        wh = requests.get(f"{TG}/getWebhookInfo", timeout=10).json()
        if wh.get("result", {}).get("url"):
            log.warning("Webhook активний — видаляємо")
            requests.post(f"{TG}/deleteWebhook",
                          json={"drop_pending_updates": False}, timeout=10)
            log.info("Webhook видалено")
        else:
            log.info("Webhook відсутній — OK")
    except Exception as ex:
        log.warning(f"Webhook check: {ex}")

    state = load_state()
    now   = time.time()

    # ── 2. Є незавершений pending preview? ─────────────────────────────────
    pending_cb = state.get("pending_cb_key")
    if pending_cb:
        sent_at = state.get("pending_sent_at", 0)
        elapsed = now - sent_at
        log.info(f"Pending preview: {pending_cb} ({elapsed/60:.1f} хв тому)")

        if MANUAL_RUN:
            # Manual run: ігноруємо pending, генеруємо новий пост
            log.info("Manual run — пропускаємо pending, генеруємо нову новину")
            do_skip(state)
            state = load_state()
            # fall through → Phase 3
        else:
            # Cron run: перевіряємо чи не натиснули кнопку після минулого run
            decision = poll(pending_cb, minutes=1)
            if decision == "publish":
                do_publish(state)
                return
            elif decision == "skip":
                do_skip(state)
                state = load_state()
                # fall through → генеруємо нову новину
            elif elapsed > POLL_MINUTES * 60:
                # >20 хв без рішення → slow режим (наступний пост через 3 год)
                log.info(f"Timeout: pending {elapsed/60:.0f} хв без рішення → slow mode")
                write_log("timeout", state.get("pending_title", ""))
                state["mode"] = "slow"
                _clear_pending(state)
                save_state(state)
                return
            else:
                log.info(f"Ще очікуємо ({(POLL_MINUTES*60 - elapsed)/60:.1f} хв left)")
                return
        # Якщо дійшли сюди — pending знято, генеруємо нову новину

    # ── 3. Перевіряємо інтервал між постами ────────────────────────────────
    mode      = state.get("mode", "active")
    interval  = ACTIVE_INTERVAL if mode == "active" else SLOW_INTERVAL
    last_sent = state.get("last_sent", 0)
    elapsed   = now - last_sent

    if not MANUAL_RUN and elapsed < interval:
        log.info(f"Не час. mode={mode}, залишилось {(interval-elapsed)/60:.0f} хв")
        return

    log.info(f"Генеруємо пост. mode={mode}, elapsed={elapsed/60:.0f} хв")

    # ── 4. Отримуємо новини ─────────────────────────────────────────────────
    articles = fetch_news()
    if not articles:
        log.warning("Новин не знайдено")
        return

    pub = load_pub()
    new_articles = [a for a in articles if a.get("link") and a["link"] not in pub]
    log.info(f"Після URL-dedup: {len(new_articles)}/{len(articles)} статей")

    if not new_articles:
        log.warning("Всі свіжі статті вже в published.json")
        # Якщо manual run і нема нових статей — очищаємо published.json
        # (щоб не застрягти назавжди)
        if MANUAL_RUN:
            log.info("Manual run: очищаємо published.json і пробуємо знову")
            save_pub({})
            new_articles = [a for a in articles if a.get("link")]
            if not new_articles:
                log.warning("Статей взагалі немає")
                return
        else:
            return

    # ── 5. Claude обирає найважливішу новину ────────────────────────────────
    recent_events = state.get("recent_events", [])
    result = call_claude(new_articles[:40], recent_events)
    if not result:
        log.error("Claude не повернув результат")
        return

    post_text    = result.get("post_text", "")
    chosen_index = result.get("chosen_index", 1) - 1
    chosen_title = result.get("chosen_title", "")
    log.info(f"Обрано: {chosen_title}")

    if not post_text:
        log.error("Claude повернув порожній post_text")
        return

    # Гарантуємо жирний заголовок
    lines = post_text.split("\n", 1)
    title_clean = lines[0].strip().strip("*").strip()
    post_text = f"*{title_clean}*" + ("\n" + lines[1] if len(lines) > 1 else "")

    # ── 6. Знаходимо фото ───────────────────────────────────────────────────
    candidates = new_articles[:40]
    image_url  = None
    chosen_url = ""
    if 0 <= chosen_index < len(candidates):
        chosen_url = candidates[chosen_index].get("link", "")
        image_url  = (candidates[chosen_index].get("image") or
                      fetch_og_image(chosen_url))

    # ── 7. Надсилаємо preview адміну ────────────────────────────────────────
    cb_key = str(int(now))
    msg_id = send_preview(post_text, image_url, cb_key)
    if not msg_id:
        log.error("send_preview повністю провалився")
        return

    # ── 8. Зберігаємо стан ─────────────────────────────────────────────────
    # Тільки URL обраної статті → published.json
    if chosen_url:
        pub[chosen_url] = now
        save_pub(pub)

    # recent_events НЕ оновлюємо тут — оновлюємо тільки в do_publish/do_skip
    # щоб уникнути дублікатів у recent_events

    state.update({
        "last_sent":       now,
        "pending_cb_key":  cb_key,
        "pending_text":    post_text,
        "pending_image":   image_url,
        "pending_title":   chosen_title,
        "pending_url":     chosen_url,
        "pending_sent_at": now,
    })
    save_state(state)

    # ── 9. Чекаємо рішення 20 хв ────────────────────────────────────────────
    decision = poll(cb_key, minutes=POLL_MINUTES)

    if decision == "publish":
        do_publish(state)
    elif decision == "skip":
        do_skip(state)
    else:
        log.info(f"Рішення не прийнято за {POLL_MINUTES} хв. "
                 "State збережено — наступний run перевірить.")


if __name__ == "__main__":
    main()
