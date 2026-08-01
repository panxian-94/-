#!/usr/bin/env python3
"""
每日新闻推送脚本 - 便利店经营者定制版
板块：国内(5) + 国际(5) + 武汉本地(5) + AI影响(5) = 20条
依赖：feedparser, requests
推送：PushPlus
"""

import os, sys, time, hashlib, logging, requests, feedparser
from datetime import datetime, timezone, timedelta
from typing import List, Dict

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
TZ_BEIJING = timezone(timedelta(hours=8))

# ---------- RSS 源 ----------
RSS_SOURCES = {
    "国内": [
        "http://www.xinhuanet.com/politics/xhsll.xml",
        "http://www.people.com.cn/rss/politics.xml",
    ],
    "国际": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.chinadaily.com.cn/rss/world_rss.xml",
    ],
    "武汉": [
        "http://www.cnhubei.com/rss/whxw.xml",          # 荆楚网武汉
        "http://www.changjiangtimes.com/rss/wh.xml",   # 长江日报（示例，可能需调整）
    ],
    "AI": [
        "https://www.technologyreview.com/feed/",      # MIT科技评论
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    ]
}

# 板块关键词筛选（用于二次过滤，确保内容贴合需求）
KEYWORDS = {
    "国内": ["CPI", "失业", "个税", "房贷", "LPR", "限购", "存款利率", "国债", "自贸区", "地铁", "学校", "医院", "台风", "暴雨", "高温", "食品召回", "流行病"],
    "国际": ["美联储", "利率", "汇率", "油价", "天然气", "粮食", "锂", "镍", "芯片", "AI", "人工智能"],
    "武汉": ["武汉交警", "施工", "封路", "地铁", "公交", "围挡", "社区", "开学", "交付", "暴雨", "高温", "白沙洲", "批发价", "租金", "消费券", "烟草", "工商", "罗森", "零食", "团购"],
    "AI": ["AI", "ChatGPT", "提示词", "替代", "失业", "职业", "工具", "投资", "监管", "诈骗"]
}

def fetch_news(category: str, source_url: str) -> List[Dict]:
    """抓取单个RSS源，返回当天新闻列表"""
    news_list = []
    try:
        # 加随机参数跳过缓存
        url = source_url + ("?" if "?" not in source_url else "&") + f"t={int(time.time())}"
        feed = feedparser.parse(url)
        today = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
        for entry in feed.entries:
            pub_time = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                pub_time = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
            if pub_time and pub_time.strftime("%Y-%m-%d") != today:
                continue  # 只要当天的
            title = entry.get("title", "无标题")
            link = entry.get("link", "")
            summary = entry.get("summary", "")
            # 去除HTML标签，取前100字
            import re
            clean_summary = re.sub(r'<[^>]+>', '', summary)[:150]
            source = source_url.split("//")[-1].split("/")[0]
            news_list.append({
                "title": title,
                "url": link,
                "summary": clean_summary,
                "source": source,
                "time": pub_time.strftime("%Y-%m-%d %H:%M") if pub_time else "未知"
            })
    except Exception as e:
        logger.warning(f"抓取 {category} 源 {source_url} 失败: {e}")
    return news_list

def filter_by_keywords(news_list: List[Dict], category: str) -> List[Dict]:
    """根据关键词二次筛选，返回匹配的新闻"""
    kw_list = KEYWORDS.get(category, [])
    if not kw_list:
        return news_list
    result = []
    for item in news_list:
        text = item["title"] + item["summary"]
        if any(kw in text for kw in kw_list):
            result.append(item)
    return result

def deduplicate(news_list: List[Dict]) -> List[Dict]:
    """简单去重：按标题相似度（MD5）"""
    seen = set()
    unique = []
    for item in news_list:
        h = hashlib.md5(item["title"].encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(item)
    return unique

def select_top(news_list: List[Dict], count: int) -> List[Dict]:
    """按时间降序取前N条"""
    return sorted(news_list, key=lambda x: x.get("time", ""), reverse=True)[:count]

def format_message(section: str, items: List[Dict]) -> str:
    if not items:
        return f"\n【{section}】今日该类热点较少\n"
    msg = f"\n━━━━━━ 【{section}】━━━━━━\n"
    for idx, item in enumerate(items, 1):
        msg += f"{idx}. {item['title']}\n"
        msg += f"   来源：{item['source']} | {item['time']}\n"
        msg += f"   摘要：{item['summary']}\n\n"
    return msg

def send_pushplus(title: str, content: str):
    if not PUSHPLUS_TOKEN:
        raise RuntimeError("缺少环境变量 PUSHPLUS_TOKEN")
    url = "http://www.pushplus.plus/send"
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "txt"
    }
    resp = requests.post(url, json=data, timeout=15)
    resp.raise_for_status()
    logger.info("推送成功")

def main():
    logger.info("开始抓取新闻...")
    all_news = {"国内": [], "国际": [], "武汉": [], "AI": []}

    for category, urls in RSS_SOURCES.items():
        for url in urls:
            raw = fetch_news(category, url)
            all_news[category].extend(raw)
        # 过滤 + 去重 + 截取
        filtered = filter_by_keywords(all_news[category], category)
        unique = deduplicate(filtered)
        selected = select_top(unique, 5)
        all_news[category] = selected
        logger.info(f"{category} 板块获取 {len(selected)} 条")

    # 拼接消息
    today_str = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
    message = f"📰 每日新闻精选 ({today_str})\n"
    message += format_message("国内新闻", all_news["国内"])
    message += format_message("国际新闻", all_news["国际"])
    message += format_message("湖北武汉本地动态", all_news["武汉"])
    message += format_message("AI对普通人的影响", all_news["AI"])

    logger.info("推送消息...")
    send_pushplus("每日新闻精选", message)

if __name__ == "__main__":
    main()
