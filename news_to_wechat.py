#!/usr/bin/env python3
"""
日报风格新闻推送（智谱AI免费翻译 + 分类修正版）
"""

import os, sys, time, hashlib, logging, requests, feedparser, re, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import List, Dict, Optional
from zhdate import ZhDate

# ---------- 配置 ----------
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
TZ_BEIJING = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
MAX_WORKERS = 8
REQUEST_TIMEOUT = 10

# AI 配置（智谱AI GLM-4-Flash）
ENABLE_AI = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------- RSS 源 ----------
RSS_FEEDS = [
    {"url": "http://www.xinhuanet.com/politics/xhsll.xml", "category": "国内", "trust": 1.0},
    {"url": "http://www.people.com.cn/rss/politics.xml", "category": "国内", "trust": 1.0},
    {"url": "https://www.chinanews.com/rss/rss_1.html", "category": "国内", "trust": 1.0},
    {"url": "https://www.thepaper.cn/rss_news_1.xml", "category": "国内", "trust": 1.0},
    {"url": "https://news.baidu.com/ns?word=%E7%BB%8F%E6%B5%8E+%E6%94%BF%E7%AD%96+%E6%B0%91%E7%94%9F&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内", "trust": 0.5},
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际", "trust": 1.0},
    {"url": "https://www.chinadaily.com.cn/rss/world_rss.xml", "category": "国际", "trust": 1.0},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际", "trust": 1.0},
    {"url": "https://www.reuters.com/tools/rss", "category": "国际", "trust": 1.0},
    {"url": "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "category": "国际", "trust": 0.7},
    {"url": "http://www.cnhubei.com/rss/whxw.xml", "category": "武汉", "trust": 1.0},
    {"url": "http://www.changjiangtimes.com/rss/wh.xml", "category": "武汉", "trust": 1.0},
    {"url": "http://hb.people.com.cn/rss/hubei.xml", "category": "武汉", "trust": 1.0},
    {"url": "https://news.baidu.com/ns?word=%E6%AD%A6%E6%B1%89+%E6%B9%96%E5%8C%97&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "武汉", "trust": 0.5},
    {"url": "https://www.technologyreview.com/feed/", "category": "AI", "trust": 1.0},
    {"url": "https://www.wired.com/feed/category/ai/latest/rss", "category": "AI", "trust": 1.0},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "category": "AI", "trust": 0.8},
    {"url": "https://news.baidu.com/ns?word=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+AI+%E5%A4%A7%E6%A8%A1%E5%9E%8B&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "AI", "trust": 0.5},
]

# 修正后的关键词权重表：每个类别加入强特征词，避免串类
KEYWORD_SCORES = {
    "国内新闻": {"中国":10, "国内":10, "经济":5, "就业":5, "CPI":5, "房贷":5, "利率":5, "医保":4, "社保":4, "交通":4, "天气":4, "预警":4, "政策":5, "人民币":5},
    "国际新闻": {"美国":10, "欧洲":10, "日本":10, "韩国":10, "俄罗斯":10, "中东":10, "美联储":5, "汇率":5, "油价":5, "芯片":5, "全球":4, "Ukraine":5, "Russia":5, "BBC":5},
    "湖北武汉本地动态": {"武汉":10, "湖北":10, "施工":5, "地铁":5, "暴雨":5, "高温":5, "消费券":5, "烟草":5, "罗森":5, "白沙洲":5},
    "AI对普通人的影响": {"AI":10, "人工智能":10, "ChatGPT":10, "大模型":5, "替代":5, "职业":5, "监管":5, "工具":5},
}

def get_lunar_date():
    today = datetime.now()
    lunar = ZhDate.from_datetime(today)
    return lunar.chinese()

def extract_date_from_text(text: str) -> Optional[str]:
    patterns = [r'(\d{4}-\d{2}-\d{2})', r'(\d{4}年\d{1,2}月\d{1,2}日)']
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            date_str = match.group(1).replace('年','-').replace('月','-').replace('日','')
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
                return date_str
            except:
                continue
    return None

def validate_today(news_item: Dict) -> bool:
    if news_item.get("time") and TODAY not in news_item["time"]:
        return False
    if news_item.get("url"):
        date_in_url = extract_date_from_text(news_item["url"])
        if date_in_url and date_in_url != TODAY:
            return False
    if news_item.get("title"):
        date_in_title = extract_date_from_text(news_item["title"])
        if date_in_title and date_in_title != TODAY:
            return False
    return True

def fetch_rss(feed_info: Dict) -> List[Dict]:
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
            clean = re.sub(r"<[^>]+>", "", summary)[:300].strip()
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
    unique = []
    seen_hashes = set()
    for item in sorted(news_list, key=lambda x: x.get("time", ""), reverse=True):
        h = hashlib.md5(item["title"].encode()).hexdigest()
        if h in seen_hashes:
            continue
        is_dup = False
        for existing in unique:
            if SequenceMatcher(None, item["title"], existing["title"]).ratio() > 0.9:
                is_dup = True
                break
        if is_dup:
            continue
        seen_hashes.add(h)
        unique.append(item)
    return unique

def score_and_select(category: str, news_list: List[Dict], target: int = 50) -> List[Dict]:
    news_list = deduplicate_strong(news_list)
    logger.info(f"{category} 备选新闻(去重后): {len(news_list)} 条")
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
            score *= item.get("trust", 0.5)
            item["_score"] = score
        sorted_news = sorted(news_list, key=lambda x: (x["_score"], x["time"]), reverse=True)
        selected = sorted_news[:5]
    for item in selected:
        if item.get("trust", 1.0) < 0.7:
            item["title"] += " [信源待核实]"
    return selected

def is_english_text(text: str) -> bool:
    """只要没有汉字就视为需翻译的英文"""
    if not text:
        return False
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            return False
    return True

def translate_text(text: str) -> str:
    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY 未配置，跳过翻译")
        return text
    try:
        prompt = f"将以下英文翻译成中文，只返回翻译结果：\n{text}"
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        url = f"{LLM_BASE_URL}/chat/completions"
        resp = requests.post(url, json=data, headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.error(f"翻译请求失败: {resp.status_code} {resp.text[:200]}")
            return text
        result = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"翻译成功: {text[:30]} -> {result[:30]}")
        return result
    except Exception as e:
        logger.error(f"翻译异常: {e}")
        return text

def translate_news_items(sections: Dict[str, List[Dict]]):
    logger.info("开始检查英文新闻...")
    for sec_name, items in sections.items():
        for item in items:
            if is_english_text(item["title"]):
                logger.info(f"发现英文标题: {item['title'][:50]}")
                item["title"] = translate_text(item["title"])
            if is_english_text(item["summary"]):
                item["summary"] = translate_text(item["summary"])

def ai_generate_intro_and_motto(sections: Dict[str, List[Dict]]) -> Dict:
    if not ENABLE_AI or not LLM_API_KEY:
        logger.info("AI未启用或无API Key，跳过概括生成")
        return {"intros": {}, "motto": ""}
    try:
        all_titles = []
        for sec_name, items in sections.items():
            for item in items:
                all_titles.append(f"[{sec_name}] {item['title']}")
        prompt = (
            "你是一位新闻主编。请根据以下今日新闻标题，完成两个任务：\n"
            "1. 为每个分类生成一句15字以内的主题概括。\n"
            "2. 生成一句50字以内的正能量微语。\n"
            "返回JSON：{\"intros\":{\"国内新闻\":\"...\",\"国际新闻\":\"...\",\"湖北武汉本地动态\":\"...\",\"AI对普通人的影响\":\"...\"},\"motto\":\"...\"}\n\n"
            + "\n".join(all_titles)
        )
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "response_format": {"type": "json_object"}
        }
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.error(f"概括生成失败: {resp.status_code} {resp.text[:200]}")
            return {"intros": {}, "motto": ""}
        result = json.loads(resp.json()["choices"][0]["message"]["content"])
        logger.info("AI概括生成成功")
        return {"intros": result.get("intros", {}), "motto": result.get("motto", "")}
    except Exception as e:
        logger.error(f"AI概括异常: {e}")
        return {"intros": {}, "motto": ""}

def format_message(sections: Dict[str, List[Dict]], ai_extra: Dict) -> str:
    now = datetime.now(TZ_BEIJING)
    weekday = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][now.weekday()]
    date_str = now.strftime("%Y年%m月%d日")
    lunar_str = get_lunar_date()
    header = f"{date_str}日报，{weekday}，农历{lunar_str}，工作愉快，生活喜乐！\n"

    order = ["国内新闻", "国际新闻", "湖北武汉本地动态", "AI对普通人的影响"]
    body = ""
    for sec_name in order:
        items = sections.get(sec_name, [])
        if not items:
            body += f"\n【{sec_name}】今日该类热点较少\n"
            continue
        intro = ai_extra.get("intros", {}).get(sec_name, "")
        body += f"\n━━━━━━ 【{sec_name}】━━━━━━\n"
        if intro:
            body += f"📌 {intro}\n"
        for i, item in enumerate(items, 1):
            body += f"{i}. {item['title']}\n"
        body += "\n"

    motto = ai_extra.get("motto", "")
    if motto:
        body += f"【微语】{motto}\n"
    return header + body

