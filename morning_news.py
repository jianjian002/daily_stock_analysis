import os
import sys

# 强制刷新标记
force = len(sys.argv) > 1 and sys.argv[1] == "force"

# GitHub定时调度自动执行时，强制清空当日缓存重新抓取
if os.getenv("GITHUB_EVENT_NAME") == "schedule":
    force = True
# 下方你的原有代码（交易日判断、假期区间抓取、大模型、飞书推送）
import os
import sys
import time
import json
import requests
from dotenv import load_dotenv
from datetime import datetime
import akshare as ak
import pandas as pd
import tempfile
print("1. 加载.env全局配置文件")
# 跨平台临时目录，兼容Windows本地 / Linux云端
tmp_dir = tempfile.gettempdir()
env_path = f"{tmp_dir}/.env"
CACHE_PATH = f"{tmp_dir}/cache.json"

# 从GitHub环境变量自动生成.env文件（云端无本地.env）
def create_env_file():
    env_text = f"""
OPENAI_API_KEY={os.getenv("OPENAI_API_KEY","")}
OPENAI_BASE_URL={os.getenv("OPENAI_BASE_URL","")}
OPENAI_MODEL={os.getenv("OPENAI_MODEL","")}
AGENT_API_KEY={os.getenv("AGENT_API_KEY","")}
AGENT_BASE_URL={os.getenv("AGENT_BASE_URL","")}
AGENT_MODEL={os.getenv("AGENT_MODEL","")}
STOCK_LIST={os.getenv("STOCK_LIST","")}
FEISHU_WEBHOOK_URL={os.getenv("FEISHU_WEBHOOK_URL","")}
TIME_ZONE=Asia/Shanghai
JUHE_KEY={os.getenv("JUHE_KEY","")}
"""
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_text.strip())

# 程序启动自动生成配置文件
create_env_file()

load_dotenv(env_path)
# 火山方舟配置
ark_api_key = os.getenv("OPENAI_API_KEY")
ark_base_url = os.getenv("OPENAI_BASE_URL")
ep_id = os.getenv("OPENAI_MODEL")
feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL")
# 聚合新闻密钥
juhe_api_key = os.getenv("JUHE_KEY")
# 全局缓存交易日列表，只加载一次
TRADE_DATE_CACHE = None
# 最大保留新闻条数，防止上下文过长超时
MAX_TOTAL_NEWS = 60
# ---------------------- AKShare 交易日判断【已修复日期类型报错】 ----------------------
def load_all_trade_dates():
    global TRADE_DATE_CACHE
    if TRADE_DATE_CACHE is None:
        print("加载A股全量交易日历...")
        df = ak.tool_trade_date_hist_sina()
        # 核心修复：字符串转datetime，解决.dt报错
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        TRADE_DATE_CACHE = set(df["trade_date"].dt.strftime("%Y%m%d").tolist())
    return TRADE_DATE_CACHE
def is_trade_day(date_str=None):
    """判断 YYYYMMDD 格式日期是否交易日，不传默认今日"""
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    trade_set = load_all_trade_dates()
    return date_str in trade_set
# ---------------------- 缓存工具函数 ----------------------
def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"market_list": [], "news_list": []}
def append_cache(market_dict, news_list):
    cache = load_cache()
    if market_dict.get("market_list"):
        cache["market_list"].extend(market_dict)
    # 新闻去重
    exist_titles = [item["title"] for item in cache["news_list"]]
    for news in news_list:
        if news["title"] not in exist_titles:
            cache["news_list"].append(news)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
