import os
import asyncio
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from google_play_scraper import search, app as play_store_app

# ---------------------------------------------------------
# কনফিগারেশন এবং এনভায়রনমেন্ট ভেরিয়েবল
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN or not ADMIN_ID or not CHANNEL_ID:
    print("Error: Environment variables are missing! Check Render config.")

# ---------------------------------------------------------
# Render.com এর জন্য ফ্লাস্ক সার্ভার
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ---------------------------------------------------------
# প্লে স্টোর স্ক্র্যাপিং ফাংশন (লিংক ছাড়া, শুধু ইমেইল)
# ---------------------------------------------------------
def scrape_emails(keyword):
    results_list = []
    try:
        # ১. কিওয়ার্ড দিয়ে সার্চ করা (প্রথম ২০টি অ্যাপ)
        search_results = search(
            keyword,
            lang='en',
            country='us',
            n_hits=20
        )

        for result in search_results:
            app_id = result['appId']
            
            # ২. প্রতিটি অ্যাপের বিস্তারিত তথ্য বের করা
            details = play_store_app(app_id)
            
            app_title = details.get('title', 'Unknown')
            support_email = details.get('developerEmail')
            rating = details.get('score', 0)
            
            # ৩. ফিল্টারিং লজিক:
            # - সাপোর্ট ইমেইল থাকতে হবে
            # - রেটিং ৪ এর নিচে হতে হবে অথবা নতুন অ্যাপ (রেটিং নেই)
            if support_email:
                if rating is None or rating < 4.0:
                    # লিংক বাদ দেওয়া হয়েছে, শুধু ইমেইল এবং অ্যাপের নাম রাখা হয়েছে
                    info = (
                        f"📧 `{support_email}`\n"
                        f"📱 App: {app_title} ({rating if rating else 'New'})"
                    )
                    results_list.append(info)
                    
    except Exception as e:
        print(f"Scraping Error: {e}")
        return None

    return results_list

# ---------------------------------------------------------
# টেলিগ্রাম বট লজিক
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != str(ADMIN_ID):
        await update.message.reply_text("⛔ আপনি এই বটের এডমিন নন।")
        return
    
    await update.message.reply_text(
        "👋 হ্যালো এডমিন!\n\n"
        "আমাকে কিওয়ার্ড দিন (যেমন: `loan app`, `vpn`)।\n"
        "আমি ৪ স্টারের নিচের অ্যাপগুলোর **সাপোর্ট ইমেইল** বের করে গ্রুপে পাঠিয়ে দেব।"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id != str(ADMIN_ID):
        return

    keyword = update.message.text
    status_msg = await update.message.reply_text(f"🔍 '{keyword}' এর ইমেইল খোঁজা হচ্ছে...")

    # স্ক্র্যাপিং শুরু
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, scrape_emails, keyword)

    if results is None:
        await status_msg.edit_text("❌ সার্চ করার সময় সমস্যা হয়েছে।")
    elif not results:
        await status_msg.edit_text("⚠️ কোনো উপযুক্ত ইমেইল পাওয়া যায়নি (৪ স্টারের নিচে)।")
    else:
        await status_msg.edit_text(f"✅ {len(results)} টি ইমেইল পাওয়া গেছে। গ্রুপে পাঠানো হচ্ছে...")
        
        # প্রাইভেট চ্যানেলে রেজাল্ট পাঠানো
        for info in results:
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=info,
                    parse_mode='Markdown'
                )
                await asyncio.sleep(1) # ফ্লাডিং আটকাতে বিরতি
            except Exception as e:
                print(f"Sending Error: {e}")
        
        await update.message.reply_text("🚀 সব ইমেইল পাঠানো শেষ!")

# ---------------------------------------------------------
# মেইন ফাংশন
# ---------------------------------------------------------
def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("Bot is polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
