# -*- coding: utf-8 -*-
"""
米舖 喊單 Bot（網頁下單版 v2.1 — 訂單編號偵測規則改嚴格，避免價目表誤觸發）
------------------------------------------------------------
跟 v2 的差異：
  - 訂單編號現在統一是 9 碼（不足會前面補0），所以把偵測規則從「4~10位數字都當候選」
    改成「剛好 9 位數字、前後都不能緊接其他數字」，價目表裡常見的 2~4 位數價錢、
    商品編號就不會再被誤判成訂單編號、觸發「找不到此訂單編號」的誤報。

其餘邏輯跟 v2 完全一樣：呼叫網站 API 驗證訂單、記錄 Discord 帳號，不直接碰 Google Sheet。

需要安裝：
  pip install discord.py aiohttp python-dotenv

.env 需要設定（放在同一資料夾）：
  DISCORD_TOKEN=你的機器人Token
  MIBU_API_BASE=https://你的網站網址              # 例如 https://minipu.vercel.app
  BOT_API_SECRET=跟 Vercel 後台環境變數同一組密碼
  ALLOWED_CATEGORIES=123456789012345678          # 選填
  ALLOWED_CHANNEL_IDS=987654321098765432         # 選填
"""

import os
import re
import asyncio

import discord
import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ========== 設定 ==========
TOKEN      = os.getenv("DISCORD_TOKEN")
API_BASE   = (os.getenv("MIBU_API_BASE") or "").rstrip("/")
BOT_SECRET = os.getenv("BOT_API_SECRET")

ALLOWED_CATEGORIES = {int(i.strip()) for i in os.getenv("ALLOWED_CATEGORIES", "").split(",") if i.strip()}
ALLOWED_CHANNEL_IDS = {int(i.strip()) for i in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if i.strip()}

SUCCESS_DELETE_DELAY = 1800  # 「喊單成功」訊息幾秒後自動刪除（1800 = 30 分鐘）；找不到/打錯的不刪

# 訂單編號現在統一是 9 碼數字（不足會前面補0）；用前後零寬斷言確保「剛好 9 碼」，
# 不會抓到價目表裡常見的 2~4 位數價錢、商品編號，也不會從更長的數字串裡誤截出剛好9碼的片段
ORDER_NO_REGEX = re.compile(r"(?<!\d)\d{9}(?!\d)")


# ========== 呼叫網站 API ==========
async def api_get_order_status(session: aiohttp.ClientSession, order_no: str):
    url = f"{API_BASE}/api/bot/order-status"
    headers = {"Authorization": f"Bearer {BOT_SECRET}"}
    async with session.get(url, params={"orderNo": order_no}, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            print(f"order-status 呼叫失敗 ({resp.status})：{await resp.text()}")
            return None
        data = await resp.json()
        return data if data.get("found") else None


async def api_link_discord(session: aiohttp.ClientSession, order_no: str, user_id: int, username: str) -> str:
    """回傳 'ok' / 'wrong_owner' / 'no_member' / 'order_not_found' / 'error'"""
    url = f"{API_BASE}/api/bot/link-discord"
    headers = {"Authorization": f"Bearer {BOT_SECRET}", "Content-Type": "application/json"}
    payload = {"orderNo": order_no, "discordUserId": str(user_id), "discordUsername": username}
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"link-discord 呼叫失敗 ({resp.status})：{await resp.text()}")
                return "error"
            data = await resp.json()
            return data.get("status", "error")
    except Exception as e:
        print(f"link-discord 例外：{e}")
        return "error"


# ========== Discord ==========
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
client = discord.Client(intents=intents)

http_session = None  # type: aiohttp.ClientSession | None


def is_watched_channel(channel) -> bool:
    if ALLOWED_CHANNEL_IDS and channel.id in ALLOWED_CHANNEL_IDS:
        return True
    if ALLOWED_CATEGORIES and getattr(channel, "category", None) and channel.category.id in ALLOWED_CATEGORIES:
        return True
    if not ALLOWED_CHANNEL_IDS and not ALLOWED_CATEGORIES:
        return True  # 兩個都沒設 = 全部頻道
    return False