def clear_cache():
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)
# ---------------------- 数据源接口 ----------------------
def fetch_global_market_data():
    url = "https://apis.juhe.cn/openApi/stock/market"
    params = {"key": juhe_api_key, "dtype": "json"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        raw = resp.json()
        print(f"【聚合行情接口原始返回】{raw}")
        if raw.get("error_code") != 0 or not raw.get("result"):
            # 标记接口失效，交由大模型全网检索完整外围+大宗商品
            return '{"market_list":[],"msg":"API行情接口失效，请联网完整检索隔夜全部外围资产数据"}'
        simple_data = {"market_list": raw["result"]}
        return json.dumps(simple_data, ensure_ascii=False)
    except Exception as e:
        print(f"行情接口请求异常：{str(e)}")
        return '{"market_list":[],"msg":"API行情接口失效，请联网完整检索隔夜全部外围资产数据"}'
def fetch_finance_news():
    url = "https://apis.juhe.cn/fapigx/caijing/query"
    params = {"key": juhe_api_key, "num": 50, "page": 1}
    try:
        resp = requests.get(url, params=params, timeout=15)
        raw = resp.json()
        print(f"【聚合新闻接口原始返回】{raw}")
        if raw.get("error_code") != 0:
            print(f"财经新闻接口返回错误：{raw.get('reason')}")
            return None
        if not raw.get("result") or not raw["result"].get("newslist"):
            print("财经新闻无数据")
            return None
        simple_news = []
        for item in raw["result"]["newslist"]:
            simple_news.append({
                "title": item.get("title"),
                "summary": item.get("description")
            })
        return json.dumps(simple_news, ensure_ascii=False)
    except Exception as e:
        print(f"新闻接口异常：{str(e)}")
        return None
# ====================== 简报生成模板【修复大宗商品缺失、接口失效联网检索】 ======================
FORMAT_TEMPLATE = """
根据下方【隔夜外围行情说明】和【隔夜财经新闻素材】整理早间简报，严格按三段顺序输出，总字数控制900字以内，仅使用提供素材；
若标注【API行情接口失效】，你必须全网实时检索完整隔夜全球金融数据，补齐全部品种，禁止留白简略。
# 强制检索清单（接口失效必须全部查到并写入）
1.美股：道琼斯、纳斯达克、标普500、纳斯达克中国金龙指数涨跌幅+驱动；
2.亚太股指：富时中国A50、日经225、韩国KOSPI隔夜表现；
3.大宗商品：布伦特原油、WTI原油、现货黄金涨跌幅与核心逻辑；
4.债券：美国10年期国债收益率变动；
5.外汇：离岸人民币兑美元汇率波动；
# 新闻筛选优先级规则
1. 素材新闻无固定条数，接口已按资讯重要度排序；优先保留宏观政策、产业重大利好/利空、外围重磅消息；
2. 若新闻总量过多超出字数限制，自动剔除行业小幅中性资讯，只保留高市场影响度消息；
3. 禁止堆砌无关零散小资讯，重点突出对A股大盘、细分赛道有实质影响的内容。
# ========== 一、隔夜全球外围完整行情（大宗商品/美债/汇率单独段落，不可省略） ==========
1.美股三大指数、纳斯达克中国金龙涨跌幅+核心驱动；
2.富时中国A50、日经225、韩国KOSPI 隔夜涨跌表现；
3.大宗商品专区（必须单独写一段）：布伦特原油、WTI原油、现货黄金日内涨跌幅、价格波动驱动；
4.美债与汇率专区（必须单独写一段）：美国10年期美债收益率变动、离岸人民币兑美元汇率涨跌；
5.一句话总结外围整体情绪，判定今日A股开盘：利好/中性/利空。
# ========== 二、全球宏观股市热点新闻 ==========
统一格式：■ 新闻标题 | 简短摘要 | 大盘影响：利好/中性/利空
# ========== 三、A股动态细分行业资讯
【规则说明】
1. 禁止固定板块列表，完全根据新闻自动识别细分板块；
2. 板块细分至产业链环节、细分产品、技术题材；
3. 一条新闻匹配一个精准细分板块；
4. 格式：
【细分板块名称】
● 消息：标题+简短摘要 | 板块影响：利好/利空
5. 无新闻板块直接省略。
【隔夜外围行情说明】
{market_info}
【隔夜财经新闻素材】
{news_info}
"""
# 统一生成并推送简报内部公共方法
def build_and_send_brief(all_market_data, all_news_data, manual_tip=""):
    # 兜底截断新闻数量
    if len(all_news_data) > MAX_TOTAL_NEWS:
        all_news_data = all_news_data[-MAX_TOTAL_NEWS:]
        print(f"新闻超{MAX_TOTAL_NEWS}条，仅保留最新{MAX_TOTAL_NEWS}条")
    market_info_text = json.dumps(all_market_data, ensure_ascii=False)
    news_info_text = json.dumps(all_news_data, ensure_ascii=False)
    full_prompt = FORMAT_TEMPLATE.format(market_info=market_info_text, news_info=news_info_text)
    ark_url = ark_base_url + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {ark_api_key}",
        "Content-Type": "application/json"
    }
    token_limit = 2000 if len(all_news_data) > 10 else 1200
    req_body = {
        "model": ep_id,
        "messages": [{"role": "user", "content": full_prompt}],
        "max_tokens": token_limit,
        "temperature": 0.2,
        "stream": False
    }
    timeout_config = (15, 1800)
    max_retry_times = 3
    final_content = ""
    for retry in range(max_retry_times):
        try:
            print(f"第{retry+1}次调用火山方舟")
            response = requests.post(ark_url, headers=headers, json=req_body, timeout=timeout_config)
            if response.status_code == 200:
                res_json = response.json()
                final_content = res_json["choices"][0]["message"]["content"]
                print("简报生成完成")
                break
            else:
                wait = (retry + 1) * 10
                print(f"接口异常，等待{wait}秒重试")
                time.sleep(wait)
        except requests.exceptions.ReadTimeout:
            wait = (retry + 1) * 10
            print(f"读取超时，等待{wait}秒重试")
            time.sleep(wait)
        except Exception as err:
            wait = (retry + 1) * 10
            print(f"调用异常{str(err)}，等待{wait}秒重试")
            time.sleep(wait)
    if not final_content:
        fail_msg = {
            "msg_type": "text",
            "content": {"text": "【早间简报生成失败】火山大模型多次超时，请稍后重试"}
        }
        requests.post(feishu_webhook, json=fail_msg, timeout=10)
        return False
    # 拼接手动提示
    send_text = ""
    if manual_tip:
        send_text = f"===== {manual_tip} =====\n\n"
    send_text += final_content
    send_payload = {"msg_type": "text", "content": {"text": send_text}}
    requests.post(feishu_webhook, json=send_payload, timeout=10)
    print("简报推送完成")
    return True
