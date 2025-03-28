# --- START OF FILE __main__.py ---

import os
import random
import time
from datetime import datetime, timedelta
from string import ascii_letters as letters
import asyncio # Import asyncio for the broadcast delay

import httpx
import telegram
# Import necessary constants for filters
from telegram.constants import ChatType, UpdateType
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import mention_html

from db.database import SessionMaker, engine
from db.model import Base, FormnStatus, MediaGroupMesssage, MessageMap, User

from . import (
    admin_group_id,
    admin_user_ids,
    app_name,
    bot_token,
    is_delete_topic_as_ban_forever,
    is_delete_user_messages,
    logger,
    welcome_message,
    disable_captcha,
    message_interval,
)
from .utils import delete_message_later

# 创建表
Base.metadata.create_all(bind=engine)
db = SessionMaker()


# 延时发送媒体组消息的回调
async def _send_media_group_later(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    media_group_id = job.data
    _, from_chat_id, target_id, dir = job.name.split("_")

    # 数据库内查找对应的媒体组消息。
    media_group_msgs = (
        db.query(MediaGroupMesssage)
        .filter(
            MediaGroupMesssage.media_group_id == media_group_id,
            MediaGroupMesssage.chat_id == from_chat_id,
        )
        .all()
    )
    if not media_group_msgs:
        logger.warning(f"No media group messages found for ID {media_group_id} from chat {from_chat_id}")
        return
    try:
        chat = await context.bot.get_chat(target_id)
        if dir == "u2a":
            # 发送给群组
            u = db.query(User).filter(User.user_id == from_chat_id).first()
            if not u or not u.message_thread_id:
                 logger.warning(f"User {from_chat_id} or message_thread_id not found for media group u2a.")
                 return
            message_thread_id = u.message_thread_id
            sents = await chat.send_copies(
                from_chat_id,
                [m.message_id for m in media_group_msgs],
                message_thread_id=message_thread_id,
            )
            for sent, msg in zip(sents, media_group_msgs):
                msg_map = MessageMap(
                    user_chat_message_id=msg.message_id,
                    group_chat_message_id=sent.message_id,
                    user_id=u.user_id,
                )
                db.add(msg_map)
            db.commit()
        else: # dir == "a2u"
            # 发送给用户
            sents = await chat.send_copies(
                from_chat_id, [m.message_id for m in media_group_msgs]
            )
            for sent, msg in zip(sents, media_group_msgs):
                msg_map = MessageMap(
                    user_chat_message_id=sent.message_id,
                    group_chat_message_id=msg.message_id,
                    user_id=target_id,
                )
                db.add(msg_map)
            db.commit()
    except BadRequest as e:
         logger.error(f"Failed to send media group {media_group_id} from {from_chat_id} to {target_id}: {e}")
    except Exception as e:
         logger.error(f"Unexpected error in _send_media_group_later: {e}", exc_info=True)


# 延时发送媒体组消息
async def send_media_group_later(
    delay: float,
    chat_id,
    target_id,
    media_group_id: int,
    dir,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = f"sendmediagroup_{chat_id}_{target_id}_{dir}"
    existing_jobs = context.job_queue.get_jobs_by_name(name)
    for job in existing_jobs:
        job.schedule_removal()
    context.job_queue.run_once(
        _send_media_group_later, delay, chat_id=chat_id, name=name, data=media_group_id
    )
    return name


# 更新用户数据库
def update_user_db(user: telegram.User):
    if db.query(User).filter(User.user_id == user.id).first():
        return
    # Add is_premium mapping if your User model has it, else fetch later
    u = User(
        user_id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name,
        username=user.username,
        # is_premium=user.is_premium or False # Uncomment if your DB model has is_premium
    )
    db.add(u)
    db.commit()


# 发送联系人卡片
async def send_contact_card(
    chat_id, message_thread_id, user: User, update: Update, context: ContextTypes
):
    buttons = []
    # Fetch Telegram user object to check premium status dynamically
    try:
        tg_user = await context.bot.get_chat(user.user_id)
        is_premium = tg_user.is_premium or False
    except Exception:
        is_premium = False # Assume not premium if fetching fails

    buttons.append(
        [
            InlineKeyboardButton(
                f"{'🏆 高级会员' if is_premium else '✈️ 普通会员' }",
                url="https://github.com/MiHaKun/Telegram-interactive-bot", # Placeholder URL
            )
        ]
    )
    if user.username:
        buttons.append(
            [InlineKeyboardButton("👤 直接联络", url=f"https://t.me/{user.username}")]
        )

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    try:
        user_photo = await context.bot.get_user_profile_photos(user.id, limit=1)

        if user_photo.total_count > 0:
            pic = user_photo.photos[0][-1].file_id
            await context.bot.send_photo(
                chat_id,
                photo=pic,
                caption=f"👤 {mention_html(user.id, user.first_name or str(user.id))}\n\n📱 {user.id}\n\n🔗 @{user.username if user.username else '无'}",
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        else:
            await context.bot.send_contact(
                chat_id,
                phone_number="11111111111",
                first_name=user.first_name or "用户",
                last_name=user.last_name,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
            )
    except Exception as e:
        logger.error(f"Error sending contact card for user {user.id}: {e}")
        try:
             await context.bot.send_message(
                chat_id,
                text=f"用户信息:\n👤 {mention_html(user.id, user.first_name or str(user.id))}\n📱 {user.id}\n🔗 @{user.username if user.username else '无'}",
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception as final_e:
             logger.error(f"Fallback send_message also failed for user {user.id}: {final_e}")


# start 命令处理
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_db(user)
    if user.id in admin_user_ids:
        logger.info(f"{user.first_name}({user.id}) is admin")
        try:
            bg = await context.bot.get_chat(admin_group_id)
            if bg.type == "supergroup" and bg.is_forum:
                logger.info(f"admin group is {bg.title}")
                await update.message.reply_html(
                   f"你好管理员 {mention_html(user.id, user.full_name)} ({user.id})\n\n欢迎使用 {app_name} 机器人。\n\n 目前你的配置完全正确。可以在群组 <b> {bg.title} </b> 中通过话题与用户互动。"
                )
            else:
                 logger.warning(f"Admin group {admin_group_id} is not a supergroup with topics enabled.")
                 await update.message.reply_html(
                     f"⚠️⚠️后台管理群组设置错误⚠️⚠️\n管理员 {mention_html(user.id, user.full_name)}，配置中的 `admin_group_id` (`{admin_group_id}`) 必须是一个启用了 **话题(Topics)** 功能的超级群组。请检查群组设置及你的配置文件。"
                 )
        except BadRequest as e:
            logger.error(f"Cannot access admin group {admin_group_id}: {e}")
            await update.message.reply_html(
                f"⚠️⚠️无法访问后台管理群组⚠️⚠️\n管理员 {mention_html(user.id, user.full_name)}，请确认机器人已被邀请加入群组 (`{admin_group_id}`) 并且拥有在该群组发送消息的权限。\n错误细节：{e}\n"
            )
        except Exception as e:
            logger.error(f"Unexpected error checking admin group: {e}", exc_info=True)
            await update.message.reply_html(
                f"⚠️⚠️检查后台管理群组时发生未知错误⚠️⚠️\n管理员 {mention_html(user.id, user.full_name)}，请检查日志获取详细信息。\n错误: {e}"
            )
    else:
        await update.message.reply_html(
            f"{mention_html(user.id, user.full_name)} 同学：\n\n{welcome_message}"
        )


# 人机验证
async def check_human(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.user_data.get("is_human", False):
        return True

    if context.user_data.get("is_human_error_time", 0) > time.time() - 120:
        reply_msg = await update.message.reply_html("你因验证码错误被临时禁言, 请 2 分钟后再尝试。")
        await delete_message_later(10, reply_msg.chat.id, reply_msg.message_id, context)
        await delete_message_later(5, update.message.chat.id, update.message.message_id, context)
        return False

    img_dir = "./assets/imgs"
    try:
        if not os.path.isdir(img_dir) or not os.listdir(img_dir):
             logger.warning(f"Captcha image directory '{img_dir}' is missing or empty. Skipping check.")
             context.user_data["is_human"] = True
             return True

        file_name = random.choice(os.listdir(img_dir))
        code = file_name.replace("image_", "").replace(".png", "")
        file_path = os.path.join(img_dir, file_name)

        codes = ["".join(random.sample(letters, len(code))) for _ in range(7)]
        codes.append(code)
        random.shuffle(codes)

        photo_file_id = context.bot_data.get(f"image|{code}")

        buttons = [
            InlineKeyboardButton(x, callback_data=f"vcode_{x}_{user.id}") for x in codes
        ]
        button_matrix = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]

        captcha_text = f"{mention_html(user.id, user.first_name or str(user.id))} 请选择图片中的文字。回答错误将导致临时禁言。"

        if photo_file_id:
             sent = await update.message.reply_photo(
                photo=photo_file_id,
                caption=captcha_text,
                reply_markup=InlineKeyboardMarkup(button_matrix),
                parse_mode="HTML",
             )
        else:
             with open(file_path, "rb") as photo_file:
                 sent = await update.message.reply_photo(
                    photo=photo_file,
                    caption=captcha_text,
                    reply_markup=InlineKeyboardMarkup(button_matrix),
                    parse_mode="HTML",
                 )
             if sent.photo:
                 biggest_photo = sorted(sent.photo, key=lambda x: x.file_size, reverse=True)[0]
                 context.bot_data[f"image|{code}"] = biggest_photo.file_id
                 logger.debug(f"Cached captcha image {code} with file_id {biggest_photo.file_id}")

        context.user_data["vcode"] = code
        context.user_data["vcode_message_id"] = sent.message_id
        await delete_message_later(60, sent.chat.id, sent.message_id, context)
        await delete_message_later(5, update.message.chat.id, update.message.message_id, context)

        return False

    except FileNotFoundError:
        logger.error(f"Captcha image file not found: {file_path}")
        await update.message.reply_html("抱歉，验证码图片加载失败，请稍后再试。")
        context.user_data["is_human"] = True
        return True
    except IndexError:
         logger.error(f"Captcha image directory '{img_dir}' appears empty.")
         await update.message.reply_html("抱歉，无法加载验证码，请联系管理员。")
         context.user_data["is_human"] = True
         return True
    except Exception as e:
         logger.error(f"Error during check_human: {e}", exc_info=True)
         await update.message.reply_html("验证过程中发生错误，请稍后再试。")
         context.user_data["is_human"] = True
         return True


# 处理验证码回调
async def callback_query_vcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    try:
        _, code_clicked, target_user_id_str = query.data.split("_")
    except ValueError:
        logger.warning(f"Received invalid callback data: {query.data}")
        await query.answer("无效操作", show_alert=True)
        return

    if target_user_id_str != str(user.id):
        await query.answer("这不是给你的验证码哦。", show_alert=True)
        return

    correct_code = context.user_data.get("vcode")
    vcode_message_id = context.user_data.get("vcode_message_id")

    if not correct_code or not vcode_message_id or (query.message and query.message.message_id != vcode_message_id):
        await query.answer("验证已过期或失效。", show_alert=True)
        if query.message:
            try:
                 await query.message.delete()
            except BadRequest:
                 pass
        return

    if code_clicked == correct_code:
        await query.answer("✅ 正确，欢迎！")
        await context.bot.send_message(
            user.id,
            f"{mention_html(user.id, user.first_name or str(user.id))} , 验证通过，欢迎！",
            parse_mode="HTML",
        )
        context.user_data["is_human"] = True
        context.user_data.pop("vcode", None)
        context.user_data.pop("vcode_message_id", None)
        context.user_data.pop("is_human_error_time", None)
    else:
        await query.answer("❌ 错误！请等待 2 分钟后再试。", show_alert=True)
        context.user_data["is_human_error_time"] = time.time()
        context.user_data.pop("vcode", None)
        context.user_data.pop("vcode_message_id", None)

    try:
        await query.message.delete()
    except BadRequest:
        pass


# 转发用户消息到管理员群组
async def forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not disable_captcha:
        if not await check_human(update, context):
            return

    if message_interval > 0:
        current_time = time.time()
        last_message_time = context.user_data.get("last_message_time", 0)
        if current_time < last_message_time + message_interval:
            wait_time = round(last_message_time + message_interval - current_time)
            if wait_time > 0:
                reply_msg = await update.message.reply_html(f"请不要频繁发送消息，请等待 {wait_time} 秒。")
                await delete_message_later(5, reply_msg.chat.id, reply_msg.message_id, context)
                await delete_message_later(3, update.message.chat.id, update.message.message_id, context)
            return
        context.user_data["last_message_time"] = current_time

    user = update.effective_user
    message = update.message
    update_user_db(user)

    admin_target_chat_id = admin_group_id

    u = db.query(User).filter(User.user_id == user.id).first()
    if not u:
        logger.error(f"User {user.id} not found in DB immediately after update_user_db.")
        await message.reply_html("发生内部错误，无法处理您的请求。")
        return

    message_thread_id = u.message_thread_id
    topic_is_closed = False

    if message_thread_id:
        f_status = db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).first()
        if f_status and f_status.status == "closed":
            topic_is_closed = True
            if is_delete_topic_as_ban_forever:
                await message.reply_html(
                    "客服已经关闭了此对话，并且不允许重开。您的消息无法发送。"
                )
            else:
                 await message.reply_html(
                    "客服已经关闭了此对话。您的消息暂时无法发送，请等待客服重新打开对话。"
                 )
            return

    if not message_thread_id:
        try:
            topic_name = f"工单{random.randint(10000,99999)}|{user.full_name}|{user.id}"
            topic_name = topic_name[:128]

            formn = await context.bot.create_forum_topic(
                admin_target_chat_id,
                name=topic_name,
            )
            message_thread_id = formn.message_thread_id
            u.message_thread_id = message_thread_id
            db.add(u)
            new_f_status = FormnStatus(message_thread_id=message_thread_id, status="opened")
            db.add(new_f_status)
            db.commit()
            logger.info(f"Created new topic {message_thread_id} for user {user.id}")

            await context.bot.send_message(
                admin_target_chat_id,
                f"🆕 新的用户 {mention_html(user.id, user.full_name)} ({user.id}) 开始了一个新的会话。",
                message_thread_id=message_thread_id,
                parse_mode="HTML",
            )
            await send_contact_card(admin_target_chat_id, message_thread_id, u, update, context)

        except BadRequest as e:
            logger.error(f"Failed to create topic for user {user.id}: {e}")
            await message.reply_html(f"无法创建客服对话，请稍后再试。\n错误: {e}")
            return
        except Exception as e:
            logger.error(f"Unexpected error creating topic for user {user.id}: {e}", exc_info=True)
            await message.reply_html("创建客服对话时发生未知错误。")
            return

    params = {"message_thread_id": message_thread_id}
    if message.reply_to_message:
        reply_in_user_chat = message.reply_to_message.message_id
        msg_map = db.query(MessageMap).filter(MessageMap.user_chat_message_id == reply_in_user_chat).first()
        if msg_map and msg_map.group_chat_message_id:
            params["reply_to_message_id"] = msg_map.group_chat_message_id
        else:
             logger.debug(f"Could not find group message mapping for user reply {reply_in_user_chat}")

    try:
        if message.media_group_id:
            is_first_message = not db.query(MediaGroupMesssage).filter(
                MediaGroupMesssage.media_group_id == message.media_group_id,
                MediaGroupMesssage.chat_id == message.chat.id
            ).first()

            msg = MediaGroupMesssage(
                chat_id=message.chat.id,
                message_id=message.message_id,
                media_group_id=message.media_group_id,
                is_header=is_first_message,
                caption_html=message.caption_html if is_first_message else None
            )
            db.add(msg)
            db.commit()

            if is_first_message:
                logger.debug(f"Scheduling media group {message.media_group_id} from user {user.id} for delayed sending.")
                await send_media_group_later(
                    3,
                    user.id,
                    admin_target_chat_id,
                    message.media_group_id,
                    "u2a",
                    context
                )
            return

        else:
            chat = await context.bot.get_chat(admin_target_chat_id)
            sent_msg = await chat.send_copy(
                from_chat_id=message.chat.id,
                message_id=message.id,
                **params
            )

            msg_map = MessageMap(
                user_chat_message_id=message.id,
                group_chat_message_id=sent_msg.message_id,
                user_id=user.id,
            )
            db.add(msg_map)
            db.commit()
            logger.debug(f"Forwarded u2a: user_msg({message.id}) -> group_msg({sent_msg.message_id}) in topic {message_thread_id}")

    except BadRequest as e:
        logger.warning(f"Failed to forward message u2a for user {user.id} (topic: {message_thread_id}): {e}")
        if "MESSAGE_THREAD_NOT_FOUND" in str(e) or "TOPIC_DELETED" in str(e) or "chat not found" in str(e).lower():
            original_thread_id = u.message_thread_id
            u.message_thread_id = None
            db.add(u)
            db.query(FormnStatus).filter(FormnStatus.message_thread_id == original_thread_id).delete()
            db.commit()
            logger.info(f"Cleared non-existent topic {original_thread_id} for user {user.id}.")
            if is_delete_topic_as_ban_forever:
                await message.reply_html(
                    "发送失败，你的对话已被客服删除且不允许重开。"
                )
            else:
                await message.reply_html(
                    "发送失败，你的对话已被客服删除。请 **重新发送刚才的消息** 以开启新的对话。"
                )
        else:
             await message.reply_html(f"发送失败: {e}\n")

    except Exception as e:
        logger.error(f"Unexpected error forwarding u2a for user {user.id}: {e}", exc_info=True)
        await message.reply_html(f"发送消息时发生未知错误: {e}\n")


# 转发管理员消息到用户
async def forwarding_message_a2u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.chat.id != admin_group_id:
        return
    message = update.message
    message_thread_id = message.message_thread_id
    admin_user = update.effective_user

    if not message_thread_id or admin_user.is_bot:
        return

    target_user_db = db.query(User).filter(User.message_thread_id == message_thread_id).first()
    target_user_id = target_user_db.user_id if target_user_db else None

    if message.forum_topic_created:
        logger.info(f"Topic created event for {message_thread_id}. Ensuring status is 'opened'.")
        f_status = db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).first()
        if not f_status:
            db.add(FormnStatus(message_thread_id=message_thread_id, status="opened"))
            db.commit()
        elif f_status.status != "opened":
             f_status.status = "opened"
             db.add(f_status)
             db.commit()
        return

    if message.forum_topic_closed:
        logger.info(f"Topic closed event for {message_thread_id}.")
        f_status = db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).first()
        if f_status:
            f_status.status = "closed"
            db.add(f_status)
        else:
             db.add(FormnStatus(message_thread_id=message_thread_id, status="closed"))
        db.commit()
        if target_user_id:
            try:
                await context.bot.send_message(
                    target_user_id, "对话已被管理员关闭。你暂时无法回复此对话。"
                )
            except Exception as e:
                 logger.warning(f"Failed to send topic closed notification to user {target_user_id}: {e}")
        return

    if message.forum_topic_reopened:
        logger.info(f"Topic reopened event for {message_thread_id}.")
        f_status = db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).first()
        if f_status:
            f_status.status = "opened"
            db.add(f_status)
        else:
             db.add(FormnStatus(message_thread_id=message_thread_id, status="opened"))
        db.commit()
        if target_user_id:
             try:
                await context.bot.send_message(target_user_id, "管理员已重新打开对话，你可以继续发送消息了。")
             except Exception as e:
                 logger.warning(f"Failed to send topic reopened notification to user {target_user_id}: {e}")
        return

    if not target_user_id:
        logger.warning(f"Received message in topic {message_thread_id}, but no associated user found in DB.")
        return

    f_status = db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).first()
    if f_status and f_status.status == "closed":
        await message.reply_html(
            "提醒：此对话当前已关闭。若要用户收到此消息，请先重新打开对话。", quote=True
        )
        return

    params = {}
    if message.reply_to_message:
        reply_in_admin = message.reply_to_message.message_id
        msg_map = db.query(MessageMap).filter(MessageMap.group_chat_message_id == reply_in_admin).first()
        if msg_map and msg_map.user_chat_message_id:
            params["reply_to_message_id"] = msg_map.user_chat_message_id
        else:
            logger.debug(f"Could not find user message mapping for admin reply {reply_in_admin}")

    try:
        target_chat = await context.bot.get_chat(target_user_id)

        if message.media_group_id:
            is_first_message = not db.query(MediaGroupMesssage).filter(
                MediaGroupMesssage.media_group_id == message.media_group_id,
                MediaGroupMesssage.chat_id == message.chat.id
            ).first()

            msg = MediaGroupMesssage(
                chat_id=message.chat.id,
                message_id=message.message_id,
                media_group_id=message.media_group_id,
                is_header=is_first_message,
                caption_html=message.caption_html if is_first_message else None,
            )
            db.add(msg)
            db.commit()

            if is_first_message:
                logger.debug(f"Scheduling media group {message.media_group_id} from admin {admin_user.id} to user {target_user_id} for delayed sending.")
                await send_media_group_later(
                    3,
                    admin_group_id,
                    target_user_id,
                    message.media_group_id,
                    "a2u",
                    context,
                )
            return

        else:
            sent_msg = await target_chat.send_copy(
                from_chat_id=message.chat.id,
                message_id=message.id,
                **params
            )

            msg_map = MessageMap(
                group_chat_message_id=message.id,
                user_chat_message_id=sent_msg.message_id,
                user_id=target_user_id,
            )
            db.add(msg_map)
            db.commit()
            logger.debug(f"Forwarded a2u: group_msg({message.id}) -> user_msg({sent_msg.message_id}) for user {target_user_id}")

    except BadRequest as e:
         if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e) or "chat not found" in str(e).lower():
              logger.warning(f"Failed to send message a2u to user {target_user_id}: User blocked bot or is deactivated.")
              await message.reply_html(f"⚠️ 无法发送消息给用户 {mention_html(target_user_id, target_user_db.first_name or str(target_user_id))}。用户可能已拉黑机器人或已停用。", quote=True, parse_mode='HTML')
         else:
              logger.error(f"Failed to forward message a2u to user {target_user_id}: {e}")
              await message.reply_html(f"发送失败: {e}\n", quote=True)
    except Exception as e:
        logger.error(f"Unexpected error forwarding a2u to user {target_user_id}: {e}", exc_info=True)
        await message.reply_html(f"发送消息时发生未知错误: {e}\n", quote=True)


