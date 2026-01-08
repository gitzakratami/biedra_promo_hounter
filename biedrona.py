# --- POCZĄTEK PEŁNEGO SKRYPTU (WERSJA 21 - PRAWDZIWA GALERIA) ---

import requests
from bs4 import BeautifulSoup
import re
from PIL import Image
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

KEYWORD_TO_FIND = "papier" 
SAVE_FOLDER = "gazetki"
MAX_WORKERS = 5

DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print_lock = threading.Lock()

# --------------------

def compress_image_for_discord(image_path):
    """Kompresuje obraz do JPG w pamięci RAM."""
    try:
        img = Image.open(image_path)
        if img.mode in ("RGBA", "P"): 
            img = img.convert("RGB")
            
        if img.width > 2000:
            ratio = 2000 / img.width
            new_height = int(img.height * ratio)
            img = img.resize((2000, new_height), Image.Resampling.LANCZOS)

        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"Błąd kompresji: {e}")
        return None

def send_discord_gallery(found_files):
    if not DISCORD_URL or not found_files:
        return

    # Discord pozwala na max 10 embedów w jednej wiadomości.
    # Ustawiamy 4, żeby zdjęcia były duże i czytelne w siatce (2x2).
    chunk_size = 4
    chunks = [found_files[i:i + chunk_size] for i in range(0, len(found_files), chunk_size)]

    print(f"\n📨 Wysyłam wyniki na Discorda w {len(chunks)} galeriach...")

    for i, chunk in enumerate(chunks):
        files = {}
        embeds = []
        buffers = []
        
        try:
            # Budujemy strukturę wiadomości
            for idx, file_path in enumerate(chunk):
                compressed_img = compress_image_for_discord(file_path)
                
                if compressed_img:
                    buffers.append(compressed_img)
                    
                    # Nazwa pliku dla Discorda (musi być unikalna w obrębie wiadomości)
                    filename = f"img_{i}_{idx}.jpg"
                    
                    # Dodajemy plik do załączników
                    files[filename] = (filename, compressed_img, "image/jpeg")
                    
                    # Dodajemy Embed, który odwołuje się do tego załącznika
                    # To jest klucz do wyświetlania jako galeria!
                    embed = {
                        "url": "https://www.biedronka.pl/pl/gazetki", # Link po kliknięciu w tytuł
                        "image": {
                            "url": f"attachment://{filename}"
                        }
                    }
                    
                    # Dodajemy tytuł tylko do pierwszego zdjęcia w paczce, żeby nie śmiecić
                    if idx == 0:
                        embed["title"] = f"Znaleziono: {KEYWORD_TO_FIND} (Paczka {i+1})"
                        embed["color"] = 5763719 # Zielony
                    
                    embeds.append(embed)

            if not files:
                continue

            # Konstruujemy payload JSON
            payload = {
                "content": "", # Pusta treść, bo wszystko jest w embedach
                "embeds": embeds
            }
            
            # Wysyłamy multipart/form-data (pliki + JSON)
            response = requests.post(
                DISCORD_URL, 
                data={"payload_json": json.dumps(payload)}, 
                files=files
            )
            
            if response.status_code not in [200, 204]:
                print(f"⚠️ Błąd Discorda przy paczce {i+1}: {response.status_code} - {response.text}")
            else:
                print(f"   -> Wysłano galerię {i+1}")
                
        except Exception as e:
            print(f"⚠️ Błąd wysyłania paczki: {e}")
        finally:
            for b in buffers:
                b.close()

def sanitize_filename(name):
    name = name.replace(" ", "_")
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name[:100]

