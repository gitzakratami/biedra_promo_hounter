import argparse
import json
import os
import platform
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

import ocr_engine

# --- KONFIGURACJA ---
load_dotenv()

# Use BIEDRONA_DATA_DIR if set (packaged Electron), otherwise script directory
DATA_DIR = os.environ.get('BIEDRONA_DATA_DIR', os.path.dirname(os.path.abspath(__file__)))

KEYWORD_TO_FIND = ""  # Zostanie ustawione przez użytkownika
SAVE_FOLDER = os.path.join(DATA_DIR, "gazetki")
MODELS_DIR = os.path.join(DATA_DIR, "models")
OCR_CACHE_DB = os.path.join(DATA_DIR, "ocr_cache.db")

# Bump when the OCR engine or model changes — cached pages read by an older
# engine are dropped so they get re-indexed with the current one.
ENGINE_ID = "rapidocr-latin-ppocrv5"

# The GPU reads one page at a time, so downloads run ahead of it in a pool.
# Fetching is chunked to keep the ~900 MB of leaflet images off the heap.
DOWNLOAD_WORKERS = 12
DOWNLOAD_CHUNK = 24

DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")
MAX_DISCORD_SIZE_BYTES = 7.5 * 1024 * 1024
MAX_DISCORD_FILES_COUNT = 10
MAX_DISCORD_EMBEDS_COUNT = 10

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print_lock = threading.Lock()

# --------------------


