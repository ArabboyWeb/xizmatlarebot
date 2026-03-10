# Xizmatlar E-Bot

Telegram bot faqat **free API** servislar bilan ishlaydi.

## Yangi servislar
- `Saqlash` - direct fayl, YouTube, Instagram va TikTok linklarini yuklab, faylni chatga qaytaradi.
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
- `YouTube` - qidiruv, link bo'yicha yuklash, video sifati tanlash va audio saqlash. Instagram/TikTok direct video linklari ham shu bo'limda yuklanadi.
- `Wikipedia` - maqola summary qidirish.
- `Rasm yaratish` - prompt asosida rasm yaratadi.
- `Sun'iy Intellekt` - Free / Premium / Pro planli AI chat, kredit dashboard, smart routing, plan/model selector va yashirin Telegram kanal arxivi.
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
5. AI uchun:
   - `OPENROUTER_API_KEY`
   - `AI_PRO_PROVIDER`
   - `AI_PRO_OPENAI_API_KEY` yoki `AI_PRO_GOOGLE_API_KEY`
   - `AI_LOG_CHANNEL_ID` yoki botni kanalga admin qilib qo'shing
   - `AI_LOG_CHANNEL_LINK`
6. Admin panel uchun:
   - `ADMIN_USER_IDS`
7. Neon/Postgres uchun ixtiyoriy:
   - `DATABASE_URL`
8. Temp mail fallback uchun ixtiyoriy:
   - `MAILTM_API_BASE`

## Eslatma
- RapidAPI bo'sh yoki blok bo'lsa, bot imkon bor joyda free fallback bilan ishlaydi.
- YouTube downloader uchun `yt-dlp` ishlatiladi.
- `Saqlash` oddiy veb-sahifa emas, to'g'ridan-to'g'ri fayl linklari uchun mo'ljallangan.
- AI free plan 5 soniyalik cooldown va kunlik limit bilan ishlaydi.
- AI chat matnlari bazaga yozilmaydi; ular Telegram arxiv kanaliga yuboriladi.
- AI arxiv kanali foydalanuvchiga ko'rsatilmaydi.
- AI ichida user-level `plan` va `model` selector bor.
- Agar `AI_LOG_CHANNEL_ID` bo'sh bo'lsa, bot kanalga qo'shilganda `channel_post` yoki `my_chat_member` orqali kanal ID auto-detect qilinadi.
- AI plan boshqaruvi uchun admin buyruqlari bor:
  - `/ai`
  - `/ai_diag`
  - `/ai_set_plan <user_id> <free|premium|pro> [credits]`
  - `/ai_set_credits <user_id> <credits>`

## Ishga tushirish
```bash
python bot.py
```

## Railway deploy
1. Repo ni Railway'ga ulang.
2. `Variables` bo'limida `.env` dagi kerakli qiymatlarni kiriting:
   - `BOT_TOKEN` (majburiy)
   - `ADMIN_USER_IDS` (tavsiya)
   - `DATABASE_URL` (ixtiyoriy)
   - boshqalar (`RAPIDAPI_KEY`, `TINYURL_API_TOKEN`, ...)
3. Deploy qiling. Bot `railway.toml` dagi `python bot.py` bilan worker sifatida ishga tushadi.
