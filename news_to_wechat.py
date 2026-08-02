#!/usr/bin/env python3
"""
新闻+天气双板块日报
新闻子类：国内/国际/武汉/股票/便利店/人口/婚恋 各5条
天气：武汉7日预报及提醒
智谱AI摘要+微语，PushPlus推送
"""

import os, sys, time, logging, requests, feedparser, re, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

# ---------- 配置 ----------
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")  # 和风天气Key
TZ_BEIJING = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
MAX_WORKERS = 8
REQUEST_TIMEOUT = 12

# 智谱AI
ENABLE_AI = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ========== 新闻子分类定义 ==========
NEWS_SUBCATEGORIES = {
    "国内": "国内新闻",
    "国际": "国际新闻",
    "武汉": "湖北武汉本地",
    "股票": "股票行业情报",
    "便利店": "便利店动态",
    "人口": "中国人口动态",
    "婚恋": "婚恋市场观察"
}

# ========== RSS 源（已按子分类分配）==========
RSS_FEEDS = [
    # 国内
    {"url": "https://news.baidu.com/ns?word=%E4%B8%AD%E5%9B%BD+%E7%BB%8F%E6%B5%8E+%E6%B0%91%E7%94%9F&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "国内"},
    {"url": "http://www.xinhuanet.com/politics/xhsll.xml", "sub": "国内"},
    # 国际
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "sub": "国际"},
    {"url": "https://news.baidu.com/ns?word=%E5%9B%BD%E9%99%85+%E7%BE%8E%E5%9B%BD+%E6%AC%A7%E6%B4%B2&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "国际"},
    # 武汉
    {"url": "https://news.baidu.com/ns?word=%E6%AD%A6%E6%B1%89+%E6%B9%96%E5%8C%97+%E5%9C%B0%E9%93%81+%E5%A4%A9%E6%B0%94&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "武汉"},
    {"url": "http://hb.people.com.cn/rss/hubei.xml", "sub": "武汉"},
    # 股票
    {"url": "https://news.baidu.com/ns?word=%E8%82%A1%E5%B8%82+%E9%9B%B6%E5%94%AE+%E6%B6%88%E8%B4%B9&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "股票"},
    # 便利店
    {"url": "https://news.baidu.com/ns?word=%E4%BE%BF%E5%88%A9%E5%BA%97+%E9%9B%B6%E5%94%AE+%E5%BF%AB%E6%B6%88&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "便利店"},
    # 人口
    {"url": "https://news.baidu.com/ns?word=%E4%B8%AD%E5%9B%BD%E4%BA%BA%E5%8F%A3+%E5%87%BA%E7%94%9F%E7%8E%87+%E8%80%81%E9%BE%84%E5%8C%96&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "人口"},
    # 婚恋
    {"url": "https://news.baidu.com/ns?word=%E5%A9%9A%E6%81%8B%E5%B8%82%E5%9C%BA+%E7%BB%93%E5%A9%9A%E7%8E%87+%E7%A6%BB%E5%A9%9A%E7%8E%87&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "sub": "婚恋"},
]

# ========== 关键词评分（用于精选）==========
KEYWORD_SCORES = {
    "国内": ["中国","国内","经济","就业","政策","社会"],
    "国际": ["美国","欧洲","俄罗斯","中东","美联储","汇率","油价"],
    "武汉": ["武汉","湖北","地铁","施工","天气","暴雨","高温"],
    "股票": ["A股","上证","指数","券商","零售","消费","研报"],
    "便利店": ["便利店","零售","快消","罗森","7-11","加盟"],
    "人口": ["人口","出生率","老龄化","三孩","生育","计生"],
    "婚恋": ["婚恋","结婚","离婚","相亲","彩礼","单身","婚介"]
}

# ========== 天气 ==========
def get_wuhan_weather() -> Optional[List[Dict]]:
    if not WEATHER_API_KEY:
        return None
    try:
        url = f"https://devapi.qweather.com/v7/weather/7d?location=101200101&key={WEATHER_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logger.error(f"天气API请求失败: {resp.status_code}")
            return None
        data = resp.json()
        if data.get("code") != "200":
            logger.error(f"天气API错误: {data.get('code')}")
            return None
        return data.get("daily", [])
    except Exception as e:
        logger.error(f"天气获取异常: {e}")
        return None

def format_weather(daily: List[Dict]) -> str:
    if not daily:
        return ""
    text = "\n🌤️ 武汉未来7日天气预报\n"
    for day in daily:
        text += f"{day['fxDate']}: {day['textDay']}，{day['tempMin']}~{day['tempMax']}℃，风力{day['windScaleDay']}级\n"
    # 智能提醒
    warnings = []
    for day in daily[:3]:
        if "雨" in day["textDay"]:
            warnings.append("未来三天有降雨，出门请带伞")
            break
    max_temps = [int(day["tempMax"]) for day in daily[:3]]
    if max_temps and max(max_temps) >= 35:
        warnings.append("高温天气，注意防暑降温")
    if warnings:
        text += "\n⚠️ 提醒: " + "；".join(warnings) + "\n"
    return text

# ========== RSS 抓取 ==========
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
            if TODAY not in time_str:
                continue
            title = entry.get("title","").strip()
            summary = re.sub(r"<[^>]+>", "", entry.get("summary",""))[:200].strip()
            news.append({
                "title": title,
                "summary": summary,
                "source": url.split("//")[-1].split("/")[0],
                "time": time_str,
                "sub": feed_info["sub"]
            })
    except Exception as e:
        logger.debug(f"源 {url[:60]} 失败: {e}")
    return news

def collect_news() -> Dict[str, List[Dict]]:
    pool = {sub: [] for sub in NEWS_SUBCATEGORIES}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_rss, f): f for f in RSS_FEEDS}
        for future in as_completed(futures):
            feed_info = futures[future]
            try:
                result = future.result()
                sub = feed_info["sub"]
                if sub in pool:
                    pool[sub].extend(result)
            except Exception as e:
                logger.warning(f"任务异常: {e}")
    return pool

def select_top(sub: str, news_list: List[Dict], count=5) -> List[Dict]:
    # 去重
    seen = set()
    unique = []
    for item in sorted(news_list, key=lambda x: x["time"], reverse=True):
        h = hash(item["title"])
        if h in seen:
            continue
        seen.add(h)
        unique.append(item)
    # 关键词评分
    kw = KEYWORD_SCORES.get(sub, [])
    for item in unique:
        score = sum(1 for w in kw if w in item["title"])
        item["_score"] = score
    ranked = sorted(unique, key=lambda x: (x["_score"], x["time"]), reverse=True)
    # 保底5条
    selected = ranked[:count]
    if len(selected) < count:
        # 从全局借用
        all_news = [item for lst in pool.values() for item in lst]
        rest = [item for item in all_news if item not in selected]
        selected += sorted(rest, key=lambda x: x["time"], reverse=True)[:count-len(selected)]
    return selected[:count]

# ========== 翻译 ==========
def is_english(text: str) -> bool:
    if not text: return False
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff': return False
    return True

def translate(text: str) -> str:
    if not LLM_API_KEY: return text
    try:
        prompt = f"将以下英文翻译成中文：{text}"
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.1}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=15)
        if resp.status_code != 200: return text
        return resp.json()["choices"][0]["message"]["content"].strip()
    except: return text

def translate_news(pool: Dict[str, List[Dict]]):
    for items in pool.values():
        for item in items:
            if is_english(item["title"]):
                item["title"] = translate(item["title"])
            if is_english(item["summary"]):
                item["summary"] = translate(item["summary"])

# ========== AI 摘要 ==========
def ai_summary(selected_pool: Dict[str, List[Dict]]) -> str:
    if not ENABLE_AI or not LLM_API_KEY: return ""
    try:
        titles = []
        for sub, items in selected_pool.items():
            for item in items:
                titles.append(f"[{NEWS_SUBCATEGORIES[sub]}] {item['title']}")
        prompt = (
            "根据以下新闻标题，生成一句15字内的今日新闻总结，并附一句50字内的正能量微语。"
            "返回JSON：{\"summary\":\"...\",\"motto\":\"...\"}"
            "\n标题：\n" + "\n".join(titles)
        )
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.7,
                "response_format":{"type":"json_object"}}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=20)
        if resp.status_code == 200:
            result = json.loads(resp.json()["choices"][0]["message"]["content"])
            return f"📌 {result.get('summary','')}\n【微语】{result.get('motto','')}"
    except Exception as e:
        logger.error(f"AI摘要失败: {e}")
    return ""