# --- 编辑同步功能函数 ---

async def handle_edited_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理来自用户私聊的已编辑消息"""
    if not update.edited_message:
        return

    edited_msg = update.edited_message
    user_id = edited_msg.from_user.id
    edited_msg_id = edited_msg.message_id

    logger.debug(f"Handling edited message {edited_msg_id} from user {user_id}")

    msg_map = db.query(MessageMap).filter(MessageMap.user_chat_message_id == edited_msg_id).first()
    if not msg_map or not msg_map.group_chat_message_id:
        logger.debug(f"No group mapping found for edited user message {edited_msg_id}")
        return

    u = db.query(User).filter(User.user_id == user_id).first()
    if not u or not u.message_thread_id:
        logger.debug(f"User {user_id} or topic thread not found when handling edit {edited_msg_id}")
        return

    group_msg_id = msg_map.group_chat_message_id

    try:
        if edited_msg.text is not None:
            await context.bot.edit_message_text(
                chat_id=admin_group_id,
                message_id=group_msg_id,
                text=edited_msg.text_html,
                parse_mode='HTML',
            )
            logger.info(f"Synced user edit (text) {edited_msg_id} to group message {group_msg_id}")
        elif edited_msg.caption is not None:
             await context.bot.edit_message_caption(
                chat_id=admin_group_id,
                message_id=group_msg_id,
                caption=edited_msg.caption_html,
                parse_mode='HTML',
             )
             logger.info(f"Synced user edit (caption) {edited_msg_id} to group message {group_msg_id}")

    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Failed to sync user edit {edited_msg_id} to group message {group_msg_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error syncing user edit {edited_msg_id} to {group_msg_id}: {e}", exc_info=True)


async def handle_edited_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理来自管理群组话题的已编辑消息"""
    if not update.edited_message or update.edited_message.chat.id != admin_group_id:
         return

    edited_msg = update.edited_message
    edited_msg_id = edited_msg.message_id
    message_thread_id = edited_msg.message_thread_id

    if not message_thread_id or edited_msg.from_user.is_bot:
        return

    logger.debug(f"Handling edited message {edited_msg_id} from admin group topic {message_thread_id}")

    msg_map = db.query(MessageMap).filter(MessageMap.group_chat_message_id == edited_msg_id).first()
    if not msg_map or not msg_map.user_chat_message_id:
        logger.debug(f"No user mapping found for edited admin message {edited_msg_id}")
        return

    user_chat_msg_id = msg_map.user_chat_message_id
    user_id = msg_map.user_id

    try:
        if edited_msg.text is not None:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=user_chat_msg_id,
                text=edited_msg.text_html,
                parse_mode='HTML',
            )
            logger.info(f"Synced admin edit (text) {edited_msg_id} to user message {user_chat_msg_id}")
        elif edited_msg.caption is not None:
             await context.bot.edit_message_caption(
                chat_id=user_id,
                message_id=user_chat_msg_id,
                caption=edited_msg.caption_html,
                parse_mode='HTML',
             )
             logger.info(f"Synced admin edit (caption) {edited_msg_id} to user message {user_chat_msg_id}")

    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.warning(f"Failed to sync admin edit {edited_msg_id} to user message {user_chat_msg_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error syncing admin edit {edited_msg_id} to {user_chat_msg_id}: {e}", exc_info=True)

