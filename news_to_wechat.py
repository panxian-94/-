#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻推送到微信（PushPlus）
功能：抓取多源新闻 → AI去重/摘要/分类 → PushPlus推送
"""

import os
import re
import time
import logging
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# ==========================================
# 日志配置
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==========================================
# 环境变量读取
# ==========================================
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_API_URL = os.environ.get('AI_API_URL', 'https://api.deepseek.com/v1/chat/completions')
AI_MODEL = os.environ.get('AI_MODEL', 'deepseek-chat')

# 请求超时时间
REQUEST_TIMEOUT = 15
# User-Agent
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# ==========================================
# 新闻源配置
# ==========================================
NEWS_SOURCES = {
    'domestic': [
        {'name': '中新网-国内', 'url': 'https://www.chinanews.com.cn/rss/china.xml', 'type': 'rss'},
        {'name': '中新网-财经', 'url': 'https://www.chinanews.com.cn/rss/finance.xml', 'type': 'rss'},
        {'name': '中新网-社会', 'url': 'https://www.chinanews.com.cn/rss/society.xml', 'type': 'rss'},
    ],
    'international': [
        {'name': '中新网-国际', 'url': 'https://www.chinanews.com.cn/rss/world.xml', 'type': 'rss'},
    ],
    'hubei_wuhan': [
        {'name': '长江日报', 'url': 'https://www.cjrbapp.cjn.cn/rss/headline.xml', 'type': 'rss'},
    ],
    'ai_impact': [
        {'name': '36氪-科技', 'type': 'scrape', 'url': 'https://36kr.com/information/technology/'},
    ]
}

# 板块分类映射
CATEGORY_MAP = {
    'domestic': '国内新闻',
    'international': '国际新闻',
    'hubei_wuhan': '湖北武汉本地动态',
    'ai_impact': 'AI对普通人影响'
}

# 子分类关键词匹配
SUBCATEGORY_KEYWORDS = {
    'domestic': {
        '经济就业': ['CPI', '失业率', '个税', '社保', 'GDP', '就业', '工资', '收入', '消费', '物价'],
        '房产金融': ['LPR', '限购', '存款利率', '房贷', '房价', '楼市', '银行', '降息', '加息', '金融'],
        '区域发展': ['自贸区', '地铁', '学校', '规划', '新区', '产业园', '基建', '拆迁', '搬迁'],
        '生活安全': ['灾害', '预警', '食品安全', '事故', '疫情', '地震', '暴雨', '高温', '火灾']
    },
    'international': {
        '全球资金流向': ['美联储', '汇率', '加息', '降息', '美元', '国债', '资本', '外资'],
        '地缘大宗商品': ['油价', '天然气', '粮食', '矿产', '黄金', '原油', '大宗商品', '战争', '制裁'],
        '科技产业变革': ['芯片', 'AI', '人工智能', '半导体', '技术', '专利', '科技', '算法']
    },
    'hubei_wuhan': {
        '客流地图': ['施工', '封路', '地铁', '公交', '改线', '搬迁', '入驻', '活动', '开业'],
        '天气进货指南': ['暴雨', '高温', '预警', '天气', '预报', '降温', '降雪'],
        '生意成本': ['批发', '租金', '水电', '消费券', '烟草', '工商', '检查', '物价', '涨价'],
        '竞争对手': ['新店', '罗森', '零食很忙', '团购', '社群', '便利店', '开业']
    },
    'ai_impact': {
        '职业冲击': ['失业', '替代', '岗位', '就业', '职业', '工作', '裁员'],
        '生活渗透': ['生活', '日常', '应用', '工具', '效率', '办公'],
        '商业机会': ['创业', '商机', '变现', '赚钱', '副业', '产业'],
        '风险监管': ['监管', '风险', '安全', '隐私', '伦理', '法规', '治理']
    }
}

# ==========================================
# 工具函数
# ==========================================

def add_timestamp_to_url(url: str) -> str:
    """给URL添加时间戳参数，跳过缓存"""
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    query_params['_t'] = [str(int(time.time() * 1000))]
    new_query = urlencode(query_params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def is_today(date_str: str) -> bool:
    """判断新闻是否为当天发布"""
    today = datetime.now().date()
    date_formats = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%a, %d %b %Y %H:%M:%S %z',
        '%Y/%m/%d %H:%M:%S',
    ]
    for fmt in date_formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.date() == today
        except ValueError:
            continue
    # 解析失败默认保留，后续AI再筛选
    return True


def parse_publish_time(entry) -> str:
    """从RSS条目解析发布时间"""
    time_str = ''
    if hasattr(entry, 'published'):
        time_str = entry.published
    elif hasattr(entry, 'updated'):
        time_str = entry.updated
    
    # 尝试格式化
    try:
        parsed = datetime(*entry.published_parsed[:6])
        return parsed.strftime('%Y-%m-%d %H:%M')
    except Exception:
        pass
    
    return time_str or datetime.now().strftime('%Y-%m-%d %H:%M')


def calc_content_hash(title: str, content: str = '') -> str:
    """计算内容哈希用于去重"""
    text = re.sub(r'\s+', '', title + content[:100])
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def match_subcategory(title: str, summary: str, section: str) -> str:
    """根据关键词匹配子分类"""
    text = title + ' ' + (summary or '')
    keywords_dict = SUBCATEGORY_KEYWORDS.get(section, {})
    
    best_subcat = '综合'
    max_count = 0
    
    for subcat, keywords in keywords_dict.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > max_count:
            max_count = count
            best_subcat = subcat
    
    return best_subcat


# ==========================================
# 新闻抓取模块
# ==========================================

def fetch_rss_news(source: Dict) -> List[Dict]:
    """从RSS源抓取新闻"""
    news_list = []
    try:
        url = add_timestamp_to_url(source['url'])
        logger.info(f"抓取RSS源: {source['name']}")
        
        feed = feedparser.parse(url)
        if feed.bozo != 0:
            logger.warning(f"RSS解析警告: {source['name']} - {feed.bozo_exception}")
        
        for entry in feed.entries[:20]:
            title = entry.get('title', '').strip()
            link = entry.get('link', '')
            summary = entry.get('summary', '')
            
            # 清理HTML标签
            if summary:
                summary = BeautifulSoup(summary, 'html.parser').get_text(strip=True)
            
            publish_time = parse_publish_time(entry)
            
            # 只保留当天新闻
            if not is_today(publish_time):
                continue
            
            news_list.append({
                'title': title,
                'url': link,
                'source': source['name'],
                'publish_time': publish_time,
                'summary_raw': summary[:200],
                'content_hash': calc_content_hash(title, summary)
            })
        
        logger.info(f"从 {source['name']} 获取到 {len(news_list)} 条当日新闻")
        
    except Exception as e:
        logger.error(f"抓取RSS源失败 {source['name']}: {e}")
    
    return news_list


def scrape_web_news(source: Dict) -> List[Dict]:
    """网页抓取新闻（备用方案）"""
    news_list = []
    try:
        url = add_timestamp_to_url(source['url'])
        headers = {'User-Agent': USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.encoding = 'utf-8'
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = soup.select('.article-item, .item, .news-item, li')
        
        for item in items[:15]:
            title_elem = item.select_one('a.title, h3 a, a')
            if not title_elem:
                continue
            
            title = title_elem.get_text(strip=True)
            if len(title) < 8:
                continue
            
            link = title_elem.get('href', '')
            if link and not link.startswith('http'):
                base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                link = base_url + link
            
            news_list.append({
                'title': title,
                'url': link,
                'source': source['name'],
                'publish_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'summary_raw': '',
                'content_hash': calc_content_hash(title)
            })
        
        logger.info(f"从 {source['name']} 网页抓取到 {len(news_list)} 条新闻")
        
    except Exception as e:
        logger.error(f"网页抓取失败 {source['name']}: {e}")
    
    return news_list


def fetch_all_news() -> Dict[str, List[Dict]]:
    """抓取所有板块的新闻"""
    all_news = {}
    
    for section, sources in NEWS_SOURCES.items():
        section_news = []
        seen_hashes = set()
        
        for source in sources:
            if source['type'] == 'rss':
                news = fetch_rss_news(source)
            else:
                news = scrape_web_news(source)
            
            # 去重
            for item in news:
                if item['content_hash'] not in seen_hashes:
                    seen_hashes.add(item['content_hash'])
                    section_news.append(item)
        
        # 按发布时间降序
        section_news.sort(key=lambda x: x['publish_time'], reverse=True)
        all_news[section] = section_news
        logger.info(f"板块 [{CATEGORY_MAP[section]}] 共获取 {len(section_news)} 条去重后新闻")
    
    return all_news


# ==========================================
# AI 智能处理模块
# ==========================================

def call_ai_api(prompt: str) -> str:
    """调用AI接口"""
    if not AI_API_KEY:
        logger.warning("未配置AI_API_KEY，跳过AI处理")
        return ''
    
    try:
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {AI_API_KEY}'
        }
        payload = {
            'model': AI_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.3,
            'max_tokens': 2000
        }
        
        resp = requests.post(
            AI_API_URL,
            headers=headers,
            json=payload,
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content'].strip()
        
    except Exception as e:
        logger.error(f"AI接口调用失败: {e}")
        return ''


def ai_process_news(news_list: List[Dict], section: str, target_count: int = 5) -> List[Dict]:
    """
    AI处理新闻：去重、剔除标题党、生成摘要、标记信源
    返回处理后的新闻列表
    """
    if not news_list:
        return []
    
    # 如果没有AI Key，使用简单规则处理
    if not AI_API_KEY:
        logger.info("无AI Key，使用规则模式处理")
        processed = []
        for item in news_list[:target_count * 2]:
            item['summary'] = item['summary_raw'] or '暂无摘要'
            item['subcategory'] = match_subcategory(item['title'], item['summary_raw'], section)
            item['source_verified'] = True
            processed.append(item)
        return processed[:target_count]
    
    # 构造AI处理Prompt
    news_json = []
    for i, item in enumerate(news_list[:target_count * 3]):
        news_json.append({
            'index': i,
            'title': item['title'],
            'summary_raw': item['summary_raw'],
            'source': item['source'],
            'publish_time': item['publish_time']
        })
    
    prompt = f"""
