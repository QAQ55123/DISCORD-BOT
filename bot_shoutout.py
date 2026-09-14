# -*- coding: utf-8 -*-
"""
米舖 喊單 Bot（v3 — 支援多商店，依 Discord 頻道判斷要查哪個網站）
------------------------------------------------------------
跟 v2.1 的差異：
  - 原本只能設定「一個」網站（MIBU_API_BASE + BOT_API_SECRET），現在可以設定「好幾個」，
    每個商店各自綁定自己的頻道 ID／分類 ID，Bot 收到訊息時會先判斷「這則訊息屬於哪個商店」，
    再去呼叫對應那個商店的網站 API 驗證訂單編號、記錄 Discord 帳號。
  - 一個頻道只會歸屬到一個商店（先比對到哪個就用哪個，不會同時查兩邊）。
  - 完全沒被任何商店的頻道/分類設定包含到的頻道，Bot 不會回應（避免商店A的訊息誤查到商店B去）。

需要安裝：
  pip install discord.py aiohttp python-dotenv

.env 需要設定（放在同一資料夾），商店用 SHOP1、SHOP2、SHOP3... 這樣編號，要幾個商店就加幾組：
  DISCORD_TOKEN=你的機器人Token

  # 商店 1
  SHOP1_NAME=商店A（只是給你自己看的識別名稱，不影響功能）
  SHOP1_API_BASE=https://shopA.vercel.app
  SHOP1_API_SECRET=（跟商店A網站 Vercel 後台設定的 BOT_API_SECRET 同一組）
  SHOP1_CHANNEL_IDS=111111111111111111,222222222222222222      # 選填，逗號分隔
  SHOP1_CATEGORY_IDS=333333333333333333                         # 選填，逗號分隔

  # 商店 2
  SHOP2_NAME=商店B
  SHOP2_API_BASE=https://shopB.vercel.app
  SHOP2_API_SECRET=（跟商店B網站 Vercel 後台設定的 BOT_API_SECRET 同一組）
  SHOP2_CHANNEL_IDS=444444444444444444
  SHOP2_CATEGORY_IDS=

  # 如果還有商店3、商店4...依此類推繼續加 SHOP3_xxx、SHOP4_xxx
"""

import os
import re
import asyncio

import discord
import aiohttp
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

SUCCESS_DELETE_DELAY = 1800  # 「喊單成功」訊息幾秒後自動刪除（1800 = 30 分鐘）；找不到/打錯的不刪
ORDER_NO_REGEX = re.compile(r"(?<!\d)\d{9}(?!\d)")  # 訂單編號固定9碼


# ========== 讀取多商店設定 ==========
class ShopConfig:
    def __init__(self, key, name, api_base, api_secret, channel_ids, category_ids):
        self.key = key
        self.name = name or key
        self.api_base = api_base.rstrip("/")
        self.api_secret = api_secret
        self.channel_ids = channel_ids
        self.category_ids = category_ids

    def matches(self, channel) -> bool:
        if channel.id in self.channel_ids:
            return True
        category = getattr(channel, "category", None)
        if category and category.id in self.category_ids:
            return True
        return False


def load_shops() -> list:
    shops = []
    i = 1
    while True:
        prefix = f"SHOP{i}_"
        api_base = os.getenv(f"{prefix}API_BASE")
        api_secret = os.getenv(f"{prefix}API_SECRET")
        if not api_base or not api_secret:
            break
        name = os.getenv(f"{prefix}NAME", f"商店{i}")
        channel_ids = {int(x.strip()) for x in os.getenv(f"{prefix}CHANNEL_IDS", "").split(",") if x.strip()}
        category_ids = {int(x.strip()) for x in os.getenv(f"{prefix}CATEGORY_IDS", "").split(",") if x.strip()}
        shops.append(ShopConfig(f"SHOP{i}", name, api_base, api_secret, channel_ids, category_ids))
        i += 1
    return shops


SHOPS = load_shops()


def find_shop_for_channel(channel):
    """找出這個頻道屬於哪個商店；都沒對到就回傳 None（Bot 不回應）。"""
    for shop in SHOPS:
        if shop.matches(channel):
            return shop
    return None


