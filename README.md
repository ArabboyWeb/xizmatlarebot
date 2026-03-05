# Xizmatlar E-Bot

Telegram bot faqat **free API** servislar bilan ishlaydi.

## Yangi servislar
- `1secmail` - temporary email yaratish, inbox ko'rish, message ID bilan xabar o'qish.
  - Agar 1secmail bloklansa, bot avtomatik `mail.tm` ga fallback qiladi.
- `TinyURL` - uzun URL ni qisqartirish (`TINYURL_API_TOKEN` bo'lsa official API, bo'lmasa legacy free endpoint).
- `ShazamIO` - audio/voice dan trek aniqlash.
- `Tarjimon` - `Googletrans + LibreTranslate` (free), faqat quyidagi tillar:
  - `uz`
  - `en`
  - `ru`
  - `zh-cn`
- `Wikipedia` - maqola summary qidirish.
- `Rembg` - rasm fonini olib tashlash.
- `Pollinations AI` - free AI image generation.
  - Agar Pollinations API vaqtincha ishlamasa, bot fallback image qaytaradi (service jim qolmaydi).
- `Ob-havo`, `Valyuta`, `Konvertor` - oldingi servislar saqlangan.

## Muhim o'zgarish
- `Saver/Downloader` (direct link/YouTube yuklash) **olib tashlandi**.

## Telegram free cloud limitlari
Bot `/limits` buyrug'ida quyidagilarni ko'rsatadi:
- Upload limiti: `TELEGRAM_FREE_UPLOAD_LIMIT_MB` (default `50 MB`)
- Download (getFile) limiti: `TELEGRAM_FREE_DOWNLOAD_LIMIT_MB` (default `20 MB`)

Bu qiymatlar `.env` orqali sozlanadi.

## O'rnatish
```bash
pip install -r requirements.txt
```

## Sozlash
1. `.env.example` ni asos qilib `.env` tayyorlang.
2. Kamida `BOT_TOKEN` ni kiriting.
3. TinyURL official API ishlatmoqchi bo'lsangiz `TINYURL_API_TOKEN` ni kiriting.
4. LibreTranslate uchun kerak bo'lsa:
   - `LIBRETRANSLATE_ENDPOINT`
   - `LIBRETRANSLATE_API_KEY`
5. Temp mail fallback uchun ixtiyoriy:
   - `MAILTM_API_BASE`

## Ishga tushirish
```bash
python bot.py
```
