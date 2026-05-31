import os
import sys
import time
import json
import sqlite3
import requests
import feedparser
import re
import html
from openai import OpenAI
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from dotenv import load_dotenv

# ==========================================
# 1. 全局与自动化配置
# ==========================================
load_dotenv()

# =================[自动化策略配置]=================
ENABLE_AI_REPORT = True         # 开关：True 开启分析与推送，False 仅抓取存库
FETCH_INTERVAL_MINUTES = 30     # 抓取频次：设置范围 10 ~ 1440 分钟
PUSH_TIME_UTC = "11:00"         # 日报推送时间：UTC 0时区时间 (11:00 对应北京时间 19:00)
# ==================================================

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
CONFIG_DB_ID = os.getenv("NOTION_DATABASE_ID")
REPORT_DB_ID = os.getenv("NOTION_REPORT_DB_ID")
RSSHUB_BASE = os.getenv("RSSHUB_BASE", "http://127.0.0.1:1200")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

ai_client = None
if DEEPSEEK_API_KEY:
    ai_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
DB_FILE = 'rss_news.db'

def load_feeds_config():
    try:
        with open('feeds.json', 'r', encoding='utf-8') as f:
            feeds_data = json.load(f)
        for feed in feeds_data:
            feed['url'] = f"{RSSHUB_BASE}{feed['route']}"
        return feeds_data
    except Exception as e:
        print(f"❌ 读取 feeds.json 失败: {e}")
        return []

RSS_FEEDS = load_feeds_config()

# ==========================================
# 2. 参数安全校验
# ==========================================
if not (10 <= FETCH_INTERVAL_MINUTES <= 1440):
    print(f"❌ 运行中止: 请求频次 (FETCH_INTERVAL_MINUTES) 必须在 10 到 1440 之间，当前为 {FETCH_INTERVAL_MINUTES}")
    sys.exit(1)

try:
    push_h, push_m = map(int, PUSH_TIME_UTC.split(':'))
    if not (0 <= push_h <= 23 and 0 <= push_m <= 59):
        raise ValueError
except ValueError:
    print(f"❌ 运行中止: 推送时间 (PUSH_TIME_UTC) 格式错误，请使用如 '04:00' 的格式，当前为 {PUSH_TIME_UTC}")
    sys.exit(1)

# ==========================================
# 3. 数据库引擎
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector TEXT,
            source TEXT,
            pub_date DATETIME,
            title TEXT,
            summary TEXT,
            content TEXT,
            url TEXT UNIQUE,
            matched_keyword TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        cursor.execute("ALTER TABLE articles ADD COLUMN content TEXT")
    except sqlite3.OperationalError:
        pass 

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('last_report_time', '1970-01-01 00:00:00')")
    conn.commit()
    conn.close()

def get_state(key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_state WHERE key=?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_state(key, value):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE system_state SET value=? WHERE key=?", (value, key))
    conn.commit()
    conn.close()

def save_to_db(articles):
    if not articles: return 0
    articles.sort(key=lambda x: x['pub_timestamp'])
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    inserted_count = 0
    for art in articles:
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO articles 
                (sector, source, pub_date, title, summary, content, url, matched_keyword)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (art['sector'], art['source'], art['pub_date'], art['title'], 
                  art['summary'], art['content'], art['url'], art['matched_keyword']))
            if cursor.rowcount > 0:
                inserted_count += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return inserted_count

# ==========================================
# 4. 抓取与配置引擎
# ==========================================
def fetch_notion_config():
    url = f"https://api.notion.com/v1/databases/{CONFIG_DB_ID}/query"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers)
    if response.status_code != 200: return []
    results = response.json().get("results", [])
    sectors_config = []
    
    for page in results:
        props = page.get("properties", {})
        sector_name = props.get("Sector", {}).get("title", [{}])[0].get("plain_text", "") if props.get("Sector", {}).get("title") else ""
        if not sector_name: continue
        
        def extract_words(col_name):
            try: return [k.strip() for k in props[col_name]["rich_text"][0]["plain_text"].split("、") if k.strip()]
            except Exception: return []

        cn_kws = extract_words("CN Keywords")
        en_kws = extract_words("EN Keywords")
        
        all_keywords = list(set(cn_kws + en_kws))
        if all_keywords: 
            sectors_config.append({"sector": sector_name, "keywords": all_keywords})
            
    return sectors_config

def clean_html(raw_html):
    clean_text = re.sub(r'<[^>]+>', '', raw_html)
    clean_text = html.unescape(clean_text)
    clean_text = re.sub(r'\s+', ' ', clean_text)
    return clean_text.strip()

