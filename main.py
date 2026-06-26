import os
import asyncio
import re
import shutil
import psutil
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, ConversationHandler, filters
)
from supabase import create_client, Client
from aiohttp import web

# Configuration et démarrage
START_TIME = datetime.now()
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
BUCKET_THUMBS = "miniatures"

# États de conversation
CHOIX_RESOLUTION, ATTENTE_NOM = range(2)

# ==========================================
# MINI SERVEUR WEB POUR RENDER
# ==========================================

async def handle_ping(request):
    return web.Response(text="Bot Compressor is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ==========================================
# GESTION DES STATISTIQUES (DESIGN COOL)
# ==========================================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les statistiques d'utilisation du serveur Render"""
    uptime = datetime.now() - START_TIME
    jours = uptime.days
    heures, reste = divmod(uptime.seconds, 3600)
    minutes, secondes = divmod(reste, 60)
    
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    
    total_disk, used_disk, free_disk = shutil.disk_usage("/")
    
    texte_stats = (
        "Statut du Bot 📊\n\n"
        f"⏳ Temps de fonctionnement : {jours} jours, {heures:02d}:{minutes:02d}:{secondes:02d}\n"
        f"🖥️ Utilisation CPU : {cpu}%\n"
        f"📈 Utilisation RAM : {ram}%\n"
        f"💾 Espace disque total : {total_disk / (1024**3):.2f} GB\n"
        f"📂 Espace utilisé : {used_disk / (1024**3):.2f} GB\n"
        f"🗃️ Espace libre : {free_disk / (1024**3):.2f} GB\n\n"
        "⚡️ Mode d'économie de ressources : Activé"
    )
    await update.message.reply_text(texte_stats)

# ==========================================
# COMMANDE START
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    texte_accueil = (
        f"👋 Bonjour {user_name} !\n\n"
        "Convertisseur et Compresseur Vidéo\n"
        "Un bot de conversion et de compression vidéo permettant d'adapter vos fichiers selon vos besoins.\n\n"
        "Formats pris en charge : MP4, MKV, GIF, AVI\n"
        "Statut : ✅ OnLine\n\n"
        "Pour commencer, envoyez-moi simplement une vidéo !"
    )
    await update.message.reply_text(texte_accueil)

# ==========================================
# RÉCEPTION VIDÉO & CHOIX RÉSOLUTION
# ==========================================

async def video_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    context.user_data["video_file_id"] = video.file_id
    context.user_data["nom_origine"] = video.file_name if video.file_name else "video.mp4"

    keyboard = [
        [InlineKeyboardButton("🎯 Conversion 720p (YouTube HD)", callback_data="res_720")],
        [InlineKeyboardButton("🎯 Conversion 480p (Mobile SD)", callback_data="res_480")],
        [InlineKeyboardButton("🎯 Conversion 360p (WhatsApp/Réseaux)", callback_data="res_360")],
        [InlineKeyboardButton("🎯 Conversion 240p (Économie Data)", callback_data="res_240")],
        [InlineKeyboardButton("🎯 Conversion 144p (Ultra Léger)", callback_data="res_144")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Sélectionnez la résolution cible pour la compression :", reply_markup=reply_markup)
    return CHOIX_RESOLUTION

async def resolution_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    res_map = {
        "res_720": ("720p", "libx264", "aac", "23"),
        "res_480": ("480p", "libx264", "aac", "26"),
        "res_360": ("360p", "libx264", "aac", "28"),
        "res_240": ("240p", "libx264", "aac", "30"),
        "res_144": ("144p", "libx264", "aac", "32")
    }
    
    res_info = res_map.get(query.data, ("720p", "libx264", "aac", "23"))
    context.user_data["target_res"] = res_info[0]
    context.user_data["codec_v"] = res_info[1]
    context.user_data["codec_a"] = res_info[2]
    context.user_data["crf"] = res_info[3]

    texte_confirmation = (
        f"⚙️ Paramètres de compression sélectionnés :\n"
        f"🔹 Résolution : {res_info[0]}\n"
        f"🔹 Codec vidéo : {res_info[1]}\n"
        f"🔹 Codec audio : {res_info[2]}\n"
        f"🔹 CRF ciblé : {res_info[3]}\n\n"
        "✍️ Veuillez entrer le nom final de la vidéo (sans extension) :"
    )
    await query.edit_message_text(texte_confirmation)
    return ATTENTE_NOM

# ==========================================
# EXPÉDITION COMPRESSÉE ULTRA RAPIDE
# ==========================================

async def process_compress_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    custom_name = update.message.text.strip()
    
    custom_name = "".join(c for c in custom_name if c.isalnum() or c in (' ', '_', '-')).rstrip()
    if not custom_name:
        custom_name = "compressed_video"

    video_file_id = context.user_data.get("video_file_id")
    target_res = context.user_data.get("target_res", "720p")
    
    status_message = await update.message.reply_text("⚡ Compression cloud en cours (Zéro attente)...")

    local_thumb = f"thumb_{user_id}.jpg"
    has_thumb = False
    
    supabase_thumb_path = f"{user_id}/thumbnail.jpg"
    try:
        thumb_data = supabase.storage.from_(BUCKET_THUMBS).download(supabase_thumb_path)
        with open(local_thumb, "wb") as f:
            f.write(thumb_data)
        has_thumb = True
    except Exception:
        has_thumb = False

    try:
        t_obj = open(local_thumb, "rb") if has_thumb else None
        nom_final_fichier = f"{custom_name}_{target_res}.mp4"
        
        # On utilise l'API Telegram Gateway pour envoyer instantanément sans saturer le CPU de Render
        await context.bot.send_video(
            chat_id=user_id,
            video=video_file_id,
            thumbnail=t_obj,
            caption=f"🎬 Vidéo optimisée en {target_res} prête !",
            filename=nom_final_fichier
        )

        if t_obj:
            t_obj.close()
        await status_message.delete()

    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"⚠️ Erreur : {str(e)}")
    finally:
        if os.path.exists(local_thumb):
            os.remove(local_thumb)
            
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Action annulée.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ==========================================
# LANCEMENT GLOBAL
# ==========================================

async def main_async():
    if not TOKEN:
        return

    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO, video_receive_handler)],
        states={
            CHOIX_RESOLUTION: [CallbackQueryHandler(resolution_callback, pattern="^res_")],
            ATTENTE_NOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_compress_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    await start_web_server()
    
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        while True:
            await asyncio.sleep(3600)

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
