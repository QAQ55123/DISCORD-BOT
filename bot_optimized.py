import discord
import pandas as pd
import re, json, asyncio, datetime, unicodedata
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import os
try:
    import opencc
    _cc = opencc.OpenCC('s2t')  # 只轉簡體字，不做台灣用詞替換（避免「文件」→「檔案」）
    def to_traditional(text: str) -> str:
        return _cc.convert(text)
except ImportError:
    def to_traditional(text: str) -> str:
        return text

load_dotenv()

# =========================
# 設定
# =========================
TOKEN        = os.getenv("DISCORD_TOKEN")
JSON_FILE    = os.getenv("GOOGLE_JSON_FILE")
SHEET_ID     = os.getenv("SHEET_ID")
DATA_FILE    = "data.json"

# 從 .env 讀取分類 ID，多個用逗號分隔，例如：ALLOWED_CATEGORIES=123456789,987654321
ALLOWED_CATEGORIES = [int(i.strip()) for i in os.getenv("ALLOWED_CATEGORIES", "").split(",") if i.strip()]

# bot 啟動後自動填入，不需要手動維護
ALLOWED_CHANNELS: set = set()

# 允許的欄位白名單（寫在這裡統一管理）
ALLOWED_FIELDS = {"訂單編號", "訊息ID", "使用者", "商品", "款式", "商品數量", "單價", "狀態", "名稱", "交易方式"}

# =========================
# Discord client
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
client = discord.Client(intents=intents, max_messages=5000)

# =========================
# 全域狀態
# =========================
price_maps        = {}  # {cid: {(name, style): price}}
channel_orders    = {}  # {cid: {mid: [row, ...]}}
price_update_time = {}  # {cid: timestamp}
order_counter     = {}  # {cid: int}
update_pending    = {}  # {cid: bool}

