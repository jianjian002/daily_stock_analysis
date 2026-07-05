import os
import sys
import time
import json
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta
import akshare as ak
import pandas as pd
import tempfile

# 跨平台临时目录，兼容Windows本地 / Linux云端
tmp_dir = tempfile.gettempdir()
env_path = f"{tmp_dir}/.env"
CLOSE_CACHE_PATH = f"{tmp_dir}/close_cache.json"

# 从环境变量自动生成.env配置文件
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

create_env_file()
load_dotenv(env_path)

# 全局缓存交易日列表（已修复dt报错）
TRADE_DATE_CACHE = None
# 固定配置
ark_api_key = os.getenv("OPENAI_API_KEY")
ark_base_url = os.getenv("OPENAI_BASE_URL")
ep_id = os.getenv("OPENAI_MODEL")
feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL")
juhe_api_key = os.getenv("JUHE_KEY")

# ---------------------- 统一交易日工具【修复dt报错】 ----------------------
def load_all_trade_dates():
    global TRADE_DATE_CACHE
    if TRADE_DATE_CACHE is None:
        print("加载A股全量交易日历...")
        df = ak.tool_trade_date_hist_sina()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        TRADE_DATE_CACHE = set(df["trade_date"].dt.strftime("%Y%m%d").tolist())
    return TRADE_DATE_CACHE

def is_trade_day(date_str=None):
    """判断 YYYYMMDD 格式日期是否交易日，不传默认今日"""
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    trade_set = load_all_trade_dates()
    return date_str in trade_set

def get_latest_trade_date():
    """获取距离今天最近的上一个交易日（用于休市时读取缓存）"""
    trade_set = load_all_trade_dates()
    today = datetime.now()
    offset = 1
    while True:
        check_dt = today - timedelta(days=offset)
        check_str = check_dt.strftime("%Y%m%d")
        if check_str in trade_set:
            return check_str
        offset += 1

