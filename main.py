import os
import re
import json
import shutil
import tempfile
import threading
import subprocess
import urllib.request
import telebot
from telebot import types
import yt_dlp
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3

# ===== НАСТРОЙКИ =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_DURATION = 10 * 60          # лимит 10 минут
SEARCH_RESULTS = 5              # сколько результатов показывать
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "musicbot")

# --- для inline-режима (@бот название песни в любом чате/группе) ---
# Твой Telegram ID (узнать у @userinfobot). Бот загружает туда трек, чтобы получить file_id.
# Сначала нажми /start у своего бота, иначе он не сможет тебе писать.
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "0"))
# Прямая ссылка на placeholder.mp3 (залей файл в GitHub и возьми ссылку Raw)
PLACEHOLDER_URL = os.getenv("PLACEHOLDER_URL", "https://raw.githubusercontent.com/ТВОЙ_НИК/my-bot/main/placeholder.mp3")
# =====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "cache.json")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
BOT_USERNAME = ""   # заполняется при запуске

URL_RE = re.compile(r"https?://\S+")
sessions = {}        # uid -> {"path","title","artist","duration","thumb","step","dir"}
search_cache = {}    # uid -> [(title, url, duration)]
lock = threading.Lock()

# кеш уже загруженных треков: video_id -> {"file_id","title","artist","duration"}
file_cache = {}
cache_lock = threading.Lock()


def load_cache():
    global file_cache
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            file_cache = json.load(f)
    except Exception:
        file_cache = {}


def save_cache():
    with cache_lock:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(file_cache, f, ensure_ascii=False)
        except Exception as e:
            print("cache save error:", e)


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def fmt_time(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def cleanup(uid):
    """Удаляет временную папку с mp3 и очищает сессию."""
    with lock:
        s = sessions.pop(uid, None)
    if s and s.get("dir"):
        shutil.rmtree(s["dir"], ignore_errors=True)


def make_cover(info, out_dir):
    """Скачивает обложку, обрезает до квадрата 320x320. Возвращает путь или None."""
    url = info.get("thumbnail")
    if not url:
        return None
    try:
        raw = os.path.join(out_dir, "cover_raw")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r, open(raw, "wb") as f:
            f.write(r.read())
        out = os.path.join(out_dir, "cover.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", raw,
             "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=320:320",
             "-q:v", "4", out],
            check=True, timeout=30)
        return out
    except Exception as e:
        print("cover error:", e)
        return None


def search_youtube(query):
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    result = []
    for e in info.get("entries", []):
        if e and e.get("url"):
            result.append((e.get("title", "Без названия"), e["url"], e.get("duration") or 0))
    return result


def prepare_track(url, out_dir):
    """Скачивает mp3, делает обложку, пишет теги. Возвращает dict."""
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if (info.get("duration") or 0) > MAX_DURATION:
        raise ValueError("too_long")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    path = os.path.join(out_dir, f"{info['id']}.mp3")
    title = info.get("track") or info.get("title") or "Unknown"
    artist = info.get("artist") or ""
    if not artist and " - " in title:          # "Артист - Название"
        artist, title = [x.strip() for x in title.split(" - ", 1)]
    if not artist:
        artist = info.get("uploader") or "Unknown"

    thumb = make_cover(info, out_dir)
    cover_bytes = None
    if thumb:
        with open(thumb, "rb") as f:
            cover_bytes = f.read()
    write_tags(path, title=title, artist=artist, cover_bytes=cover_bytes)
    return {"id": info["id"], "path": path, "title": title, "artist": artist,
            "duration": int(info.get("duration") or 0), "thumb": thumb}


def write_tags(path, title=None, artist=None, cover_bytes=None):
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        audio = MP3(path)
        audio.add_tags()
        audio.save()
        tags = EasyID3(path)
    if title:
        tags["title"] = title
    if artist:
        tags["artist"] = artist
    tags.save()

    if cover_bytes:
        id3 = ID3(path)
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
        id3.save(v2_version=3)


def action_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📥 Отправить как есть", callback_data="send"),
        types.InlineKeyboardButton("✏️ Изменить название / артиста", callback_data="edit"),
        types.InlineKeyboardButton("🖼 Сменить обложку", callback_data="cover"),
    )
    return kb


