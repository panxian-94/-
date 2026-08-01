#!/usr/bin/env python3
"""
高可靠新闻精选推送脚本
特性：多重时效校验、来源可信分级、AI事实核查(可选)、去重增强
"""

import os, sys, time, hashlib, logging, requests, feedparser, re, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import List, Dict, Optional

# ---------- 配置 ----------
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
TZ_BEIJING = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
MAX_WORKERS = 8
REQUEST_TIMEOUT = 10

# 可选 AI 核验（推荐 DeepSeek，免费额度）
ENABLE_AI_FACTCHECK = os.environ.get("ENABLE_AI_FACTCHECK", "false").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# 缓存文件路径（在 GitHub Actions 中每次运行都是全新环境，故仅用于单次运行内去重）
SEEN_HASHES = set()

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------- 高可信源列表（可信度权重 1.0）----------
HIGH_TRUST_SOURCES = [
    "xinhuanet.com", "people.com.cn", "chinanews.com", "cctv.com",
    "bbc.co.uk", "reuters.com", "aljazeera.com", "nytimes.com",
    "chinadaily.com.cn", "thepaper.cn", "hubei.gov.cn", "wuhan.gov.cn",
    "technologyreview.com", "wired.com"
]

# ---------- 扩展 RSS 源（已标注分类）----------
RSS_FEEDS = [
    # 国内高可信
    {"url": "http://www.xinhuanet.com/politics/xhsll.xml", "category": "国内", "trust": 1.0},
    {"url": "http://www.people.com.cn/rss/politics.xml", "category": "国内", "trust": 1.0},
    {"url": "https://www.chinanews.com/rss/rss_1.html", "category": "国内", "trust": 1.0},
    {"url": "https://www.thepaper.cn/rss_news_1.xml", "category": "国内", "trust": 1.0},
    # 国内一般（百度RSS）
    {"url": "https://news.baidu.com/ns?word=%E7%BB%8F%E6%B5%8E+%E6%94%BF%E7%AD%96+%E6%B0%91%E7%94%9F&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内", "trust": 0.5},
    # 国际高可信
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际", "trust": 1.0},
    {"url": "https://www.chinadaily.com.cn/rss/world_rss.xml", "category": "国际", "trust": 1.0},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际", "trust": 1.0},
    {"url": "https://www.reuters.com/tools/rss", "category": "国际", "trust": 1.0},
    # 国际一般
    {"url": "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "category": "国际", "trust": 0.7},
    # 武汉本地高可信
    {"url": "http://www.cnhubei.com/rss/whxw.xml", "category": "武汉", "trust": 1.0},
    {"url": "http://www.changjiangtimes.com/rss/wh.xml", "category": "武汉", "trust": 1.0},
    {"url": "http://hb.people.com.cn/rss/hubei.xml", "category": "武汉", "trust": 1.0},
    # 武汉一般
    {"url": "https://news.baidu.com/ns?word=%E6%AD%A6%E6%B1%89+%E6%B9%96%E5%8C%97&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "武汉", "trust": 0.5},
    # AI 高可信
    {"url": "https://www.technologyreview.com/feed/", "category": "AI", "trust": 1.0},
    {"url": "https://www.wired.com/feed/category/ai/latest/rss", "category": "AI", "trust": 1.0},
    # AI 一般
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "category": "AI", "trust": 0.8},
    {"url": "https://news.baidu.com/ns?word=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+AI+%E5%A4%A7%E6%A8%A1%E5%9E%8B&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "AI", "trust": 0.5},
]

# 板块关键词评分表
KEYWORD_SCORES = {
    "国内新闻": {"经济":5,"就业":5,"CPI":5,"房贷":5,"利率":5,"医保":4,"社保":4,"交通":4,"天气":4,"预警":4,"政策":5},
    "国际新闻": {"美联储":5,"汇率":5,"油价":5,"芯片":5,"俄乌":4,"中东":4,"欧洲":4,"全球":4},
    "湖北武汉本地动态": {"武汉":5,"湖北":5,"施工":4,"地铁":4,"暴雨":4,"高温":4,"消费券":4,"烟草":5,"罗森":4},
    "AI对普通人的影响": {"AI":5,"人工智能":5,"ChatGPT":5,"替代":4,"职业":4,"监管":4,"工具":4},
}

def extract_date_from_text(text: str) -> Optional[str]:
    """尝试从文本中提取日期 YYYY-MM-DD"""
    patterns = [
        r'(\d{4}-\d{2}-\d{2})',
        r'(\d{4}年\d{1,2}月\d{1,2}日)',
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            date_str = match.group(1).replace('年','-').replace('月','-').replace('日','')
            try:
                # 验证有效性
                datetime.strptime(date_str, "%Y-%m-%d")
                return date_str
            except:
                continue
    return None

def validate_today(news_item: Dict) -> bool:
    """多重方式校验新闻是否属于今天"""
    # 1. RSS 时间检查
    if news_item.get("time") and TODAY not in news_item["time"]:
        return False
    # 2. 尝试从链接中提取日期
    if news_item.get("url"):
        date_in_url = extract_date_from_text(news_item["url"])
        if date_in_url and date_in_url != TODAY:
            return False
    # 3. 尝试从标题中提取日期
    if news_item.get("title"):
        date_in_title = extract_date_from_text(news_item["title"])
        if date_in_title and date_in_title != TODAY:
            return False
    # 如果有摘要，也可检查（但可能过严，暂不强制）
    return True

def fetch_rss(feed_info: Dict) -> List[Dict]:
    """抓取单个RSS源，返回通过时效性校验的新闻"""
    news = []
    try:
        url = feed_info["url"]
        sep = "?" if "?" not in url else "&"
        full_url = f"{url}{sep}_t={int(time.time())}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            pub_time = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                pub_time = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
            if not pub_time:
                continue
            time_str = pub_time.strftime("%Y-%m-%d %H:%M")
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            summary = entry.get("summary", "")
            clean = re.sub(r"<[^>]+>", "", summary)[:200].strip()
            item = {
                "title": title,
                "url": link,
                "summary": clean,
                "source": url.split("//")[-1].split("/")[0],
                "time": time_str,
                "category": feed_info["category"],
                "trust": feed_info.get("trust", 0.5),
            }
            if validate_today(item):
                news.append(item)
    except Exception as e:
        logger.debug(f"源 {url[:60]} 失败: {e}")
    return news

def collect_all_news() -> Dict[str, List[Dict]]:
    """多线程抓取，按板块汇总"""
    pool = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_rss, f): f for f in RSS_FEEDS}
        for future in as_completed(futures):
            feed_info = futures[future]
            cat = feed_info["category"]
            try:
                result = future.result()
                if cat not in pool:
                    pool[cat] = []
                pool[cat].extend(result)
            except Exception as e:
                logger.warning(f"任务异常: {e}")
    return pool

def deduplicate_strong(news_list: List[Dict]) -> List[Dict]:
    """强去重：标题MD5 + 编辑距离相似度 (>0.9 视为重复)"""
    unique = []
    seen_hashes = set()
    for item in sorted(news_list, key=lambda x: x.get("time", ""), reverse=True):
        # MD5 快速去重
        h = hashlib.md5(item["title"].encode()).hexdigest()
        if h in seen_hashes:
            continue
        # 编辑距离去重
        title_a = item["title"]
        is_dup = False
        for existing in unique:
            ratio = SequenceMatcher(None, title_a, existing["title"]).ratio()
            if ratio > 0.9:
                is_dup = True
                break
        if is_dup:
            continue
        seen_hashes.add(h)
        unique.append(item)
    return unique

def ai_factcheck(news_items: List[Dict]) -> List[Dict]:
    """使用 AI 验证新闻真实性（需配置 LLM_API_KEY）"""
    if not ENABLE_AI_FACTCHECK or not LLM_API_KEY:
        return news_items

    try:
        titles = [item["title"] for item in news_items]
        prompt = (
            "你是一个新闻真实性校验助手。请分析以下新闻标题，判断哪些可能是谣言、标题党或夸大事实，"
            "并简要说明理由。返回格式：{'index':0, 'reliable':true/false, 'reason':'...'}"
            f"\n新闻列表：{json.dumps(titles, ensure_ascii=False)}"
        )
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=20)
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"]
        # 简单解析 AI 返回（实际需更稳健，此处示例）
        check_map = {}
        try:
            checks = json.loads(result)
            for c in checks:
                check_map[c["index"]] = c
        except:
            # 如果返回格式不对，只记录日志，不修改新闻
            logger.warning("AI返回解析失败，跳过事实核查")
            return news_items

        filtered = []
        for idx, item in enumerate(news_items):
            if idx in check_map and not check_map[idx].get("reliable", True):
                item["title"] += " [AI判定: 低可信]"
                item["trust"] *= 0.3
            filtered.append(item)
        return filtered
    except Exception as e:
        logger.error(f"AI核验失败: {e}")
        return news_items