# --- 结束 编辑同步功能函数 ---


# 清理话题命令
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if user.id not in admin_user_ids:
        await message.reply_html("你没有权限执行此操作。")
        return

    message_thread_id = message.message_thread_id
    if not message_thread_id:
         await message.reply_html("请在需要清除的用户对话（话题）内使用此命令。")
         return

    logger.info(f"Admin {user.id} attempting to clear topic {message_thread_id}")
    target_user = db.query(User).filter(User.message_thread_id == message_thread_id).first()

    try:
        await context.bot.delete_forum_topic(
            chat_id=admin_group_id,
            message_thread_id=message_thread_id
        )
        logger.info(f"Successfully deleted topic {message_thread_id} via API.")

        db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).delete()
        if target_user:
            target_user.message_thread_id = None
            db.add(target_user)
        db.commit()
        logger.info(f"Cleared topic {message_thread_id} associations from database.")

    except BadRequest as e:
        logger.error(f"Failed to delete topic {message_thread_id}: {e}")
        await message.reply_html(f"清除话题失败: {e}", quote=True)
        try:
            db.query(FormnStatus).filter(FormnStatus.message_thread_id == message_thread_id).delete()
            if target_user:
                target_user.message_thread_id = None
                db.add(target_user)
            db.commit()
            logger.info(f"Cleaned topic {message_thread_id} associations from DB after API error.")
        except Exception as db_e:
             logger.error(f"Failed to clean DB for topic {message_thread_id} after API error: {db_e}")
    except Exception as e:
        logger.error(f"Unexpected error during clear command for topic {message_thread_id}: {e}", exc_info=True)
        await message.reply_html(f"清除话题时发生未知错误: {e}", quote=True)

    if is_delete_user_messages and target_user:
        logger.info(f"Proceeding to delete messages for user {target_user.user_id} associated with cleared topic {message_thread_id}.")
        all_messages_map = db.query(MessageMap).filter(MessageMap.user_id == target_user.user_id).all()
        user_message_ids = [msg.user_chat_message_id for msg in all_messages_map if msg.user_chat_message_id]

        if not user_message_ids:
            logger.info(f"No mapped user messages found for user {target_user.user_id} to delete.")
            return

        deleted_count = 0
        failed_count = 0
        batch_size = 100

        for i in range(0, len(user_message_ids), batch_size):
            batch = user_message_ids[i : i + batch_size]
            try:
                success = await context.bot.delete_messages(
                    chat_id=target_user.user_id,
                    message_ids=batch
                )
                if success:
                    deleted_count += len(batch)
                else:
                    logger.warning(f"delete_messages reported failure for a batch for user {target_user.user_id}. Some messages might remain.")
                    failed_count += len(batch)
            except BadRequest as e:
                 logger.error(f"BadRequest deleting message batch for user {target_user.user_id}: {e}")
                 failed_count += len(batch)
            except Exception as e:
                 logger.error(f"Unexpected error deleting message batch for user {target_user.user_id}: {e}", exc_info=True)
                 failed_count += len(batch)

        logger.info(f"Attempted to delete {len(user_message_ids)} messages for user {target_user.user_id}. Success: {deleted_count}, Failed/Skipped: {failed_count}.")

        try:
            db.query(MessageMap).filter(MessageMap.user_id == target_user.user_id).delete()
            db.commit()
            logger.info(f"Deleted MessageMap entries for user {target_user.user_id}.")
        except Exception as db_e:
            logger.error(f"Failed to delete MessageMap entries for user {target_user.user_id}: {db_e}")