你是一名资深新闻编辑，请对以下新闻列表进行专业处理：

任务要求：
1. 去重：内容相似的新闻只保留一条
2. 剔除标题党：去掉夸张、误导、震惊体标题的新闻
3. 生成摘要：为每条新闻生成50-80字的客观摘要，不重复标题
4. 信源判断：权威媒体标注为可信，来源不明标注"信源待核实"
5. 筛选出最重要的{target_count}条，按重要性排序

新闻列表：
{news_json}

请严格按以下JSON格式返回，不要有其他文字：
{{
    "processed_news": [
        {{
            "original_index": 原序号,
            "summary": "摘要内容",
            "source_verified": true/false,
            "keep": true/false
        }}
    ]
}}
"""
    
    result = call_ai_api(prompt)
    if not result:
        logger.warning("AI处理失败，回退到规则模式")
        return ai_process_news(news_list, section, target_count)
    
    # 解析AI返回结果
    try:
        # 提取JSON部分
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            import json
            ai_data = json.loads(json_match.group())
        else:
            raise ValueError("无法解析AI返回的JSON")
        
        processed_map = {}
        for item in ai_data.get('processed_news', []):
            if item.get('keep', True):
                processed_map[item['original_index']] = {
                    'summary': item['summary'],
                    'source_verified': item.get('source_verified', True)
                }
        
        # 合并结果
        result_list = []
        for i, news in enumerate(news_list[:target_count * 3]):
            if i in processed_map:
                news['summary'] = processed_map[i]['summary']
                news['source_verified'] = processed_map[i]['source_verified']
                news['subcategory'] = match_subcategory(news['title'], news['summary'], section)
                result_list.append(news)
                if len(result_list) >= target_count:
                    break
        
        logger.info(f"AI处理完成，筛选出 {len(result_list)} 条新闻")
        return result_list
        
    except Exception as e:
        logger.error(f"解析AI结果失败: {e}")
        return ai_process_news(news_list, section, target_count)


# ==========================================
# 格式化输出模块
# ==========================================

def format_news_item(news: Dict, section: str) -> str:
    """格式化单条新闻"""
    subcat = news.get('subcategory', '综合')
    source_text = news['source']
    if not news.get('source_verified', True):
        source_text += "（信源待核实）"
    
    return f"""[{CATEGORY_MAP[section]}-{subcat}] {news['title']}
