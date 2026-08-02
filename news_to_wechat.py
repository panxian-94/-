#!/usr/bin/env python3
"""
日报推送（全中文、多源、智谱AI免费翻译+摘要）
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
MAX_WORKERS = 10
REQUEST_TIMEOUT = 12

# AI 配置（智谱免费模型）
ENABLE_AI = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------- 扩展 RSS 源 ----------
RSS_FEEDS = [
    # 国内新闻（大幅扩充）
    {"url": "http://www.xinhuanet.com/politics/xhsll.xml", "category": "国内"},
    {"url": "http://www.people.com.cn/rss/politics.xml", "category": "国内"},
    {"url": "https://www.chinanews.com/rss/rss_1.html", "category": "国内"},
    {"url": "https://www.thepaper.cn/rss_news_1.xml", "category": "国内"},
    {"url": "https://news.sina.com.cn/rss/1.xml", "category": "国内"},
    {"url": "https://news.163.com/special/002341KK/rss_news.xml", "category": "国内"},
    # 百度新闻动态源（国内关键词）
    {"url": "https://news.baidu.com/ns?word=%E4%B8%AD%E5%9B%BD+%E7%BB%8F%E6%B5%8E+%E6%B0%91%E7%94%9F&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},

    # 国际新闻
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"url": "https://www.chinadaily.com.cn/rss/world_rss.xml", "category": "国际"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际"},
    {"url": "https://www.reuters.com/tools/rss", "category": "国际"},
    {"url": "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "category": "国际"},
    {"url": "https://news.baidu.com/ns?word=%E5%9B%BD%E9%99%85+%E7%BE%8E%E5%9B%BD+%E6%AC%A7%E6%B4%B2&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国际"},

    # 武汉本地（增加湖北全域源）
    {"url": "http://www.cnhubei.com/rss/whxw.xml", "category": "武汉"},
    {"url": "http://www.changjiangtimes.com/rss/wh.xml", "category": "武汉"},
    {"url": "http://hb.people.com.cn/rss/hubei.xml", "category": "武汉"},
    {"url": "http://www.hb.chinanews.com/rss/hubei.xml", "category": "武汉"},
    {"url": "https://news.baidu.com/ns?word=%E6%AD%A6%E6%B1%89+%E6%B9%96%E5%8C%97+%E5%9C%B0%E9%93%81+%E5%A4%A9%E6%B0%94&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "武汉"},

    # AI影响（侧重国内信息）
    {"url": "https://www.technologyreview.com/feed/", "category": "AI"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "category": "AI"},
    # 国内AI源
    {"url": "https://www.36kr.com/feed", "category": "AI"},  # 36氪
    {"url": "https://www.huxiu.com/rss/0.html", "category": "AI"},  # 虎嗅
    {"url": "https://news.baidu.com/ns?word=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+AI+%E5%A4%A7%E6%A8%A1%E5%9E%8B+%E6%99%BA%E8%83%BD&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "AI"},
]

# ---------- 关键词评分表（已调整权重，AI类倾向国内）----------
KEYWORD_SCORES = {
    "国内新闻": {"中国":10, "国内":10, "经济":5, "就业":5, "CPI":5, "房贷":5, "利率":5,
                 "医保":4, "社保":4, "交通":4, "天气":4, "预警":4, "政策":5, "人民币":5},
    "国际新闻": {"美国":10, "欧洲":10, "日本":10, "俄罗斯":10, "中东":10, "美联储":5,
                 "汇率":5, "油价":5, "芯片":5, "全球":4, "BBC":5, "路透":5},
    "湖北武汉本地动态": {"武汉":10, "湖北":10, "施工":5, "地铁":5, "暴雨":5, "高温":5,
                         "消费券":5, "烟草":5, "罗森":5, "白沙洲":5},
    "AI对普通人的影响": {"AI":10, "人工智能":10, "ChatGPT":10, "大模型":5, "替代":5,
                         "职业":5, "监管":5, "工具":5, "国内":8, "中国":8},  # 倾向国内
}

# 兜底关键词（分类不够时用）
FALLBACK_KEYWORDS = {
    "国内新闻": ["中国", "国内", "经济", "政策", "社会"],
    "国际新闻": ["美国", "欧洲", "国际", "全球", "外交"],
    "湖北武汉本地动态": ["武汉", "湖北"],
    "AI对普通人的影响": ["人工智能", "AI", "智能", "大模型"],
}

# ---------- 工具函数 ----------
def get_lunar_date():
    return ZhDate.from_datetime(datetime.now()).chinese()

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

def validate_today(item: Dict) -> bool:
    if item.get("time") and TODAY not in item["time"]:
        return False
    if item.get("url"):
        if extract_date_from_text(item["url"]) not in (None, TODAY):
            return False
    if item.get("title"):
        if extract_date_from_text(item["title"]) not in (None, TODAY):
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
            if not pub_time: continue
            time_str = pub_time.strftime("%Y-%m-%d %H:%M")
            title = entry.get("title", "").strip()
            summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:300].strip()
            item = {
                "title": title,
                "summary": summary,
                "source": url.split("//")[-1].split("/")[0],
                "time": time_str,
                "category": feed_info["category"],
                "url": entry.get("link", "")
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
                pool.setdefault(cat, []).extend(result)
            except Exception as e:
                logger.warning(f"任务异常: {e}")
    return pool

def deduplicate_strong(news_list: List[Dict]) -> List[Dict]:
    unique = []
    seen = set()
    for item in sorted(news_list, key=lambda x: x["time"], reverse=True):
        h = hashlib.md5(item["title"].encode()).hexdigest()
        if h in seen: continue
        # 编辑距离去重
        if any(SequenceMatcher(None, item["title"], u["title"]).ratio() > 0.9 for u in unique):
            continue
        seen.add(h)
        unique.append(item)
    return unique

def select_with_fallback(category_name: str, all_news: List[Dict], target=5) -> List[Dict]:
    """优先按关键词评分选取，不足则用兜底关键词从所有新闻中补足"""
    news_list = deduplicate_strong(all_news)
    logger.info(f"{category_name} 去重后共 {len(news_list)} 条")

    # 正常评分
    scores = KEYWORD_SCORES.get(category_name, {})
    for item in news_list:
        score = sum(v for k, v in scores.items() if k in item["title"])
        item["_score"] = score
    ranked = sorted(news_list, key=lambda x: (x["_score"], x["time"]), reverse=True)
    selected = ranked[:target]

    # 不足5条，放宽关键词再次筛选
    if len(selected) < target:
        logger.info(f"{category_name} 不足5条，使用兜底关键词扩大范围...")
        fallback_kw = FALLBACK_KEYWORDS.get(category_name, [])
        supplement = []
        for item in ranked:
            if item in selected: continue
            if any(kw in item["title"] for kw in fallback_kw):
                supplement.append(item)
            if len(selected) + len(supplement) >= target:
                break
        selected += supplement[:target-len(selected)]

    # 仍然不足，直接从当日所有新闻中取最新补足
    if len(selected) < target:
        logger.info(f"{category_name} 仍不足，从剩余新闻中取最新补足...")
        remaining = [item for item in ranked if item not in selected]
        selected += sorted(remaining, key=lambda x: x["time"], reverse=True)[:target-len(selected)]

    # 低可信标注（根据来源）
    for item in selected:
        if item.get("source", "") not in ["xinhuanet.com","people.com.cn","chinanews.com","cctv.com"]:
            item["title"] += " [信源待核实]"
    return selected[:target]

def is_english_text(text: str) -> bool:
    if not text: return False
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff': return False
    return True

def translate_text(text: str) -> str:
    if not LLM_API_KEY: return text
    try:
        prompt = f"将以下英文翻译成中文，只返回翻译结果：\n{text}"
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.1}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.error(f"翻译失败: {resp.status_code}")
            return text
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"翻译异常: {e}")
        return text

def translate_all(sections: Dict[str, List[Dict]]):
    for sec, items in sections.items():
        for item in items:
            if is_english_text(item["title"]):
                item["title"] = translate_text(item["title"])
            if is_english_text(item["summary"]):
                item["summary"] = translate_text(item["summary"])

def ai_summary_and_motto(sections: Dict[str, List[Dict]]) -> Dict:
    if not ENABLE_AI or not LLM_API_KEY: return {"intros":{}, "motto":""}
    try:
        all_titles = [f"[{sec}] {item['title']}" for sec, items in sections.items() for item in items]
        prompt = (
            "你是新闻主编。根据以下标题，为四个分类各生成一句15字内概括，并生成一句50字内正能量微语。"
            "返回JSON：{\"intros\":{\"国内新闻\":\"\",\"国际新闻\":\"\",\"湖北武汉本地动态\":\"\",\"AI对普通人的影响\":\"\"},\"motto\":\"\"}"
            "\n标题：\n" + "\n".join(all_titles)
        )
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.7,
                "response_format":{"type":"json_object"}}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=25)
        if resp.status_code == 200:
            result = json.loads(resp.json()["choices"][0]["message"]["content"])
            return {"intros": result.get("intros",{}), "motto": result.get("motto","")}
    except Exception as e:
        logger.error(f"AI摘要失败: {e}")
    return {"intros":{}, "motto":""}

def format_daily(sections: Dict[str, List[Dict]], ai_extra: Dict) -> str:
    now = datetime.now(TZ_BEIJING)
    weekday = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][now.weekday()]
    date_str = now.strftime("%Y年%m月%d日")
    header = f"{date_str}日报，{weekday}，农历{get_lunar_date()}，工作愉快，生活喜乐！\n"
    order = ["国内新闻","国际新闻","湖北武汉本地动态","AI对普通人的影响"]
    body = ""
    for sec in order:
        items = sections.get(sec, [])
        if not items:
            body += f"\n【{sec}】今日该类热点较少\n"
            continue
        intro = ai_extra.get("intros",{}).get(sec,"")
        body += f"\n━━━━━━ 【{sec}】━━━━━━\n"
        if intro: body += f"📌 {intro}\n"
        for i, item in enumerate(items, 1):
            body += f"{i}. {item['title']}\n"
        body += "\n"
    motto = ai_extra.get("motto","")
    if motto: body += f"【微语】{motto}\n"
    return header + body

def push_wechat(content: str):
    if not PUSHPLUS_TOKEN: raise RuntimeError("缺 PUSHPLUS_TOKEN")
    requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": "每日日报", "content": content, "template": "txt"
    }, timeout=15).raise_for_status()

def main():
    logger.info("开始抓取多源新闻...")
    raw = collect_all_news()
    # 分类映射
    section_map = {"国内":"国内新闻","国际":"国际新闻","武汉":"湖北武汉本地动态","AI":"AI对普通人的影响"}
    sections = {v:[] for v in section_map.values()}
    for cat, news in raw.items():
        sec = section_map.get(cat)
        if sec: sections[sec].extend(news)

    # 每个类别智能选取
    for sec_name, all_news in sections.items():
        sections[sec_name] = select_with_fallback(sec_name, all_news)

    # 翻译英文
    translate_all(sections)
    # AI 摘要
    ai_extra = ai_summary_and_motto(sections)
    # 生成消息
    msg = format_daily(sections, ai_extra)
    push_wechat(msg)
    logger.info("推送完成")

if __name__ == "__main__":
    main()
