#!/usr/bin/env python3
"""
日报（30条综合新闻 + 天气）
无分类，去重精选30条当天新闻
天气和AI摘要为可选项
"""

import os, sys, time, logging, requests, feedparser, re, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

# ---------- 配置 ----------
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")  # 和风天气，可选
TZ_BEIJING = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
MAX_WORKERS = 8
REQUEST_TIMEOUT = 12
TARGET_NEWS = 30  # 推送30条

# 智谱AI（可选）
ENABLE_AI = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ========== 多源 RSS（无分类）==========
RSS_FEEDS = [
    "http://www.xinhuanet.com/politics/xhsll.xml",
    "http://www.people.com.cn/rss/politics.xml",
    "https://www.chinanews.com/rss/rss_1.html",
    "https://www.thepaper.cn/rss_news_1.xml",
    "https://news.sina.com.cn/rss/1.xml",
    "https://news.163.com/special/002341KK/rss_news.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.chinadaily.com.cn/rss/world_rss.xml",
    "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "https://www.36kr.com/feed",
    "https://www.huxiu.com/rss/0.html",
    "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=8.4.6",  # 财联社
    # 百度聚合（综合）
    "https://news.baidu.com/ns?word=%E7%83%AD%E7%82%B9&tn=newsrss&sr=0&cl=2&rn=50&ct=0",
    "https://news.baidu.com/ns?word=%E5%9B%BD%E5%86%85+%E5%9B%BD%E9%99%85&tn=newsrss&sr=0&cl=2&rn=50&ct=0",
]

# ========== 天气 ==========
def get_wuhan_weather() -> Optional[List[Dict]]:
    if not WEATHER_API_KEY:
        return None
    try:
        url = f"https://devapi.qweather.com/v7/weather/7d?location=101200101&key={WEATHER_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logger.error(f"天气请求失败: {resp.status_code}")
            return None
        data = resp.json()
        if data.get("code") != "200":
            logger.error(f"天气API错误: {data.get('code')}")
            return None
        return data.get("daily", [])
    except Exception as e:
        logger.error(f"天气异常: {e}")
        return None

def format_weather(daily: List[Dict]) -> str:
    if not daily:
        return ""
    text = "\n🌤️ 武汉未来7日天气预报\n"
    for day in daily:
        text += f"{day['fxDate']}: {day['textDay']}，{day['tempMin']}~{day['tempMax']}℃，风力{day['windScaleDay']}级\n"
    warnings = []
    for day in daily[:3]:
        if "雨" in day["textDay"]:
            warnings.append("未来三天有降雨，出门请带伞")
            break
    temps = [int(day["tempMax"]) for day in daily[:3]]
    if temps and max(temps) >= 35:
        warnings.append("高温天气，注意防暑降温")
    if warnings:
        text += "\n⚠️ 提醒: " + "；".join(warnings) + "\n"
    return text

# ========== RSS 抓取 ==========
def fetch_rss(url: str) -> List[Dict]:
    news = []
    try:
        sep = "?" if "?" not in url else "&"
        full_url = f"{url}{sep}_t={int(time.time())}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        # 特殊处理财联社API
        if "cls.cn" in url:
            data = resp.json()
            articles = data.get("data", {}).get("roll_data", [])[:50]
            for art in articles:
                title = art.get("title", "")
                ctime = art.get("ctime", 0)
                if ctime:
                    pub_time = datetime.fromtimestamp(ctime, tz=TZ_BEIJING)
                    if pub_time.strftime("%Y-%m-%d") != TODAY: continue
                    summary = re.sub(r"<[^>]+>", "", art.get("brief", ""))[:150]
                    news.append({"title": title, "summary": summary,
                                 "source": "cls.cn", "time": pub_time.strftime("%Y-%m-%d %H:%M")})
            return news

        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            pub_time = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                pub_time = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
            if not pub_time:
                continue
            if pub_time.strftime("%Y-%m-%d") != TODAY:
                continue
            title = entry.get("title", "").strip()
            summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:150].strip()
            news.append({
                "title": title,
                "summary": summary,
                "source": url.split("//")[-1].split("/")[0],
                "time": pub_time.strftime("%Y-%m-%d %H:%M")
            })
    except Exception as e:
        logger.debug(f"源 {url[:60]} 失败: {e}")
    return news

def collect_all_news() -> List[Dict]:
    all_news = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_rss, url): url for url in RSS_FEEDS}
        for future in as_completed(futures):
            try:
                result = future.result()
                all_news.extend(result)
            except Exception as e:
                logger.warning(f"任务异常: {e}")
    return all_news

def deduplicate_and_select(news_list: List[Dict], target=30) -> List[Dict]:
    """去重并按时间降序取前 target 条"""
    seen = set()
    unique = []
    for item in sorted(news_list, key=lambda x: x["time"], reverse=True):
        h = hash(item["title"])
        if h in seen:
            continue
        seen.add(h)
        unique.append(item)
    logger.info(f"去重后共 {len(unique)} 条，选取前 {target} 条")
    return unique[:target]

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

def translate_news(news: List[Dict]):
    for item in news:
        if is_english(item["title"]):
            item["title"] = translate(item["title"])
        if is_english(item["summary"]):
            item["summary"] = translate(item["summary"])

# ========== AI 摘要 ==========
def ai_summary(news: List[Dict]) -> str:
    if not ENABLE_AI or not LLM_API_KEY:
        return ""
    try:
        titles = [item["title"] for item in news[:15]]  # 只传前15条避免过长
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
def build_message(news: List[Dict], weather_text: str, ai_text: str) -> str:
    now = datetime.now(TZ_BEIJING)
    weekday = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][now.weekday()]
    date_str = now.strftime("%Y年%m月%d日")
    header = f"{date_str} 日报 {weekday}\n"

    if weather_text:
        header += weather_text + "\n"

    body = "━━━━━━ 今日新闻 TOP 30 ━━━━━━\n"
    for i, item in enumerate(news, 1):
        body += f"{i}. {item['title']}\n"
        body += f"   {item['source']} · {item['time']}\n\n"

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
    logger.info("开始采集新闻...")
    raw_news = collect_all_news()
    translate_news(raw_news)

    # 精选30条
    selected = deduplicate_and_select(raw_news, TARGET_NEWS)

    # 天气（可选）
    daily = get_wuhan_weather()
    weather_text = format_weather(daily) if daily else ""

    # AI 摘要（可选）
    ai_text = ai_summary(selected)

    # 组装并推送
    msg = build_message(selected, weather_text, ai_text)
    push_wechat(msg)
    logger.info("推送完成")

if __name__ == "__main__":
    main()
