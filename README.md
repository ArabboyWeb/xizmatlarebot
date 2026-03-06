# Xizmatlar E-Bot

Telegram bot faqat **free API** servislar bilan ishlaydi.

## Yangi servislar
- `1secmail` - temporary email yaratish, inbox ko'rish, message ID bilan xabar o'qish.
  - Agar 1secmail bloklansa, bot avtomatik `mail.tm` ga fallback qiladi.
- `TinyURL` - uzun URL ni qisqartirish (`TINYURL_API_TOKEN` bo'lsa official API, bo'lmasa legacy free endpoint).
- `Shazam Auto-Complete` - RapidAPI ishlasa o'sha ishlatiladi, bo'lmasa Deezer fallback.
- `JSearch Jobs` - RapidAPI ishlasa o'sha ishlatiladi, bo'lmasa free public jobs fallback.
- `Tarjimon` - RapidAPI Text-Translator2 ishlasa o'sha ishlatiladi, bo'lmasa free translator fallback. Tillar:
  - `uz`
  - `en`
  - `ru`
  - `zh`
- `YouTube Channel Search` - RapidAPI ishlasa o'sha ishlatiladi, bo'lmasa YouTube HTML fallback.
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
4. RapidAPI servislar uchun:
   - `RAPIDAPI_KEY`
   - kerak bo'lsa `YOUTUBE_CHANNEL_ID`
5. Temp mail fallback uchun ixtiyoriy:
   - `MAILTM_API_BASE`

## RapidAPI eslatma
- Agar RapidAPI obunasi bo'lmasa, bot avtomatik free fallbackga o'tadi.
- Agar aniq RapidAPI javobi kerak bo'lsa, RapidAPI dashboard orqali o'sha API ga obuna bo'lish kerak.
- Free plan mavjud bo'lsa free obuna qiling; pullik bo'lsa ishlatmaslik mumkin.

## Ishga tushirish
```bash
python bot.py
```