# 記住每一則使用者訊息對應的 bot 回覆，之後使用者編輯訊息時就「改同一則回覆」
claim_replies = {}  # {使用者訊息id: bot 回覆 Message}


async def upsert_reply(user_msg, text: str):
    """已經回覆過就編輯那則；沒有就新發一則。"""
    existing = claim_replies.get(user_msg.id)
    if existing:
        try:
            await existing.edit(content=text)
            return existing
        except Exception:
            pass  # 舊回覆可能被刪了，改成重發
    reply = await user_msg.channel.send(text)
    claim_replies[user_msg.id] = reply
    # 避免無限成長：超過一定量就清掉最舊的
    if len(claim_replies) > 2000:
        for k in list(claim_replies.keys())[:500]:
            claim_replies.pop(k, None)
    return reply


async def schedule_delete(reply_msg, user_msg_id, delay: int = SUCCESS_DELETE_DELAY):
    """指定秒數後刪除這則 bot 回覆（用於喊單成功）。"""
    async def _del():
        try:
            await asyncio.sleep(delay)
            await reply_msg.delete()
        except Exception:
            pass
        finally:
            if claim_replies.get(user_msg_id) is reply_msg:
                claim_replies.pop(user_msg_id, None)
    asyncio.create_task(_del())


async def handle_claim(message):
    """驗證訊息中的訂單編號，並把 bot 回覆更新成對應狀態。"""
    if message.author.bot or not is_watched_channel(message.channel):
        return

    content = message.content or ""
    candidates = ORDER_NO_REGEX.findall(content)
    if not candidates:
        return  # 沒有符合「剛好9碼」的數字，當一般聊天，不回應

    # 逐一驗證，找到第一個「真的存在」的訂單編號
    matched = None
    for cand in candidates:
        info = await api_get_order_status(http_session, cand)
        if info:
            matched = (cand, info)
            break

    mention = message.author.mention

    if not matched:
        # 找不到 → 提示留著（不刪），等使用者把訊息編輯成正確編號就會自動變成功
        await upsert_reply(message, f"{mention} 找不到此訂單編號，請重新確認並修改此訊息。")
        return

    cand, info = matched

    # 記錄 Discord 帳號到訂單所屬的會員資料
    status = await api_link_discord(http_session, cand, message.author.id, message.author.name)

    if status == "wrong_owner":
        await upsert_reply(message, f"{mention} 這訂單編號不是你的，是不是打錯了？")
    else:
        # no_member / error 這種對不到會員或系統小狀況，仍然視為喊單成功（訂單編號本身是對的）
        reply = await upsert_reply(message, f"{mention} 喊單成功！（訂單編號 {cand}）")
        await schedule_delete(reply, message.id)  # 成功訊息 30 分鐘後自動刪除


@client.event
async def on_ready():
    print(f"已登入：{client.user}")
    if ALLOWED_CHANNEL_IDS or ALLOWED_CATEGORIES:
        print(f"監聽：分類 {ALLOWED_CATEGORIES or '—'}；頻道 {ALLOWED_CHANNEL_IDS or '—'}")
    else:
        print("監聽：所有頻道（未設定 ALLOWED_CATEGORIES / ALLOWED_CHANNEL_IDS）")


@client.event
async def on_message(message):
    await handle_claim(message)


@client.event
async def on_message_edit(before, after):
    # 使用者編輯訊息（例如把打錯的編號改對）→ 重新驗證並更新同一則 bot 回覆
    await handle_claim(after)


async def main():
    global http_session
    if not TOKEN or not API_BASE or not BOT_SECRET:
        raise SystemExit("請先在 .env 設定 DISCORD_TOKEN、MIBU_API_BASE、BOT_API_SECRET")
    async with aiohttp.ClientSession() as session:
        http_session = session
        await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
