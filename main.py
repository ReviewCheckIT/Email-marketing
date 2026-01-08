# -*- coding: utf-8 -*-
import logging
import os
import json
import asyncio
import csv
import io
import sys
import random
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from google_play_scraper import search as play_search, app as app_details
from google.genai import Client
import firebase_admin
from firebase_admin import credentials, db

# --- Logging Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Env Variables ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
OWNER_ID = os.environ.get('BOT_OWNER_ID')
FB_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')
FB_URL = os.environ.get('FIREBASE_DATABASE_URL')
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL')
PORT = int(os.environ.get('PORT', '8080'))

# --- Global Flags ---
IS_SCRAPING = False  # একসাথে একাধিক স্ক্যান বন্ধ করার জন্য

# --- Firebase Init ---
try:
    if not firebase_admin._apps:
        cred_dict = json.loads(FB_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': FB_URL})
    logger.info("🔥 Firebase Global Database Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI Deep Keyword Expansion (Improved Logic) ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        # প্রম্পট পরিবর্তন করা হয়েছে: জনপ্রিয় অ্যাপ বাদ দিয়ে নতুন এবং আনকমন অ্যাপ খোঁজার জন্য
        prompt = f"Generate 50 specific, niche, and low-competition search terms related to '{base_kw}' for Google Play Store to find newly released apps. Do NOT use famous brand names. Provide only comma-separated values."
        
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        
        # ডাটা ক্লিনিং: নিউলাইন বা অন্য ক্যারেক্টার থাকলে কমা দিয়ে রিপ্লেস করা
        cleaned_text = response.text.replace('\n', ',').replace('•', '')
        kws = [k.strip() for k in cleaned_text.split(',') if k.strip()]
        
        # ইউনিক কিওয়ার্ড ফিল্টার
        final_kws = list(set([base_kw] + kws))
        logger.info(f"🤖 Gemini Generated {len(final_kws)} keywords.")
        return final_kws[:60] # সর্বোচ্চ ৬০টি কিওয়ার্ড
    except Exception as e:
        logger.error(f"⚠️ Gemini Error: {e}")
        return [base_kw]

# --- Global Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    global IS_SCRAPING
    IS_SCRAPING = True
    
    keywords = await get_expanded_keywords(base_kw)
    
    # সারা বিশ্বের ৩০টি দেশ (লিস্ট ঠিক রাখা হয়েছে)
    countries = [
        'us', 'gb', 'in', 'ca', 'br', 'au', 'de', 'id', 'ph', 'pk', 
        'za', 'mx', 'tr', 'sa', 'ae', 'ru', 'fr', 'it', 'es', 'nl',
        'bd', 'sg', 'my', 'vn', 'th', 'ng', 'eg', 'ar', 'co', 'pl'
    ]
    
    await context.bot.send_message(uid, f"🚀 **মিশন শুরু!**\n🔍 নিস: {base_kw}\n📝 কিওয়ার্ড: {len(keywords)}টি\n🌍 টার্গেট: সারা বিশ্ব (৩০টি দেশ)\n\n⚠️ গুগল যাতে ব্লক না করে তাই ধীরে কাজ হবে। ধৈর্য ধরুন...")
    
    new_count = 0
    consecutive_errors = 0
    session_leads = []
    ref = db.reference('scraped_emails')
    processed_apps = set() # ডুপ্লিকেট চেকার

    for kw in keywords:
        # প্রতি কিওয়ার্ডের জন্য দেশের লিস্ট এলোমেলো (Shuffle) করা হবে যাতে প্যাটার্ন না বোঝে
        random.shuffle(countries)
        
        # প্রতি কিওয়ার্ডের জন্য সর্বোচ্চ ৫-৭টি র্যান্ডম দেশ চেক করবে (সব দেশ চেক করলে ব্যান খাবে)
        target_countries = countries[:8] 

        for lang_country in target_countries:
            # যদি পরপর ৫ বার এরর আসে (মানে আইপি ব্লক), তাহলে ১ মিনিট ঘুমাবে
            if consecutive_errors >= 5:
                await context.bot.send_message(uid, "⏳ গুগল সাময়িক ব্লক দিয়েছে। বট ৬০ সেকেন্ড বিশ্রাম নিচ্ছে...")
                await asyncio.sleep(60)
                consecutive_errors = 0 # রিসেট
            
            try:
                # n_hits কমিয়ে ৮০ করা হয়েছে সেফ থাকার জন্য
                results = play_search(kw, n_hits=80, lang='en', country=lang_country)
                
                if not results:
                    consecutive_errors += 1
                    continue # রেজাল্ট না পেলে পরের দেশে যাও

                # সফল সার্চ হলে এরর কাউন্ট রিসেট
                consecutive_errors = 0

                for r in results:
                    app_id = r['appId']
                    if app_id in processed_apps: continue
                    processed_apps.add(app_id)

                    try:
                        # অ্যাপ ডিটেইলস ফেচ করা
                        app = app_details(app_id, lang='en', country=lang_country)
                        
                        email_raw = app.get('developerEmail')
                        if email_raw:
                            email_raw = email_raw.lower().strip()
                            score = app.get('score', 0)
                            reviews = app.get('reviews', 0)

                            # শর্ত: জিরো রেটিং এবং জিরো রিভিউ
                            if (score == 0 or score is None) and (reviews == 0 or reviews is None):
                                email_key = email_raw.replace('.', '_').replace('@', '_at_')
                                
                                # ডাটাবেজে আছে কিনা চেক
                                if not ref.child(email_key).get():
                                    data = {
                                        'app_name': app.get('title'),
                                        'email': email_raw,
                                        'rating': 0,
                                        'reviews': 0,
                                        'installs': app.get('installs'),
                                        'country': lang_country,
                                        'dev': app.get('developer'),
                                        'timestamp': datetime.now().isoformat()
                                    }
                                    ref.child(email_key).set(data)
                                    session_leads.append(data)
                                    new_count += 1
                                    
                                    # লগ প্রিন্ট (Render কনসোলে দেখার জন্য)
                                    logger.info(f"✅ Found: {email_raw} from {lang_country}")
                    except Exception as e:
                        continue # অ্যাপ ডিটেইলস না পেলে স্কিপ
                
                # প্রতি সার্চের পর ২ সেকেন্ড বিরতি (ব্যান ঠেকানোর আসল ওষুধ)
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"Search Error ({kw} - {lang_country}): {e}")
                consecutive_errors += 1
                await asyncio.sleep(5) # এরর খেলে ৫ সেকেন্ড থামবে

        # একটি কিওয়ার্ড শেষ হলে আপডেট
        if new_count > 0 and new_count % 10 == 0:
             # ইউজারকে বিরক্ত না করে শুধু লগ আপডেট
             pass

    IS_SCRAPING = False # কাজ শেষ, লক খুলে দাও

    # ফাইল জেনারেশন ও ডেলিভারি
    if session_leads:
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Country', 'Developer', 'Date'])
        for v in session_leads:
            cw.writerow([v.get('app_name'), v.get('email'), 0, 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
        
        output = io.BytesIO(si.getvalue().encode())
        output.name = f"Leads_{base_kw}_{datetime.now().strftime('%d_%m_%H%M')}.csv"
        
        await context.bot.send_document(
            chat_id=uid, 
            document=output, 
            caption=f"✅ **মিশন সম্পন্ন!**\n🔥 নতুন লিড: {new_count}টি\n📂 ফাইলটি ডাউনলোড করে নিন।"
        )
    else:
        await context.bot.send_message(uid, "❌ এই সেশনে কোনো নতুন জিরো-রেটিং অ্যাপ পাওয়া যায়নি। একটু পরে অন্য কিওয়ার্ড দিয়ে চেষ্টা করুন।")

# --- Handlers ---

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    
    # লক চেক
    status = "🔴 ব্যস্ত (Scraping running)" if IS_SCRAPING else "🟢 ফ্রি (Ready)"
    
    btn = [[InlineKeyboardButton("🌍 স্টার্ট মেগা স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text(
        f"🤖 **বট ড্যাশবোর্ড**\nঅবস্থা: {status}\n\nবট এখন স্মার্ট মোডে কাজ করবে। ব্যান এড়াতে ধীরে ধীরে খুঁজবে।", 
        reply_markup=InlineKeyboardMarkup(btn)
    )

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 ডাটাবেজে মোট লিড সংখ্যা: {count}")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data:
        await u.message.reply_text("কোনো ডেটা নেই!")
        return

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Country', 'Developer', 'Date'])
    for k, v in data.items():
        cw.writerow([v.get('app_name'), v.get('email'), 0, 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"Global_DB_{datetime.now().strftime('%d_%m')}.csv"
    await u.message.reply_document(document=output, caption=f"✅ সম্পূর্ণ ডাটাবেজ। মোট: {len(data)}")

async def clear_db(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    db.reference('scraped_emails').delete()
    await u.message.reply_text("🗑️ সব ডেটা ডিলিট করা হয়েছে।")

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id): return
    await q.answer()
    
    if IS_SCRAPING:
        await q.edit_message_text("⚠️ একটি কাজ ইতিমধ্যে চলছে! শেষ হওয়া পর্যন্ত অপেক্ষা করুন।")
        return

    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("কোন নিশের ইমেল চান? কিওয়ার্ড দিন (যেমন: VPN, Dating, Crypto):")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    
    # যদি স্ক্র্যাপিং চলতে থাকে, নতুন কমান্ড নিবে না
    if IS_SCRAPING:
        await u.message.reply_text("⚠️ দয়া করে অপেক্ষা করুন, আগের মিশন শেষ হয়নি।")
        return

    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        base_kw = u.message.text
        # ব্যাকগ্রাউন্ডে টাস্ক স্টার্ট
        asyncio.create_task(scrape_task(base_kw, c, u.effective_user.id))
        await u.message.reply_text(f"🔍 '{base_kw}' রিসিভ করেছি।\nAI কিওয়ার্ড জেনারেট করছে এবং ৩০টি দেশে খোঁজ শুরু হচ্ছে...")

def main():
    if not TOKEN: 
        logger.error("Token not found!")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("clear", clear_db))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN[-10:], 
                        webhook_url=f"{RENDER_URL}/{TOKEN[-10:]}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