# ========== 呼叫商店網站 API ==========
async def api_get_order_status(session: aiohttp.ClientSession, shop: "ShopConfig", order_no: str):
    url = f"{shop.api_base}/api/bot/order-status"
    headers = {"Authorization": f"Bearer {shop.api_secret}"}
    async with session.get(url, params={"orderNo": order_no}, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            print(f"[{shop.name}] order-status 呼叫失敗 ({resp.status})：{await resp.text()}")
            return None
        data = await resp.json()
        return data if data.get("found") else None


async def api_link_discord(session: aiohttp.ClientSession, shop: "ShopConfig", order_no: str, user_id: int, username: str) -> str:
    """回傳 'ok' / 'wrong_owner' / 'no_member' / 'order_not_found' / 'error'"""
    url = f"{shop.api_base}/api/bot/link-discord"
    headers = {"Authorization": f"Bearer {shop.api_secret}", "Content-Type": "application/json"}
    payload = {"orderNo": order_no, "discordUserId": str(user_id), "discordUsername": username}
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"[{shop.name}] link-discord 呼叫失敗 ({resp.status})：{await resp.text()}")
                return "error"
            data = await resp.json()
            return data.get("status", "error")
    except Exception as e:
        print(f"[{shop.name}] link-discord 例外：{e}")
        return "error"


# ========== Discord ==========
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
client = discord.Client(intents=intents)

http_session = None  # type: aiohttp.ClientSession | None

claim_replies = {}  # {使用者訊息id: bot 回覆 Message}


async def upsert_reply(user_msg, text: str):
    existing = claim_replies.get(user_msg.id)
    if existing:
        try:
            await existing.edit(content=text)
            return existing
        except Exception:
            pass
    reply = await user_msg.channel.send(text)
    claim_replies[user_msg.id] = reply
    if len(claim_replies) > 2000:
        for k in list(claim_replies.keys())[:500]:
            claim_replies.pop(k, None)
    return reply


async def schedule_delete(reply_msg, user_msg_id, delay: int = SUCCESS_DELETE_DELAY):
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
    if message.author.bot:
        return

    shop = find_shop_for_channel(message.channel)
    if shop is None:
        return  # 這個頻道沒有歸屬到任何商店，不處理

    content = message.content or ""
    candidates = ORDER_NO_REGEX.findall(content)
    if not candidates:
        return

    matched = None
    for cand in candidates:
        info = await api_get_order_status(http_session, shop, cand)
        if info:
            matched = (cand, info)
            break

    mention = message.author.mention

    if not matched:
        await upsert_reply(message, f"{mention} 找不到此訂單編號，請重新確認並修改此訊息。")
        return

    cand, info = matched
    status = await api_link_discord(http_session, shop, cand, message.author.id, message.author.name)

    if status == "wrong_owner":
        await upsert_reply(message, f"{mention} 這訂單編號不是你的，是不是打錯了？")
    else:
        reply = await upsert_reply(message, f"{mention} 喊單成功！（訂單編號 {cand}）")
        await schedule_delete(reply, message.id)


@client.event
async def on_ready():
    print(f"已登入：{client.user}")
    if not SHOPS:
        print("警告：完全沒有設定任何商店（SHOP1_API_BASE 等），Bot 不會對任何訊息有反應")
    for shop in SHOPS:
        print(f"商店「{shop.name}」：{shop.api_base}　監聽頻道 {shop.channel_ids or '—'}　監聽分類 {shop.category_ids or '—'}")


@client.event
async def on_message(message):
    await handle_claim(message)


@client.event
async def on_message_edit(before, after):
    await handle_claim(after)


async def main():
    global http_session
    if not TOKEN:
        raise SystemExit("請先在 .env 設定 DISCORD_TOKEN")
    if not SHOPS:
        raise SystemExit("請先在 .env 設定至少一組商店（SHOP1_API_BASE、SHOP1_API_SECRET）")
    async with aiohttp.ClientSession() as session:
        http_session = session
        await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
