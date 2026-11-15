#!/usr/bin/env python3
"""
yt480.py — загрузка видео и плейлистов с YouTube в 480p
(вертикальные видео / shorts — 480x854).

Использование:
    python yt480.py                      # спросит папку, затем ссылку
    python yt480.py D:\\Video             # папка задана сразу
    python yt480.py D:\\Video ССЫЛКА      # папка и ссылка (ссылок может быть несколько)

Если в корне выбранной папки лежит links.txt (по одной ссылке в строке),
скрипт скачивает всё из него по очереди и ссылку не спрашивает.

Для плейлиста внутри выбранной папки создаётся папка с его названием.
Уже скачанные видео пропускаются. Недоступные видео записываются в
unavailable.txt в папке плейлиста и при следующих запусках не проверяются
(чтобы попробовать снова — удалите строку из этого файла).

Нужно:  
    pip install -U yt-dlp yt-dlp-ejs
    winget install Gyan.FFmpeg
    winget install DenoLand.Deno
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
    sys.exit("""Не установлен yt-dlp. Выполните:  
    pip install -U yt-dlp
    winget install Gyan.FFmpeg
    winget install DenoLand.Deno
    """)

# ---------------------------------------------------------------- настройки
COOKIES_BROWSER = "firefox"   # из какого браузера брать вход в YouTube
MAX_REFRESH = 3               # сколько раз подряд пробовать освежить cookies
REFRESH_WAIT = 25             # секунд ждать, пока Firefox загрузит YouTube
LINKS_FILE = "links.txt"
UNAVAILABLE_FILE = "unavailable.txt"

VIDEO_EXTS = ("mp4", "mkv", "webm")
# Готовый файл называется "Название [ID].mp4" — по ID и узнаём скачанное.
DONE_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.(?:%s)$" % "|".join(VIDEO_EXTS))
URL_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")

# Ошибки, означающие, что видео недоступно насовсем (а не сбой сети).
PERMANENT_ERRORS = (
    "private video", "video is private", "video unavailable", "removed",
    "terminated", "no longer available", "members",
    "not made this video available", "copyright",
)


def build_format(has_ffmpeg: bool) -> str:
    """480p по короткой стороне (и для вертикальных видео), без HDR."""
    # Вертикальное видео: узкое (ширина <= 480) и при этом высокое.
    # У горизонтальных такого сочетания не бывает.
    vert = "[width<=480][height>=600]"
    hor = "[height<=480]"
    if not has_ffmpeg:
        return f"b{vert}/b{hor}/b"
    # HDR-дорожки в обычных плеерах выглядят пересвеченными — берём SDR.
    sdr = "[dynamic_range=?SDR]"
    alts = []
    for dr in (sdr, ""):            # сначала SDR, HDR — только если иначе никак
        for size in (vert, hor):
            alts.append(f"bv{size}{dr}[ext=mp4]+ba[ext=m4a]")
            alts.append(f"bv{size}{dr}+ba")
    alts += [f"b{hor}", "b"]
    return "/".join(alts)


# ------------------------------------------------------------------ cookies
class YDL(yt_dlp.YoutubeDL):
    """YoutubeDL, который запоминает ошибку и замечает устаревшие cookies."""
    cookies_bad = False
    last_error = None

    def report_warning(self, message, *args, **kwargs):
        if "cookies are no longer valid" in message:
            if not self.cookies_bad:
                print("! Cookies YouTube устарели.")
            self.cookies_bad = True
            return
        super().report_warning(message, *args, **kwargs)

    def report_error(self, message, *args, **kwargs):
        self.last_error = str(message)
        super().report_error(message, *args, **kwargs)


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


# ------------------------------------------------------------- учёт файлов
def downloaded_ids(folder: Path) -> set:
    """ID видео, которые уже полностью скачаны в папку."""
    ids = set()
    if folder.is_dir():
        for f in folder.iterdir():
            m = DONE_RE.search(f.name)
            if f.is_file() and m:
                ids.add(m.group(1))
    return ids


def unavailable_ids(folder: Path) -> set:
    """ID видео, записанных в unavailable.txt."""
    path = folder / UNAVAILABLE_FILE
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return set(URL_ID_RE.findall(text))


def add_unavailable(folder: Path, entry: dict) -> None:
    author = entry.get("channel") or entry.get("uploader") or "автор неизвестен"
    title = entry.get("title") or "название неизвестно"
    line = f"https://www.youtube.com/watch?v={entry['id']} ({author}: {title})\n"
    with open(folder / UNAVAILABLE_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def read_links(base: Path) -> list:
    path = base / LINKS_FILE
    if not path.is_file():
        return []
    links = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            links.append(line)
    return links


# ----------------------------------------------------------------- загрузка
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


def download(items: list, folder: Path, album: str = None) -> set:
    """items — список пар (номер в плейлисте или None, запись о видео).
    Возвращает ID видео, признанных недоступными."""
    has_ffmpeg = shutil.which("ffmpeg") is not None
    if not has_ffmpeg:
        print("! ffmpeg не найден — беру готовые файлы со звуком "
              "(качество может быть ниже).")

    opts = {
        "format": build_format(has_ffmpeg),
        "outtmpl": str(folder / "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "ignoreerrors": True,      # недоступное видео не останавливает остальные
        "continuedl": True,        # докачка оборванных файлов
        "retries": 10,
        "fragment_retries": 10,
        "windowsfilenames": True,
        "cookiesfrombrowser": (COOKIES_BROWSER,),
    }
    if has_ffmpeg:
        opts["merge_output_format"] = "mp4"
        # Записывает в файл теги: название, автор (канал), дата,
        # ссылка на оригинал (в комментарии), номер и название плейлиста.
        opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]

    unavailable = set()
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
                ydl.last_error = None
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
                err = (ydl.last_error or "").lower()
                if (err and "requested format" not in err
                        and any(k in err for k in PERMANENT_ERRORS)):
                    add_unavailable(folder, e)
                    unavailable.add(e["id"])
                queue.pop(0)
    return unavailable


def process_url(url: str, base: Path) -> None:
    print(f"\n=== {url}")
    print("Получаю информацию...")
    try:
        info = get_info(url)
    except yt_dlp.utils.DownloadError as e:
        print(f"Не удалось получить информацию: {e}")
        return
    if not info:
        print("Не удалось получить информацию.")
        return

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
    skip = unavailable_ids(folder)

    todo, n_have, n_skip = [], 0, 0
    for idx, e in entries:
        if e["id"] in have:
            n_have += 1
        elif e["id"] in skip:
            n_skip += 1
        else:
            todo.append((idx, e))

    print(f"Уже скачано: {n_have}, недоступных (из {UNAVAILABLE_FILE}): {n_skip}, "
          f"осталось скачать: {len(todo)}")
    if not todo:
        print("Новых видео нет.")
        return

    unavailable = download(todo, folder, album)

    have = downloaded_ids(folder)
    failed = [e for _, e in todo
              if e["id"] not in have and e["id"] not in unavailable]
    done = sum(1 for _, e in todo if e["id"] in have)
    print(f"\nСкачано: {done} из {len(todo)}.")
    if unavailable:
        print(f"Недоступных видео: {len(unavailable)} — записаны в "
              f"{folder / UNAVAILABLE_FILE}")
    if failed:
        print("Не скачались из-за временной ошибки (будут повторены "
              "при следующем запуске):")
        for e in failed:
            print(f"  - {e.get('title') or e['id']}  (https://youtu.be/{e['id']})")
    print(f"Папка: {folder}")


def main() -> None:
    args = sys.argv[1:]
    urls = [a for a in args if a.lower().startswith(("http://", "https://"))]
    dirs = [a for a in args if a not in urls]

    if dirs:
        base = Path(dirs[0])
    else:
        base = Path(input("Папка для загрузки (Enter — текущая): ").strip().strip('"') or ".")
    base = base.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    if not urls:
        urls = read_links(base)
        if urls:
            print(f"Найден {LINKS_FILE}: ссылок — {len(urls)}.")
        else:
            print(f"Файл {base / LINKS_FILE} не найден или пуст.\n"
                  f"Можно создать его и перечислить ссылки для загрузки, "
                  f"каждую с новой строки — тогда скрипт скачает всё сам.")
            url = input("Ссылка на видео или плейлист: ").strip()
            if not url:
                sys.exit("Ссылка не указана.")
            urls = [url]

    for url in urls:
        process_url(url, base)
    print("\nГотово.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nПрервано. При следующем запуске загрузка продолжится.")
