# -*- coding: utf-8 -*-
"""
米舖 喊單 Bot（網頁下單版）
------------------------------------------------------------
流程：
  1) 使用者在網頁下單 → 取得 6 位數「訂單編號」。
  2) 使用者到 Discord 頻道貼出訂單編號。
  3) Bot 去米舖試算表確認這個編號「真的存在」：
       - 存在  → 回覆「喊單成功！」，該回覆 1 小時後自動刪除。
       - 不存在 → 回覆提示，同樣 1 小時後自動刪除。
  4) 用訂單編號找到那筆訂單所屬「會員」，把發文者的
     Discord「使用者ID＋帳號名稱(username)」補寫到會員資料
     （寫在 E、F 欄，不覆蓋原本的 Discord 暱稱）。

需要安裝：
  pip install discord.py gspread oauth2client python-dotenv

.env 需要設定（放在同一資料夾）：
  DISCORD_TOKEN=你的機器人Token
  GOOGLE_JSON_FILE=service_account.json   # 服務帳號金鑰檔（要把該帳號 email 加成米舖表的編輯者）
  SHEET_ID=米舖試算表的ID
  # 下面兩個擇一或都填；都留空＝所有頻道都監聽
  ALLOWED_CATEGORIES=123456789012345678          # 監聽這些「分類」底下的所有文字頻道（逗號分隔，選填）
  ALLOWED_CHANNEL_IDS=987654321098765432         # 直接指定要監聽的頻道（逗號分隔，選填）
"""

import os
import re
import time
import asyncio

import discord
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

# ========== 設定 ==========
TOKEN     = os.getenv("DISCORD_TOKEN")
JSON_FILE = os.getenv("GOOGLE_JSON_FILE")
SHEET_ID  = os.getenv("SHEET_ID")

ALLOWED_CATEGORIES = {int(i.strip()) for i in os.getenv("ALLOWED_CATEGORIES", "").split(",") if i.strip()}
ALLOWED_CHANNEL_IDS = {int(i.strip()) for i in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if i.strip()}

DELETE_DELAY = 3600          # 回覆訊息幾秒後刪除（3600 = 1 小時）
DELETE_USER_MESSAGE = False  # 若也想在 1 小時後刪掉「使用者貼的訂單編號訊息」，改成 True

# 米舖試算表的系統分頁（不是企劃分頁，掃訂單時要跳過）
SYSTEM_SHEETS = {"企劃清單", "會員資料", "設定", "疑似重複"}
MEMBER_SHEET_NAME = "會員資料"
ORDER_HEADER_KEY = "訂單編號"   # 訂單區標題列，A 欄會是這個字

# 訂單編號長度（米舖是 6 位數；抓 4~10 位數字當候選，再用試算表驗證）
ORDER_NO_REGEX = re.compile(r"\d{4,10}")

SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ========== Google 試算表 ==========
def get_spreadsheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, SCOPE)
    gs = gspread.authorize(creds)
    return gs.open_by_key(SHEET_ID)