def push_to_wechat(title: str, content: str):
    if not PUSHPLUS_TOKEN:
        raise RuntimeError("缺少 PUSHPLUS_TOKEN")
    r = requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "txt"
    }, timeout=15)
    r.raise_for_status()
    logger.info("推送成功")

def main():
    logger.info("=== 开始日报新闻抓取 ===")
    logger.info(f"AI启用: {ENABLE_AI}, 模型: {LLM_MODEL}, BaseURL: {LLM_BASE_URL}")
    logger.info(f"LLM_API_KEY 是否配置: {'是' if LLM_API_KEY else '否'}")

    raw_pool = collect_all_news()
    # 严格按RSS源类别分配板块，防止串类
    section_map = {
        "国内": "国内新闻",
        "国际": "国际新闻",
        "武汉": "湖北武汉本地动态",
        "AI": "AI对普通人的影响",
    }
    sections = {v: [] for v in section_map.values()}
    for raw_cat, news_list in raw_pool.items():
        sec_name = section_map.get(raw_cat)
        if sec_name:
            sections[sec_name].extend(news_list)

    for sec_name in sections:
        sections[sec_name] = score_and_select(sec_name, sections[sec_name])

    translate_news_items(sections)
    ai_extra = ai_generate_intro_and_motto(sections)
    message = format_message(sections, ai_extra)
    push_to_wechat("每日日报", message)

if __name__ == "__main__":
    main()
