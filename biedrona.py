# --- POCZĄTEK PEŁNEGO SKRYPTU (WERSJA 26 - HYBRYDA: STANDARD + KANAŁ ZIELONY) ---

import requests
from bs4 import BeautifulSoup
import re
from PIL import Image, ImageOps, ImageEnhance
import pytesseract
from io import BytesIO
import os
import threading
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# --- KONFIGURACJA ---
load_dotenv() 

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

KEYWORD_TO_FIND = "DADA" 
SAVE_FOLDER = "gazetki"
MAX_WORKERS = 5 # Utrzymujemy 5 wątków (każdy robi teraz 2x więcej pracy, więc nie zwiększamy)

DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")
MAX_DISCORD_SIZE_BYTES = 7.5 * 1024 * 1024 
MAX_DISCORD_FILES_COUNT = 10

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print_lock = threading.Lock()

# --------------------

def preprocess_red_background(img):
    """
    Metoda 'Snajper' z Wersji 25.
    Idealna na czerwone tła, słaba na turkusowe.
    Wyciąga kanał Zielony.
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    r, g, b = img.split()
    
    # Używamy kanału G (Zielonego) jako bazy
    img = g 
    
    # Powiększenie dla małych liter
    img = img.resize((img.width * 2, img.height * 2), Image.Resampling.BILINEAR)
    
    # Progowanie
    fn = lambda x : 255 if x > 100 else 0
    img = img.point(fn, mode='1')
    return img

def preprocess_standard(img):
    """
    Metoda Standardowa.
    Dobra na białe, żółte, turkusowe tła.
    """
    # Konwersja na szarość
    img = img.convert('L')
    
    # Lekkie powiększenie pomaga zawsze
    img = img.resize((int(img.width * 1.5), int(img.height * 1.5)), Image.Resampling.BILINEAR)
    
    # Auto-kontrast
    img = ImageOps.autocontrast(img)
    return img

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
        else:
            with print_lock:
                print(f"\n📨 Wysłano paczkę nr {batch_num}")
    except Exception:
        pass

def send_discord_gallery_dynamic(found_files):
    if not DISCORD_URL or not found_files: return
    print(f"\n📦 Pakowanie {len(found_files)} zdjęć dla Discorda...")

    current_batch_files = {}
    current_batch_embeds = []
    current_batch_size = 0
    current_batch_count = 0
    open_buffers = []
    batch_counter = 1

    for idx, file_path in enumerate(found_files):
        compressed_img = compress_image_for_discord(file_path)
        if not compressed_img: continue
        img_size = compressed_img.getbuffer().nbytes
        
        if (current_batch_size + img_size > MAX_DISCORD_SIZE_BYTES) or (current_batch_count >= MAX_DISCORD_FILES_COUNT):
            send_single_batch(current_batch_files, current_batch_embeds, batch_counter)
            batch_counter += 1
            current_batch_files = {}
            current_batch_embeds = []
            current_batch_size = 0
            current_batch_count = 0
            for b in open_buffers: b.close()
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
        for b in open_buffers: b.close()

def sanitize_filename(name):
    name = name.replace(" ", "_")
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name[:100]

def get_all_leaflet_uuids():
    main_page_url = "https://www.biedronka.pl/pl/gazetki"
    print(f"🔎 KROK 1: Skanuję stronę główną...")
    try:
        response = requests.get(main_page_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        leaflet_links = soup.find_all('a', href=re.compile(r'/pl/press,id,'))
        unique_links = list(set([link.get('href') for link in leaflet_links]))
        
        if not unique_links: return []
        
        print(f"✅ Wykryto {len(unique_links)} gazetek. Pobieram ID...")
        long_ids = set()
        for i, link in enumerate(unique_links):
            full_url = link if link.startswith("http") else f"https://www.biedronka.pl{link}"
            try:
                page_resp = requests.get(full_url, headers=HEADERS, timeout=10)
                match = re.search(r'window\.galleryLeaflet\.init\("([a-f0-9\-]{36})"\)', page_resp.text)
                if match: long_ids.add(match.group(1))
            except: pass
        return list(long_ids)
    except: return []

def get_leaflet_pages(leaflet_id):
    try:
        api_url = f"https://leaflet-api.prod.biedronka.cloud/api/leaflets/{leaflet_id}?ctx=web"
        response = requests.get(api_url, headers=HEADERS, timeout=10)
        data = response.json()
        pages_info = []
        name = data.get('name', f'Gazetka_{leaflet_id}')
        for page_data in data.get('images_desktop', []):
            valid_images = [img for img in page_data.get('images', []) if img]
            if valid_images:
                pages_info.append({"leaflet_name": name, "page_number": page_data.get('page') + 1, "url": valid_images[0]})
        return name, pages_info
    except: return "Nieznana", []

def process_page(task_data):
    url = task_data['url']
    name = task_data['leaflet_name']
    page = task_data['page_number']
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        content = resp.content
        
        # Wczytujemy oryginał
        img_original = Image.open(BytesIO(content))
        
        # --- SKAN 1: STANDARDOWY (Dla turkusowych, białych itp.) ---
        img_std = preprocess_standard(img_original.copy()) # Kopia, żeby nie zepsuć oryginału
        text_std = pytesseract.image_to_string(img_std, lang='pol')
        
        # --- SKAN 2: SNAJPER (Dla czerwonych i trudnych kontrastów) ---
        img_red = preprocess_red_background(img_original.copy())
        # Tutaj używamy konfiguracji psm 6 (blok tekstu), bo po progowaniu napisy są wyraźne
        text_red = pytesseract.image_to_string(img_red, lang='pol', config='--psm 6')
        
        # Łączymy wyniki z obu skanów
        full_text = text_std + " " + text_red
        
        if KEYWORD_TO_FIND.lower() in full_text.lower():
            safe_name = sanitize_filename(name)
            filename = f"{safe_name}_strona_{page}.png"
            path = os.path.join(SAVE_FOLDER, filename)
            with open(path, 'wb') as f: f.write(content)
            
            msg = f"🔥 ZNALEZIONO! {name} (Str. {page})"
            return True, msg, path 
        
        return False, None, None
    except Exception:
        return False, None, None

def main():
    os.makedirs(SAVE_FOLDER, exist_ok=True)
    print("="*60)
    print(f"   START SYSTEMU WYSZUKIWANIA PROMOCJI: '{KEYWORD_TO_FIND}'")
    print("="*60 + "\n")

    uuids = get_all_leaflet_uuids()
    if not uuids: return

    all_tasks = []
    print(f"\n📂 KROK 2: Przygotowuję listę stron...")
    for uuid in uuids:
        name, pages = get_leaflet_pages(uuid)
        if pages:
            print(f"   📄 {name[:50]:<50} ... {len(pages)} str.")
            all_tasks.extend(pages)
    
    total_pages = len(all_tasks)
    print(f"\n🚀 KROK 3: SKANOWANIE HYBRYDOWE (Standard + Anty-Czerwony)")
    
    processed = 0
    all_found_images_paths = []
    found_count = 0
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_task = {executor.submit(process_page, task): task for task in all_tasks}
        
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            processed += 1
            progress = (processed / total_pages) * 100
            status_msg = f"⏳ {processed}/{total_pages} ({progress:.0f}%) | {task['leaflet_name'][:20]}... S.{task['page_number']}"
            with print_lock: print(f"\r{status_msg:<80}", end="", flush=True)
            
            found, msg, saved_path = future.result()
            if found:
                found_count += 1
                all_found_images_paths.append(saved_path)
                with print_lock:
                    print(f"\r{' '*80}\r", end="") 
                    print(msg)

    print(f"\n\n{'='*60}")
    print(f"   Znaleziono: {len(all_found_images_paths)}")
    
    if DISCORD_URL and all_found_images_paths:
        send_discord_gallery_dynamic(all_found_images_paths)
    
    print("="*60)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Błąd: {e}")
        input("Enter...")