# Telegram Direct Link Downloader Bot

Bu bot foydalanuvchi yuborgan direct download linkdan faylni stream qilib yuklab oladi va Telegram chatga yuboradi.

## Nimalar yaxshilandi

- Kuchli error handling (URL, network, Telegram, disk xatolari alohida ushlanadi)
- Retry + exponential backoff
- Progress: yuklash/yuborish foiz, MB/s, elapsed sec, ETA
- Per-user va global parallel limit
- 4GB gacha konfiguratsiya (`MAX_FILE_SIZE_MB=4096`)
- Dual-worker (parallel range) tezlashtirilgan yuklash
- `/process`, `/stats`, `/cancel` orqali runtime kuzatuv
- Video/audio/photo/animation auto-yuborish (fallback: document)
- Telegram URL send rejimi (server-to-server yuborish, upload bottleneckni kamaytiradi)
- Rotating log fayllar (`logs/bot.log`)
- Polling yiqilsa avtomatik qayta ishga tushish
- `caption` ichidagi URL ham qo'llab-quvvatlanadi

## Muhim limit (4GB haqida)

Telegram cloud Bot API odatda katta uploadlarda cheklovga ega bo'lishi mumkin.  
4GB ga yaqin fayl yuborish uchun amalda self-hosted Telegram Bot API server kerak bo'ladi va `BOT_API_BASE` bilan ulanish kerak.

## 1) O'rnatish

Tavsiya etilgan Python versiya: `3.10`, `3.11` yoki `3.12`.

### Windows uchun eng oson usul (1 buyruq)

PowerShell'da loyiha papkasida:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

Bu script avtomatik:
- `.venv` yaratadi (kerak bo'lsa)
- paketlarni o'rnatadi
- eski `bot.py` processlarini tozalaydi
- `bot.lock` ni tozalaydi
- botni ishga tushiradi

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` ichida kamida `BOT_TOKEN` ni to'ldiring.

## 2) Ishga tushirish (local)

```powershell
python bot.py
```

## 3) Foydalanish

Botga link yuboring:

```text
https://example.com/archive.zip
```

Qo'llanadi:
- oddiy text ichidagi URL
- caption ichidagi URL
- kontent turiga qarab yuborish: video/audio/photo/animation/document

Buyruqlar:
- `/start`
- `/help`
- `/limits`
- `/process`
- `/stats`
- `/cancel`

## 4) PythonAnywhere deploy (Always-on task)

1. Kodni PythonAnywhere serverga yuklang (`Files` yoki `git clone`).
2. Bash consoleni oching.
3. Loyihaga kiring va virtualenv yarating:

```bash
cd /home/<username>/dowloader2v
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

4. `.env` ni to'ldiring (`BOT_TOKEN`, kerak bo'lsa `BOT_API_BASE`).
5. Start scriptga execute ruxsat bering:

```bash
chmod +x /home/<username>/dowloader2v/start_pythonanywhere.sh
```

6. PythonAnywhere panelda `Tasks` -> `Always-on tasks` oching.
7. Yangi taskga buyruq kiriting:

```bash
cd /home/<username>/dowloader2v && ./start_pythonanywhere.sh
```

8. Loglarni tekshiring:
- App log: `logs/bot.log`
- Always-on task output: PythonAnywhere task log oynasi

## 5) Tavsiya etilgan production sozlamalari

- `MAX_FILE_SIZE_MB=4096`
- `CONCURRENT_DOWNLOADS=4`
- `PER_USER_DOWNLOAD_LIMIT=1`
- `DOWNLOAD_WORKERS=4`
- `PARALLEL_DOWNLOAD_MIN_MB=64`
- `DOWNLOAD_CHUNK_KB=8192`
- `UPLOAD_CHUNK_KB=2048`
- `PROGRESS_INTERVAL_SECONDS=4`
- `SEND_PROGRESS_INTERVAL_SECONDS=3`
- `HTTP_CONNECTOR_LIMIT=300`
- `HTTP_CONNECTOR_LIMIT_PER_HOST=100`
- `TELEGRAM_URL_SEND=true`
- `PREFER_NATIVE_MEDIA=true`
- `NATIVE_MEDIA_MAX_MB=1024`
- `MAX_RETRIES=3`
- `RETRY_BACKOFF_SECONDS=2`
- `READ_TIMEOUT_SECONDS=120`
- `PROCESS_MONITOR_INTERVAL_SECONDS=30`

## 6) Xavfsizlik

- Ixtiyoriy ravishda `ALLOWED_USER_IDS` bilan botni private qiling.
- Local/private host linklar bloklanadi (`localhost`, private IP).
- Bot bitta instance rejimida ishlaydi (`LOCK_FILE`) va dublikat ishga tushirishni bloklaydi.

## 7) Diagnostika

- URL direct bo'lmasa bot xabar beradi.
- Telegram upload xatosi bo'lsa aniq sababi chiqariladi.
- Tarmoq nosozligida bot retry qiladi, so'ng foydalanuvchiga xabar beradi.
- Agar siz localda Python 3.14 ishlatsangiz, ayrim dependencylar wheel muammosi berishi mumkin; 3.11 tavsiya qilinadi.
- `TelegramUnauthorizedError` bo'lsa: token noto'g'ri yoki revoke qilingan. BotFather'dan yangi token olib `.env` dagi `BOT_TOKEN` ni yangilang.
- `TelegramConflictError` bo'lsa: odatda boshqa bot instance yurib turadi. Avval eski `python bot.py` jarayonini to'xtating.
- Windowsda `WinError 32` bo'lsa: bot endi temp faylni retry bilan o'chiradi; baribir qolsa keyingi ishga tushishda tozalanadi.