# 广播回调
async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    if not isinstance(job_data, str) or "_" not in job_data:
        logger.error(f"Invalid broadcast job data: {job_data}")
        return

    try:
        msg_id_str, chat_id_str = job_data.split("_", 1)
        msg_id = int(msg_id_str)
        chat_id = int(chat_id_str)
    except ValueError:
        logger.error(f"Could not parse broadcast job data: {job_data}")
        return

    users = db.query(User).all()
    logger.info(f"Starting broadcast of msg {msg_id} from chat {chat_id} to {len(users)} users.")

    success = 0
    failed = 0
    blocked = 0
    send_delay = 0.1

    for u in users:
        try:
            await context.bot.copy_message(
                chat_id=u.user_id,
                from_chat_id=chat_id,
                message_id=msg_id
            )
            success += 1
        except BadRequest as e:
            if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                blocked += 1
                logger.debug(f"Broadcast to user {u.user_id} failed: Blocked or deactivated.")
            else:
                failed += 1
                logger.warning(f"Broadcast to user {u.user_id} failed with BadRequest: {e}")
        except Exception as e:
            failed += 1
            logger.error(f"Unexpected error broadcasting to user {u.user_id}: {e}", exc_info=True)
        await asyncio.sleep(send_delay)

    logger.info(f"Broadcast finished. Success: {success}, Failed: {failed}, Blocked/Deactivated: {blocked}")


