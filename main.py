import asyncio
import html
import ipaddress
import os
import re
from urllib.parse import urlparse

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
PROXY = os.getenv("PROXY")  # e.g. socks5://127.0.0.1:1080 (only if running from Iran)
AUTO = (
    os.getenv("AUTO_DETECT") == "1"
)  # offer "add" button on any pasted link (needs privacy mode OFF)
DB = "villas.db"
URL_RE = re.compile(r"https?://\S+")
HTML = ParseMode.HTML
NOPREV = LinkPreviewOptions(is_disabled=True)
router = Router()

HELP = (
    "🏡 <b>ربات انتخاب ویلا</b>\n\n"
    "/add لینک [نام] — افزودن ویلا\n"
    "/villas — نمایش لیست و رأی‌گیری\n"
    "/remove شماره — حذف ویلا (فقط اضافه‌کننده یا ادمین)\n"
    "/close — بستن رأی‌گیری و اعلام برنده (ادمین)\n"
    "/open — باز کردن دوباره (ادمین)\n"
    "/reset — پاک کردن همه چیز (ادمین)\n\n"
    "هر نفر یک رأی دارد؛ دوباره روی همان ویلا بزنید تا رأی پس گرفته شود."
)


# ---------- helpers ----------
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS villas(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, url TEXT, title TEXT, added_by INTEGER,
            UNIQUE(chat_id, url));
        CREATE TABLE IF NOT EXISTS votes(
            chat_id INTEGER, user_id INTEGER, villa_id INTEGER, name TEXT,
            PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, closed INTEGER DEFAULT 0);
        """)
        await db.commit()


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    mem = await bot.get_chat_member(chat_id, user_id)
    return mem.status in ("creator", "administrator")


async def is_public_host(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def fetch_title(url: str) -> str | None:
    try:
        host = urlparse(url).hostname
        if not host or not await is_public_host(host):
            return None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                page = (await r.content.read(300_000)).decode("utf-8", "ignore")
        m = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            page,
            re.I | re.S,
        ) or re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        return html.unescape(m.group(1)).strip()[:60] if m else None
    except Exception:
        return None


async def add_villa(chat_id, user_id, url, title):
    try:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "INSERT INTO villas(chat_id,url,title,added_by) VALUES(?,?,?,?)",
                (chat_id, url, title, user_id),
            )
            await db.commit()
            return cur.lastrowid
    except aiosqlite.IntegrityError:
        return None


async def board(chat_id: int):
    """returns (closed, [(id, url, title, [voter names])] sorted by votes)"""
    async with aiosqlite.connect(DB) as db:
        row = await (
            await db.execute("SELECT closed FROM chats WHERE chat_id=?", (chat_id,))
        ).fetchone()
        villas = await (
            await db.execute(
                "SELECT id,url,title FROM villas WHERE chat_id=?", (chat_id,)
            )
        ).fetchall()
        votes = await (
            await db.execute(
                "SELECT villa_id,name FROM votes WHERE chat_id=?", (chat_id,)
            )
        ).fetchall()
    names = {}
    for vid, n in votes:
        names.setdefault(vid, []).append(n)
    rows = [(v, u, t, names.get(v, [])) for v, u, t in villas]
    rows.sort(key=lambda r: (-len(r[3]), r[0]))
    return bool(row and row[0]), rows


async def render(chat_id: int):
    closed, rows = await board(chat_id)
    if not rows:
        return "هنوز ویلایی اضافه نشده.\nبا /add لینک [نام] اضافه کنید.", None

    head = "🔒 <b>رأی‌گیری بسته شد</b>" if closed else "🗳 <b>رأی‌گیری ویلا</b>"
    lines, kb, total = [head, ""], InlineKeyboardBuilder(), 0
    for vid, url, title, voters in rows:
        total += len(voters)
        lines.append(
            f"<b>#{vid}</b> <a href='{html.escape(url, quote=True)}'>{html.escape(title)}</a> — <b>{len(voters)}</b> رأی"
        )
        if voters:
            lines.append("   👤 " + "، ".join(html.escape(n) for n in voters))
        kb.button(text=f"#{vid} {title[:25]} ({len(voters)})", callback_data=f"v:{vid}")
    lines += ["", f"مجموع رأی‌ها: {total}"]
    kb.adjust(1)
    return "\n".join(lines)[:4000], (None if closed else kb.as_markup())


async def refresh(msg: Message):
    text, kb = await render(msg.chat.id)
    try:
        await msg.edit_text(
            text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV
        )
    except TelegramBadRequest:
        pass  # message is not modified


# ---------- commands ----------
@router.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(HELP, parse_mode=HTML)


@router.message(Command("add"))
async def add(m: Message, command: CommandObject):
    args = command.args or ""
    mt = URL_RE.search(args)
    if not mt:
        return await m.reply("استفاده: /add لینک [نام ویلا]")
    url = mt.group()
    title = (
        args.replace(url, "").strip()
        or await fetch_title(url)
        or urlparse(url).hostname
        or url[:40]
    )
    vid = await add_villa(m.chat.id, m.from_user.id, url, title)
    if not vid:
        return await m.reply("⚠️ این لینک قبلاً اضافه شده.")
    await m.reply(
        f"✅ ویلا <b>#{vid}</b> اضافه شد: {html.escape(title)}\nبرای رأی دادن: /villas",
        parse_mode=HTML,
        link_preview_options=NOPREV,
    )


@router.message(Command("villas", "vote"))
async def show(m: Message):
    text, kb = await render(m.chat.id)
    await m.answer(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV)


@router.message(Command("remove"))
async def remove(m: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.reply("استفاده: /remove شماره")
    vid = int(command.args)
    async with aiosqlite.connect(DB) as db:
        row = await (
            await db.execute(
                "SELECT added_by FROM villas WHERE id=? AND chat_id=?", (vid, m.chat.id)
            )
        ).fetchone()
        if not row:
            return await m.reply("چنین ویلایی پیدا نشد.")
        if row[0] != m.from_user.id and not await is_admin(
            m.bot, m.chat.id, m.from_user.id
        ):
            return await m.reply("⛔ فقط اضافه‌کننده یا ادمین می‌تواند حذف کند.")
        await db.execute("DELETE FROM villas WHERE id=?", (vid,))
        await db.execute("DELETE FROM votes WHERE villa_id=?", (vid,))
        await db.commit()
    await m.reply(f"🗑 ویلا #{vid} حذف شد.")


@router.message(Command("close"))
async def close(m: Message):
    if not await is_admin(m.bot, m.chat.id, m.from_user.id):
        return await m.reply("⛔ فقط ادمین‌ها می‌توانند رأی‌گیری را ببندند.")
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO chats VALUES(?,1) ON CONFLICT(chat_id) DO UPDATE SET closed=1",
            (m.chat.id,),
        )
        await db.commit()
    _, rows = await board(m.chat.id)
    top = len(rows[0][3]) if rows else 0
    if top == 0:
        return await m.answer("🔒 رأی‌گیری بسته شد؛ اما هیچ رأیی ثبت نشده بود.")
    winners = [r for r in rows if len(r[3]) == top]
    if len(winners) == 1:
        _, url, title, _ = winners[0]
        msg = f"🏆 <b>برنده:</b> <a href='{html.escape(url, quote=True)}'>{html.escape(title)}</a>\nبا {top} رأی 🎉"
    else:
        items = "\n".join(
            f"• <a href='{html.escape(u, quote=True)}'>{html.escape(t)}</a>"
            for _, u, t, _ in winners
        )
        msg = f"🤝 <b>مساوی!</b> هر کدام {top} رأی:\n{items}\n\nبا /open دوباره باز کنید و تصمیم بگیرید."
    await m.answer(
        "🔒 رأی‌گیری بسته شد.\n\n" + msg, parse_mode=HTML, link_preview_options=NOPREV
    )


@router.message(Command("open"))
async def reopen(m: Message):
    if not await is_admin(m.bot, m.chat.id, m.from_user.id):
        return await m.reply("⛔ فقط ادمین‌ها.")
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO chats VALUES(?,0) ON CONFLICT(chat_id) DO UPDATE SET closed=0",
            (m.chat.id,),
        )
        await db.commit()
    await m.answer("🔓 رأی‌گیری دوباره باز شد. /villas")


@router.message(Command("reset"))
async def reset(m: Message):
    if not await is_admin(m.bot, m.chat.id, m.from_user.id):
        return await m.reply("⛔ فقط ادمین‌ها.")
    async with aiosqlite.connect(DB) as db:
        for t in ("villas", "votes", "chats"):
            await db.execute(f"DELETE FROM {t} WHERE chat_id=?", (m.chat.id,))
        await db.commit()
    await m.answer("♻️ همه چیز پاک شد.")


# ---------- callbacks ----------
@router.callback_query(F.data.startswith("v:"))
async def vote(cb: CallbackQuery):
    vid, uid, cid = int(cb.data[2:]), cb.from_user.id, cb.message.chat.id
    async with aiosqlite.connect(DB) as db:
        closed = await (
            await db.execute("SELECT closed FROM chats WHERE chat_id=?", (cid,))
        ).fetchone()
        if closed and closed[0]:
            return await cb.answer("رأی‌گیری بسته شده است 🔒", show_alert=True)
        if not await (
            await db.execute(
                "SELECT 1 FROM villas WHERE id=? AND chat_id=?", (vid, cid)
            )
        ).fetchone():
            await cb.answer("این ویلا دیگر وجود ندارد", show_alert=True)
            return await refresh(cb.message)
        prev = await (
            await db.execute(
                "SELECT villa_id FROM votes WHERE chat_id=? AND user_id=?", (cid, uid)
            )
        ).fetchone()
        if prev and prev[0] == vid:
            await db.execute(
                "DELETE FROM votes WHERE chat_id=? AND user_id=?", (cid, uid)
            )
            note = "رأی شما پس گرفته شد"
        else:
            await db.execute(
                "INSERT OR REPLACE INTO votes VALUES(?,?,?,?)",
                (cid, uid, vid, cb.from_user.full_name),
            )
            note = f"به ویلا #{vid} رأی دادید ✅"
        await db.commit()
    await refresh(cb.message)
    await cb.answer(note)


# ---------- optional: auto-detect pasted links ----------
async def offer(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزودن به لیست ویلاها", callback_data="a")
    await m.reply("این لینک ویلاست؟", reply_markup=kb.as_markup())


@router.callback_query(F.data == "a")
async def auto_add(cb: CallbackQuery):
    src = cb.message.reply_to_message
    mt = src and src.text and URL_RE.search(src.text)
    if not mt:
        return await cb.answer("لینک پیدا نشد", show_alert=True)
    url = mt.group()
    title = await fetch_title(url) or urlparse(url).hostname or url[:40]
    vid = await add_villa(cb.message.chat.id, src.from_user.id, url, title)
    await cb.message.edit_text(
        f"✅ ویلا #{vid} اضافه شد: {html.escape(title)}"
        if vid
        else "⚠️ این لینک قبلاً اضافه شده.",
        parse_mode=HTML,
    )
    await cb.answer()


if AUTO:
    router.message.register(offer, F.text.regexp(URL_RE), ~F.text.startswith("/"))


async def main():
    await init_db()
    bot = Bot(TOKEN, session=AiohttpSession(proxy=PROXY) if PROXY else None)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