# ---------------------- 收盘缓存读写 ----------------------
def save_close_cache(trade_date, market_json):
    """保存当日收盘行情到缓存，记录对应交易日"""
    cache_data = {
        "latest_trade_date": trade_date,
        "market_data": market_json
    }
    with open(CLOSE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

def load_close_cache():
    """读取最近交易日缓存，无缓存返回None"""
    if not os.path.exists(CLOSE_CACHE_PATH):
        return None
    with open(CLOSE_CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------------------- 飞书消息推送封装 ----------------------
def send_feishu_msg(content):
    try:
        requests.post(
            feishu_webhook,
            json={"msg_type": "text", "content": {"text": content}},
            timeout=10
        )
    except Exception as e:
        print(f"飞书推送异常：{e}")

# ---------------------- 1. 行情数据抓取 ----------------------
def get_market_data():
    try:
        url = "https://apis.juhe.cn/openApi/stock/market"
        params = {"key": juhe_api_key, "dtype": "json"}
        resp = requests.get(url, params=params, timeout=10)
        raw = resp.json()
        if raw.get("error_code") == 0 and raw.get("result"):
            print("✅ 聚合行情接口调用成功")
            return json.dumps(raw["result"], ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 聚合行情接口异常：{e}")
    
    # 接口不可用告警
    warn_msg = "⚠️【行情接口告警】A股行情接口临时不可用，已自动切换为大模型联网检索真实数据生成复盘，请知悉！"
    print(warn_msg)
    send_feishu_msg(warn_msg)
    return "INTERFACE_FAILED"

# ---------------------- 2. 复盘Prompt模板 ----------------------
def get_prompt(market_data, trade_date):
    today_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    retrieve_instruction = ""
    
    if market_data == "INTERFACE_FAILED":
        retrieve_instruction = "📢 行情接口不可用，请你【联网检索当日A股真实全量数据】，确保数据准确无误！"
        market_data = "无接口数据，由你检索最新真实行情"
    return f"""
你是A股顶级专业复盘师，严格按照下方【固定格式】生成《收盘板块梯队复盘》，日期：{today_fmt}。
{retrieve_instruction}
所有表格、结构、分析、数据校验必须和模板完全一致！
【固定输出格式】
【收盘板块梯队复盘】{today_fmt}
市场概况
上证指数：XXX点（+X.XX%）
深证成指：XXX点（+X.XX%）
创业板指：XXX点（+X.XX%）
科创50：-X.XX%
成交额：XX万亿（较前日缩量/放量约XX亿）
全市场XXXX只上涨，XXXX只下跌
涨停XXX只（含ST），跌停XXX只（含ST）
不含ST：XXX只涨停，连板股XX只，封板率XX%
1. 行业板块涨幅排名TOP5
表格
排名	板块名称	涨跌幅
第1名	XXX	+X.XX%
第2名	XXX	+X.XX%
第3名	XXX	+X.XX%
第4名	XXX	+X.XX%
第5名	XXX	+X.XX%
2. 行业板块跌幅排名TOP5
表格
排名	板块名称	涨跌幅
第1名	XXX	-X.XX%
第2名	XXX	-X.XX%
第3名	XXX	-X.XX%
第4名	XXX	-X.XX%
第5名	XXX	-X.XX%
3. 热门概念板块涨幅TOP5
表格
排名	概念名称	涨跌幅	核心驱动
第1名	XXX	+X.XX%	XXX
第2名	XXX	+X.XX%	XXX
第3名	XXX	+X.XX%	XXX
第4名	XXX	+X.XX%	XXX
第5名	XXX	+X.XX%	XXX
其他活跃概念：XXX、XXX、XXX
4. 热门概念板块跌幅TOP5
表格
排名	概念名称	涨跌幅
第1名	XXX	-X.XX%
第2名	XXX	-X.XX%
第3名	XXX	-X.XX%
第4名	XXX	-X.XX%
第5名	XXX	-X.XX%
其他下跌概念：XXX、XXX、XXX
5. 涨停数量第一板块
板块名称：XXX
涨停家数：XX只
核心个股：XXX
涨停第二板块（参考）：XXX
涨停个股完整清单（按行业板块归类，标注细分概念，不含ST）
XXX —— XX只
表格
细分概念	个股名称	连板数
XXX	XXX	XXX
6. 跌停数量第一板块
板块名称：XXX
跌停家数：XX只
跌停个股完整清单（按行业板块归类，标注细分概念，不含ST）
XXX —— XX只
表格
细分概念	个股名称	连跌数
XXX	XXX	XXX
驱动因素：XXX
7. 连板高度最高板块
板块名称：XXX
最高连板个股：XXX | 细分概念：XXX
连板高度：X板
连板梯队完整一览（不含ST）
表格
连板数	个股名称	所属行业	细分概念
X连板	XXX	XXX	XXX
其他高辨识度个股：XXX
8. 拥有完整连板梯队的板块及梯队结构
XXX
原因分析：XXX
最接近完整梯队的板块：
XXX
评价：XXX
9. 数据复查
涨停总数校验：XXX
跌停总数校验：XXX
连板高度校验：XXX
梯队完整性校验：XXX
交叉验证：XXX
数据来源：东方财富/界面新闻、财联社、证券时报/数据宝、第一财经、证券日报
【当日行情数据】
{market_data}
"""

# ---------------------- 3. 调用大模型生成复盘 ----------------------
def generate_review(prompt):
    url = f"{ark_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {ark_api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": ep_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3000,
        "temperature": 0.1,
        "stream": False
    }
    # 3次重试
    for _ in range(3):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=1800)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            time.sleep(10)
        except Exception as e:
            time.sleep(10)
    return "❌ 复盘生成失败：大模型请求超时"

# ---------------------- 内部统一生成&推送复盘 ----------------------
def build_and_send_review(market_data, trade_date, manual_tip=""):
    prompt = get_prompt(market_data, trade_date)
    content = generate_review(prompt)
    send_text = ""
    if manual_tip:
        send_text = f"===== {manual_tip} =====\n\n"
    send_text += content
    send_feishu_msg(send_text)
    print("✅ 复盘推送完成")

# ---------------------- 定时自动主逻辑（15:30自动生成缓存，长期定时专用） ----------------------
def main():
    today_str = datetime.now().strftime("%Y%m%d")
    trade_flag = is_trade_day(today_str)
    # 今日非交易日：只拉行情存入缓存，不推送
    if not trade_flag:
        print(f"今日{today_str}为休市日，不推送复盘，仅缓存最新收盘数据")
        market_raw = get_market_data()
        latest_dt = get_latest_trade_date()
        save_close_cache(latest_dt, market_raw)
        return
    # 今日交易日：正常拉取、推送、更新缓存
    print("📊 开始生成当日收盘板块梯队复盘...")
    market_raw = get_market_data()
    save_close_cache(today_str, market_raw)
    build_and_send_review(market_raw, today_str)

# ---------------------- 手动强制汇总（节假日读取已有缓存推送） ----------------------
def force_send_close():
    today_str = datetime.now().strftime("%Y%m%d")
    trade_flag = is_trade_day(today_str)
    cache = load_close_cache()
    if cache is None:
        tip = "⚠️ 无历史收盘缓存，无法推送复盘，请等交易日15:30自动生成一次"
        send_feishu_msg(tip)
        print(tip)
        return
    cache_dt = cache["latest_trade_date"]
    market_data = cache["market_data"]
    if not trade_flag:
        tip = f"⚠️ 今日{today_str}为A股休市，本次推送截止【{cache_dt[:4]}-{cache_dt[4:6]}-{cache_dt[6:]}】的收盘完整复盘"
    else:
        tip = f"📌 手动强制推送当日收盘复盘，日期：{cache_dt[:4]}-{cache_dt[4:6]}-{cache_dt[6:]}"
    print(f"执行强制推送，使用缓存交易日：{cache_dt}")
    build_and_send_review(market_data, cache_dt, manual_tip=tip)

if __name__ == "__main__":
    # 命令行参数区分自动定时 / 手动强制推送
    if len(sys.argv) > 1 and sys.argv[1] == "force":
        force_send_close()
    else:
        main()