# ========== 格式化 ==========
def build_message(selected_pool: Dict[str, List[Dict]], weather_text: str, ai_text: str) -> str:
    now = datetime.now(TZ_BEIJING)
    weekday = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][now.weekday()]
    date_str = now.strftime("%Y年%m月%d日")
    header = f"{date_str} 日报 {weekday}\n"

    # 天气板块
    if weather_text:
        header += weather_text + "\n"

    # 新闻板块
    body = "━━━━━━ 今日新闻 ━━━━━━\n"
    for sub, full_name in NEWS_SUBCATEGORIES.items():
        items = selected_pool.get(sub, [])
        body += f"\n【{full_name}】\n"
        if not items:
            body += "  今日暂无更新\n"
        else:
            for i, item in enumerate(items, 1):
                body += f"  {i}. {item['title']}\n"
    body += "\n"

    # AI 摘要
    if ai_text:
        body += f"{ai_text}\n"

    return header + body

def push_wechat(content: str):
    if not PUSHPLUS_TOKEN:
        raise RuntimeError("缺少 PUSHPLUS_TOKEN")
    r = requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": "每日日报", "content": content, "template": "txt"
    }, timeout=15)
    r.raise_for_status()
    logger.info("推送成功")

def main():
    logger.info("开始采集新闻与天气...")
    raw_news = collect_news()
    translate_news(raw_news)

    # 精选每个子类5条
    selected = {}
    for sub in NEWS_SUBCATEGORIES:
        selected[sub] = select_top(sub, raw_news.get(sub, []))

    # 天气
    daily = get_wuhan_weather()
    weather_text = format_weather(daily) if daily else ""

    # AI摘要
    ai_text = ai_summary(selected)

    # 组合并推送
    msg = build_message(selected, weather_text, ai_text)
    push_wechat(msg)
    logger.info("推送完成")

if __name__ == "__main__":
    main()