def score_and_select(category: str, news_list: List[Dict], target: int = 50) -> List[Dict]:
    """评分、精选，确保备选量>=50条时择优5条"""
    # 先强去重
    news_list = deduplicate_strong(news_list)
    logger.info(f"{category} 备选新闻(去重后): {len(news_list)} 条")

    # AI 事实核查（如果开启）
    if ENABLE_AI_FACTCHECK:
        news_list = ai_factcheck(news_list)

    if len(news_list) <= target:
        selected = news_list[:5]
    else:
        scores = KEYWORD_SCORES.get(category, {})
        for item in news_list:
            score = 0
            title = item["title"]
            for kw, val in scores.items():
                if kw in title:
                    score += val
            # 可信度加权
            score *= item.get("trust", 0.5)
            item["_score"] = score
        sorted_news = sorted(news_list, key=lambda x: (x["_score"], x["time"]), reverse=True)
        selected = sorted_news[:5]
        # 标注低可信源
        for item in selected:
            if item.get("trust", 1.0) < 0.7:
                item["title"] += " [信源待核实]"
        logger.info(f"精选后推送 {len(selected)} 条")
    return selected

def format_message(sections: Dict[str, List[Dict]]) -> str:
    today_str = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
    msg = f"📰 每日新闻精选 ({today_str})\n"
    for sec_name, items in sections.items():
        if not items:
            msg += f"\n【{sec_name}】今日该类热点较少\n"
            continue
        msg += f"\n━━━━━━ 【{sec_name}】━━━━━━\n"
        for i, item in enumerate(items, 1):
            msg += f"{i}. {item['title']}\n"
            msg += f"   ⏱ {item['time']}  来源：{item['source']}\n"
            if item['summary']:
                msg += f"   📄 {item['summary']}\n"
            msg += "\n"
    return msg

def push_to_wechat(title: str, content: str):
    if not PUSHPLUS_TOKEN:
        raise RuntimeError("缺少 PUSHPLUS_TOKEN")
    r = requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "txt"
    }, timeout=15)
    r.raise_for_status()
    logger.info("推送成功")

def main():
    logger.info("开始高可靠性新闻抓取...")
    raw_pool = collect_all_news()

    section_map = {
        "国内": "国内新闻",
        "国际": "国际新闻",
        "武汉": "湖北武汉本地动态",
        "AI": "AI对普通人的影响",
    }
    final_sections = {
        "国内新闻": [],
        "国际新闻": [],
        "湖北武汉本地动态": [],
        "AI对普通人的影响": [],
    }

    for raw_cat, news_list in raw_pool.items():
        sec_name = section_map.get(raw_cat, "国内新闻")
        final_sections[sec_name].extend(news_list)

    for sec_name in final_sections:
        final_sections[sec_name] = score_and_select(sec_name, final_sections[sec_name])

    message = format_message(final_sections)
    push_to_wechat("每日新闻精选", message)

if __name__ == "__main__":
    main()
