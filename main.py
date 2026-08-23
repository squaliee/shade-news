import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from supabase import Client, create_client

# ==================================================
# 1. Конфігурація та змінні середовища
# ==================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8415128660:AAF0pcIL3w5Qkj8MsYLKqZpfDy3UvHKCh94")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://shade-news-app.vercel.app/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "shadenews_bot")  # без "@", потрібно для реф. посилань

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://mdkupzrojmulqernsspo.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1ka3VwenJvam11bHFlcm5zc3BvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE2ODY2MjksImV4cCI6MjA4NzI2MjYyOX0.N0NWe4ANf2hm-zUFhlCM6dV_8daTIOJWRnx-5e4uxdc")

# Назва каналу без "@" (для авто-збереження постів у Supabase)
TARGET_CHANNEL_USERNAME = "sh4denews"

# --- НАЛАШТУВАННЯ ПІДТРИМКИ ТА АДМІНІВ ---
# Встав сюди СВІЙ числовий Telegram ID (дізнатись можна в бота @userinfobot).
# Можна кілька через кому: "111111,222222"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# Куди форвардяться звернення з /support — особистий чат адміна або окремий чат/група.
# Якщо не задано окремо — беремо перший ID з ADMIN_IDS.
_admin_chat_env = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_CHAT_ID = int(_admin_chat_env) if _admin_chat_env.isdigit() else (next(iter(ADMIN_IDS), None))

# Раз на скільки годин один юзер може ставити +/- іншому
REP_COOLDOWN_HOURS = 24

# ==================================================
# 2. Supabase — потрібні таблиці
# ==================================================
# players            (user_id, username, total_points, monthly_points,
#                      total_correct_answers, total_wrong_answers, last_month)
# ratings            (user_id, username, rating, last_daily, tasks_done jsonb,
#                      referred_by, referred_count, used_promos jsonb)
# completed_quizzes  (user_id, quiz_id, created_at)
# news               (message_id, text, image_url)
#
# НОВА таблиця для системи репутації (створи в Supabase SQL editor):
#
#   create table reputation_votes (
#     id bigint generated always as identity primary key,
#     voter_id bigint not null,
#     target_id bigint not null,
#     delta smallint not null,
#     created_at timestamptz not null default now()
#   );
#
# Без цієї таблиці +/- все одно працюватимуть (рейтинг рахуватиметься),
# просто не буде обмеження "раз на добу" — бот залогує попередження і піде далі.

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    logging.warning(f"Supabase connection warning: {e}")
    supabase = None

dp = Dispatcher(storage=MemoryStorage())


class SupportStates(StatesGroup):
    waiting_message = State()


def webapp_keyboard(text: str = "🎮 Відкрити Shade News") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


# ==================================================
# 3. Supabase-хелпери (спільні для профілю, бонусів, репутації)
# ==================================================
def get_rating_row(user_id: int) -> dict | None:
    if supabase is None:
        return None
    try:
        res = supabase.table("ratings").select("*").eq("user_id", user_id).maybe_single().execute()
        return res.data if res else None
    except Exception as e:
        logging.error(f"get_rating_row({user_id}) error: {e}")
        return None


def get_player_row(user_id: int) -> dict | None:
    if supabase is None:
        return None
    try:
        res = supabase.table("players").select("*").eq("user_id", user_id).maybe_single().execute()
        return res.data if res else None
    except Exception as e:
        logging.error(f"get_player_row({user_id}) error: {e}")
        return None


def add_rating(user_id: int, username: str, amount: int, extra: dict | None = None) -> int:
    """Додає (або віднімає, якщо amount від'ємний) рейтинг і повертає нове значення.
    Логіка узгоджена з addRating() в index.html, аби рейтинг у боті й у веб-застосунку
    завжди був однаковим."""
    if supabase is None:
        return 0
    existing = get_rating_row(user_id)
    current = (existing or {}).get("rating", 0)
    new_rating = current + amount

    payload = {"user_id": user_id, "username": username, "rating": new_rating}
    if extra:
        payload.update(extra)

    try:
        supabase.table("ratings").upsert(payload, on_conflict="user_id").execute()
    except Exception as e:
        logging.error(f"add_rating upsert error for {user_id}: {e}")
    return new_rating


