# -*- coding: utf-8 -*-
# Advanced Play Store Scraper Bot (Production Ready)
# Deploy Target: Render.com
# Secrets Management: Environment Variables

import logging
import os
import sys
import json
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from google_play_scraper import search as play_search
from google_play_scraper.exceptions import GooglePlayScraperException
from google import genai # ✅ CORRECTED: Using the modern 'google.genai' import

# --- Firebase Import ---
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_INITIALIZED = False
except ImportError:
    logging.error("Firebase libraries not found. Please install 'firebase-admin'.")
    sys.exit(1)

# --- Environment Variables (Load from Render) ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TARGET_CHAT_ID = os.environ.get('TARGET_CHAT_ID') # Group ID to send leads
BOT_OWNER_ID = os.environ.get('BOT_OWNER_ID')     # Your Telegram ID
FIREBASE_CREDENTIALS_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON') # Full JSON string
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY') # AI Key

# Webhook Config for Render
WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL') # Render automatically sets this usually, or set manually
PORT = int(os.environ.get('PORT', '8080'))

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
COLLECTION_EMAILS = 'scraped_app_emails'
COLLECTION_ADMINS = 'admins'

# --- Firebase Initialization ---
def initialize_firebase():
    global FIREBASE_INITIALIZED, db
    if FIREBASE_INITIALIZED: return
        
    if not FIREBASE_CREDENTIALS_JSON:
        return
        
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        FIREBASE_INITIALIZED = True
        logger.info("🔥 Firebase Connected Successfully!")
    except Exception as e:
        logger.error(f"Firebase Init Error: {e}", exc_info=True)
        sys.exit(1)

# Initialize Firebase immediately
initialize_firebase()

# --- AI Logic (Gemini) ---
def get_ai_keywords(base_keyword: str) -> list:
    """Generates targeted search queries for NEW apps using Gemini AI."""
    if not GEMINI_API_KEY:
        logger.warning("Gemini API Key missing. Using basic search.")
        return [base_keyword]

    try:
        # Use the new genai client
        client = genai.Client(api_key=GEMINI_API_KEY) 
        
        prompt = (
            f"Generate 6 specific Play Store search terms related to '{base_keyword}'. "
            "Focus on finding NEWLY RELEASED apps, Indie apps, or apps with low competition. "
            "Keywords should help find apps that likely have 0 ratings or low downloads. "
            "Output format: Comma separated string only. Example: New {base_keyword} 2025, Simple {base_keyword}."
        )
        
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt
        )
        
        keywords = [k.strip() for k in response.text.split(',') if k.strip()]
        keywords.insert(0, base_keyword) # Add original keyword
        return keywords[:7] # Limit to 7 queries
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return [base_keyword]

# --- Database Helpers ---
async def is_admin(user_id: int) -> bool:
    # Need to check if db is initialized before trying to use it
    if not FIREBASE_INITIALIZED: return False
    
    if str(user_id) == str(BOT_OWNER_ID): 
        # Ensure owner is added to admin list if db is ready
        try:
            db.collection(COLLECTION_ADMINS).document(str(user_id)).set({'user_id': user_id, 'added_by': 'System/Owner', 'added_at': datetime.now().isoformat()})
        except Exception as e:
            logger.warning(f"Could not confirm owner status in DB: {e}")
        return True
        
    doc = db.collection(COLLECTION_ADMINS).document(str(user_id)).get()
    return doc.exists

async def check_if_email_exists(email: str) -> bool:
    if not FIREBASE_INITIALIZED: return False
    doc = db.collection(COLLECTION_EMAILS).document(email.lower()).get()
    return doc.exists