def get_all_leaflet_uuids():
    main_page_url = "https://www.biedronka.pl/pl/gazetki"
    print(f"🔎 KROK 1: Wchodzę na stronę główną: {main_page_url}...")
    
    try:
        response = requests.get(main_page_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        leaflet_links = soup.find_all('a', href=re.compile(r'/pl/press,id,'))
        unique_links = list(set([link.get('href') for link in leaflet_links]))
        
        if not unique_links:
            print("❌ Nie znaleziono linków. Strona mogła się zmienić.")
            return []
        
        print(f"✅ Znaleziono {len(unique_links)} gazetek. Rozpoczynam namierzanie ID...")

        long_ids = set()
        for i, link in enumerate(unique_links):
            full_url = link if link.startswith("http") else f"https://www.biedronka.pl{link}"
            try:
                page_resp = requests.get(full_url, headers=HEADERS, timeout=10)
                match = re.search(r'window\.galleryLeaflet\.init\("([a-f0-9\-]{36})"\)', page_resp.text)
                if match:
                    long_ids.add(match.group(1))
            except:
                pass
        
        return list(long_ids)

    except Exception as e:
        print(f"❌ Błąd krytyczny: {e}")
        return []

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
                pages_info.append({
                    "leaflet_name": name,
                    "page_number": page_data.get('page') + 1,
                    "url": valid_images[0]
                })
        return name, pages_info
    except:
        return "Nieznana", []

def process_page(task_data):
    url = task_data['url']
    name = task_data['leaflet_name']
    page = task_data['page_number']
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        content = resp.content
        
        img = Image.open(BytesIO(content))
        text = pytesseract.image_to_string(img, lang='pol')
        
        if KEYWORD_TO_FIND.lower() in text.lower():
            safe_name = sanitize_filename(name)
            filename = f"{safe_name}_strona_{page}.png"
            path = os.path.join(SAVE_FOLDER, filename)
            
            with open(path, 'wb') as f:
                f.write(content)
            
            msg = f"🔥 ZNALEZIONO! {name} (Str. {page})"
            return True, msg, path 
        
        return False, None, None

    except Exception:
        return False, None, None

def main():
    os.makedirs(SAVE_FOLDER, exist_ok=True)
    print("="*60)
    print(f"   START SYSTEMU WYSZUKIWANIA PROMOCJI: '{KEYWORD_TO_FIND}'")
    
    if DISCORD_URL:
        print("   ✅ Discord Webhook aktywny.")
    
    print("="*60 + "\n")

    uuids = get_all_leaflet_uuids()
    if not uuids: return

    all_tasks = []
    print(f"\n📂 KROK 2: Przygotowuję listę stron do sprawdzenia:")
    for uuid in uuids:
        name, pages = get_leaflet_pages(uuid)
        if pages:
            print(f"   📄 {name[:50]:<50} ... ma {len(pages)} stron")
            all_tasks.extend(pages)
    
    total_pages = len(all_tasks)
    print(f"\n🚀 KROK 3: URUCHAMIAM TURBO SKANOWANIE ({MAX_WORKERS} wątki na raz)")
    
    processed = 0
    all_found_images_paths = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_task = {executor.submit(process_page, task): task for task in all_tasks}
        
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            processed += 1
            
            progress = (processed / total_pages) * 100
            status_msg = f"⏳ Postęp: {processed}/{total_pages} ({progress:.1f}%) | Analizuję: {task['leaflet_name'][:30]}... Str. {task['page_number']}"
            
            with print_lock:
                print(f"\r{status_msg:<100}", end="", flush=True)
            
            found, msg, saved_path = future.result()
            
            if found:
                all_found_images_paths.append(saved_path)
                with print_lock:
                    print(f"\r{' '*100}\r", end="") 
                    print(msg)
                    print(f"   -> Zapisano: {saved_path}")

    print(f"\n\n{'='*60}")
    print(f"   KONIEC SKANOWANIA")
    print(f"   Znaleziono łącznie: {len(all_found_images_paths)} stron z frazą '{KEYWORD_TO_FIND}'.")
    
    # KROK 4: Wysyłanie grupowe na Discorda (Galerie)
    if DISCORD_URL and all_found_images_paths:
        send_discord_gallery(all_found_images_paths)
    elif DISCORD_URL and not all_found_images_paths:
        print("   Brak wyników do wysłania na Discorda.")
    
    print("="*60)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Wystąpił niespodziewany błąd: {e}")
        input("Naciśnij Enter, aby zamknąć...")