# 广播命令
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in admin_user_ids:
        await update.message.reply_html("你没有权限执行此操作。")
        return

    if not update.message.reply_to_message:
        await update.message.reply_html(
            "请回复你想要广播的消息来使用此命令。"
        )
        return

    broadcast_msg = update.message.reply_to_message
    job_data = f"{broadcast_msg.id}_{broadcast_msg.chat.id}"
    job_name = f"broadcast_{broadcast_msg.id}"

    context.job_queue.run_once(
        _broadcast,
        when=timedelta(seconds=2),
        data=job_data,
        name=job_name,
    )

    await update.message.reply_html(f"📢 广播任务已创建。将广播消息 ID: {broadcast_msg.id}")


# (这个函数似乎未被使用)
async def error_in_send_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("error_in_send_media_group called, possibly from a removed ConversationHandler state.")
    if update.message:
        await update.message.reply_html(
            "处理媒体组时发生错误。后续对话将尝试直接转发。"
        )
    # return ConversationHandler.END # Cannot return this outside a ConversationHandler


# 错误处理
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """记录错误日志"""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)


# --- Main Application Setup ---
if __name__ == "__main__":

    pickle_persistence = PicklePersistence(filepath=f"./assets/{app_name}.pickle")
    application = (
        ApplicationBuilder()
        .token(bot_token)
        .persistence(persistence=pickle_persistence)
        .build()
    )

    # --- Command Handlers ---
    application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("clear", clear, filters.Chat(admin_group_id) & filters.IS_TOPIC_MESSAGE))
    application.add_handler(CommandHandler("broadcast", broadcast, filters.Chat(admin_group_id) & filters.REPLY))

    # --- Message Handlers ---
    # 1. Handler for NEW messages from user in PRIVATE chat
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
            forwarding_message_u2a
        )
    )
    # 2. Handler for NEW messages from admin in ADMIN_GROUP topics
    application.add_handler(
        MessageHandler(
            filters.Chat(admin_group_id) & filters.IS_TOPIC_MESSAGE & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
            forwarding_message_a2u
        )
    )
    # 3. Handler for EDITED messages from user in PRIVATE chat
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.UpdateType.EDITED_MESSAGE,
            handle_edited_user_message # New handler
        )
    )
    # 4. Handler for EDITED messages from admin in ADMIN_GROUP topics
    application.add_handler(
        MessageHandler(
            filters.Chat(admin_group_id) & filters.IS_TOPIC_MESSAGE & filters.UpdateType.EDITED_MESSAGE,
            handle_edited_admin_message # New handler
        )
    )

    # --- Callback Query Handler ---
    application.add_handler(
        CallbackQueryHandler(callback_query_vcode, pattern="^vcode_")
    )

    # --- Error Handler ---
    application.add_error_handler(error_handler)

    # --- Run the Bot ---
    logger.info(f"Starting bot {app_name}...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

    # 添加了编辑消息后的同步功能