# 定时自动执行入口（原有逻辑不变）
def run_morning_report():
    today_date = datetime.now().strftime("%Y%m%d")
    trade_flag = is_trade_day(today_date)
    print("2. 拉取外围行情数据")
    market_raw = fetch_global_market_data()
    print("3. 拉取财经新闻（最大50条，按重要度排序）")
    news_raw = fetch_finance_news()
    if not news_raw:
        fail_msg = {
            "msg_type": "text",
            "content": {"text": "【早间简报生成失败】聚合财经新闻接口无有效素材，请检查接口权限/免费额度"}
        }
        requests.post(feishu_webhook, json=fail_msg, timeout=10)
        return
    news_arr = json.loads(news_raw)
    try:
        market_dict = json.loads(market_raw)
    except:
        market_dict = {"market_list": [], "msg": "API行情接口失效，请联网完整检索隔夜全部外围资产数据"}
    # 非交易日：仅缓存，不推送，不清空缓存
    if not trade_flag:
        print(f"今日{today_date}非A股交易日，行情新闻存入缓存，开盘统一汇总推送")
        append_cache(market_dict, news_arr)
        return
    # 交易日：合并缓存+当日，推送后清空缓存
    cache_data = load_cache()
    all_market_data = {"market_list": cache_data["market_list"] + market_dict["market_list"]}
    all_news_data = cache_data["news_list"] + news_arr
    print(f"缓存累积{len(cache_data['news_list'])}条节假日新闻，合并今日数据生成简报")
    success = build_and_send_brief(all_market_data, all_news_data)
    if success:
        clear_cache()
        print("缓存已清空")
# 新增：强制汇总推送（手动调用，节假日可用，缓存不清空）
def force_send_all_cached():
    today_date = datetime.now().strftime("%Y%m%d")
    trade_flag = is_trade_day(today_date)
    print("===== 手动强制汇总推送全部缓存消息 =====")
    print("拉取当前最新行情+新闻")
    market_raw = fetch_global_market_data()
    news_raw = fetch_finance_news()
    if not news_raw:
        fail_msg = {
            "msg_type": "text",
            "content": {"text": "【强制汇总失败】聚合财经新闻接口无有效素材"}
        }
        requests.post(feishu_webhook, json=fail_msg, timeout=10)
        return
    news_arr = json.loads(news_raw)
    try:
        market_dict = json.loads(market_raw)
    except:
        market_dict = {"market_list": [], "msg": "API行情接口失效，请联网完整检索隔夜全部外围资产数据"}
    # 先把本次最新数据追加进缓存
    append_cache(market_dict, news_arr)
    cache_data = load_cache()
    all_market_data = {"market_list": cache_data["market_list"]}
    all_news_data = cache_data["news_list"]
    # 构造提示文案
    if not trade_flag:
        tip = f"⚠️ 警告：今日{today_date}为A股休市日（法定节假日/周末），本次为手动强制汇总截止当前全部累积消息，定时任务开盘日才会清空缓存"
    else:
        tip = f"📌 手动强制汇总推送，今日{today_date}为正常交易日，缓存推送后不会自动清空"
    print(f"合计待推送新闻{len(all_news_data)}条")
    build_and_send_brief(all_market_data, all_news_data, manual_tip=tip)
    print("本次强制推送完成，缓存保留，不会自动删除")
if __name__ == "__main__":
    # 命令行参数判断：python morning_news.py force 执行强制汇总
    if len(sys.argv) > 1 and sys.argv[1] == "force":
        force_send_all_cached()
    else:
        run_morning_report()