def run_scraper(sectors_config):
    matched_articles = []
    threshold_bj = datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=24)
    
    for feed_info in RSS_FEEDS:
        print(f"抓取来源: {feed_info['name']}...")
        try:
            response = requests.get(feed_info['url'], headers=HEADERS, timeout=15)
            if response.status_code != 200: continue
            feed = feedparser.parse(response.content)
            
            for entry in feed.entries:
                pub_date = date_parser.parse(entry.published)
                if pub_date.tzinfo is None: pub_date = pub_date.replace(tzinfo=timezone.utc)
                pub_date_bj = pub_date.astimezone(timezone(timedelta(hours=8)))
                
                if pub_date_bj < threshold_bj:
                    continue
                
                title = entry.get('title', '')
                raw_summary = entry.get('summary', '')
                clean_content_text = clean_html(raw_summary)
                match_text = f"{title} {clean_content_text}".lower()
                
                matched = False
                for sector in sectors_config:
                    if matched: break
                    for keyword in sector['keywords']:
                        if keyword.lower() in match_text:
                            matched_articles.append({
                                "sector": sector['sector'],
                                "source": feed_info['name'],
                                "pub_date": pub_date_bj.strftime("%Y-%m-%d %H:%M:%S"),
                                "pub_timestamp": pub_date_bj.timestamp(),
                                "title": title.strip(),
                                "summary": raw_summary.strip(),
                                "content": clean_content_text,
                                "url": entry.link,
                                "matched_keyword": keyword
                            })
                            matched = True
                            break
        except Exception:
            continue
    return matched_articles