# ==================================================
# 4. /start — вітання + обробка реферальних посилань
# ==================================================
@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    first_name = html.quote(user.first_name or "геймере")
    display_name = user.username or user.first_name or "Гравець"

    # --- Реферальне посилання виду /start ref_<id> ---
    args = message.text.split(maxsplit=1) if message.text else []
    if len(args) > 1 and args[1].startswith("ref_") and supabase is not None:
        raw_id = args[1].removeprefix("ref_")
        if raw_id.isdigit():
            referrer_id = int(raw_id)
            if referrer_id != user.id:
                try:
                    my_rating = get_rating_row(user.id)
                    already_referred = bool((my_rating or {}).get("referred_by"))

                    if not already_referred:
                        # Позначаємо, хто запросив цього юзера (без зміни його рейтингу)
                        add_rating(user.id, display_name, 0, extra={"referred_by": referrer_id})

                        referrer_row = get_rating_row(referrer_id) or {}
                        referred_count = (referrer_row.get("referred_count") or 0) + 1
                        tasks_done = list(referrer_row.get("tasks_done") or [])
                        if "invite" not in tasks_done:
                            tasks_done.append("invite")

                        referrer_name = referrer_row.get("username") or "Гравець"
                        add_rating(
                            referrer_id,
                            referrer_name,
                            150,  # відповідає TASKS.invite.reward у index.html
                            extra={"referred_count": referred_count, "tasks_done": tasks_done},
                        )

                        try:
                            await message.bot.send_message(
                                referrer_id,
                                f"🎉 За твоїм посиланням прийшов новий гравець! +150 рейтингу.\n"
                                f"Всього запрошено: {referred_count}",
                            )
                        except Exception:
                            pass  # юзер міг заблокувати бота — це не критично
                except Exception as e:
                    logging.warning(f"Помилка обробки реферального посилання: {e}")

    welcome_text = (
        f"Привіт, {first_name}! 👾\n\n"
        "Ласкаво просимо до <b>Shade News</b> — твого головного хабу ігрових новин, "
        "релізів та знижок.\n\n"
        "Натискай кнопку нижче, щоб запустити додаток, або скористайся командами:\n"
        "/profile — твій профіль і рейтинг\n"
        "/support — зв'язок з підтримкою"
    )
    await message.answer(welcome_text, reply_markup=webapp_keyboard())


# ==================================================
# 5. /profile — швидка статистика + кнопка в застосунок
# ==================================================
@dp.message(Command("profile"))
async def command_profile_handler(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    if supabase is None:
        await message.answer("⚠️ Профіль тимчасово недоступний, спробуй пізніше.")
        return

    rating_row = get_rating_row(user.id) or {}
    player_row = get_player_row(user.id) or {}

    rating = rating_row.get("rating", 0)
    total_points = player_row.get("total_points", 0)
    monthly_points = player_row.get("monthly_points", 0)

    text = (
        "👤 <b>Твій профіль</b>\n\n"
        f"⭐ Репутація: <b>{rating}</b>\n"
        f"🏆 Очки за квізи (всього): <b>{total_points}</b>\n"
        f"📅 Очки за місяць: <b>{monthly_points}</b>\n\n"
        "Повна статистика, досягнення й місце в топі — у застосунку 👇"
    )
    await message.answer(text, reply_markup=webapp_keyboard("📊 Відкрити профіль"))


# ==================================================
# 6. /support — двостороннє спілкування з підтримкою
# ==================================================
@dp.message(Command("support"))
async def command_support_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_message)
    await message.answer(
        "✍️ Опиши своє питання чи проблему одним повідомленням — я одразу передам його підтримці.\n"
        "Щоб скасувати — напиши /cancel."
    )


