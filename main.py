import os
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, ConversationHandler, filters
)
from supabase import create_client, Client
from aiohttp import web

# Chargement des variables d'environnement
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialisation du client Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
BUCKET_THUMBS = "miniatures"

# Configuration des états de la conversation
CHOIX_FORMAT, ATTENTE_NOM = range(2)

# ==========================================
# MINI SERVEUR WEB POUR RENDER (KEEP-ALIVE)
# ==========================================

async def handle_ping(request):
    """Répond aux pings pour garder le serveur éveillé"""
    return web.Response(text="Bot is alive and running on Render!")

async def start_web_server():
    """Lance un serveur web asynchrone en arrière-plan"""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    print(f"🌍 Serveur de Keep-Alive démarré sur le port {port}")
    await site.start()

# ==========================================
# COMMANDE START (ACCUEIL)
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Message d'accueil lorsque l'utilisateur tape /start"""
    user_name = update.effective_user.first_name
    texte_accueil = (
        f"👋 Bonjour {user_name} !\n\n"
        "📥 Comment utiliser ce bot :\n"
        "1. Envoyez-moi une vidéo directement dans le chat.\n"
        "2. Choisissez si vous voulez la recevoir en format Vidéo ou Document.\n"
        "3. Donnez-lui un nouveau nom... et voilà ! ✨\n\n"
        "🖼️ Gestion des miniatures :\n"
        "• Envoyez-moi une simple photo pour l'enregistrer comme miniature par défaut.\n"
        "• Tapez /viewthumb pour voir votre miniature actuelle.\n"
        "• Tapez /delthumb pour supprimer votre miniature."
    )
    await update.message.reply_text(texte_accueil)

# ==========================================
# RÉCEPTION VIDÉO & CHOIX FORMAT
# ==========================================

async def video_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    
    # Stockage de l'ID sans télécharger le fichier lourd en RAM
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
        await query.edit_message_text("📄 Option choisie : Document.\n\n✍️ Veuillez m'envoyer le nouveau nom pour votre fichier (sans extension) :")
    elif query.data == "format_vid":
        context.user_data["chosen_format"] = "video"
        await query.edit_message_text("🎬 Option choisie : Vidéo.\n\n✍️ Veuillez m'envoyer le nouveau nom pour votre fichier (sans extension) :")
    return ATTENTE_NOM

# ==========================================
# RÉCEPTION DU NOM & TRAITEMENT ULTRA-LÉGER
# ==========================================

async def rename_and_process_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    custom_name = update.message.text.strip()
    
    # Nettoyage du nom de fichier
    custom_name = "".join(c for c in custom_name if c.isalnum() or c in (' ', '_', '-')).rstrip()
    if not custom_name:
        custom_name = "video_converted"

    video_file_id = context.user_data.get("video_file_id")
    chosen_format = context.user_data.get("chosen_format", "document")

    status_message = await update.message.reply_text("⚙️ Application de la miniature et du format...")

    local_thumb = f"thumb_{user_id}.jpg"
    has_thumb = False
    
    # Téléchargement local de la miniature uniquement (très léger en RAM)
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
        
        # Envoi selon le format choisi avec gestion de la miniature locale
        if chosen_format == "video":
            await context.bot.send_video(
                chat_id=user_id,
                video=video_file_id,
                thumbnail=t_obj,
                caption=f"🎬 {custom_name}.mp4 prêt !",
                filename=f"{custom_name}.mp4"
            )
        else:
            await context.bot.send_document(
                chat_id=user_id,
                document=video_file_id,
                thumbnail=t_obj,
                caption=f"📄 {custom_name}.mp4 prêt !",
                filename=f"{custom_name}.mp4"
            )

        if t_obj:
            t_obj.close()

        await status_message.delete()

    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"⚠️ Erreur durant le traitement : {str(e)}")
    finally:
        # Nettoyage du fichier temporaire de miniature
        if os.path.exists(local_thumb):
            os.remove(local_thumb)
    
    context.user_data.clear()
    return ConversationHandler.END

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
            await update.message.reply_photo(photo=p_obj, caption="✅ Miniature enregistrée avec succès !")
    except Exception as e:
        await status_msg.edit_text(f"❌ Erreur : {str(e)}")
    finally:
        if os.path.exists(local_thumb):
            os.remove(local_thumb)

async def view_thumbnail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    supabase_thumb_path = f"{user_id}/thumbnail.jpg"
    local_thumb = f"view_thumb_{user_id}.jpg"
    try:
        thumb_data = supabase.storage.from_(BUCKET_THUMBS).download(supabase_thumb_path)
        with open(local_thumb, "wb") as f:
            f.write(thumb_data)
        with open(local_thumb, "rb") as p_obj:
            await update.message.reply_photo(photo=p_obj, caption="🖼️ Votre miniature actuelle.")
    except Exception:
        await update.message.reply_text("❌ Aucune miniature personnalisée sur Supabase.")
    finally:
        if os.path.exists(local_thumb):
            os.remove(local_thumb)

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
# INITIALISATION ET LANCEMENT ASYNC
# ==========================================

async def main_async():
    if not TOKEN:
        print("Erreur : Le TOKEN Telegram n'est pas configuré.")
        return

    application = Application.builder().token(TOKEN).build()
    
    video_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO, video_receive_handler)],
        states={
            CHOIX_FORMAT: [CallbackQueryHandler(format_selection_callback, pattern="^format_")],
            ATTENTE_NOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_and_process_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    application.add_handler(video_conversation)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("viewthumb", view_thumbnail_command))
    application.add_handler(CommandHandler("delthumb", delete_thumbnail_command))
    application.add_handler(MessageHandler(filters.PHOTO, save_thumbnail))
    
    # Démarrage du mini-serveur Web pour empêcher la mise en veille Render
    await start_web_server()
    
    print("🚀 Le bot et son serveur Keep-Alive sont opérationnels sur Render...")
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
