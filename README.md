# biedra_promo_hounter

BiedraBOT — wyszukiwarka promocji w gazetkach Biedronki z OCR.

Aplikacja desktopowa (Electron + Python) uruchamiana lokalnie. Nie jest pakowana
do `.exe` ani do AppImage — odpalasz ją z katalogu projektu.

## Jak to działa

- **Indekser** (`biedrona.py --serve`) startuje razem z aplikacją, sprawdza jakie
  gazetki są aktualnie na stronie i OCR-uje w tle strony, których nie ma jeszcze
  w indeksie. Postęp widać na pasku na dole okna.
- **Wyszukiwanie** działa od pierwszej sekundy na tym, co już jest w indeksie.
  Wyniki dopisują się na żywo w miarę postępu OCR-u. Koszt OCR jest za stronę,
  nie za hasło, więc kolejne wyszukiwania są natychmiastowe.
- **Lista haseł** — wpisujesz swoje produkty raz, zapisują się w `config.json`
  i po zakończeniu indeksowania widzisz wszystkie trafienia naraz.

## Silnik OCR

RapidOCR (ONNX Runtime) z modelem rozpoznawania `latin_PP-OCRv5_mobile_rec`,
który obejmuje polskie znaki. Model (8 MB) dociąga się sam przy pierwszym
uruchomieniu do katalogu `models/`.

Domyślnie liczy na GPU przez **DirectML**, z automatycznym fallbackiem na CPU.
Przełącznik GPU/CPU jest w zakładce Ustawienia — zmiana restartuje indekser.
Rząd wielkości dla strony 1146x1800 na typowym desktopie:

| | s/strona | pełny indeks (~366 stron) |
|---|---|---|
| DirectML | 0,36 | ~4 min z pobieraniem |
| CPU | 1,28 | ~10 min |

Wynik rozpoznawania jest w obu przypadkach identyczny. `--cpu` wymusza CPU.

Tekst i pozycje trafień lądują w `ocr_cache.db` (SQLite + FTS5, tokenizer
`unicode61 remove_diacritics 2`). Zapytania są prefiksowe, więc „mleko" znajduje
też „mleka" i „mlekiem". Nieaktualne gazetki są usuwane z indeksu automatycznie.

## Wymagania

- **Node.js** 18+
- **Python** 3.10+

## Instalacja

```bash
npm install
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install --force-reinstall onnxruntime-directml
```

Ostatnia linia jest istotna: `rapidocr-onnxruntime` ciągnie zwykłe
`onnxruntime` jako zależność, a oba pakiety dostarczają ten sam moduł.
`onnxruntime-directml` musi je nadpisać, inaczej GPU nie zostanie użyte.

Aplikacja szuka Pythona najpierw w `.venv/`, potem w systemie.

## Uruchamianie

```bash
npm start
```

## Tryby z linii poleceń

```bash
.venv\Scripts\python biedrona.py --index    # zindeksuj wszystko i zakończ
.venv\Scripts\python biedrona.py --serve    # tryb GUI, komendy JSON na stdin
.venv\Scripts\python biedrona.py            # stary interaktywny tryb konsolowy
```

## Discord

Webhook ustawiasz w zakładce Ustawienia albo w `.env`
(`DISCORD_WEBHOOK_URL=...`). Wysyłka jest ręczna — przycisk „Wyślij na
Discorda" w widoku wyników.