@dp.message(Command("cancel"), StateFilter(SupportStates.waiting_message))
async def command_cancel_support_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Скасовано.")


@dp.message(StateFilter(SupportStates.waiting_message))
async def process_support_message_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user

    if ADMIN_CHAT_ID is None:
        await message.answer(
            "⚠️ Підтримка зараз не налаштована. Спробуй написати пізніше."
        )
        logging.warning(
            "ADMIN_CHAT_ID не заданий — нема кому передати звернення з /support. "
            "Задай змінну середовища ADMIN_IDS або ADMIN_CHAT_ID."
        )
        return

    header = (
        f"🆘 Звернення від {html.quote(user.full_name)}"
        + (f" (@{user.username})" if user.username else "")
        + f"\nID: {user.id}\n\n"
    )
    body = message.text or message.caption or "[повідомлення без тексту]"

    try:
        await message.bot.send_message(ADMIN_CHAT_ID, header + html.quote(body))
        await message.answer("✅ Звернення передано підтримці. Відповідь прийде сюди ж, у цей чат.")
    except Exception as e:
        logging.error(f"Не вдалось переслати звернення підтримки: {e}")
        await message.answer("⚠️ Не вдалось відправити звернення, спробуй пізніше.")


# Адмін відповідає юзеру, зробивши Telegram-Reply на переслане звернення.
# ID юзера бот бере прямо з тексту звернення ("ID: 12345"), тому нічого
# додатково зберігати між перезапусками бота не потрібно.
@dp.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_handler(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        return  # у цьому чаті може писати хтось інший — ігноруємо

    original = message.reply_to_message
    source_text = original.text or original.caption or ""
    match = re.search(r"ID:\s*(\d+)", source_text)
    if not match:
        return  # це не реплай на звернення підтримки — ігноруємо мовчки

    target_id = int(match.group(1))
    reply_text = message.text or message.caption
    if not reply_text:
        return

    try:
        await message.bot.send_message(
            target_id, f"💬 <b>Відповідь підтримки:</b>\n\n{html.quote(reply_text)}"
        )
        await message.reply("✅ Відповідь надіслана користувачу.")
    except Exception as e:
        logging.error(f"Не вдалось надіслати відповідь користувачу {target_id}: {e}")
        await message.reply(f"⚠️ Не вдалось надіслати відповідь: {e}")


# ==================================================
# 7. Репутація в чатах: + / - у відповідь на повідомлення
# ==================================================
REP_TRIGGERS_UP = {"+", "+1", "👍"}
REP_TRIGGERS_DOWN = {"-", "-1", "👎"}


@dp.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.reply_to_message,
    F.text.in_(REP_TRIGGERS_UP | REP_TRIGGERS_DOWN),
)
async def reputation_handler(message: Message) -> None:
    voter = message.from_user
    target = message.reply_to_message.from_user

    if voter is None or target is None or target.is_bot:
        return
    if voter.id == target.id:
        await message.reply("🙅 Не можна змінювати репутацію самому собі.")
        return

    delta = 1 if message.text in REP_TRIGGERS_UP else -1

    # Обмеження "раз на добу на пару юзерів" — щоб не крутили рейтинг спамом.
    # Якщо таблиці reputation_votes ще нема в Supabase — просто пропускаємо перевірку
    # (з попередженням у логах), голос все одно рахується.
    if supabase is not None:
        try:
            since = (datetime.now(timezone.utc) - timedelta(hours=REP_COOLDOWN_HOURS)).isoformat()
            existing_vote = (
                supabase.table("reputation_votes")
                .select("id")
                .eq("voter_id", voter.id)
                .eq("target_id", target.id)
                .gte("created_at", since)
                .limit(1)
                .execute()
            )
            if existing_vote.data:
                await message.reply(
                    f"⏳ Ти вже голосував за {html.quote(target.first_name)} "
                    f"за останні {REP_COOLDOWN_HOURS} год. Спробуй пізніше."
                )
                return
        except Exception as e:
            logging.warning(
                f"Перевірка кулдауну репутації не вдалась (можливо, нема таблиці "
                f"reputation_votes — див. коментар на початку файлу): {e}"
            )

    target_name = target.username or target.first_name or "Гравець"
    new_rating = add_rating(target.id, target_name, delta)

    if supabase is not None:
        try:
            supabase.table("reputation_votes").insert({
                "voter_id": voter.id,
                "target_id": target.id,
                "delta": delta,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            logging.warning(f"Не вдалось записати голос у reputation_votes: {e}")

    emoji = "⬆️" if delta > 0 else "⬇️"
    await message.reply(
        f"{emoji} {html.quote(target.first_name)} тепер має <b>{new_rating}</b> репутації."
    )


# ==================================================
# 8. /stats — швидка адмінська статистика прямо в Telegram
# ==================================================
@dp.message(Command("stats"))
async def command_stats_handler(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        return  # звичайним юзерам команда не показується і не відповідає

    if supabase is None:
        await message.answer("⚠️ Supabase не підключено.")
        return

    try:
        total_users_res = (
            supabase.table("players").select("user_id", count="exact", head=True).execute()
        )
        total_quizzes_res = (
            supabase.table("completed_quizzes").select("user_id", count="exact", head=True).execute()
        )
        top_rating_res = (
            supabase.table("ratings")
            .select("username, rating")
            .order("rating", desc=True)
            .limit(5)
            .execute()
        )

        lines = [
            "📊 <b>Статистика Shade News</b>\n",
            f"👥 Гравців у базі: <b>{total_users_res.count or 0}</b>",
            f"📝 Пройдено квізів: <b>{total_quizzes_res.count or 0}</b>\n",
            "🏆 <b>Топ-5 за репутацією:</b>",
        ]
        for i, row in enumerate(top_rating_res.data or [], start=1):
            name = html.quote(row.get("username") or "Гравець")
            lines.append(f"{i}. {name} — {row.get('rating', 0)}")

        await message.answer("\n".join(lines))
    except Exception as e:
        logging.error(f"Помилка /stats: {e}")
        await message.answer("⚠️ Не вдалось отримати статистику. Перевір, чи не призупинений Supabase.")


# ==================================================
# 9. Обробник постів з каналу — автозбереження новин у Supabase
# ==================================================
@dp.channel_post(F.chat.username == TARGET_CHANNEL_USERNAME)
async def capture_channel_post(message: Message) -> None:
    if supabase is None:
        logging.error("Supabase is not connected. Cannot save post.")
        return

    try:
        raw_text = message.html_text or message.caption or "Медіа файл 📸"
        safe_text = raw_text.strip()

        # Заглушка. Telegram не дає вічних лінків на фото без окремого завантаження.
        img_url = "https://images.unsplash.com/photo-1542751371-adc38448a05e?ixlib=rb-1.2.1&auto=format&fit=crop&w=600&q=80"

        data = {
            "message_id": message.message_id,
            "text": safe_text,
            "image_url": img_url,
        }
        supabase.table("news").insert(data).execute()
        logging.info(f"✅ Пост {message.message_id} успішно збережено в Supabase!")
    except Exception as e:
        logging.error(f"❌ Помилка при збереженні посту {message.message_id}: {e}")


# ==================================================
# 10. Точка входу
# ==================================================
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )

    if not ADMIN_IDS:
        logging.warning(
            "ADMIN_IDS не заданий — /support і /stats не працюватимуть, поки не впишеш "
            "свій Telegram ID у змінну середовища ADMIN_IDS (або в код нагорі файлу)."
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    logging.info("Starting Shade News Bot...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())