def search_kb():
    """Кнопка 🔍 под отправленным треком — открывает выбор чата с @ботом."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔍", switch_inline_query=""))
    return kb


def wait_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏳ Загружаю…", callback_data="wait"))
    return kb


def process_url(chat_id, uid, url):
    """Скачивание в отдельном потоке, чтобы бот не вис."""
    cleanup(uid)
    wait = bot.send_message(chat_id, "⏳ Загружаю трек, подожди немного…")
    out_dir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        t = prepare_track(url, out_dir)
        with lock:
            sessions[uid] = {"path": t["path"], "title": t["title"], "artist": t["artist"],
                             "duration": t["duration"], "thumb": t["thumb"],
                             "step": None, "dir": out_dir}
        bot.edit_message_text(
            f"✅ Готово!\n\n🎵 <b>{t['title']}</b>\n👤 {t['artist']}\n\nЧто делаем дальше?",
            chat_id, wait.message_id, reply_markup=action_keyboard())
    except ValueError:
        shutil.rmtree(out_dir, ignore_errors=True)
        bot.edit_message_text("⚠️ Трек длиннее 10 минут. Выбери что-нибудь покороче.",
                              chat_id, wait.message_id)
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        print("download error:", e)
        bot.edit_message_text("❌ Не получилось скачать трек. Проверь ссылку или попробуй другой запрос.",
                              chat_id, wait.message_id)


# ---------- ХЭНДЛЕРЫ (личный чат) ----------
@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    bot.send_message(
        m.chat.id,
        "🎧 <b>Музыкальный загрузчик</b>\n\n"
        "Напиши название песни или артиста — найду и скачаю MP3.\n"
        "Можно прислать ссылку на YouTube / SoundCloud.\n"
        f"В любом чате или группе: <code>@{BOT_USERNAME} название песни</code>\n\n"
        "После загрузки можно поменять название, артиста и обложку.")


@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    text = m.text.strip()

    s = sessions.get(uid)
    if s and s.get("step") == "edit":
        if "-" in text:
            artist, title = [x.strip() for x in text.split("-", 1)]
        else:
            artist, title = s["artist"], text
        try:
            write_tags(s["path"], title=title, artist=artist)
            s["title"], s["artist"], s["step"] = title, artist, None
            bot.send_message(m.chat.id, f"✅ Обновлено:\n🎵 <b>{title}</b>\n👤 {artist}",
                             reply_markup=action_keyboard())
        except Exception as e:
            print("tag error:", e)
            bot.send_message(m.chat.id, "❌ Не удалось изменить теги.")
        return

    link = URL_RE.search(text)
    if link:
        threading.Thread(target=process_url, args=(m.chat.id, uid, link.group(0)),
                         daemon=True).start()
        return

    wait = bot.send_message(m.chat.id, "🔎 Ищу…")
    try:
        results = search_youtube(text)
    except Exception as e:
        print("search error:", e)
        bot.edit_message_text("❌ Ошибка поиска. Попробуй позже.", m.chat.id, wait.message_id)
        return
    if not results:
        bot.edit_message_text("😕 Ничего не найдено. Попробуй изменить запрос.",
                              m.chat.id, wait.message_id)
        return

    search_cache[uid] = results
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (title, _, dur) in enumerate(results):
        kb.add(types.InlineKeyboardButton(f"{fmt_time(dur)} · {title[:45]}",
                                          callback_data=f"pick:{i}"))
    bot.edit_message_text("Выбери трек:", m.chat.id, wait.message_id, reply_markup=kb)


@bot.message_handler(content_types=["photo"])
def on_photo(m):
    uid = m.from_user.id
    s = sessions.get(uid)
    if not s or s.get("step") != "cover":
        bot.send_message(m.chat.id, "Сначала выбери трек 🎵")
        return
    try:
        file_info = bot.get_file(m.photo[-1].file_id)
        data = bot.download_file(file_info.file_path)
        write_tags(s["path"], cover_bytes=data)
        s["thumb"] = None
        s["step"] = None
        bot.send_message(m.chat.id, "🖼 Обложка обновлена!", reply_markup=action_keyboard())
    except Exception as e:
        print("cover error:", e)
        bot.send_message(m.chat.id, "❌ Не удалось поставить обложку.")


@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    uid = c.from_user.id
    bot.answer_callback_query(c.id)
    if c.message is None or c.data == "wait":
        return
    chat_id = c.message.chat.id

    if c.data.startswith("pick:"):
        idx = int(c.data.split(":")[1])
        results = search_cache.get(uid)
        if not results or idx >= len(results):
            bot.send_message(chat_id, "Результаты устарели, повтори поиск.")
            return
        bot.delete_message(chat_id, c.message.message_id)
        threading.Thread(target=process_url, args=(chat_id, uid, results[idx][1]),
                         daemon=True).start()
        return

    s = sessions.get(uid)
    if not s:
        bot.send_message(chat_id, "Сессия истекла. Отправь запрос заново.")
        return

    if c.data == "edit":
        s["step"] = "edit"
        bot.send_message(chat_id, "✏️ Пришли новое название.\n"
                                  "Формат: <code>Артист - Название</code>\n"
                                  "Или просто название, артист останется прежним.")
    elif c.data == "cover":
        s["step"] = "cover"
        bot.send_message(chat_id, "🖼 Пришли картинку для обложки (как фото).")
    elif c.data == "send":
        thumb_f = open(s["thumb"], "rb") if s.get("thumb") else None
        try:
            with open(s["path"], "rb") as f:
                bot.send_audio(chat_id, f, title=s["title"], performer=s["artist"],
                               duration=s.get("duration"), thumbnail=thumb_f,
                               caption=f"@{BOT_USERNAME}", reply_markup=search_kb())
        except Exception as e:
            print("send error:", e)
            bot.send_message(chat_id, "❌ Не удалось отправить файл (возможно, он больше 50 МБ).")
        finally:
            if thumb_f:
                thumb_f.close()
            cleanup(uid)   # обязательно удаляем mp3 с диска


# ---------- INLINE-РЕЖИМ ----------
inline_search_cache = {}   # запрос -> список результатов
inline_sem = threading.Semaphore(2)   # не больше 2 загрузок одновременно


def search_inline(query):
    if query in inline_search_cache:
        return inline_search_cache[query]
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    res = []
    for e in info.get("entries", []):
        if e and e.get("id"):
            res.append({"id": e["id"], "title": e.get("title", "Без названия"),
                        "artist": e.get("uploader") or e.get("channel") or "",
                        "duration": e.get("duration") or 0})
    if len(inline_search_cache) > 200:
        inline_search_cache.clear()
    inline_search_cache[query] = res
    return res


@bot.inline_handler(func=lambda q: True)
def on_inline_query(q):
    query = q.query.strip()
    if len(query) < 3:
        bot.answer_inline_query(q.id, [], cache_time=1)
        return
    try:
        results = search_inline(query)
    except Exception as e:
        print("inline search error:", e)
        bot.answer_inline_query(q.id, [], cache_time=1)
        return

    caption = f"@{BOT_USERNAME}"
    items = []
    for r in results:
        meta = file_cache.get(r["id"])
        if meta:
            # уже скачивали: показывается с обложкой и отправляется мгновенно
            items.append(types.InlineQueryResultCachedAudio(
                id=r["id"], audio_file_id=meta["file_id"],
                caption=caption, reply_markup=search_kb()))
        else:
            items.append(types.InlineQueryResultAudio(
                id=r["id"], audio_url=PLACEHOLDER_URL,
                title=r["title"][:60], performer=r["artist"][:40],
                audio_duration=int(r["duration"]), reply_markup=wait_kb()))
    bot.answer_inline_query(q.id, items, cache_time=10)


def finish_inline(video_id, inline_message_id):
    with inline_sem:
        out_dir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
        try:
            meta = file_cache.get(video_id)
            if not meta:
                t = prepare_track(f"https://www.youtube.com/watch?v={video_id}", out_dir)
                thumb_f = open(t["thumb"], "rb") if t["thumb"] else None
                try:
                    with open(t["path"], "rb") as f:
                        sent = bot.send_audio(STORAGE_CHAT_ID, f, title=t["title"],
                                              performer=t["artist"], duration=t["duration"],
                                              thumbnail=thumb_f)
                finally:
                    if thumb_f:
                        thumb_f.close()
                meta = {"file_id": sent.audio.file_id, "title": t["title"],
                        "artist": t["artist"], "duration": t["duration"]}
                file_cache[video_id] = meta
                save_cache()
            bot.edit_message_media(
                types.InputMediaAudio(meta["file_id"], caption=f"@{BOT_USERNAME}"),
                inline_message_id=inline_message_id, reply_markup=search_kb())
        except ValueError:
            try:
                bot.edit_message_caption(caption="⚠️ Трек длиннее 10 минут.",
                                         inline_message_id=inline_message_id)
            except Exception:
                pass
        except Exception as e:
            print("inline finish error:", e)
            try:
                bot.edit_message_caption(caption="❌ Не удалось загрузить трек.",
                                         inline_message_id=inline_message_id)
            except Exception:
                pass
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)   # удаляем временный mp3


@bot.chosen_inline_handler(func=lambda r: True)
def on_chosen(r):
    # если трек уже в кеше, он ушёл сразу готовым — делать нечего
    if not r.inline_message_id or r.result_id in file_cache:
        return
    threading.Thread(target=finish_inline, args=(r.result_id, r.inline_message_id),
                     daemon=True).start()


if __name__ == "__main__":
    load_cache()
    BOT_USERNAME = bot.get_me().username
    if not STORAGE_CHAT_ID:
        print("ВНИМАНИЕ: STORAGE_CHAT_ID не задан, inline-режим работать не будет")
    print(f"Бот @{BOT_USERNAME} запущен")
    bot.infinity_polling(skip_pending=True)