# --- Bot Handlers (No change in logic) ---
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): 
        await update.message.reply_text("দুঃখিত, আপনি অ্যাডমিন নন।")
        return
        
    keyboard = [
        [InlineKeyboardButton("🚀 AI Smart Search (New Apps)", callback_data='start_search')],
        [InlineKeyboardButton("📂 Export CSV", callback_data='export_data')],
        [InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin_panel')]
    ]
    await update.message.reply_text("🤖 **Advanced Scraper Bot Ready!**\nSelect an action:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    if not await is_admin(query.from_user.id): 
        await query.edit_message_text("অ্যাডমিন অনুমতি প্রয়োজন।")
        return
        
    if query.data == 'start_search':
        context.user_data['state'] = 'await_keyword'
        await query.edit_message_text("🔍 **Enter a Topic/Keyword:**\n(AI will expand this to find hidden/new apps)")
    
    elif query.data == 'export_data':
        await export_logic(update.effective_user.id, context)
        await query.edit_message_text("CSV ফাইল প্রস্তুত হচ্ছে, মেসেজ দেখুন।")
        
    elif query.data == 'admin_panel':
        await query.edit_message_text("Admin Panel feature coming soon.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_admin(user_id): return
    
    state = context.user_data.get('state')
    text = update.message.text.strip()
    
    if state == 'await_keyword':
        context.user_data['keyword'] = text
        context.user_data['state'] = None
        
        await update.message.reply_text(f"🧠 AI Processing: **{text}**... Please wait.")
        asyncio.create_task(run_smart_search(text, context, user_id))

# --- Core Search Logic ---
async def run_smart_search(base_keyword: str, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    
    if not FIREBASE_INITIALIZED:
        await context.bot.send_message(user_id, "❌ Firebase Initialization Failed. Check FIREBASE_CREDENTIALS_JSON.")
        return
        
    # 1. Get AI Keywords
    keywords = get_ai_keywords(base_keyword)
    status_msg = await context.bot.send_message(user_id, f"📝 **Scanning Keywords:**\n" + ", ".join(keywords))
    
    new_leads = []
    
    for kw in keywords:
        try:
            # Search logic
            results = play_search(kw, lang='en', country='us', n_hits=60)
            
            for app in results:
                email = app.get('developerEmail')
                score = app.get('score')
                
                # STRICT FILTERS for NEW/STRUGGLING APPS
                # Target: No Rating (New) OR Rating <= 3.7 (Needs help)
                is_target = (score is None) or (score == 0) or (score <= 3.7)
                
                if email and is_target:
                    clean_email = email.strip().lower()
                    
                    # Duplicate Check
                    if not await check_if_email_exists(clean_email):
                        data = {
                            'name': app['title'],
                            'email': clean_email,
                            'score': score if score else 0.0,
                            'installs': app.get('installs'),
                            'keyword': kw,
                            'scraped_at': datetime.now().isoformat()
                        }
                        # Save to Firebase
                        db.collection(COLLECTION_EMAILS).document(clean_email).set(data)
                        new_leads.append(data)
                        
        except Exception as e:
            logger.error(f"Search error for {kw}: {e}")
            
    # Reporting
    if new_leads:
        report = f"✅ **Mission Success!**\nTopic: {base_keyword}\n🆕 New Unique Leads: {len(new_leads)}\n\n"
        for lead in new_leads[:10]:
            report += f"📱 {lead['name']} ({lead['score'] or 'New'})\n📧 `{lead['email']}`\n\n"
        
        if len(new_leads) > 10: report += f"...and {len(new_leads)-10} more saved to database."
        
        # Send to Admin
        await context.bot.send_message(user_id, report, parse_mode=ParseMode.MARKDOWN)
        
        # Send to Channel if Configured
        if TARGET_CHAT_ID:
            try:
                await context.bot.send_message(TARGET_CHAT_ID, report, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Channel send failed: {e}")
    else:
        await context.bot.send_message(user_id, f"⚠️ No NEW unique apps found for '{base_keyword}'. Try a different niche.")

async def export_logic(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    if not FIREBASE_INITIALIZED:
        await context.bot.send_message(user_id, "❌ Firebase Initialization Failed. Cannot Export.")
        return
        
    docs = db.collection(COLLECTION_EMAILS).stream()
    csv_data = "Email,App Name,Rating,Installs,Source\n"
    count = 0
    for doc in docs:
        d = doc.to_dict()
        csv_data += f"{d.get('email')},{d.get('name', '').replace(',', '')},{d.get('score')},{d.get('installs')},{d.get('keyword')}\n"
        count += 1
        
    if count > 0:
        await context.bot.send_document(user_id, document=csv_data.encode(), filename="leads.csv", caption=f"📊 Total Leads: {count}")
    else:
        await context.bot.send_message(user_id, "Database is empty.")

# --- Main Execution ---
def main() -> None:
    # CRITICAL CHECK: Bot will crash if this is missing
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ CRITICAL ERROR: TELEGRAM_BOT_TOKEN missing in Render Environment Variables! Deployment failed.")
        sys.exit(1)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Render Webhook Logic
    if WEBHOOK_URL:
        # We need to explicitly set the webhook path for deployment
        webhook_path = f'/{TELEGRAM_BOT_TOKEN}'
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=f"{WEBHOOK_URL}{webhook_path}"
        )
        logger.info(f"🚀 Webhook running on {WEBHOOK_URL}{webhook_path}")
    else:
        # Fallback to polling for local testing
        logger.info("Starting in Polling Mode (WEBHOOK_URL not set).")
        application.run_polling()

if __name__ == "__main__":
    main()
