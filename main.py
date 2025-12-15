from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from config import BOT_TOKEN, ADMIN_ID, PRIVATE_CHAT_ID
from playstore_scraper import search_apps, extract_support_email

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "✅ Bot Active\n\nUse:\n/search keyword"
    )

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("❌ Keyword দিন")
        return

    keyword = " ".join(context.args)
    await update.message.reply_text(f"🔍 Searching: {keyword}")

    apps = search_apps(keyword)
    result = []

    for app in apps:
        emails = extract_support_email(app)
        if emails:
            for e in emails:
                result.append(f"{e}\n{app}")

    if result:
        msg = "\n\n".join(result)
        await context.bot.send_message(
            chat_id=PRIVATE_CHAT_ID,
            text=f"📩 Emails Found for: {keyword}\n\n{msg}"
        )
    else:
        await update.message.reply_text("❌ No emails found")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))

    app.run_polling()

if __name__ == "__main__":
    main()