来源：{source_text}
发布时间：{news['publish_time']}
摘要：{news.get('summary', news.get('summary_raw', '暂无摘要'))}
"""


def build_push_content(all_processed: Dict[str, List[Dict]]) -> str:
    """构建推送内容"""
    today_str = datetime.now().strftime('%Y年%m月%d日')
    content = f"📰 每日新闻简报 - {today_str}\n\n"
    
    section_order = ['domestic', 'international', 'hubei_wuhan', 'ai_impact']
    
    for section in section_order:
        news_list = all_processed.get(section, [])
        section_name = CATEGORY_MAP[section]
        
        content += f"━━━ {section_name} ━━━\n\n"
        
        if not news_list:
            content += "今日该类热点较少\n\n"
            continue
        
        for i, news in enumerate(news_list, 1):
            content += f"{i}. {format_news_item(news, section)}\n"
        
        if len(news_list) < 5:
            content += "（今日该类热点较少）\n\n"
    
    content += "\n—— 由自动化新闻推送系统生成 ——"
    return content


# ==========================================
# PushPlus 推送模块
# ==========================================

def push_to_wechat(title: str, content: str) -> bool:
    """通过PushPlus推送到微信"""
    if not PUSHPLUS_TOKEN:
        logger.error("未配置PUSHPLUS_TOKEN环境变量，无法推送")
        return False
    
    try:
        url = 'http://www.pushplus.plus/send'
        payload = {
            'token': PUSHPLUS_TOKEN,
            'title': title,
            'content': content,
            'template': 'txt'
        }
        
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        
        if result.get('code') == 200:
            logger.info("PushPlus推送成功")
            return True
        else:
            logger.error(f"PushPlus推送失败: {result.get('msg', '未知错误')}")
            return False
            
    except Exception as e:
        logger.error(f"PushPlus请求异常: {e}")
        return False


# ==========================================
# 主流程
# ==========================================

def main():
    """主函数"""
    logger.info("=" * 50)
    logger.info("开始执行每日新闻推送任务")
    logger.info("=" * 50)
    
    try:
        # 1. 抓取所有新闻
        logger.info("步骤1: 抓取新闻源...")
        all_news = fetch_all_news()
        
        # 2. AI处理各板块
        logger.info("步骤2: AI智能处理新闻...")
        all_processed = {}
        for section, news_list in all_news.items():
            logger.info(f"处理板块: {CATEGORY_MAP[section]}")
            processed = ai_process_news(news_list, section, target_count=5)
            all_processed[section] = processed
        
        # 3. 构建推送内容
        logger.info("步骤3: 构建推送内容...")
        today_str = datetime.now().strftime('%m月%d日')
        title = f"📰 每日新闻简报 {today_str}"
        content = build_push_content(all_processed)
        
        # 输出预览（前500字）
        logger.info(f"内容预览:\n{content[:500]}...")
        
        # 4. 推送
        logger.info("步骤4: 推送到微信...")
        success = push_to_wechat(title, content)
        
        if success:
            logger.info("✅ 任务执行完成，推送成功")
        else:
            logger.warning("⚠️ 推送失败，请检查Token配置")
            
    except Exception as e:
        logger.error(f"❌ 任务执行异常: {e}", exc_info=True)
        # 异常时也尝试推送告警
        if PUSHPLUS_TOKEN:
            push_to_wechat("新闻推送异常", f"每日新闻推送任务执行出错：{e}")
    
    logger.info("=" * 50)


if __name__ == '__main__':
    main()