# ==========================================
# 5. DeepSeek AI 研报生成引擎 (极简买方内参版)
# ==========================================
def get_report_data(start_bj_str, end_bj_str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT sector, content, url 
        FROM articles 
        WHERE pub_date > ? AND pub_date <= ?
        ORDER BY sector, pub_date ASC
    ''', (start_bj_str, end_bj_str))
    rows = cursor.fetchall()
    conn.close()
    
    data_by_sector = {}
    for row in rows:
        sector = row[0]
        if sector not in data_by_sector:
            data_by_sector[sector] = []
        data_by_sector[sector].append({
            "内容": row[1], 
            "URL": row[2]
        })
    return data_by_sector

def generate_ai_report(data_by_sector):
    if not data_by_sector: return None
    print("\n🧠 正在生成【极简决策内参】，请稍候...")
    
    # 彻底重构的超级 Prompt：强制提取信号，拒绝废话
    prompt = f"""你是一位服务于顶尖游资和量化基金的首席硬科技策略分析师。你的目标不是写长篇大论，而是为时间宝贵的操盘手提供【一分钟可读完】的极简投资决策内参。

    【🚨 核心排版与输出铁律】
    1. 极简主义：绝不废话，绝对禁止输出任何寒暄语或免责声明。第一行直接以标题开始正文！拒绝正确的废话和宏观套话。
    2. 按板块输出：遍历传入的每个板块，对每个板块采用以下“快准狠”的结构进行提炼（使用Markdown列表）：
       - 🎯 **【核心阵眼】**：用短短一句话，总结今天该板块最重大的事件、突破或异动。
       - 💡 **【投资信号】**：明确指出该事件代表什么信号（如：利好催化、情绪退潮、商业化加速、底层技术突破等），10个字以内。
       - 🧠 **【底层逻辑】**：用1-2个要点，最简练地说明它为何重要，以及在产业链中谁最受益。强制在关键句尾部带上引证：[来源](URL)。
    3. 全局推演：在全文最后，单独开一个二级标题「## 🧭 操盘手全局定调」，用一小段话横向对比今天各板块的热度，选出1个最值得重点盯盘的核心赛道并说明理由。

    【今日干净文本数据】
    {json.dumps(data_by_sector, ensure_ascii=False, indent=2)}
    """
    
    if not ai_client:
        print("❌ 未配置 DEEPSEEK_API_KEY，跳过分析。")
        return None
        
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": "你是一个严格执行指令的买方投研机器人，只输出直击要害的极简内参。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ AI 分析过程发生异常: {e}")
        return None

# ==========================================
# 6. Notion 渲染与推送
# ==========================================
def parse_inline_text(text):
    rich_text = []
    parts = re.split(r'(\[[^\]]+\]\([^)]+\))', text)
    for part in parts:
        if not part: continue
        if part.startswith('[') and '](' in part:
            match = re.match(r'\[([^\]]+)\]\(([^)]+)\)', part)
            if match:
                link_text, url = match.groups()
                rich_text.append({
                    "type": "text", 
                    "text": {"content": link_text, "link": {"url": url}},
                    "annotations": {"bold": True, "color": "blue"}
                })
        else:
            bold_parts = re.split(r'(\*\*.*?\*\*)', part)
            for bp in bold_parts:
                if not bp: continue
                if bp.startswith('**') and bp.endswith('**') and len(bp) > 4:
                    rich_text.append({"type": "text", "text": {"content": bp[2:-2]}, "annotations": {"bold": True}})
                else:
                    rich_text.append({"type": "text", "text": {"content": bp}})
    return rich_text

def parse_markdown_to_notion_blocks(md_text):
    blocks = []
    for para in md_text.split('\n'):
        para = para.strip()
        if not para: continue
        if para.startswith('### '):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": parse_inline_text(para[4:])}})
        elif para.startswith('## '):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": parse_inline_text(para[3:])}})
        elif para.startswith('# '):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": parse_inline_text(para[2:])}})
        elif para.startswith('- ') or para.startswith('* '):
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": parse_inline_text(para[2:])}})
        elif re.match(r'^\d+\.\s', para):
            match = re.match(r'^(\d+\.\s)', para)
            blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": parse_inline_text(para[len(match.group(1)):])}})
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": parse_inline_text(para)}})
    return blocks

def push_report_to_notion(report_text):
    today_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d")
    page_title = f"{today_str} 核心行业追踪研报"
    
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    data = {
        "parent": {"database_id": REPORT_DB_ID},
        "properties": {"Name": {"title": [{"text": {"content": page_title}}]}},
        "children": parse_markdown_to_notion_blocks(report_text)
    }
    
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200: print(f"✅ 研报已成功推送到 Notion: {response.json().get('url')}")
    else: print(f"❌ 推送失败: {response.text}")

# ==========================================
# 7. 主控引擎 (永动机模式)
# ==========================================
if __name__ == "__main__":
    init_db()
    print("="*60)
    print(f" ⚙️ 自动化系统已启动 | 抓取频次: 每 {FETCH_INTERVAL_MINUTES} 分钟")
    print(f" 🕒 AI 日报推送功能: {'开启' if ENABLE_AI_REPORT else '关闭'} | 目标触达: {PUSH_TIME_UTC} (UTC)")
    print("="*60)
    
    while True:
        now_bj_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n[{now_bj_str}] ⏳ 开始执行高频抓取任务...")
        
        sectors = fetch_notion_config()
        if sectors:
            results = run_scraper(sectors)
            new_inserts = save_to_db(results)
            print(f" 💾 数据落库完成：共命中 {len(results)} 条近期有效资讯，新增 {new_inserts} 条不重复记录。")
            
        if ENABLE_AI_REPORT:
            now_utc = datetime.now(timezone.utc)
            today_target_utc = now_utc.replace(hour=push_h, minute=push_m, second=0, microsecond=0)
            
            if now_utc < today_target_utc:
                latest_passed_target = today_target_utc - timedelta(days=1)
                next_target_utc = today_target_utc
            else:
                latest_passed_target = today_target_utc
                next_target_utc = today_target_utc + timedelta(days=1)
                
            last_pushed_str = get_state('last_report_time')
            
            if last_pushed_str == '1970-01-01 00:00:00':
                set_state('last_report_time', latest_passed_target.strftime("%Y-%m-%d %H:%M:%S"))
                last_pushed_utc = latest_passed_target
                print(f" 🔄 系统首次初始化：已将发报基准线防呆对齐至 {latest_passed_target.strftime('%Y-%m-%d %H:%M:%S')} (UTC)")
            else:
                last_pushed_utc = datetime.strptime(last_pushed_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                
            if last_pushed_utc < latest_passed_target:
                print(f"🎯 触发机制：当前时间已跨越设定的 {PUSH_TIME_UTC} (UTC) 触发线，准备生成研报。")
                
                start_utc = latest_passed_target - timedelta(days=1)
                end_utc = latest_passed_target
                
                start_bj = start_utc.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                end_bj = end_utc.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                
                print(f"📊 提取数据周期：{start_bj} 至 {end_bj}")
                data_to_analyze = get_report_data(start_bj, end_bj)
                
                if data_to_analyze:
                    report_md = generate_ai_report(data_to_analyze)
                    if report_md:
                        print("\n📝 研报生成完毕！执行推送...")
                        push_report_to_notion(report_md)
                        set_state('last_report_time', latest_passed_target.strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    print(f"📭 当前周期内未发现新增资讯，跳过推送并更新时间锁。")
                    set_state('last_report_time', latest_passed_target.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                print(f" 🛡️ AI 分析处于休眠防重复状态。下次生成研报时间: {next_target_utc.strftime('%Y-%m-%d %H:%M:%S')} (UTC)")
        else:
            print(" ⏸️ AI 日报配置已关闭，本次仅执行数据抓取。")
            
        print(f"💤 本轮循环结束。系统静默休眠 {FETCH_INTERVAL_MINUTES} 分钟...")
        time.sleep(FETCH_INTERVAL_MINUTES * 60)