def norm_fb(url: str) -> str:
    """與米舖 Code.gs 的 normFb_ 對齊：正規化 FB 網址，當作同一人的鍵。"""
    s = str(url or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(www\.|m\.|web\.|mobile\.)", "", s)
    q = ""
    qi = s.find("?")
    if qi >= 0:
        q = s[qi + 1:]
        s = s[:qi]
    s = re.sub(r"/+$", "", s)
    if "facebook.com/profile.php" in s and q:
        m = re.search(r"(?:^|&)id=(\d+)", q)
        if m:
            return "facebook.com/profile.php?id=" + m.group(1)
    return s


# ---------- 訂單編號索引（快取，降低 API 呼叫）----------
_order_index = {}        # 訂單編號(str) -> {"fb","source","nick","sheet"}
_order_index_time = 0.0


def build_order_index():
    """掃描所有企劃分頁的訂單區，建立 訂單編號 -> 訂單資訊 的對照。"""
    global _order_index, _order_index_time
    idx = {}
    ss = get_spreadsheet()
    for ws in ss.worksheets():
        title = ws.title
        if title in SYSTEM_SHEETS or title.startswith("_"):
            continue
        try:
            values = ws.get_all_values()
        except Exception as e:
            print(f"讀取分頁失敗 {title}: {e}")
            continue
        # 找訂單區標題列（A 欄 == 訂單編號）
        header_row = -1
        for i, row in enumerate(values):
            if row and str(row[0]).strip() == ORDER_HEADER_KEY:
                header_row = i
                break
        if header_row < 0:
            continue
        for row in values[header_row + 1:]:
            if not row or not str(row[0]).strip():
                continue
            order_no = str(row[0]).strip()
            idx[order_no] = {
                "fb":     row[3].strip() if len(row) > 3 else "",
                "source": row[1].strip() if len(row) > 1 else "",
                "nick":   row[2].strip() if len(row) > 2 else "",
                "sheet":  title,
            }
    _order_index = idx
    _order_index_time = time.time()
    print(f"訂單索引已更新，共 {len(idx)} 筆")


def find_order(order_no: str):
    """查訂單編號是否存在；找不到且索引有點舊時，重建一次再找（抓剛下的新單）。"""
    global _order_index_time
    if time.time() - _order_index_time > 15:
        build_order_index()
    info = _order_index.get(order_no)
    if info is None and time.time() - _order_index_time > 3:
        build_order_index()
        info = _order_index.get(order_no)
    return info


def link_dc_to_member(order_info: dict, user_id: int, username: str) -> bool:
    """用訂單資訊找到會員，把 DC 帳號名稱/ID 寫到會員資料 E、F 欄。"""
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(MEMBER_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        print("找不到『會員資料』分頁")
        return False

    values = ws.get_all_values()
    if not values:
        return False

    # 確保表頭有 DC 欄（E=第5欄 帳號名稱、F=第6欄 使用者ID）
    header = values[0] if values else []
    if len(header) < 5 or str(header[4]).strip() != "DC帳號名稱":
        ws.update_cell(1, 5, "DC帳號名稱")
    if len(header) < 6 or str(header[5]).strip() != "DC使用者ID":
        ws.update_cell(1, 6, "DC使用者ID")

    # 找會員列：先用 FB 正規化比對
    target_fb = norm_fb(order_info.get("fb", ""))
    row_idx = -1
    if target_fb:
        for i in range(1, len(values)):
            fb = values[i][0] if len(values[i]) > 0 else ""
            if norm_fb(fb) == target_fb:
                row_idx = i + 1  # 1-based
                break

    # 後援：來源是 Discord 時，用 Discord 暱稱(C欄)比對
    if row_idx < 0 and order_info.get("source", "") in ("Discord", "DC"):
        nick = order_info.get("nick", "")
        if nick:
            for i in range(1, len(values)):
                dc_nick = values[i][2] if len(values[i]) > 2 else ""
                if str(dc_nick).strip() == nick:
                    row_idx = i + 1
                    break

    if row_idx < 0:
        print(f"訂單 {order_info} 對不到會員，略過寫入")
        return False

    # 寫入 E、F；ID 前面加 ' 讓試算表當文字存（避免 19 位數字被轉成科學記號失真）
    ws.update_cell(row_idx, 5, username)
    ws.update_cell(row_idx, 6, "'" + str(user_id))
    print(f"已把 DC 帳號 {username}({user_id}) 寫到會員第 {row_idx} 列")
    return True


# ========== Discord ==========
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
client = discord.Client(intents=intents)


def is_watched_channel(channel) -> bool:
    if ALLOWED_CHANNEL_IDS and channel.id in ALLOWED_CHANNEL_IDS:
        return True
    if ALLOWED_CATEGORIES and getattr(channel, "category", None) and channel.category.id in ALLOWED_CATEGORIES:
        return True
    if not ALLOWED_CHANNEL_IDS and not ALLOWED_CATEGORIES:
        return True  # 兩個都沒設 = 全部頻道
    return False


async def run_blocking(fn, *args):
    """把會阻塞的 gspread 呼叫丟到執行緒，避免卡住 Discord。"""
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


async def send_and_delete(channel, text: str, delay: int = DELETE_DELAY):
    msg = await channel.send(text)

    async def _del():
        try:
            await asyncio.sleep(delay)
            await msg.delete()
        except Exception:
            pass

    asyncio.create_task(_del())
    return msg


async def delete_later(message, delay: int = DELETE_DELAY):
    async def _del():
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except Exception:
            pass
    asyncio.create_task(_del())


@client.event
async def on_ready():
    print(f"已登入：{client.user}")
    try:
        await run_blocking(build_order_index)
    except Exception as e:
        print(f"初次建立訂單索引失敗：{e}")
    # 印出監聽範圍
    if ALLOWED_CHANNEL_IDS or ALLOWED_CATEGORIES:
        print(f"監聽：分類 {ALLOWED_CATEGORIES or '—'}；頻道 {ALLOWED_CHANNEL_IDS or '—'}")
    else:
        print("監聽：所有頻道（未設定 ALLOWED_CATEGORIES / ALLOWED_CHANNEL_IDS）")


@client.event
async def on_message(message):
    if message.author.bot or not is_watched_channel(message.channel):
        return

    content = message.content or ""
    candidates = ORDER_NO_REGEX.findall(content)
    if not candidates:
        return  # 沒有數字，當一般聊天，不回應

    # 逐一驗證，找到第一個「真的存在」的訂單編號
    matched = None
    for cand in candidates:
        info = await run_blocking(find_order, cand)
        if info:
            matched = (cand, info)
            break

    if not matched:
        await send_and_delete(message.channel,
                              f"{message.author.mention} 找不到這個訂單編號，請確認後再貼一次。")
        return

    cand, info = matched

    # 記錄 DC 帳號到會員資料（失敗不影響喊單成功回覆）
    try:
        await run_blocking(link_dc_to_member, info, message.author.id, message.author.name)
    except Exception as e:
        print(f"寫入會員資料失敗：{e}")

    await send_and_delete(message.channel,
                          f"{message.author.mention} 喊單成功！（訂單編號 {cand}）")

    if DELETE_USER_MESSAGE:
        await delete_later(message)


if __name__ == "__main__":
    if not TOKEN or not JSON_FILE or not SHEET_ID:
        raise SystemExit("請先在 .env 設定 DISCORD_TOKEN、GOOGLE_JSON_FILE、SHEET_ID")
    client.run(TOKEN)