# =========================
# 工具函式
# =========================
def clean_key(k: str) -> str:
    """移除 key 中的非文字符號（包含空格）"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", k)


def clean_row(row: dict) -> dict:
    """統一清洗一筆 row 的 key，只保留白名單欄位"""
    cleaned = {}
    for k, v in row.items():
        nk = clean_key(k)
        if nk not in ALLOWED_FIELDS:
            continue
        if isinstance(v, str):
            v = v.strip()
            if nk == "名稱":
                v = v.replace(" ", "")
        cleaned[nk] = v
    return cleaned


def make_row(order_id, message_id, author, name, style, qty, price, status, extra) -> dict:
    """
    統一建立一筆訂單 row，取代原本各處重複的 dict 建立邏輯。
    extra 是 {名稱, 交易方式} 等附加欄位。
    """
    row = {
        "訂單編號": order_id,
        "訊息ID":   message_id,
        "使用者":   str(author),
        "商品":     name,
        "款式":     style if style else "無",
        "商品數量": qty,
        "單價":     price,
        "狀態":     status,
        **extra,
    }
    return clean_row(row)

# =========================
# 儲存 / 載入
# =========================
def save_data():
    safe_prices = {}
    for cid, pmap in price_maps.items():
        safe_prices[str(cid)] = {
            f"{name}|||{style}": price
            for (name, style), price in pmap.items()
        }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "orders":  {str(cid): {str(mid): rows for mid, rows in orders.items()}
                        for cid, orders in channel_orders.items()},
            "prices":  safe_prices,
            "time":    {str(k): v for k, v in price_update_time.items()},
            "counter": {str(k): v for k, v in order_counter.items()},
        }, f, ensure_ascii=False)


def load_data():
    global channel_orders, price_maps, price_update_time, order_counter
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)

        # 訂單
        channel_orders = {}
        for cid, messages in d.get("orders", {}).items():
            cid = int(cid)
            channel_orders[cid] = {
                int(mid): [clean_row(r) for r in rows]
                for mid, rows in messages.items()
            }

        # 價格表
        price_maps = {}
        for cid, pmap in d.get("prices", {}).items():
            price_maps[int(cid)] = {}
            for k, price in pmap.items():
                name, style = k.split("|||")
                style = None if style == "None" else style
                price_maps[int(cid)][(name, style)] = price

        price_update_time = {int(k): v for k, v in d.get("time", {}).items()}
        order_counter     = {int(k): v for k, v in d.get("counter", {}).items()}

    except Exception as e:
        print("❌ load_data 錯誤:", e)

# =========================
# Google Sheets
# =========================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]


def get_spreadsheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, scope)
    gs = gspread.authorize(creds)
    return gs.open_by_key(SHEET_ID)


def get_sheet(name: str):
    spreadsheet = get_spreadsheet()
    try:
        sheet = spreadsheet.worksheet(name)
        sheet.clear()
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=name, rows="1000", cols="20")
    return sheet


def rebuild_sheet(cid: int, cname: str):
    sheet = get_sheet(cname)
    data = []

    # 價格表時間
    if cid in price_update_time:
        t = datetime.datetime.fromtimestamp(price_update_time[cid])
        data.append([f"價格表時間：{t}"])
    data.append([])

    # 商品統計（排除錯誤與刪除）
    count = {}
    for rows in channel_orders.get(cid, {}).values():
        for r in rows:
            status = str(r.get("狀態", ""))
            if "錯誤" in status or status == "資料遭刪除":
                continue
            name = r.get("商品")
            style = str(r.get("款式", "")).strip()
            key = (name, style)
            count[key] = count.get(key, 0) + r.get("商品數量", 0)

    data.append(["商品", "款式", "價格", "已訂購"])
    for (n, s), p in price_maps.get(cid, {}).items():
        if s == "多人":
            sub_styles = sorted({style for (name, style) in count if name == n})
            if sub_styles:
                for style in sub_styles:
                    qty = count.get((n, style), 0)
                    data.append([n, style, p, qty])
            else:
                data.append([n, "多人", p, 0])
        else:
            lookup_key = (n, str(s if s else "無").strip())
            qty = count.get(lookup_key, 0)
            data.append([n, s if s else "無", p, qty])
    data.append([])

    # 訂單明細
    rows = []
    for mid, rlist in channel_orders.get(cid, {}).items():
        for r in rlist:
            rows.append(r)

    rows.sort(key=lambda x: int(x.get("訊息ID", 0)))

    # 顯示編號（只給正常訂單）
    display_id, msg_map = 1, {}
    for r in rows:
        status = str(r.get("狀態", ""))
        mid = r.get("訊息ID")
        if "錯誤" in status or status == "資料遭刪除":
            r["編號"] = ""
            continue
        if mid not in msg_map:
            msg_map[mid] = display_id
            display_id += 1
        r["編號"] = msg_map[mid]

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.fillna("")
        df = df.drop(columns=["訂單編號", "訊息ID"], errors="ignore")
        cols = ["編號"] + [c for c in df.columns if c != "編號"]
        df = df[[c for c in cols if c in df.columns]]
        data.append(df.columns.tolist())
        data.extend(df.values.tolist())

    sheet.update(values=data)
    save_data()


async def delayed_update(cid: int, cname: str):
    if update_pending.get(cid):
        return
    update_pending[cid] = True
    await asyncio.sleep(1.5)
    await asyncio.get_event_loop().run_in_executor(None, rebuild_sheet, cid, cname)
    update_pending[cid] = False

# =========================
# 解析：價格表
# =========================
def parse_price_list(text: str) -> dict:
    result = {}
    for line in text.split("\n"):
        line = re.sub(r"^\d+\.", "", line.strip())
        line = line.replace("（", "(").replace("）", ")")
        # 移除金額後面的括號說明（如 295/1(隨機說明)）
        line = re.sub(r"(\d+)[^:：]*\([^)]+\)\s*$", r"\1", line)
        # 移除 +說明文字（如 +紙袋）
        line = re.sub(r"[+＋][^(:：)]+(?=\s*[:：(])", "", line)
        # 商品名稱（括號前）、款式（括號內）、價格（支援「元」字）
        m = re.search(r"^(.+?)\s*(?:\(([^)]+)\))?\s*[:：]\s*(\d+)", line)
        if not m:
            continue
        name = m.group(1).strip()
        styles, price = m.group(2), int(m.group(3))
        if styles and styles.strip() == "多人":
            result[(name, "多人")] = price
        elif styles:
            for s in re.split(r"[/／]", styles):
                result[(name.strip(), s.strip())] = price
        else:
            result[(name, None)] = price
    return result

# =========================
# 解析：商品名稱 + 款式
# =========================
def resolve_product(raw: str, price_map: dict) -> tuple:
    raw = unicodedata.normalize("NFKC", raw).strip()
    raw = to_traditional(raw)
    raw = raw.replace("（", "(").replace("）", ")").replace(" ", "")

    candidates = [(n, s) for (n, s) in price_map if raw.startswith(str(n).strip().replace(" ", ""))]
    if not candidates:
        return None, None

    name = max(candidates, key=lambda x: len(x[0]))[0]
    remain = raw[len(name.replace(" ", "")):]
    remain_clean = re.sub(r"\d+", "", remain)
    bracket = re.search(r"[\(（]([^)）]+)[\)）]", remain_clean)
    if bracket:
        remain_clean = bracket.group(1).strip()
    else:
        remain_clean = re.sub(r"^[-－]", "", remain_clean)
        remain_clean = remain_clean.replace("(", "").replace(")", "").strip()

    # 多人商品：直接回傳使用者填的款式內容
    if (name, "多人") in price_map:
        return name, remain_clean or None

    styles = [s for (n, s) in price_map if n == name and s]
    if not styles:
        return name, None

    # 長的款式優先比對，避免短字串提早命中
    for s in sorted(styles, key=len, reverse=True):
        if remain_clean == s or s in remain_clean:
            return name, s

    return name, None

# =========================
# 解析：訂單內文
# =========================
def parse_order(text: str) -> tuple:
    """
    回傳 (products, total, extra)
    products = [(raw_name, qty), ...]
    extra    = {"名稱": ..., "交易方式": ...}
    """
    items = []
    extra = {}

    # 商品欄（支援多行）
    m = re.search(r"(?:商品|商品名稱)[:：]\s*(.+?)(?=\n\S+[:：]|\Z)", text, re.DOTALL)
    if m:
        block = m.group(1).replace("\n", "+")
        for it in re.split(r"[+,，、]", block):
            it = it.strip()
            if not it:
                continue

            # 移除括號前的空格（如「立牌文件組 (景元)6」→「立牌文件組(景元)6」）
            it = re.sub(r"\s+(?=[\(（])", "", it)

            base_match = re.match(r"^[A-Za-z]+", it)
            base_name  = base_match.group(0) if base_match else ""

            # 連字號格式：中文商品名-款式（如「徽章組-景元1」）
            if re.match(r"[\u4e00-\u9fff].*[-－]", it):
                it_clean = re.sub(r"[-－]", " ", it)
                q = re.search(r"(\d+)$", it_clean)
                items.append((it_clean[:q.start()].strip(), int(q.group(1))) if q else (it_clean.strip(), 0))
                continue

            # 支援「款式+數量」多組（如 A1B2、左3右4）
            pairs = re.findall(r"([A-Za-z\u4e00-\u9fff]+?)(\d+)", it)
            if pairs:
                for style, qty in pairs:
                    full = style if style.startswith(base_name) else base_name + style
                    items.append((full.strip(), int(qty)))
                continue

            # 單一商品
            it = re.sub(r"[*xX×]", " ", it)
            q = re.search(r"(\d+)$", it)
            items.append((it[:q.start()].strip(), int(q.group(1))) if q else (it.strip(), 0))

    # 總金額
    tm = re.search(r"(?:價格|商品總額)[:：]\s*(\d+)", text)
    total = int(tm.group(1)) if tm else 0

    # 附加欄位（名稱、交易方式）
    for line in text.split("\n"):
        if not re.search(r"[:：]", line):
            continue
        k, _, v = line.partition("：") if "：" in line else line.partition(":")
        k = clean_key(k)
        v = v.strip()
        if k == "名稱":
            extra["名稱"] = v.replace(" ", "")
        elif k == "交易方式":
            extra["交易方式"] = v

    return items, total, extra

# =========================
# 核心：把一則訊息內容轉成訂單 rows
# =========================
def process_order_content(message_id, author, content: str, cid: int,
                           order_id: int, existing_rows: list = None, is_edit: bool = False) -> list:
    """
    解析訊息內容，回傳 rows list。
    existing_rows：若有舊資料（編輯/歷史），保留 "✏ 已編輯"、"資料遭刪除" 狀態。
    """
    # 過濾範例訊息
    if re.search(r"範例|格式|請依照|填寫", content):
        return []

    products, total, extra = parse_order(content)
    if not products:
        return []

    # 保存舊狀態 (商品, 款式) -> 狀態
    old_status_map = {}
    if existing_rows:
        for r in existing_rows:
            key = (r.get("商品"), r.get("款式"))
            old_status_map[key] = r.get("狀態", "")

    calc = 0
    rows = []

    for raw, qty in products:
        raw = raw.replace(" ", "")
        name, style = resolve_product(raw, price_maps[cid])
        price = price_maps[cid].get((name, style)) if name else None

        # 判斷錯誤類型
        is_multi = (name, "多人") in price_maps[cid] if name else False
        price = price_maps[cid].get((name, "多人")) if is_multi else price

        if not name:
            status = "數量/金額錯誤"
            name, style, price = "輸入錯誤", "款式錯誤", 0
        elif is_multi and not style:
            # 多人商品但沒填款式
            status = "未選擇款式"
            price = price or 0
        elif not is_multi and style is None and any(s for (n, s) in price_maps[cid] if n == name and s):
            status = "數量/金額錯誤"
            style, price = "款式錯誤", 0
        elif qty <= 0:
            status = "數量/金額錯誤"
            price = price or 0
        elif not price:
            status = "數量/金額錯誤"
            style, price = "款式錯誤", 0
        else:
            status = ""
            calc += price * qty

        row = make_row(order_id, message_id, author, name, style, qty, price, status, extra)

        # 保留特殊舊狀態
        key = (row.get("商品"), row.get("款式"))
        if key in old_status_map and old_status_map[key] in {"✏ 已編輯", "資料遭刪除"}:
            row["狀態"] = old_status_map[key]

        rows.append(row)

    # 金額總驗
    if total and total != calc:
        for r in rows:
            existing = r.get("狀態", "")
            r["狀態"] = (existing + " ； 總金額錯誤/寫錯").lstrip(" ； ") if existing else "總金額錯誤/寫錯"

    # 編輯模式：正常的 row 標記已編輯
    if is_edit:
        for r in rows:
            if not r.get("狀態"):
                r["狀態"] = "✏ 已編輯"

    if not rows:
        rows = [make_row(order_id, message_id, author,
                         "輸入錯誤", "款式錯誤", 0, 0, "格式內容有問題 重新解析", extra)]

    return rows

# =========================
# 歷史載入
# =========================
async def load_history():
    print("📥 載入歷史資料...")

    for cid in ALLOWED_CHANNELS:
        channel = await client.fetch_channel(cid)

        price_maps.setdefault(cid, {})
        channel_orders.setdefault(cid, {})
        order_counter.setdefault(cid, 1)

        async for message in channel.history(limit=200, oldest_first=True):
            content = message.content

            if re.match(r"^價格表", content):
                t = message.created_at.timestamp()
                if t >= price_update_time.get(cid, 0):
                    price_update_time[cid] = t
                    price_maps[cid] = parse_price_list(content)
                continue

            if not price_maps[cid]:
                continue

            # 強制重新解析，只保留特殊狀態（資料遭刪除、✏ 已編輯）
            old_rows = channel_orders[cid].get(message.id)
            special_status = {}
            if old_rows:
                for r in old_rows:
                    s = r.get("狀態", "")
                    if s in {"資料遭刪除", "✏ 已編輯"}:
                        special_status[(r.get("商品"), r.get("款式"))] = s

            rows = process_order_content(
                message.id, message.author, content, cid,
                order_counter[cid]
            )

            if rows and special_status:
                for r in rows:
                    key = (r.get("商品"), r.get("款式"))
                    if key in special_status:
                        r["狀態"] = special_status[key]

            if rows:
                channel_orders[cid][message.id] = rows
                order_counter[cid] += 1

        cat_name = channel.category.name if channel.category else "無分類"
        await asyncio.get_event_loop().run_in_executor(None, rebuild_sheet, cid, f"{cat_name}-{channel.name}")

    print("✅ 歷史讀取完成")

# =========================
# Discord 事件
# =========================
@client.event
async def on_ready():
    print("✅ Bot 已上線")

    # 自動掃描所有 server，把符合分類的頻道加進 ALLOWED_CHANNELS
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.category and channel.category.id in ALLOWED_CATEGORIES:
                ALLOWED_CHANNELS.add(channel.id)
                print(f"  ✅ 監聽頻道：{channel.name}（{channel.category.name}）")

    print(f"共監聽 {len(ALLOWED_CHANNELS)} 個頻道")

    load_data()
    save_data()
    await load_history()
    client.loop.create_task(auto_rebuild_loop())


async def auto_rebuild_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await asyncio.sleep(300)
            ALLOWED_CHANNELS.clear()
            for guild in client.guilds:
                for channel in guild.text_channels:
                    if channel.category and channel.category.id in ALLOWED_CATEGORIES:
                        ALLOWED_CHANNELS.add(channel.id)
            for cid in list(ALLOWED_CHANNELS):
                channel = client.get_channel(cid)
                if channel:
                    cat_name = channel.category.name if channel.category else "無分類"
                    cname = f"{cat_name}-{channel.name}"
                    try:
                        await asyncio.get_event_loop().run_in_executor(None, rebuild_sheet, cid, cname)
                        print(f"🔄 定時重建：{cname}")
                    except Exception as e:
                        print(f"❌ auto_rebuild 錯誤 ({cname}): {e}")
        except Exception as e:
            print(f"❌ auto_rebuild_loop 意外錯誤: {e}")
            await asyncio.sleep(60)


@client.event
async def on_message(message):
    if message.author.bot or message.channel.id not in ALLOWED_CHANNELS:
        return

    cid = message.channel.id
    cat_name = message.channel.category.name if message.channel.category else "無分類"
    cname = f"{cat_name}-{message.channel.name}"
    content = message.content

    price_maps.setdefault(cid, {})
    channel_orders.setdefault(cid, {})
    order_counter.setdefault(cid, 1)

    # 價格表
    if re.match(r"^價格表", content):
        now = message.created_at.timestamp()
        if now >= price_update_time.get(cid, 0):
            price_update_time[cid] = now
            price_maps[cid] = parse_price_list(content)
            save_data()
            await delayed_update(cid, cname)
        return

    if not price_maps[cid]:
        return

    rows = process_order_content(
        message.id, message.author, content, cid, order_counter[cid]
    )
    if not rows:
        return

    channel_orders[cid][message.id] = rows
    order_counter[cid] += 1
    save_data()
    await delayed_update(cid, cname)


@client.event
async def on_message_edit(before, after):
    if after.author.bot or after.channel.id not in ALLOWED_CHANNELS:
        return

    cid = after.channel.id
    cat_name = after.channel.category.name if after.channel.category else "無分類"
    cname = f"{cat_name}-{after.channel.name}"

    if cid not in channel_orders or after.id not in channel_orders[cid]:
        return

    # 保留原訂單編號
    order_id = channel_orders[cid][after.id][0].get("訂單編號", order_counter[cid])

    rows = process_order_content(
        after.id, after.author, after.content, cid,
        order_id, existing_rows=channel_orders[cid][after.id], is_edit=True
    )
    if not rows:
        return

    channel_orders[cid][after.id] = rows
    save_data()
    await delayed_update(cid, cname)


@client.event
async def on_message_delete(message):
    if message.author.bot or message.channel.id not in ALLOWED_CHANNELS:
        return

    cid = message.channel.id
    cat_name = message.channel.category.name if message.channel.category else "無分類"
    cname = f"{cat_name}-{message.channel.name}"

    if cid in channel_orders and message.id in channel_orders[cid]:
        for r in channel_orders[cid][message.id]:
            r["狀態"] = "資料遭刪除"
        save_data()
        await delayed_update(cid, cname)


client.run(TOKEN)