def chunked(items, size=900):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def init_cache_db():
    conn = sqlite3.connect(OCR_CACHE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            image_url TEXT PRIMARY KEY,
            leaflet_id TEXT,
            leaflet_name TEXT,
            page_number INTEGER,
            ocr_text TEXT,
            indexed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_leaflet_id ON pages(leaflet_id)")
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS ocr_fts
        USING fts5(
            image_url,
            leaflet_name,
            page_number UNINDEXED,
            ocr_text,
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """
    )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(pages)")}
    if "boxes" not in existing:
        conn.execute("ALTER TABLE pages ADD COLUMN boxes TEXT")
    if "engine" not in existing:
        conn.execute("ALTER TABLE pages ADD COLUMN engine TEXT")

    drop_stale_engine_rows(conn)
    conn.commit()
    return conn


def drop_stale_engine_rows(conn):
    """Discard pages read by a previous OCR engine so they are read again."""
    stale = [
        row[0] for row in conn.execute(
            "SELECT image_url FROM pages WHERE engine IS NULL OR engine != ?",
            (ENGINE_ID,),
        )
    ]
    for urls_chunk in chunked(stale):
        placeholders = ",".join(["?"] * len(urls_chunk))
        conn.execute(f"DELETE FROM pages WHERE image_url IN ({placeholders})", urls_chunk)
        conn.execute(f"DELETE FROM ocr_fts WHERE image_url IN ({placeholders})", urls_chunk)
    return len(stale)


def get_cached_urls(conn, tasks):
    urls = [task["url"] for task in tasks]
    cached_urls = set()
    for urls_chunk in chunked(urls):
        placeholders = ",".join(["?"] * len(urls_chunk))
        query = f"SELECT image_url FROM pages WHERE image_url IN ({placeholders})"
        rows = conn.execute(query, urls_chunk).fetchall()
        cached_urls.update(row[0] for row in rows)
    return cached_urls


def build_fts_match_query(keyword):
    """Prefix query, so 'mleko' also matches 'mleka' and 'mlekiem'."""
    safe_keyword = keyword.replace('"', '""').strip()
    return f'"{safe_keyword}"*'


def search_index(conn, keyword, urls=None):
    """Return [(image_url, leaflet_name, page_number)] matching the keyword."""
    match_query = build_fts_match_query(keyword)
    if urls is None:
        return conn.execute(
            "SELECT image_url, leaflet_name, page_number FROM ocr_fts WHERE ocr_fts MATCH ?",
            (match_query,),
        ).fetchall()

    hits = []
    for urls_chunk in chunked(list(urls)):
        placeholders = ",".join(["?"] * len(urls_chunk))
        query = f"""
            SELECT image_url, leaflet_name, page_number
            FROM ocr_fts
            WHERE ocr_fts MATCH ? AND image_url IN ({placeholders})
        """
        hits.extend(conn.execute(query, [match_query, *urls_chunk]).fetchall())
    return hits


def prune_cache_for_active_leaflets(conn, active_leaflet_ids):
    if not active_leaflet_ids:
        removed_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM ocr_fts")
        return removed_pages

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS active_leaflets(leaflet_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM active_leaflets")
    conn.executemany(
        "INSERT OR IGNORE INTO active_leaflets(leaflet_id) VALUES (?)",
        [(leaflet_id,) for leaflet_id in active_leaflet_ids],
    )

    obsolete_rows = conn.execute(
        """
        SELECT p.image_url
        FROM pages p
        LEFT JOIN active_leaflets a ON p.leaflet_id = a.leaflet_id
        WHERE a.leaflet_id IS NULL
        """
    ).fetchall()
    obsolete_urls = [row[0] for row in obsolete_rows]

    for urls_chunk in chunked(obsolete_urls):
        placeholders = ",".join(["?"] * len(urls_chunk))
        conn.execute(f"DELETE FROM pages WHERE image_url IN ({placeholders})", urls_chunk)
        conn.execute(f"DELETE FROM ocr_fts WHERE image_url IN ({placeholders})", urls_chunk)

    return len(obsolete_urls)


def save_page_to_cache(conn, task_data, ocr_text, boxes):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO pages (image_url, leaflet_id, leaflet_name, page_number,
                           ocr_text, indexed_at, boxes, engine)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(image_url) DO UPDATE SET
            leaflet_id=excluded.leaflet_id,
            leaflet_name=excluded.leaflet_name,
            page_number=excluded.page_number,
            ocr_text=excluded.ocr_text,
            indexed_at=excluded.indexed_at,
            boxes=excluded.boxes,
            engine=excluded.engine
        """,
        (
            task_data["url"],
            task_data["leaflet_id"],
            task_data["leaflet_name"],
            task_data["page_number"],
            ocr_text,
            now,
            json.dumps(boxes, ensure_ascii=False),
            ENGINE_ID,
        ),
    )
    conn.execute("DELETE FROM ocr_fts WHERE image_url = ?", (task_data["url"],))
    conn.execute(
        "INSERT INTO ocr_fts (image_url, leaflet_name, page_number, ocr_text) VALUES (?, ?, ?, ?)",
        (
            task_data["url"],
            task_data["leaflet_name"],
            str(task_data["page_number"]),
            ocr_text,
        ),
    )


def save_image_bytes(leaflet_name, page_number, image_bytes):
    safe_name = sanitize_filename(leaflet_name)
    filename = f"{safe_name}_strona_{page_number}.png"
    path = os.path.join(SAVE_FOLDER, filename)
    with open(path, 'wb') as f:
        f.write(image_bytes)
    return path


def fetch_image(task_data):
    try:
        resp = requests.get(task_data["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"[DOWNLOAD ERROR] {task_data['url']}: {e}", file=sys.stderr)
        return None


def download_and_save_image(task_data):
    content = fetch_image(task_data)
    if content is None:
        return None
    return save_image_bytes(task_data["leaflet_name"], task_data["page_number"], content)


def iter_downloaded(tasks):
    """Yield (task, image_bytes) with downloads running ahead of the consumer."""
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        for batch in chunked(tasks, DOWNLOAD_CHUNK):
            for task, content in zip(batch, pool.map(fetch_image, batch)):
                yield task, content


def compress_image_for_discord(image_path):
    try:
        img = Image.open(image_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        if img.width > 2000:
            ratio = 2000 / img.width
            new_height = int(img.height * ratio)
            img = img.resize((2000, new_height), Image.Resampling.LANCZOS)

        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=75)
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"Błąd kompresji: {e}")
        return None


def send_single_batch(files_dict, embeds_list, batch_num):
    try:
        payload = {"content": "", "embeds": embeds_list}
        response = requests.post(DISCORD_URL, data={"payload_json": json.dumps(payload)}, files=files_dict)
        if response.status_code not in [200, 204]:
            print(f"\n⚠️ Błąd Discorda: {response.status_code}")
            if response.text:
                print(f"   Odpowiedź API: {response.text[:500]}")
        else:
            with print_lock:
                print(f"\n📨 Wysłano paczkę nr {batch_num}")
    except Exception as e:
        print(f"\n⚠️ Błąd podczas wysyłania do Discorda: {e}")


def send_discord_gallery_dynamic(found_files):
    if not DISCORD_URL:
        print("\n⚠️ Brak zmiennej DISCORD_WEBHOOK_URL w pliku .env. Pomijam wysyłanie na Discorda.")
        return
    if not found_files:
        return
    print(f"\n📦 Pakowanie {len(found_files)} zdjęć dla Discorda...")

    current_batch_files = {}
    current_batch_embeds = []
    current_batch_size = 0
    current_batch_count = 0
    open_buffers = []
    batch_counter = 1

    for idx, file_path in enumerate(found_files):
        compressed_img = compress_image_for_discord(file_path)
        if not compressed_img:
            continue
        img_size = compressed_img.getbuffer().nbytes

        if (
            (current_batch_size + img_size > MAX_DISCORD_SIZE_BYTES)
            or (current_batch_count >= MAX_DISCORD_FILES_COUNT)
            or (len(current_batch_embeds) >= MAX_DISCORD_EMBEDS_COUNT)
        ):
            send_single_batch(current_batch_files, current_batch_embeds, batch_counter)
            batch_counter += 1
            current_batch_files = {}
            current_batch_embeds = []
            current_batch_size = 0
            current_batch_count = 0
            for b in open_buffers:
                b.close()
            open_buffers = []

        open_buffers.append(compressed_img)
        filename = f"img_{batch_counter}_{idx}.jpg"
        current_batch_files[filename] = (filename, compressed_img, "image/jpeg")

        embed = {"url": "https://www.biedronka.pl/pl/gazetki", "image": {"url": f"attachment://{filename}"}}
        if current_batch_count == 0:
            embed["title"] = f"Znaleziono: {KEYWORD_TO_FIND} (Paczka {batch_counter})"
            embed["color"] = 5763719
        current_batch_embeds.append(embed)
        current_batch_size += img_size
        current_batch_count += 1

    if current_batch_files:
        send_single_batch(current_batch_files, current_batch_embeds, batch_counter)
        for b in open_buffers:
            b.close()


def sanitize_filename(name):
    name = name.replace(" ", "_")
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name[:100]


def name_from_link(link):
    """Turn a leaflet URL slug into a readable name.

    The leaflet API has no name field and the listing anchors hold only images,
    so the slug is the only title available: 'hity-i-inspiracje-31-08'.
    """
    slug = link.split('#')[0].split('title,')[-1]
    return slug.replace('-', ' ').strip().capitalize() or "Gazetka"


def get_all_leaflet_uuids():
    """Return ([(uuid, name)], skipped_count) for the leaflets listed on the site."""
    main_page_url = "https://www.biedronka.pl/pl/gazetki"
    try:
        response = requests.get(main_page_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        leaflet_links = soup.find_all('a', href=re.compile(r'/pl/press,id,'))
        unique_links = list(set([link.get('href') for link in leaflet_links]))
    except Exception as e:
        print(f"[SCRAPE ERROR] strona główna: {e}", file=sys.stderr)
        return [], 0

    leaflets = {}
    skipped = 0
    for link in unique_links:
        url = link.split('#')[0]
        if not url.startswith("http"):
            url = f"https://www.biedronka.pl{url}"
        try:
            page_resp = requests.get(url, headers=HEADERS, timeout=15)
            match = re.search(r'window\.galleryLeaflet\.init\("([a-f0-9\-]{36})"\)', page_resp.text)
            if match:
                leaflets.setdefault(match.group(1), name_from_link(link))
            else:
                # Expired leaflets stay linked on the listing but render an
                # empty shell with no viewer.
                skipped += 1
                print(f"[SKIP] wygasła gazetka (brak podglądu): {url}", file=sys.stderr)
        except Exception as e:
            skipped += 1
            print(f"[SKIP] {url}: {e}", file=sys.stderr)
    return list(leaflets.items()), skipped


def get_leaflet_pages(leaflet_id, fallback_name=None):
    """Read every page of a leaflet.

    images_mobile is a flat, one-entry-per-page list at the same 1146x1800
    resolution as images_desktop, whose entries pair two pages into a spread.
    Reading only the first image of each desktop spread dropped ~48% of pages.
    """
    try:
        api_url = f"https://leaflet-api.prod.biedronka.cloud/api/leaflets/{leaflet_id}?ctx=web"
        response = requests.get(api_url, headers=HEADERS, timeout=15)
        data = response.json()
    except Exception as e:
        print(f"[API ERROR] {leaflet_id}: {e}", file=sys.stderr)
        return fallback_name or "Nieznana", []

    name = data.get('name') or fallback_name or f'Gazetka_{leaflet_id}'
    pages_info = []
    seen = set()

    for page_data in data.get('images_mobile', []):
        url = page_data.get('image')
        if not url or url in seen:
            continue
        seen.add(url)
        pages_info.append({
            "leaflet_id": leaflet_id,
            "leaflet_name": name,
            "page_number": (page_data.get('page') or 0) + 1,
            "url": url,
        })

    if not pages_info:
        # Fall back to the desktop spreads, taking both halves of each one.
        for page_data in data.get('images_desktop', []):
            for offset, url in enumerate(page_data.get('images', [])):
                if not url or url in seen:
                    continue
                seen.add(url)
                pages_info.append({
                    "leaflet_id": leaflet_id,
                    "leaflet_name": name,
                    "page_number": (page_data.get('page') or 0) * 2 + offset + 1,
                    "url": url,
                })

    return name, pages_info


def collect_tasks():
    """Return (tasks, uuids, skipped) covering every page of every leaflet."""
    leaflets, skipped = get_all_leaflet_uuids()
    if not leaflets:
        return [], [], skipped

    all_tasks = []
    for uuid, name in leaflets:
        _, pages = get_leaflet_pages(uuid, name)
        all_tasks.extend(pages)
    return all_tasks, [uuid for uuid, _ in leaflets], skipped


def emit(event_type, **kwargs):
    """Emit a JSON event to stdout for the GUI app."""
    msg = {"type": event_type, **kwargs}
    sys.stdout.write("JSON:" + json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def open_db(read_only=False):
    """Open the cache. WAL lets the search connection read while indexing writes."""
    if read_only and os.path.isfile(OCR_CACHE_DB):
        conn = sqlite3.connect(f"file:{OCR_CACHE_DB}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
        return conn
    return init_cache_db()


def run_index(use_gpu=False, cancel=None):
    """Read every uncached leaflet page into the index, reporting progress.

    cancel is an Event the GUI can set to stop the run between pages.
    """
    os.makedirs(SAVE_FOLDER, exist_ok=True)

    try:
        ocr_engine.get_engine(MODELS_DIR, use_gpu)
    except Exception as e:
        emit("error", message=f"Nie udało się uruchomić silnika OCR: {e}")
        emit("index-done", indexed=0, total=0)
        return

    emit("status", message="Sprawdzam, jakie gazetki są dostępne...")
    all_tasks, uuids, skipped = collect_tasks()
    if not uuids:
        emit("error", message="Nie znaleziono żadnych gazetek na stronie.")
        emit("index-done", indexed=0, total=0)
        return
    if skipped:
        emit("status", message=f"Pominięto {skipped} wygasłych gazetek")

    total_pages = len(all_tasks)
    conn = init_cache_db()
    prune_cache_for_active_leaflets(conn, uuids)
    cached_urls = get_cached_urls(conn, all_tasks)
    uncached_tasks = [t for t in all_tasks if t["url"] not in cached_urls]
    conn.commit()

    emit("status",
         message=f"{total_pages} stron | w indeksie: {len(cached_urls)} | do OCR: {len(uncached_tasks)}")
    emit("progress", current=len(cached_urls), total=total_pages, leaflet="", page=0)

    if not uncached_tasks:
        emit("index-done", indexed=0, total=total_pages)
        conn.close()
        return

    processed = len(cached_urls)
    indexed = 0
    writes_since_commit = 0

    for task, content in iter_downloaded(uncached_tasks):
        if cancel is not None and cancel.is_set():
            conn.commit()
            conn.close()
            emit("status", message="Indeksowanie przerwane.")
            emit("index-done", indexed=indexed, total=total_pages, cancelled=True)
            return

        processed += 1
        if content is None:
            emit("progress", current=processed, total=total_pages,
                 leaflet=task['leaflet_name'][:30], page=task['page_number'])
            continue

        try:
            ocr_text, boxes = ocr_engine.ocr_image_bytes(content, MODELS_DIR, use_gpu)
        except Exception as e:
            print(f"[OCR ERROR] {task['url']}: {e}", file=sys.stderr)
            ocr_text = None
        if ocr_text is None:
            emit("progress", current=processed, total=total_pages,
                 leaflet=task['leaflet_name'][:30], page=task['page_number'])
            continue

        save_page_to_cache(conn, task, ocr_text, boxes)
        indexed += 1
        writes_since_commit += 1
        # Commit often so searches running alongside see fresh pages.
        if writes_since_commit >= 10:
            conn.commit()
            writes_since_commit = 0

        emit("progress", current=processed, total=total_pages,
             leaflet=task['leaflet_name'][:30], page=task['page_number'])

    conn.commit()
    conn.close()
    emit("index-done", indexed=indexed, total=total_pages)


def search_pages(keyword):
    """Return hits for one keyword, newest leaflets first."""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    conn = open_db(read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT f.image_url, f.leaflet_name, f.page_number, p.boxes
            FROM ocr_fts f
            LEFT JOIN pages p ON p.image_url = f.image_url
            WHERE ocr_fts MATCH ?
            ORDER BY f.leaflet_name, CAST(f.page_number AS INTEGER)
            """,
            (build_fts_match_query(keyword),),
        ).fetchall()
    finally:
        conn.close()

    hits = []
    for image_url, leaflet_name, page_number, boxes_json in rows:
        boxes = []
        if boxes_json:
            try:
                # Only the boxes whose text matches are worth highlighting.
                needle = keyword.lower()
                boxes = [b for b in json.loads(boxes_json) if needle in b[0].lower()]
            except Exception:
                boxes = []
        hits.append({
            "image_url": image_url,
            "leaflet_name": leaflet_name,
            "page_number": int(page_number),
            "boxes": boxes,
        })
    return hits


def save_hit(image_url, leaflet_name, page_number):
    """Download one matched page into the gazetki folder."""
    os.makedirs(SAVE_FOLDER, exist_ok=True)
    return download_and_save_image({
        "url": image_url,
        "leaflet_name": leaflet_name,
        "page_number": page_number,
    })


def clear_cache():
    """Empty the OCR index and the saved pages, so everything is read again.

    Rows are deleted through SQL rather than by removing the file: the indexer
    thread may still hold the database open.
    """
    conn = init_cache_db()
    try:
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM ocr_fts")
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    removed_files = 0
    if os.path.isdir(SAVE_FOLDER):
        for name in os.listdir(SAVE_FOLDER):
            path = os.path.join(SAVE_FOLDER, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed_files += 1
                except OSError as e:
                    print(f"[RESET] nie usunięto {path}: {e}", file=sys.stderr)

    return pages, removed_files


def index_status():
    conn = open_db(read_only=True)
    try:
        indexed = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        last = conn.execute("SELECT MAX(indexed_at) FROM pages").fetchone()[0]
    finally:
        conn.close()
    return indexed, last


def stop_indexing(indexing, cancel, timeout=30.0):
    """Ask the indexer thread to stop and wait for it to actually finish."""
    if not indexing.is_set():
        return True
    emit("status", message="Zatrzymuję indeksowanie...")
    cancel.set()
    deadline = time.monotonic() + timeout
    while indexing.is_set() and time.monotonic() < deadline:
        time.sleep(0.2)
    cancel.clear()
    return not indexing.is_set()


def handle_command(command, indexing, start_index, cancel):
    """Dispatch one JSON command from the GUI. Returns True to keep serving."""
    global KEYWORD_TO_FIND, DISCORD_URL

    cmd = command.get("cmd")
    print(f"[CMD] {cmd}", file=sys.stderr, flush=True)

    if cmd == "search":
        keyword = command.get("keyword", "")
        emit("results", keyword=keyword, hits=search_pages(keyword))

    elif cmd == "search-many":
        for keyword in command.get("keywords", []):
            emit("results", keyword=keyword, hits=search_pages(keyword))

    elif cmd == "save":
        path = save_hit(command.get("image_url"), command.get("leaflet_name", "gazetka"),
                        command.get("page_number", 0))
        emit("saved", path=os.path.abspath(path) if path else None,
             image_url=command.get("image_url"))

    elif cmd == "discord":
        KEYWORD_TO_FIND = command.get("keyword", "")
        DISCORD_URL = command.get("webhook") or DISCORD_URL
        paths = []
        for hit in command.get("hits", []):
            path = save_hit(hit.get("image_url"), hit.get("leaflet_name", "gazetka"),
                            hit.get("page_number", 0))
            if path:
                paths.append(path)
        send_discord_gallery_dynamic(paths)
        emit("discord-done", sent=len(paths))

    elif cmd == "status":
        indexed, last = index_status()
        emit("index-status", indexed=indexed, last_indexed_at=last,
             indexing=indexing.is_set(), backend=ocr_engine.current_backend())

    elif cmd == "reset":
        # Clearing itself takes milliseconds; stopping a running index is the
        # only part that can take a moment.
        if not stop_indexing(indexing, cancel):
            emit("error", message="Nie udało się zatrzymać indeksowania. Zrestartuj aplikację.")
        else:
            pages, files = clear_cache()
            # Clearing only clears. Indexing restarts on demand or next launch.
            emit("cache-cleared", pages=pages, files=files)

    elif cmd == "stop-index":
        stop_indexing(indexing, cancel)
        emit("index-stopped")

    elif cmd == "reindex":
        if indexing.is_set():
            emit("error", message="Indeksowanie już trwa.")
        else:
            start_index()

    elif cmd == "quit":
        return False

    else:
        emit("error", message=f"Nieznana komenda: {cmd}")

    return True


def serve_main(use_gpu=False, auto_index=True):
    """Long-lived process: index in the background, answer searches immediately."""
    diag = {"platform": platform.system(), "python": sys.version, "DATA_DIR": DATA_DIR}
    print(f"[DIAG] {json.dumps(diag, ensure_ascii=False)}", file=sys.stderr)

    init_cache_db().close()
    indexing = threading.Event()
    cancel = threading.Event()

    # Build the engine here, on the main thread. Creating the ONNX sessions
    # from the indexer thread deadlocks and the indexer never starts.
    emit("status", message="Uruchamiam silnik OCR...")
    try:
        ocr_engine.get_engine(MODELS_DIR, use_gpu)
        emit("engine", backend=ocr_engine.current_backend())
    except Exception as e:
        import traceback
        print(f"[ENGINE FATAL] {traceback.format_exc()}", file=sys.stderr)
        emit("error", message=f"Nie udało się uruchomić silnika OCR: {e}")
        return

    def index_worker():
        indexing.set()
        try:
            run_index(use_gpu, cancel)
        except Exception as e:
            import traceback
            print(f"[INDEX FATAL] {traceback.format_exc()}", file=sys.stderr)
            emit("error", message=f"Błąd indeksowania: {e}")
            emit("index-done", indexed=0, total=0)
        finally:
            indexing.clear()

    def start_index():
        if indexing.is_set():
            return
        cancel.clear()
        threading.Thread(target=index_worker, daemon=True).start()

    if auto_index:
        start_index()

    indexed, last = index_status()
    emit("ready", indexed=indexed, last_indexed_at=last)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            emit("error", message=f"Niepoprawny JSON: {line[:120]}")
            continue
        try:
            if not handle_command(command, indexing, start_index, cancel):
                break
        except Exception as e:
            import traceback
            print(f"[CMD ERROR] {traceback.format_exc()}", file=sys.stderr)
            emit("error", message=f"Błąd komendy: {e}")

    emit("bye")


def main():
    global KEYWORD_TO_FIND

    print("=" * 60)
    KEYWORD_TO_FIND = input("Wpisz czego szukasz (np. mleko, masło): ").strip()
    while not KEYWORD_TO_FIND:
        print("Hasło nie może być puste!")
        KEYWORD_TO_FIND = input("Wpisz czego szukasz (np. mleko, masło): ").strip()

    os.makedirs(SAVE_FOLDER, exist_ok=True)
    print("=" * 60)
    print(f"   START SYSTEMU WYSZUKIWANIA PROMOCJI: '{KEYWORD_TO_FIND}'")
    print("=" * 60 + "\n")

    print("🔎 KROK 1: Skanuję stronę główną...")
    all_tasks, uuids, skipped = collect_tasks()
    if not uuids:
        print("❌ Nie znaleziono gazetek.")
        return
    if skipped:
        print(f"⚠️ Pominięto {skipped} gazetek bez czytelnego ID")

    total_pages = len(all_tasks)
    print(f"📂 KROK 2: {len(uuids)} gazetek, {total_pages} stron")
    print(f"🗂️ KROK 3: Ładuję indeks OCR ({OCR_CACHE_DB})")

    conn = init_cache_db()
    removed_pages = prune_cache_for_active_leaflets(conn, uuids)
    if removed_pages:
        print(f"   🧹 Usunięto z cache nieaktualne strony: {removed_pages}")
    cached_urls = get_cached_urls(conn, all_tasks)
    cached_tasks = [task for task in all_tasks if task["url"] in cached_urls]
    uncached_tasks = [task for task in all_tasks if task["url"] not in cached_urls]

    print(f"   ✅ W cache: {len(cached_tasks)} stron")
    print(f"   🆕 Do OCR: {len(uncached_tasks)} stron")

    task_by_url = {task["url"]: task for task in all_tasks}
    all_found_images_paths = []
    found_count = 0

    print("\n🔍 KROK 4: Wyszukiwanie w indeksie...")
    for image_url, leaflet_name, page_number in search_index(conn, KEYWORD_TO_FIND, cached_urls):
        task = task_by_url.get(image_url)
        if not task:
            continue
        saved_path = download_and_save_image(task)
        if saved_path:
            found_count += 1
            all_found_images_paths.append(saved_path)
            print(f"🔥 ZNALEZIONO (CACHE)! {leaflet_name} (Str. {page_number})")

    print("\n🚀 KROK 5: OCR nowych stron")
    processed = 0
    writes_since_commit = 0

    for task, content in iter_downloaded(uncached_tasks):
        processed += 1
        progress = (processed / len(uncached_tasks)) * 100 if uncached_tasks else 100
        status_msg = f"⏳ {processed}/{len(uncached_tasks)} ({progress:.0f}%) | {task['leaflet_name'][:20]}... S.{task['page_number']}"
        print(f"\r{status_msg:<80}", end="", flush=True)
        if content is None:
            continue

        try:
            ocr_text, boxes = ocr_engine.ocr_image_bytes(content, MODELS_DIR)
        except Exception as e:
            print(f"\n[OCR ERROR] {task['url']}: {e}", file=sys.stderr)
            continue
        if ocr_text is None:
            continue

        save_page_to_cache(conn, task, ocr_text, boxes)
        writes_since_commit += 1
        if writes_since_commit >= 25:
            conn.commit()
            writes_since_commit = 0

        if search_index(conn, KEYWORD_TO_FIND, [task["url"]]):
            saved_path = save_image_bytes(task['leaflet_name'], task['page_number'], content)
            found_count += 1
            all_found_images_paths.append(saved_path)
            print(f"\r{' ' * 80}\r", end="")
            print(f"🔥 ZNALEZIONO! {task['leaflet_name']} (Str. {task['page_number']})")

    conn.commit()
    conn.close()

    print(f"\n\n{'=' * 60}")
    print(f"   Znaleziono: {found_count}")

    if all_found_images_paths:
        if DISCORD_URL:
            send_discord_gallery_dynamic(all_found_images_paths)
        else:
            print("\n⚠️ Brak zmiennej DISCORD_WEBHOOK_URL w pliku .env. Pomijam wysyłanie na Discorda.")

    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Indekser gazetek Biedronki")
    parser.add_argument("--serve", action="store_true",
                        help="Tryb GUI: indeksuj w tle, odpowiadaj na komendy z stdin")
    parser.add_argument("--index", action="store_true",
                        help="Zindeksuj wszystkie gazetki i zakończ")
    parser.add_argument("--no-auto-index", action="store_true", default=False,
                        help="W trybie --serve nie startuj indeksowania automatycznie")
    parser.add_argument("--gpu", action="store_true", default=False,
                        help="Eksperymentalnie: licz OCR na GPU przez DirectML")
    args = parser.parse_args()

    use_gpu = args.gpu
    if args.serve:
        try:
            serve_main(use_gpu=use_gpu, auto_index=not args.no_auto_index)
        except Exception as e:
            import traceback
            print(f"[FATAL] {traceback.format_exc()}", file=sys.stderr)
            emit("error", message=f"Krytyczny błąd: {e}")
    elif args.index:
        try:
            run_index(use_gpu)
        except Exception as e:
            import traceback
            print(f"[FATAL] {traceback.format_exc()}", file=sys.stderr)
            emit("error", message=f"Krytyczny błąd: {e}")
    else:
        try:
            main()
        except Exception as e:
            print(f"\n❌ Błąd: {e}")
            input("Enter...")
