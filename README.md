# Xizmatlar E-Bot

Telegram bot faqat **free API** servislar bilan ishlaydi.

## Yangi servislar
- `Saqlash` - faqat direct fayl linkini yuklab, faylni chatga qaytaradi.
- `1secmail` - temporary email yaratish, inbox ko'rish, message ID bilan xabar o'qish.
  - Agar 1secmail bloklansa, bot avtomatik `mail.tm` ga fallback qiladi.
- `TinyURL` - uzun URL ni qisqartirish (`TINYURL_API_TOKEN` bo'lsa official API, bo'lmasa legacy free endpoint).
- `Musiqa qidirish` - qo'shiq nomi bo'yicha natija topadi.
- `Ish qidirish` - public vakansiyalarni topadi.
- `Tarjimon` - UZ/EN/RU/ZH oralig'ida tarjima qiladi. Tillar:
  - `uz`
  - `en`
  - `ru`
  - `zh`
- `YouTube` - qidiruv, link bo'yicha yuklash, video sifati tanlash va audio saqlash.
- `Wikipedia` - maqola summary qidirish.
- `Rasm yaratish` - prompt asosida rasm yaratadi.
- `Ob-havo`, `Valyuta`, `Konvertor` - oldingi servislar saqlangan.
- `Admin panel` - detal statistikalar va broadcast/reklama yuborish.
- `Neon/Postgres` - `DATABASE_URL` bo'lsa analytics va admin statistikalar bazada saqlanadi.

## Telegram free cloud limitlari
Bot `/limits` buyrug'ida quyidagilarni ko'rsatadi:
- Saqlash/upload limiti: `TELEGRAM_FREE_UPLOAD_LIMIT_MB` (default `50 MB`)
- Botga yuboriladigan fayl limiti: `TELEGRAM_FREE_DOWNLOAD_LIMIT_MB` (default `20 MB`)

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
5. Admin panel uchun:
   - `ADMIN_USER_IDS`
6. Neon/Postgres uchun ixtiyoriy:
   - `DATABASE_URL`
7. Temp mail fallback uchun ixtiyoriy:
   - `MAILTM_API_BASE`

## Eslatma
- RapidAPI bo'sh yoki blok bo'lsa, bot imkon bor joyda free fallback bilan ishlaydi.
- YouTube downloader uchun `yt-dlp` ishlatiladi.
- `Saqlash` oddiy veb-sahifa emas, to'g'ridan-to'g'ri fayl linklari uchun mo'ljallangan.

## Ishga tushirish
```bash
python bot.py
```
