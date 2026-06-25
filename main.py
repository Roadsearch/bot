import os
import asyncio
import time
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, ConversationHandler, filters
)
from supabase import create_client, Client
from aiohttp import web  # Dépendance intégrée / facile à installer

# Chargement des variables d'environnement
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialisation du client Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET_THUMBS = "miniatures"
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 Mo

CHOIX_FORMAT, ATTENTE_NOM = range(2)
verrou_queue = asyncio.Lock()

# ==========================================
# MINI SERVEUR WEB POUR RENDERS (KEEP-ALIVE)
# ==========================================

async def handle_ping(request):
    """Répond aux pings pour garder le serveur éveillé"""
    return web.Response(text="Bot is alive and running!")

async def start_web_server():
    """Lance un serveur web asynchrone en arrière-plan"""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render passe automatiquement le port via la variable d'environnement PORT
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    print(f"🌍 Serveur de Keep-Alive démarré sur le port {port}")
    await site.start()

# ==========================================
# RÉCEPTION VIDÉO & CHOIX FORMAT
# ==========================================

async def video_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    if video.file_size >= MAX_FILE_SIZE:
        taille_en_mo = round(video.file_size / (1024 * 1024), 1)
        await update.message.reply_text(f"❌ **Fichier refusé !** ({taille_en_mo} Mo). Maximum 200 Mo.")
        return ConversationHandler.END

    context.user_data["video_file_id"] = video.file_id

    keyboard = [
        [
            InlineKeyboardButton("📄 Qualité Originale (Document)", callback_data="format_doc"),
            InlineKeyboardButton("🎬 Format Vidéo Standard", callback_data="format_vid")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Comment souhaitez-vous recevoir ce fichier ?", reply_markup=reply_markup)
    return CHOIX_FORMAT

async def format_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "format_doc":
        context.user_data["chosen_format"] = "document"
        await query.edit_message_text("📄 Option choisie : Document.\n\n✍️ **Veuillez m'envoyer le nouveau nom pour votre fichier (sans extension) :**")
    elif query.data == "format_vid":
        context.user_data["chosen_format"] = "video"
        await query.edit_message_text("🎬 Option choisie : Vidéo.\n\n✍️ **Veuillez m'envoyer le nouveau nom pour votre fichier (sans extension) :**")
    return ATTENTE_NOM

# ==========================================
# RÉCEPTION DU NOM & TRAITEMENT GLOBAL
# ==========================================

async def rename_and_process_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    custom_name = update.message.text.strip()
    
    custom_name = "".join(c for c in custom_name if c.isalnum() or c in (' ', '_', '-')).rstrip()
    if not custom_name:
        custom_name = "video_converted"

    video_file_id = context.user_data.get("video_file_id")
    chosen_format = context.user_data.get("chosen_format", "document")

    status_message = await update.message.reply_text("⚙️ Préparation du téléchargement...")

    async with verrou_queue:
        await execute_pure_rename_flow(status_message, context, user_id, video_file_id, chosen_format, custom_name)

    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# FLUX DE RENOMMAGE PUR
# ==========================================

async def execute_pure_rename_flow(status_message, context, user_id, video_file_id, chosen_format, custom_name):
    local_video = f"{custom_name}.mp4"
    local_thumb = f"thumb_{user_id}.jpg"
    has_thumb = False
    
    supabase_thumb_path = f"{user_id}/thumbnail.jpg"
    try:
        thumb_data = supabase.storage.from_(BUCKET_THUMBS).download(supabase_thumb_path)
        with open(local_thumb, "wb") as f: f.write(thumb_data)
        has_thumb = True
    except Exception:
        has_thumb = False

    try:
        tg_file = await context.bot.get_file(video_file_id)
        telegram_video_url = tg_file.file_path

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", telegram_video_url) as stream:
                total_size = int(stream.headers.get("Content-Length", 0))
                downloaded = 0
                derniere_maj = 0
                debut = time.time()
                
                with open(local_video, "wb") as f:
                    async for chunk in stream.iter_bytes():
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        maintenant = time.time()
                        if total_size > 0 and (maintenant - derniere_maj > 3.5 or downloaded == total_size):
                            derniere_maj = maintenant
                            pct = (downloaded / total_size) * 100
                            vitesse = downloaded / (maintenant - debut) / (1024 * 1024)
                            await status_message.edit_text(
                                f"📥 Téléchargement de la vidéo : `{pct:.1f}%` ({downloaded // (1024*1024)} Mo)\n"
                                f"🚀 Vitesse : {vitesse:.2f} Mo/s"
                            )

        await status_message.edit_text("📤 Envoi vers Telegram sous son nouveau nom...")
        await context.bot.send_chat_action(chat_id=user_id, action="upload_document" if chosen_format == "document" else "upload_video")
        
        with open(local_video, "rb") as v_obj:
            t_obj = open(local_thumb, "rb") if has_thumb else None
            try:
                if chosen_format == "video":
                    await context.bot.send_video(chat_id=user_id, video=v_obj, thumbnail=t_obj, caption=f"🎬 `{custom_name}.mp4` prêt !")
                else:
                    await context.bot.send_document(chat_id=user_id, document=v_obj, thumbnail=t_obj, caption=f"📄 `{custom_name}.mp4` prêt !")
            finally:
                if t_obj: t_obj.close()

        await status_message.delete()

    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"⚠️ Erreur durant le traitement : {str(e)}")
    finally:
        if os.path.exists(local_video): os.remove(local_video)
        if os.path.exists(local_thumb): os.remove(local_thumb)

# ==========================================
# GESTION DES MINIATURES SUPABASE
# ==========================================

async def save_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo = update.message.photo[-1]
    local_thumb = f"thumb_temp_{user_id}.jpg"
    supabase_thumb_path = f"{user_id}/thumbnail.jpg"
    
    status_msg = await update.message.reply_text("🔄 Traitement de votre image...")
    try:
        file_info = await context.bot.get_file(photo.file_id)
        await file_info.download_to_drive(local_thumb)
        
        await status_msg.edit_text("☁️ Sauvegarde sécurisée sur le Cloud Supabase...")
        with open(local_thumb, "rb") as f:
            supabase.storage.from_(BUCKET_THUMBS).upload(path=supabase_thumb_path, file=f, file_options={"upsert": "true"})
        
        await status_msg.delete()
        with open(local_thumb, "rb") as p_obj:
            await update.message.reply_photo(photo=p_obj, caption="✅ **Miniature enregistrée avec succès !**")
    except Exception as e:
        await status_msg.edit_text(f"❌ Erreur : {str(e)}")
    finally:
        if os.path.exists(local_thumb): os.remove(local_thumb)

async def view_thumbnail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    supabase_thumb_path = f"{user_id}/thumbnail.jpg"
    local_thumb = f"view_thumb_{user_id}.jpg"
    try:
        thumb_data = supabase.storage.from_(BUCKET_THUMBS).download(supabase_thumb_path)
        with open(local_thumb, "wb") as f: f.write(thumb_data)
        with open(local_thumb, "rb") as p_obj:
            await update.message.reply_photo(photo=p_obj, caption="🖼️ Votre miniature actuelle.")
    except Exception:
        await update.message.reply_text("❌ Aucune miniature personnalisée sur Supabase.")
    finally:
        if os.path.exists(local_thumb): os.remove(local_thumb)

async def delete_thumbnail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try:
        supabase.storage.from_(BUCKET_THUMBS).remove([f"{user_id}/thumbnail.jpg"])
        await update.message.reply_text("🗑️ Miniature personnalisée supprimée.")
    except Exception:
        await update.message.reply_text("❌ Impossible de supprimer.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Action annulée.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ==========================================
# INITIALISATION ET LANCEMENT PROPRE
# ==========================================

async def au_demarrage(application: Application) -> None:
    """Cette fonction s'exécute automatiquement AU SEIN de la boucle 
    d'événements active de Telegram juste avant de lancer le bot."""
    await start_web_server()

def main():
    if not TOKEN:
        print("Erreur : Le TOKEN Telegram n'est pas configuré.")
        return

    # On configure l'application en lui passant la tâche de démarrage du serveur web
    application = Application.builder().token(TOKEN).post_init(au_demarrage).build()
    
    video_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO, video_receive_handler)],
        states={
            CHOIX_FORMAT: [CallbackQueryHandler(format_selection_callback, pattern="^format_")],
            ATTENTE_NOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_and_process_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True  # Optionnel: Évite le warning PTBUserWarning affiché dans vos logs
    )
    
    application.add_handler(video_conversation)
    application.add_handler(CommandHandler("viewthumb", view_thumbnail_command))
    application.add_handler(CommandHandler("delthumb", delete_thumbnail_command))
    application.add_handler(MessageHandler(filters.PHOTO, save_thumbnail))
    
    print("Le bot et son serveur Keep-Alive s'initialisent...")
    application.run_polling()

if __name__ == "__main__":
    main()
