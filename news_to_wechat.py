#!/usr/bin/env python3
"""
超级日报推送：抖音/小红书/公众号/Reddit 等全平台覆盖
智谱AI免费翻译+摘要，6大分类，每类5条，共30条
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
MAX_WORKERS = 12
REQUEST_TIMEOUT = 15

# 智谱AI
ENABLE_AI = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ========== 超级 RSS 源列表 ==========
RSS_FEEDS = [
    # ---------- 国内新闻 ----------
    {"url": "http://www.xinhuanet.com/politics/xhsll.xml", "category": "国内"},
    {"url": "http://www.people.com.cn/rss/politics.xml", "category": "国内"},
    {"url": "https://www.chinanews.com/rss/rss_1.html", "category": "国内"},
    {"url": "https://www.thepaper.cn/rss_news_1.xml", "category": "国内"},
    {"url": "https://news.sina.com.cn/rss/1.xml", "category": "国内"},
    {"url": "https://news.163.com/special/002341KK/rss_news.xml", "category": "国内"},
    # 百度新闻聚合（国内综合）
    {"url": "https://news.baidu.com/ns?word=%E4%B8%AD%E5%9B%BD+%E7%BB%8F%E6%B5%8E+%E6%B0%91%E7%94%9F&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},

    # ---------- 国际新闻 ----------
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"url": "https://www.chinadaily.com.cn/rss/world_rss.xml", "category": "国际"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际"},
    {"url": "https://www.reuters.com/tools/rss", "category": "国际"},
    {"url": "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "category": "国际"},
    {"url": "https://news.baidu.com/ns?word=%E5%9B%BD%E9%99%85+%E7%BE%8E%E5%9B%BD+%E6%AC%A7%E6%B4%B2&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国际"},

    # ---------- 湖北武汉本地 ----------
    {"url": "http://www.cnhubei.com/rss/whxw.xml", "category": "武汉"},
    {"url": "http://www.changjiangtimes.com/rss/wh.xml", "category": "武汉"},
    {"url": "http://hb.people.com.cn/rss/hubei.xml", "category": "武汉"},
    {"url": "http://www.hb.chinanews.com/rss/hubei.xml", "category": "武汉"},
    {"url": "http://www.wuhan.gov.cn/site/rss/whxw.xml", "category": "武汉"},
    {"url": "https://news.baidu.com/ns?word=%E6%AD%A6%E6%B1%89+%E6%B9%96%E5%8C%97+%E5%9C%B0%E9%93%81+%E5%A4%A9%E6%B0%94&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "武汉"},

    # ---------- AI 影响（侧重国内）----------
    {"url": "https://www.36kr.com/feed", "category": "AI"},
    {"url": "https://www.huxiu.com/rss/0.html", "category": "AI"},
    {"url": "https://www.technologyreview.com/feed/", "category": "AI"},
    {"url": "https://news.baidu.com/ns?word=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+AI+%E5%A4%A7%E6%A8%A1%E5%9E%8B+%E6%99%BA%E8%83%BD&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "AI"},

    # ---------- 便利店行业动态 ----------
    {"url": "https://news.baidu.com/ns?word=%E4%BE%BF%E5%88%A9%E5%BA%97+%E9%9B%B6%E5%94%AE+%E5%BF%AB%E6%B6%88+%E4%BE%9B%E5%BA%94%E9%93%BE&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "便利店"},
    {"url": "https://www.linkshop.com/rss/news.xml", "category": "便利店"},

    # ---------- 股票行业情报 ----------
    {"url": "https://news.baidu.com/ns?word=%E8%82%A1%E5%B8%82+%E9%9B%B6%E5%94%AE+%E6%B6%88%E8%B4%B9+%E5%AE%8F%E8%A7%82%E7%BB%8F%E6%B5%8E&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "股票"},
    {"url": "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=8.4.6", "category": "股票"},

    # ===== 新增：社交媒体和流量平台 =====
    # 抖音热门（百度新闻聚合）
    {"url": "https://news.baidu.com/ns?word=%E6%8A%96%E9%9F%B3+%E7%83%AD%E6%90%9C&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    # 小红书热门
    {"url": "https://news.baidu.com/ns?word=%E5%B0%8F%E7%BA%A2%E4%B9%A6+%E7%83%AD%E9%97%A8&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    # 微信公众号热文
    {"url": "https://news.baidu.com/ns?word=%E5%BE%AE%E4%BF%A1%E5%85%AC%E4%BC%97%E5%8F%B7+%E7%83%AD%E6%96%87&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    # 微博热搜
    {"url": "https://news.baidu.com/ns?word=%E5%BE%AE%E5%8D%9A%E7%83%AD%E6%90%9C+%E8%AF%9D%E9%A2%98&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    # 知乎热榜
    {"url": "https://news.baidu.com/ns?word=%E7%9F%A5%E4%B9%8E+%E7%83%AD%E6%A6%9C&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    # 百度贴吧热议
    {"url": "https://news.baidu.com/ns?word=%E8%B4%B4%E5%90%A7+%E7%83%AD%E8%AE%AE&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},

    # 国外平台：Reddit、Twitter 等（通过 RSSHub 公共实例）
    # Reddit 热门（r/all）
    {"url": "https://rsshub.app/reddit/all", "category": "国际"},
    # Twitter（X）用户推文（以 Elon Musk 为例，可自行更换）
    {"url": "https://rsshub.app/twitter/user/elonmusk", "category": "国际"},
    # Telegram 频道（示例：每日新闻频道）
    {"url": "https://rsshub.app/telegram/channel/tnews365", "category": "国际"},
    # 如果上面失效，备选另一个RSSHub实例
    {"url": "https://rsshub.rssforever.com/reddit/all", "category": "国际"},
]

# ---------- 关键词评分表 ----------
KEYWORD_SCORES = {
    "国内新闻": {"中国":10,"国内":10,"抖音":8,"小红书":8,"公众号":8,"微博":8,"知乎":8,"经济":5,"就业":5},
    "国际新闻": {"美国":10,"欧洲":10,"日本":10,"俄罗斯":10,"Reddit":8,"Twitter":8,"美联储":5,"汇率":5},
    "湖北武汉本地动态": {"武汉":10,"湖北":10,"施工":5,"地铁":5,"暴雨":5,"高温":5,"消费券":5,"烟草":5,"罗森":5},
    "AI对普通人的影响": {"AI":10,"人工智能":10,"ChatGPT":10,"大模型":5,"替代":5,"职业":5,"国内":8,"中国":8},
    "便利店行业动态": {"便利店":10,"零售":8,"快消":8,"供应链":5,"加盟":5,"鲜食":5,"罗森":8,"7-11":8,"全家":8},
    "股票行业情报": {"A股":10,"指数":8,"板块":8,"零售":5,"消费":5,"券商":5,"研报":5,"利率":5,"上市公司":5},
}

FALLBACK_KEYWORDS = {
    "国内新闻": ["中国","国内","经济","政策","社会"],
    "国际新闻": ["美国","欧洲","国际","全球"],
    "湖北武汉本地动态": ["武汉","湖北"],
    "AI对普通人的影响": ["人工智能","AI","智能"],
    "便利店行业动态": ["零售","便利店","超市"],
    "股票行业情报": ["股市","A股","股票"],
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
        # 财联社API特殊处理
        if "cls.cn" in url:
            try:
                data = resp.json()
                articles = data.get("data", {}).get("roll_data", [])[:50]
                for art in articles:
                    title = art.get("title", "")
                    ctime = art.get("ctime", 0)
                    if ctime:
                        pub_time = datetime.fromtimestamp(ctime, tz=TZ_BEIJING)
                        if pub_time.strftime("%Y-%m-%d") != TODAY: continue
                        time_str = pub_time.strftime("%Y-%m-%d %H:%M")
                        summary = re.sub(r"<[^>]+>", "", art.get("brief", ""))[:200]
                        item = {"title":title,"summary":summary,"source":"cls.cn","time":time_str,"category":feed_info["category"],"url":art.get("url","")}
                        news.append(item)
            except: pass
        else:
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                pub_time = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_time = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
                if not pub_time: continue
                time_str = pub_time.strftime("%Y-%m-%d %H:%M")
                title = entry.get("title","").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary",""))[:300].strip()
                item = {"title":title,"summary":summary,"source":url.split("//")[-1].split("/")[0],"time":time_str,"category":feed_info["category"],"url":entry.get("link","")}
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
        if any(SequenceMatcher(None, item["title"], u["title"]).ratio() > 0.9 for u in unique): continue
        seen.add(h)
        unique.append(item)
    return unique

def select_with_fallback(category_name: str, all_news: List[Dict], target=5) -> List[Dict]:
    news_list = deduplicate_strong(all_news)
    logger.info(f"{category_name} 去重后共 {len(news_list)} 条")

    scores = KEYWORD_SCORES.get(category_name, {})
    for item in news_list:
        score = sum(v for k, v in scores.items() if k in item["title"])
        item["_score"] = score
    ranked = sorted(news_list, key=lambda x: (x["_score"], x["time"]), reverse=True)
    selected = ranked[:target]

    if len(selected) < target:
        logger.info(f"{category_name} 不足，使用兜底关键词扩大范围...")
        fallback_kw = FALLBACK_KEYWORDS.get(category_name, [])
        supplement = [item for item in ranked if item not in selected and any(kw in item["title"] for kw in fallback_kw)]
        selected += supplement[:target-len(selected)]

    if len(selected) < target:
        logger.info(f"{category_name} 仍不足，从剩余最新新闻中补足...")
        remaining = [item for item in ranked if item not in selected]
        selected += sorted(remaining, key=lambda x: x["time"], reverse=True)[:target-len(selected)]

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
        if resp.status_code != 200: return text
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
            "你是新闻主编。根据以下标题，为六个分类各生成一句15字内概括，并生成一句50字内正能量微语。"
            "分类：国内新闻、国际新闻、湖北武汉本地动态、AI对普通人的影响、便利店行业动态、股票行业情报。"
            "返回JSON：{\"intros\":{\"国内新闻\":\"\",\"国际新闻\":\"\",\"湖北武汉本地动态\":\"\",\"AI对普通人的影响\":\"\",\"便利店行业动态\":\"\",\"股票行业情报\":\"\"},\"motto\":\"\"}"
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
    order = ["国内新闻","国际新闻","湖北武汉本地动态","AI对普通人的影响","便利店行业动态","股票行业情报"]
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
    logger.info("超级日报抓取启动...")
    raw = collect_all_news()
    section_map = {
        "国内": "国内新闻", "国际": "国际新闻", "武汉": "湖北武汉本地动态",
        "AI": "AI对普通人的影响", "便利店": "便利店行业动态", "股票": "股票行业情报"
    }
    sections = {v:[] for v in section_map.values()}
    for cat, news in raw.items():
        sec = section_map.get(cat)
        if sec: sections[sec].extend(news)

    for sec_name, all_news in sections.items():
        sections[sec_name] = select_with_fallback(sec_name, all_news)

    translate_all(sections)
    ai_extra = ai_summary_and_motto(sections)
    msg = format_daily(sections, ai_extra)
    push_wechat(msg)
    logger.info("推送完成")

if __name__ == "__main__":
    main()
