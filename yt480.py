#!/usr/bin/env python3
"""
yt480.py — загрузка видео и плейлистов с YouTube в 480p.

Использование:
    python yt480.py                      # спросит ссылку и папку
    python yt480.py ССЫЛКА               # скачает в текущую папку
    python yt480.py ССЫЛКА D:\\Video      # скачает в указанную папку

Для плейлиста внутри выбранной папки создаётся папка с названием плейлиста.
Если она уже есть, скрипт смотрит, какие видео в ней уже лежат,
и докачивает только недостающие.

Нужно:  pip install -U yt-dlp
Желательно: ffmpeg в PATH (без него качество может оказаться ниже 480p).
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yt_dlp
    from yt_dlp.utils import sanitize_filename
except ImportError:
    sys.exit("Не установлен yt-dlp. Выполните:  pip install -U yt-dlp")

VIDEO_EXTS = ("mp4", "mkv", "webm")
# Готовый файл называется "Название [ID].mp4" — по ID и узнаём скачанное.
COOKIES_BROWSER = "firefox"   # из какого браузера брать вход в YouTube

MAX_REFRESH = 3               # сколько раз подряд пробовать освежить cookies
REFRESH_WAIT = 25             # секунд ждать, пока Firefox загрузит YouTube

DONE_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.(?:%s)$" % "|".join(VIDEO_EXTS))


def downloaded_ids(folder: Path) -> set:
    """ID видео, которые уже полностью скачаны в папку."""
    ids = set()
    if folder.is_dir():
        for f in folder.iterdir():
            m = DONE_RE.search(f.name)
            if f.is_file() and m:
                ids.add(m.group(1))
    return ids


class YDL(yt_dlp.YoutubeDL):
    """YoutubeDL, который замечает предупреждение об устаревших cookies."""
    cookies_bad = False

    def report_warning(self, message, *args, **kwargs):
        if "cookies are no longer valid" in message:
            if not self.cookies_bad:
                print("! Cookies YouTube устарели.")
            self.cookies_bad = True
            return
        super().report_warning(message, *args, **kwargs)


def find_firefox():
    exe = shutil.which("firefox")
    if exe:
        return exe
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            cand = Path(root) / "Mozilla Firefox" / "firefox.exe"
            if cand.is_file():
                return str(cand)
    return None


def firefox_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq firefox.exe"],
                         capture_output=True, text=True).stdout
    return "firefox.exe" in out.lower()


def refresh_cookies(attempt: int) -> bool:
    """Открыть YouTube в обычном Firefox, чтобы браузер сам обновил сессию."""
    exe = find_firefox()
    if not exe:
        print("! Firefox не найден — освежить cookies не могу.")
        return False
    print(f"Освежаю cookies через Firefox (попытка {attempt} из {MAX_REFRESH})...")
    was_running = firefox_running()
    subprocess.Popen([exe, "https://www.youtube.com/"])
    time.sleep(REFRESH_WAIT)
    if os.name == "nt" and not was_running:
        # Вежливо закрываем Firefox (без /F), чтобы он записал cookies на диск.
        subprocess.run(["taskkill", "/IM", "firefox.exe"], capture_output=True)
        time.sleep(5)
    return True


def get_info(url: str) -> dict:
    """Быстро получить сведения о ссылке (без скачивания)."""
    opts = {"quiet": True, "extract_flat": "in_playlist", "skip_download": True}
    opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    attempt = 0
    while True:
        with YDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            bad = ydl.cookies_bad
        if bad and attempt < MAX_REFRESH:
            attempt += 1
            if refresh_cookies(attempt):
                continue
        return info


def download(items: list, folder: Path, album: str = None) -> None:
    """items — список пар (номер в плейлисте или None, запись о видео)."""
    has_ffmpeg = shutil.which("ffmpeg") is not None
    if has_ffmpeg:
        fmt = ("bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
               "bestvideo[height<=480]+bestaudio/best[height<=480]/best")
    else:
        print("! ffmpeg не найден — беру готовые файлы со звуком "
              "(качество может быть ниже 480p).")
        fmt = "best[height<=480]/best"

    opts = {
        "format": fmt,
        "outtmpl": str(folder / "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "ignoreerrors": True,      # недоступное видео не останавливает остальные
        "continuedl": True,        # докачка оборванных файлов
        "retries": 10,
        "fragment_retries": 10,
        "windowsfilenames": True,
    }
    if has_ffmpeg:
        opts["merge_output_format"] = "mp4"
        # Записывает в файл теги: название, автор (канал), дата,
        # ссылка на оригинал (в комментарии), номер и название плейлиста.
        opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]
    opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)

    queue = list(items)
    attempts = 0        # попыток освежить cookies подряд
    gave_up = False
    while queue:
        # Новый YDL заново читает cookies из браузера.
        with YDL(opts) as ydl:
            while queue:
                idx, e = queue[0]
                extra = {}
                if idx is not None:
                    extra = {"track_number": idx, "playlist_index": idx}
                    if album:
                        extra["album"] = album
                ydl.cookies_bad = False
                ydl.extract_info(f"https://www.youtube.com/watch?v={e['id']}",
                                 download=True, extra_info=extra)
                if not ydl.cookies_bad:
                    attempts = 0
                elif not gave_up:
                    if attempts < MAX_REFRESH and refresh_cookies(attempts + 1):
                        attempts += 1
                        break       # повторить это же видео со свежими cookies
                    gave_up = True
                    print("! Освежить cookies не удалось — продолжаю без входа. "
                          "Откройте Firefox и войдите в YouTube заново.")
                queue.pop(0)


def main() -> None:
    args = sys.argv[1:]
    url = args[0] if args else input("Ссылка на видео или плейлист: ").strip()
    if not url:
        sys.exit("Ссылка не указана.")
    if len(args) > 1:
        base = Path(args[1])
    elif args:
        base = Path.cwd()
    else:
        base = Path(input("Папка для загрузки (Enter — текущая): ").strip().strip('"') or ".")
    base = base.expanduser().resolve()

    print("Получаю информацию...")
    try:
        info = get_info(url)
    except yt_dlp.utils.DownloadError as e:
        sys.exit(f"Не удалось получить информацию: {e}")

    if info.get("_type") == "playlist":
        title = info.get("title") or info.get("id") or "playlist"
        name = sanitize_filename(title).strip(" .") or "playlist"
        folder = base / name
        album = title
        entries = [(i, e) for i, e in enumerate(info.get("entries") or [], 1)
                   if e and e.get("id")]

        print(f"Плейлист: {title}  (видео: {len(entries)})")
        if folder.is_dir():
            print(f"Папка уже существует: {folder}")
        else:
            print(f"Создаю папку: {folder}")
    else:
        folder = base
        album = None
        entries = [(None, info)]

    folder.mkdir(parents=True, exist_ok=True)
    have = downloaded_ids(folder)

    todo = []
    for idx, e in entries:
        if e["id"] in have:
            print(f"  [есть]  {e.get('title') or e['id']}")
        else:
            todo.append((idx, e))

    print(f"Уже скачано: {len(entries) - len(todo)}, осталось скачать: {len(todo)}")
    if not todo:
        print("Всё уже загружено.")
        return

    download(todo, folder, album)

    have = downloaded_ids(folder)
    failed = [e for _, e in todo if e["id"] not in have]
    print(f"\nГотово. Скачано: {len(todo) - len(failed)} из {len(todo)}.")
    if failed:
        print("Не удалось скачать:")
        for e in failed:
            print(f"  - {e.get('title') or e['id']}  (https://youtu.be/{e['id']})")
    print(f"Папка: {folder}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nПрервано. При следующем запуске загрузка продолжится.")
