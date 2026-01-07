import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import os
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import numpy as np  # 新增：用于OBV中的np.sign

st.set_page_config(page_title="股票監控儀表板", layout="wide")

load_dotenv()
# 异动阈值设定
REFRESH_INTERVAL = 144  # 秒，5 分钟自动刷新

# Gmail 发信者帐号设置
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# ==================== Telegram 設定與函數 (保持不變) ====================
try:
    # 假設 secrets.toml 已經設定
    BOT_TOKEN = st.secrets["telegram"]["BOT_TOKEN"]
    CHAT_ID = st.secrets["telegram"]["CHAT_ID"]
    telegram_ready = True
except Exception:
    BOT_TOKEN = CHAT_ID = None
    telegram_ready = False
    # st.sidebar.error("Telegram 設定錯誤，請檢查 secrets.toml") # 避免過度提醒

def send_telegram_alert(msg: str) -> bool:
    if not (BOT_TOKEN and CHAT_ID):
        return False
    # ... (Telegram 發送邏輯，保持不變)
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        response = requests.get(url, params=payload, timeout=10)
        if response.status_code == 200 and response.json().get("ok"):
            return True
        else:
            # st.warning(f"Telegram API 錯誤: {response.json()}")
            return False
    except Exception as e:
        # st.warning(f"Telegram 發送失敗: {e}")
        return False

# MACD 计算函数
def calculate_macd(data, fast=12, slow=26, signal=9):
    exp1 = data["Close"].ewm(span=fast, adjust=False).mean()
    exp2 = data["Close"].ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram

# RSI 计算函数
def calculate_rsi(data, periods=14):
    delta = data["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# 新增：VWAP 计算函数
def calculate_vwap(data):
    typical_price = (data['High'] + data['Low'] + data['Close']) / 3
    vwap = (typical_price * data['Volume']).cumsum() / data['Volume'].cumsum()
    return vwap

# 新增：MFI 计算函数
def calculate_mfi(data, periods=14):
    typical_price = (data['High'] + data['Low'] + data['Close']) / 3
    money_flow = typical_price * data['Volume']
    positive_flow = money_flow.where(typical_price > typical_price.shift(1), 0).rolling(window=periods).sum()
    negative_flow = money_flow.where(typical_price < typical_price.shift(1), 0).rolling(window=periods).sum()
    money_ratio = positive_flow / negative_flow
    mfi = 100 - (100 / (1 + money_ratio))
    return mfi

# 新增：OBV 计算函数
def calculate_obv(data):
    obv = (np.sign(data['Close'].diff()) * data['Volume']).fillna(0).cumsum()
    return obv

# 新增：VIX 获取函数
def get_vix_data(period, interval):
    vix_ticker = yf.Ticker("^VIX")
    vix_data = vix_ticker.history(period=period, interval=interval).reset_index()
    if "Date" in vix_data.columns:
        vix_data = vix_data.rename(columns={"Date": "Datetime"})
    vix_data["VIX Change %"] = vix_data["Close"].pct_change().round(4) * 100
    return vix_data

# 新增：VIX 趨勢計算（EMA交叉）
def calculate_vix_trend(vix_data, fast=5, slow=10):
    vix_ema_fast = vix_data["Close"].ewm(span=fast, adjust=False).mean()
    vix_ema_slow = vix_data["Close"].ewm(span=slow, adjust=False).mean()
    return vix_ema_fast, vix_ema_slow

# 计算所有信号的成功率
def calculate_signal_success_rate(data):
    data["Next_Close_Higher"] = data["Close"].shift(-1) > data["Close"]
    data["Next_Close_Lower"] = data["Close"].shift(-1) < data["Close"]
    data["Next_High_Higher"] = data["High"].shift(-1) > data["High"]
    data["Next_Low_Lower"] = data["Low"].shift(-1) < data["Low"]
    
    sell_signals = [
        "📉 High<Low", "📉 MACD賣出", "📉 EMA賣出", "📉 價格趨勢賣出", "📉 價格趨勢賣出(量)", 
        "📉 價格趨勢賣出(量%)", "📉 普通跳空(下)", "📉 突破跳空(下)", "📉 持續跳空(下)", 
        "📉 衰竭跳空(下)", "📉 連續向下賣出", "📉 SMA50下降趨勢", "📉 SMA50_200下降趨勢", 
        "📉 新卖出信号", "📉 RSI-MACD Overbought Crossover", "📉 EMA-SMA Downtrend Sell", 
        "📉 Volume-MACD Sell", "📉 EMA10_30賣出", "📉 EMA10_30_40強烈賣出", "📉 看跌吞沒", 
        "📉 上吊線", "📉 黃昏之星", "📉 VWAP賣出", "📉 MFI熊背離賣出", "📉 OBV量能確認賣出",
        "📉 VIX恐慌賣出", "📉 VIX上升趨勢賣出"
    ]
    
    all_signals = set()
    for signals in data["異動標記"].dropna():
        for signal in signals.split(", "):
            if signal:
                all_signals.add(signal)
    
    success_rates = {}
    for signal in all_signals:
        signal_rows = data[data["異動標記"].str.contains(signal, na=False)]
        total_signals = len(signal_rows)
        if total_signals == 0:
            success_rates[signal] = {"success_rate": 0.0, "total_signals": 0, "direction": "up" if signal not in sell_signals else "down"}
        else:
            if signal in sell_signals:
                success_count = (signal_rows["Next_Low_Lower"] & signal_rows["Next_Close_Lower"]).sum() if not signal_rows.empty else 0
                success_rates[signal] = {
                    "success_rate": (success_count / total_signals) * 100,
                    "total_signals": total_signals,
                    "direction": "down"
                }
            else:
                success_count = (signal_rows["Next_High_Higher"] & signal_rows["Next_Close_Higher"]).sum() if not signal_rows.empty else 0
                success_rates[signal] = {
                    "success_rate": (success_count / total_signals) * 100,
                    "total_signals": total_signals,
                    "direction": "up"
                }
    
    return success_rates

# 邮件发送函数（新增参数）
def send_email_alert(ticker, price_pct, volume_pct, low_high_signal=False, high_low_signal=False, 
                     macd_buy_signal=False, macd_sell_signal=False, ema_buy_signal=False, ema_sell_signal=False,
                     price_trend_buy_signal=False, price_trend_sell_signal=False,
                     price_trend_vol_buy_signal=False, price_trend_vol_sell_signal=False,
                     price_trend_vol_pct_buy_signal=False, price_trend_vol_pct_sell_signal=False,
                     gap_common_up=False, gap_common_down=False, gap_breakaway_up=False, gap_breakaway_down=False,
                     gap_runaway_up=False, gap_runaway_down=False, gap_exhaustion_up=False, gap_exhaustion_down=False,
                     continuous_up_buy_signal=False, continuous_down_sell_signal=False,
                     sma50_up_trend=False, sma50_down_trend=False,
                     sma50_200_up_trend=False, sma50_200_down_trend=False,
                     new_buy_signal=False, new_sell_signal=False, new_pivot_signal=False,
                     ema10_30_buy_signal=False, ema10_30_40_strong_buy_signal=False,
                     ema10_30_sell_signal=False, ema10_30_40_strong_sell_signal=False,
                     bullish_engulfing=False, bearish_engulfing=False, hammer=False, hanging_man=False,
                     morning_star=False, evening_star=False,
                     # 新增参数
                     vwap_buy_signal=False, vwap_sell_signal=False,
                     mfi_bull_divergence=False, mfi_bear_divergence=False,
                     obv_breakout_buy=False, obv_breakout_sell=False,
                     # 新增 VIX 参数
                     vix_panic_sell=False, vix_calm_buy=False,
                     # 新增 VIX 趨勢参数
                     vix_uptrend_sell=False, vix_downtrend_buy=False):
    subject = f"📣 股票異動通知：{ticker}"
    body = f"""
    股票代號：{ticker}
    股價變動：{price_pct:.2f}%
    成交量變動：{volume_pct:.2f}%
    """
    if low_high_signal:
        body += f"\n⚠️ 當前最低價高於前一時段最高價！"
    if high_low_signal:
        body += f"\n⚠️ 當前最高價低於前一時段最低價！"
    if macd_buy_signal:
        body += f"\n📈 MACD 買入訊號：MACD 線由負轉正！"
    if macd_sell_signal:
        body += f"\n📉 MACD 賣出訊號：MACD 線由正轉負！"
    if ema_buy_signal:
        body += f"\n📈 EMA 買入訊號：EMA5 上穿 EMA10，成交量放大！"
    if ema_sell_signal:
        body += f"\n📉 EMA 賣出訊號：EMA5 下破 EMA10，成交量放大！"
    if price_trend_buy_signal:
        body += f"\n📈 價格趨勢買入訊號：最高價、最低價、收盤價均上漲！"
    if price_trend_sell_signal:
        body += f"\n📉 價格趨勢賣出訊號：最高價、最低價、收盤價均下跌！"
    if price_trend_vol_buy_signal:
        body += f"\n📈 價格趨勢買入訊號（量）：最高價、最低價、收盤價均上漲且成交量放大！"
    if price_trend_vol_sell_signal:
        body += f"\n📉 價格趨勢賣出訊號（量）：最高價、最低價、收盤價均下跌且成交量放大！"
    if price_trend_vol_pct_buy_signal:
        body += f"\n📈 價格趨勢買入訊號（量%）：最高價、最低價、收盤價均上漲且成交量變化 > 15%！"
    if price_trend_vol_pct_sell_signal:
        body += f"\n📉 價格趨勢賣出訊號（量%）：最高價、最低價、收盤價均下跌且成交量變化 > 15%！"
    if gap_common_up:
        body += f"\n📈 普通跳空(上)：價格向上跳空，未伴隨明顯趨勢或成交量放大！"
    if gap_common_down:
        body += f"\n📉 普通跳空(下)：價格向下跳空，未伴隨明顯趨勢或成交量放大！"
    if gap_breakaway_up:
        body += f"\n📈 突破跳空(上)：價格向上跳空，突破前高且成交量放大！"
    if gap_breakaway_down:
        body += f"\n📉 突破跳空(下)：價格向下跳空，跌破前低且成交量放大！"
    if gap_runaway_up:
        body += f"\n📈 持續跳空(上)：價格向上跳空，處於上漲趨勢且成交量放大！"
    if gap_runaway_down:
        body += f"\n📉 持續跳空(下)：價格向下跳空，處於下跌趨勢且成交量放大！"
    if gap_exhaustion_up:
        body += f"\n📈 衰竭跳空(上)：價格向上跳空，趨勢末端且隨後價格下跌，成交量放大！"
    if gap_exhaustion_down:
        body += f"\n📉 衰竭跳空(下)：價格向下跳空，趨勢末端且隨後價格上漲，成交量放大！"
    if continuous_up_buy_signal:
        body += f"\n📈 連續向上策略買入訊號：至少連續上漲！"
    if continuous_down_sell_signal:
        body += f"\n📉 連續向下策略賣出訊號：至少連續下跌！"
    if sma50_up_trend:
        body += f"\n📈 SMA50 上升趨勢：當前價格高於 SMA50！"
    if sma50_down_trend:
        body += f"\n📉 SMA50 下降趨勢：當前價格低於 SMA50！"
    if sma50_200_up_trend:
        body += f"\n📈 SMA50_200 上升趨勢：當前價格高於 SMA50 且 SMA50 高於 SMA200！"
    if sma50_200_down_trend:
        body += f"\n📉 SMA50_200 下降趨勢：當前價格低於 SMA50 且 SMA50 低於 SMA200！"
    if new_buy_signal:
        body += f"\n📈 新买入信号：今日收盘价大于开盘价且今日开盘价大于前日收盘价！"
    if new_sell_signal:
        body += f"\n📉 新卖出信号：今日收盘价小于开盘价且今日开盘价小于前日收盘价！"
    if new_pivot_signal:
        body += f"\n🔄 新转折点：|Price Change %| > {PRICE_CHANGE_THRESHOLD}% 且 |Volume Change %| > {VOLUME_CHANGE_THRESHOLD}%！"
    if ema10_30_buy_signal:
        body += f"\n📈 EMA10_30 買入訊號：EMA10 上穿 EMA30！"
    if ema10_30_40_strong_buy_signal:
        body += f"\n📈 EMA10_30_40 強烈買入訊號：EMA10 上穿 EMA30 且高於 EMA40！"
    if ema10_30_sell_signal:
        body += f"\n📉 EMA10_30 賣出訊號：EMA10 下破 EMA30！"
    if ema10_30_40_strong_sell_signal:
        body += f"\n📉 EMA10_30_40 強烈賣出訊號：EMA10 下破 EMA30 且低於 EMA40！"
    if bullish_engulfing:
        body += f"\n📈 看漲吞沒形態：當前K線完全包圍前一根看跌K線，成交量放大！"
    if bearish_engulfing:
        body += f"\n📉 看跌吞沒形態：當前K線完全包圍前一根看漲K線，成交量放大！"
    if hammer:
        body += f"\n📈 錘頭線：下影線較長，買方介入，預示反轉！"
    if hanging_man:
        body += f"\n📉 上吊線：下影線較長，賣方介入，預示反轉！"
    if morning_star:
        body += f"\n📈 早晨之星：下跌後出現小實體K線，隨後強烈看漲K線，預示反轉！"
    if evening_star:
        body += f"\n📉 黃昏之星：上漲後出現小實體K線，隨後強烈看跌K線，預示反轉！"
    # 新增：VWAP、MFI、OBV 描述
    if vwap_buy_signal:
        body += f"\n📈 VWAP 買入訊號：價格上穿 VWAP，作為主進場基準！"
    if vwap_sell_signal:
        body += f"\n📉 VWAP 賣出訊號：價格下破 VWAP，作為主出場基準！"
    if mfi_bull_divergence:
        body += f"\n📈 MFI 牛背離買入：價格新低但 MFI 未新低，偵測超賣背離！"
    if mfi_bear_divergence:
        body += f"\n📉 MFI 熊背離賣出：價格新高但 MFI 未新高，偵測超買背離！"
    if obv_breakout_buy:
        body += f"\n📈 OBV 突破買入：OBV 新高確認價格上漲量能！"
    if obv_breakout_sell:
        body += f"\n📉 OBV 突破賣出：OBV 新低確認價格下跌量能！"
    # 新增：VIX 描述
    if vix_panic_sell:
        body += f"\n📉 VIX 恐慌賣出訊號：VIX > 30 且上升，市場恐慌加劇！"
    if vix_calm_buy:
        body += f"\n📈 VIX 平靜買入訊號：VIX < 20 且下降，市場穩定！"
    # 新增：VIX 趨勢描述
    if vix_uptrend_sell:
        body += f"\n📉 VIX 上升趨勢賣出訊號：VIX EMA5 上穿 EMA10，恐慌增加，建議減持！"
    if vix_downtrend_buy:
        body += f"\n📈 VIX 下降趨勢買入訊號：VIX EMA5 下破 EMA10，市場平靜，適合進場！"
    
    body += "\n系統偵測到異常變動，請立即查看市場情況。"
    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        server.quit()
        st.toast(f"📬 Email 已發送給 {RECIPIENT_EMAIL}")
    except Exception as e:
        st.error(f"Email 發送失敗：{e}")

# UI 设定
period_options = ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]
interval_options = ["1m", "5m", "2m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"]
percentile_options = [1, 5, 10, 20]
refresh_options = [30, 60, 90, 144, 150, 180, 210, 240, 270, 300]

st.title("📊 股票監控儀表板（含異動提醒與 Email 通知 ✅）")
input_tickers = st.text_input("請輸入股票代號（逗號分隔）", value="TSLA, NIO, TSLL, XPEV, META, GOOGL, AAPL, NVDA, AMZN, MSFT, TSM")
selected_tickers = [t.strip().upper() for t in input_tickers.split(",") if t.strip()]
selected_period = st.selectbox("選擇時間範圍", period_options, index=5)
selected_interval = st.selectbox("選擇資料間隔", interval_options, index=8)
HIGH_N_HIGH_THRESHOLD = st.number_input("Close to high", min_value=0.1, max_value=1.0, value=0.9, step=0.1)
LOW_N_LOW_THRESHOLD = st.number_input("Close to low", min_value=0.1, max_value=1.0, value=0.9, step=0.1)
PRICE_THRESHOLD = st.number_input("價格異動閾值 (%)", min_value=0.1, max_value=200.0, value=80.0, step=0.1)
VOLUME_THRESHOLD = st.number_input("成交量異動閾值 (%)", min_value=0.1, max_value=200.0, value=80.0, step=0.1)
PRICE_CHANGE_THRESHOLD = st.number_input("新转折点 Price Change % 阈值 (%)", min_value=0.1, max_value=200.0, value=5.0, step=0.1)
VOLUME_CHANGE_THRESHOLD = st.number_input("新转折点 Volume Change % 阈值 (%)", min_value=0.1, max_value=200.0, value=10.0, step=0.1)
GAP_THRESHOLD = st.number_input("跳空幅度閾值 (%)", min_value=0.1, max_value=50.0, value=1.0, step=0.1)
CONTINUOUS_UP_THRESHOLD = st.number_input("連續上漲閾值 (根K線)", min_value=1, max_value=20, value=3, step=1)
CONTINUOUS_DOWN_THRESHOLD = st.number_input("連續下跌閾值 (根K線)", min_value=1, max_value=20, value=3, step=1)
PERCENTILE_THRESHOLD = st.selectbox("選擇 Price Change %、Volume Change %、Volume、股價漲跌幅 (%)、成交量變動幅 (%) 數據範圍 (%)", percentile_options, index=1)
REFRESH_INTERVAL = st.selectbox("选择刷新间隔 (秒)", refresh_options, index=refresh_options.index(300))
time.sleep(2)
#
all_signal_types = [
    "📉 High<Low", "📉 MACD賣出", "📉 EMA賣出", "📉 價格趨勢賣出", "📉 價格趨勢賣出(量)", 
        "📉 價格趨勢賣出(量%)", "📉 普通跳空(下)", "📉 突破跳空(下)", "📉 持續跳空(下)", 
        "📉 衰竭跳空(下)", "📉 連續向下賣出", "📉 SMA50下降趨勢", "📉 SMA50_200下降趨勢", 
        "📉 新卖出信号", "📉 RSI-MACD Overbought Crossover", "📉 EMA-SMA Downtrend Sell", 
        "📉 Volume-MACD Sell", "📉 EMA10_30賣出", "📉 EMA10_30_40強烈賣出", "📉 看跌吞沒", "📉 烏雲蓋頂",
        "📉 上吊線", "📉 黃昏之星","📈 Low>High", "📈 MACD買入", "📈 EMA買入", "📈 價格趨勢買入", "📈 價格趨勢買入(量)", 
        "📈 價格趨勢買入(量%)", "📈 普通跳空(上)", "📈 突破跳空(上)", "📈 持續跳空(上)", 
        "📈 衰竭跳空(上)", "📈 連續向上買入", "📈 SMA50上升趨勢", "📈 SMA50_200上升趨勢", 
        "📈 新买入信号", "📈 RSI-MACD Oversold Crossover", "📈 EMA-SMA Uptrend Buy", 
        "📈 Volume-MACD Buy", "📈 EMA10_30買入", "📈 EMA10_30_40強烈買入", "📈 看漲吞沒", 
        "📈 錘頭線", "📈 早晨之星","✅ 量價","🔄 新转折点",
        # 新增：VWAP、MFI、OBV 信号
        "📈 VWAP買入", "📉 VWAP賣出", "📈 MFI牛背離買入", "📉 MFI熊背離賣出", "📈 OBV突破買入", "📉 OBV突破賣出",
        # 新增：VIX 信号
        "📉 VIX恐慌賣出", "📈 VIX平靜買入",
        # 新增：VIX 趨勢信號
        "📉 VIX上升趨勢賣出", "📈 VIX下降趨勢買入"
    # ...其他K栏位信号. 注意不要遗漏你的所有信号
]

selected_signals = st.multiselect(
    "选择哪些信号需要推送Telegram",
    all_signal_types,
    default=["📈 新买入信号"]
)


# 新增：K线形态阈值调整（动态阈值优化）
BODY_RATIO_THRESHOLD = st.number_input("K線實體占比閾值 (大陽/大陰線)", min_value=0.1, max_value=0.9, value=0.6, step=0.05)
SHADOW_RATIO_THRESHOLD = st.number_input("K線影線長度閾值 (錘子/射擊線)", min_value=0.1, max_value=3.0, value=2.0, step=0.1)
DOJI_BODY_THRESHOLD = st.number_input("十字星實體閾值占比", min_value=0.01, max_value=0.2, value=0.1, step=0.01)

# 新增：MFI背离窗口（最小改动，添加一个input）
MFI_DIVERGENCE_WINDOW = st.number_input("MFI背离检测窗口 (根K線)", min_value=3, max_value=20, value=5, step=1)

# 新增：VIX 阈值
VIX_HIGH_THRESHOLD = st.number_input("VIX 恐慌閾值 (高)", min_value=20.0, max_value=50.0, value=30.0, step=1.0)
VIX_LOW_THRESHOLD = st.number_input("VIX 平靜閾值 (低)", min_value=10.0, max_value=25.0, value=20.0, step=1.0)

# 新增：VIX EMA 期數（趨勢信號）
VIX_EMA_FAST = st.number_input("VIX 快速 EMA 期數", min_value=3, max_value=15, value=5, step=1)
VIX_EMA_SLOW = st.number_input("VIX 慢速 EMA 期數", min_value=8, max_value=25, value=10, step=1)

# 新增：Telegram 觸發條件表格（可編輯）
st.subheader("📋 Telegram 觸發條件配置（可隨時編輯）")
default_telegram_conditions = pd.DataFrame({
    "排名": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    "異動標記": [
        "📈 價格趨勢買入, 📈 持續跳空(上), 📈 SMA50上升趨勢, 📈 SMA50_200上升趨勢, 📈 EMA-SMA Uptrend Buy, 📈 OBV突破買入",
        "📈 Low>High, 📈 價格趨勢買入, 📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy",
        "📈 價格趨勢買入, 📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy, 📈 VIX平靜買入",
        "📈 連續向上買入, 📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy",
        "📈 突破跳空(上), 📈 SMA50上升趨勢, 📈 新买入信号, 📈 EMA-SMA Uptrend Buy",
        "📈 普通跳空(上), 📈 連續向上買入, 📉 SMA50下降趨勢, 📈 新买入信号",
        "📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy, 📈 OBV突破買入",
        "📉 普通跳空(下), 📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy",
        "📈 價格趨勢買入(量), 📈 價格趨勢買入(量%), 📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy",
        "📈 EMA買入, 📈 連續向上買入, 📈 SMA50上升趨勢, 📈 EMA-SMA Uptrend Buy"
    ],
    "成交量標記": ["放量", "縮量", "放量", "縮量", "放量", "縮量", "放量", "縮量", "放量", "縮量"],
    "K線形態": ["大陽線", "普通K線", "普通K線", "大陽線", "射擊之星", "普通K線", "十字星", "大陽線", "早晨之星", "看漲吞沒"]
})
default_telegram_conditions["排名"] = default_telegram_conditions["排名"].astype("string")
telegram_conditions = st.data_editor(
    default_telegram_conditions,
    num_rows="dynamic",
    column_config={
        "排名": st.column_config.TextColumn("排名"),
        "異動標記": st.column_config.TextColumn("異動標記", help="輸入多個信號，用逗號分隔"),
        "成交量標記": st.column_config.SelectboxColumn("成交量標記", options=["放量", "縮量"]),
        "K線形態": st.column_config.TextColumn("K線形態", help="輸入K線形態名稱")
    },
    use_container_width=True,
    hide_index=False
)

placeholder = st.empty()

@st.cache_data(ttl=300)  # 性能优化：缓存K线形态计算结果，TTL=5分钟
def compute_kline_patterns(data, body_ratio_threshold, shadow_ratio_threshold, doji_body_threshold):
    """缓存K线形态计算"""
    data = data.copy()
    data["成交量標記"] = data.apply(
        lambda row: "放量" if row["Volume"] > row["前5均量"] else "縮量", axis=1
    )
    
    def identify_candlestick_pattern(row, index, data):
        pattern = "普通K線"
        interpretation = "波動有限，方向不明顯"
        if index > 0:
            prev_close = data["Close"].iloc[index-1]
            prev_open = data["Open"].iloc[index-1]
            prev_high = data["High"].iloc[index-1]
            prev_low = data["Low"].iloc[index-1]
            curr_open = row["Open"]
            curr_close = row["Close"]
            curr_high = row["High"]
            curr_low = row["Low"]
            body_size = abs(curr_close - curr_open)
            candle_range = curr_high - curr_low
            prev_body_size = abs(prev_close - prev_open)
            is_uptrend = data["Close"].iloc[max(0, index-5):index].mean() < curr_close if index >= 5 else False
            is_downtrend = data["Close"].iloc[max(0, index-5):index].mean() > curr_close if index >= 5 else False
            is_high_volume = row["Volume"] > row["前5均量"]

            # 锤子线
            if (body_size < candle_range * 0.3 and
                (min(curr_open, curr_close) - curr_low) >= shadow_ratio_threshold * body_size and
                (curr_high - max(curr_open, curr_close)) < (min(curr_open, curr_close) - curr_low) and
                is_downtrend):
                pattern = "錘子線"
                interpretation = "下方出現支撐，空方雖打壓但多方承接" + ("，放量增強買入信號" if is_high_volume else "")

            # 射击之星
            elif (body_size < candle_range * 0.3 and
                  (curr_high - max(curr_open, curr_close)) >= shadow_ratio_threshold * body_size and
                  (min(curr_open, curr_close) - curr_low) < (curr_high - max(curr_open, curr_close)) and
                  is_uptrend):
                pattern = "射擊之星"
                interpretation = "高位拋壓沉重，短期見頂風險" + ("，放量增強賣出信號" if is_high_volume else "")

            # 十字星
            elif body_size < doji_body_threshold * candle_range:
                pattern = "十字星"
                interpretation = "市場猶豫，方向未明確"

            # 大阳线
            elif (curr_close > curr_open and
                  body_size > body_ratio_threshold * candle_range):
                pattern = "大陽線"
                interpretation = "多方強勢推升" + ("，放量更有力" if is_high_volume else "")

            # 大阴线
            elif (curr_close < curr_open and
                  body_size > body_ratio_threshold * candle_range):
                pattern = "大陰線"
                interpretation = "空方強勢壓制" + ("，放量更偏空" if is_high_volume else "")

            # 看涨吞噬
            elif (curr_close > curr_open and
                  prev_close < prev_open and
                  curr_open < prev_close and
                  curr_close > prev_open and
                  is_high_volume):
                pattern = "看漲吞噬"
                interpretation = "當前陽線完全包覆前日陰線，買方強勢反攻，預示反轉"

            # 看跌吞噬
            elif (curr_close < curr_open and
                  prev_close > prev_open and
                  curr_open > prev_close and
                  curr_close < prev_open and
                  is_high_volume):
                pattern = "看跌吞噬"
                interpretation = "當前陰線完全包覆前日陽線，賣方強勢壓制，預示反轉"

            # 乌云盖顶
            elif (is_uptrend and
                  curr_close < curr_open and
                  prev_close > prev_open and
                  curr_open > prev_close and
                  curr_close < (prev_open + prev_close) / 2):
                pattern = "烏雲蓋頂"
                interpretation = "上升趨勢中陰線壓制，賣壓加重，短期可能下跌"

            # 刺透形态
            elif (is_downtrend and
                  curr_close > curr_open and
                  prev_close < prev_open and
                  curr_open < prev_close and
                  curr_close > (prev_open + prev_close) / 2):
                pattern = "刺透形態"
                interpretation = "下跌趨勢中陽線反攻，買方介入，短期可能上漲"

            # 新增：早晨之星（扩展形态）
            elif (index > 1 and
                  data["Close"].iloc[index-2] < data["Open"].iloc[index-2] and  # 第一根阴线
                  abs(data["Close"].iloc[index-1] - data["Open"].iloc[index-1]) < 0.3 * abs(data["Close"].iloc[index-2] - data["Open"].iloc[index-2]) and  # 第二根小实体
                  curr_close > curr_open and  # 第三根阳线
                  curr_close > (prev_open + prev_close) / 2 and  # 收盘高于前日中点
                  is_high_volume):
                pattern = "早晨之星"
                interpretation = "下跌後小實體K線後強陽線，預示反轉，多方力量增強"

            # 新增：黃昏之星（扩展形态）
            elif (index > 1 and
                  data["Close"].iloc[index-2] > data["Open"].iloc[index-2] and  # 第一根阳线
                  abs(data["Close"].iloc[index-1] - data["Open"].iloc[index-1]) < 0.3 * abs(data["Close"].iloc[index-2] - data["Open"].iloc[index-2]) and  # 第二根小实体
                  curr_close < curr_open and  # 第三根阴线
                  curr_close < (prev_open + prev_close) / 2 and  # 收盘低于前日中点
                  is_high_volume):
                pattern = "黃昏之星"
                interpretation = "上漲後小實體K線後強陰線，預示反轉，空方力量增強"

        return pattern, interpretation

    data[["K線形態", "單根解讀"]] = [
        identify_candlestick_pattern(row, i, data) for i, row in data.iterrows()
    ]
    return data

while True:
    with placeholder.container():
        st.subheader(f"⏱ 更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        for ticker in selected_tickers:
            try:
                stock = yf.Ticker(ticker)
                data = stock.history(period=selected_period, interval=selected_interval).reset_index()

                if data.empty or len(data) < 2:
                    st.warning(f"⚠️ {ticker} 無數據或數據不足（期間：{selected_period}，間隔：{selected_interval}），請嘗試其他時間範圍或間隔")
                    continue

                if "Date" in data.columns:
                    data = data.rename(columns={"Date": "Datetime"})
                elif "Datetime" not in data.columns:
                    st.warning(f"⚠️ {ticker} 數據缺少時間列，無法處理")
                    continue

                data["Price Change %"] = data["Close"].pct_change().round(4) * 100
                data["Volume Change %"] = data["Volume"].pct_change().round(4) * 100
                data["Close_Difference"] = data['Close'].diff().round(2)
                data["High_Difference"] = data['High'].diff().round(2)
                data["Low_Difference"] = data['Low'].diff().round(2)
                data["Close_N_High"] = (data['Close']-data['Low'])/(data['High']-data['Low'])
                data["HIGH_High_%"] = ((data["High"].shift(-1)-data["High"])/data["High"])*100
                data["Close_N_Low"] = (data['High']-data['Close'])/(data['High']-data['Low'])
                
                data["前5均價"] = data["Price Change %"].rolling(window=5).mean()
                data["前5均價ABS"] = abs(data["Price Change %"]).rolling(window=5).mean()
                data["前5均量"] = data["Volume"].rolling(window=5).mean()
                data["📈 股價漲跌幅 (%)"] = ((abs(data["Price Change %"]) - data["前5均價ABS"]) / data["前5均價ABS"]).round(4) * 100
                data["📊 成交量變動幅 (%)"] = ((data["Volume"] - data["前5均量"]) / data["前5均量"]).round(4) * 100

                data["MACD"], data["Signal"], data["Histogram"] = calculate_macd(data)
                data["EMA5"] = data["Close"].ewm(span=5, adjust=False).mean()
                data["EMA10"] = data["Close"].ewm(span=10, adjust=False).mean()
                data["EMA30"] = data["Close"].ewm(span=30, adjust=False).mean()
                data["EMA40"] = data["Close"].ewm(span=40, adjust=False).mean()
                data["RSI"] = calculate_rsi(data)
                
                # 新增：计算 VWAP、MFI、OBV
                data["VWAP"] = calculate_vwap(data)
                data["MFI"] = calculate_mfi(data)
                data["OBV"] = calculate_obv(data)
                
                # 新增：获取 VIX 数据并合并
                vix_data = get_vix_data(selected_period, selected_interval)
                if not vix_data.empty:
                    data = data.merge(vix_data[["Datetime", "Close", "VIX Change %"]], on="Datetime", how="left", suffixes=("", "_VIX"))
                    data.rename(columns={"Close_VIX": "VIX"}, inplace=True)
                else:
                    data["VIX"] = np.nan
                    data["VIX Change %"] = np.nan
                
                # 新增：計算 VIX 趨勢 EMA
                if not data["VIX"].isna().all():
                    data["VIX_EMA_Fast"], data["VIX_EMA_Slow"] = calculate_vix_trend(data, VIX_EMA_FAST, VIX_EMA_SLOW)
                else:
                    data["VIX_EMA_Fast"] = np.nan
                    data["VIX_EMA_Slow"] = np.nan
                
                data['Up'] = (data['Close'] > data['Close'].shift(1)).astype(int)
                data['Down'] = (data['Close'] < data['Close'].shift(1)).astype(int)
                data['Continuous_Up'] = data['Up'] * (data['Up'].groupby((data['Up'] == 0).cumsum()).cumcount() + 1)
                data['Continuous_Down'] = data['Down'] * (data['Down'].groupby((data['Down'] == 0).cumsum()).cumcount() + 1)
                
                data["SMA50"] = data["Close"].rolling(window=50).mean()
                data["SMA200"] = data["Close"].rolling(window=200).mean()
                
                # 新增：MFI背离检测（预计算列）
                window = MFI_DIVERGENCE_WINDOW
                data['Close_Roll_Max'] = data['Close'].rolling(window=window).max()
                data['MFI_Roll_Max'] = data['MFI'].rolling(window=window).max()
                data['Close_Roll_Min'] = data['Close'].rolling(window=window).min()
                data['MFI_Roll_Min'] = data['MFI'].rolling(window=window).min()
                data['MFI_Bear_Div'] = (data['Close'] == data['Close_Roll_Max']) & (data['MFI'] < data['MFI_Roll_Max'].shift(1))
                data['MFI_Bull_Div'] = (data['Close'] == data['Close_Roll_Min']) & (data['MFI'] > data['MFI_Roll_Min'].shift(1))
                data["High_Max"] = data["High"].rolling(window=window).max()
                data["Low_Min"] = data["Low"].rolling(window=window).min()
                # 新增：OBV突破（预计算，20期滚动新高/新低）
                data['OBV_Roll_Max'] = data['OBV'].rolling(window=20).max()
                data['OBV_Roll_Min'] = data['OBV'].rolling(window=20).min()
                
                def mark_signal(row, index):
                    signals = []
                    if abs(row["📈 股價漲跌幅 (%)"]) >= PRICE_THRESHOLD and abs(row["📊 成交量變動幅 (%)"]) >= VOLUME_THRESHOLD:
                        signals.append("✅ 量價")
                    if index > 0 and row["Low"] > data["High"].iloc[index-1]:
                        signals.append("📈 Low>High")
                    if index > 0 and row["High"] < data["Low"].iloc[index-1]:
                        signals.append("📉 High<Low")

                    if index > 0 and row["Close_N_High"] >=HIGH_N_HIGH_THRESHOLD:
                        signals.append("📈 HIGH_N_HIGH")
                        
                    if index > 0 and row["Close_N_Low"] >= LOW_N_LOW_THRESHOLD:
                        signals.append("📉 LOW_N_LOW")
                        
                    if index > 0 and row["MACD"] > 0 and data["MACD"].iloc[index-1] <= 0 and row["RSI"] < 50:
                        signals.append("📈 MACD買入")
                    if index > 0 and row["MACD"] <= 0 and data["MACD"].iloc[index-1] > 0 and row["RSI"] > 50:
                        signals.append("📉 MACD賣出")
                    if (index > 0 and row["EMA5"] > row["EMA10"] and 
                        data["EMA5"].iloc[index-1] <= data["EMA10"].iloc[index-1] and 
                        row["Volume"] > data["Volume"].iloc[index-1] and row["RSI"] < 50):
                        signals.append("📈 EMA買入")
                    if (index > 0 and row["EMA5"] < row["EMA10"] and 
                        data["EMA5"].iloc[index-1] >= data["EMA10"].iloc[index-1] and 
                        row["Volume"] > data["Volume"].iloc[index-1] and row["RSI"] > 50):
                        signals.append("📉 EMA賣出")
                    if (index > 0 and row["High"] > data["High"].iloc[index-1] and 
                        row["Low"] > data["Low"].iloc[index-1] and 
                        row["Close"] > data["Close"].iloc[index-1] and row["MACD"] > 0):
                        signals.append("📈 價格趨勢買入")
                    if (index > 0 and row["High"] < data["High"].iloc[index-1] and 
                        row["Low"] < data["Low"].iloc[index-1] and 
                        row["Close"] < data["Close"].iloc[index-1] and row["MACD"] < 0):
                        signals.append("📉 價格趨勢賣出")
                    if (index > 0 and row["High"] > data["High"].iloc[index-1] and 
                        row["Low"] > data["Low"].iloc[index-1] and 
                        row["Close"] > data["Close"].iloc[index-1] and 
                        row["Volume"] > data["前5均量"].iloc[index] and row["RSI"] < 50):
                        signals.append("📈 價格趨勢買入(量)")
                    if (index > 0 and row["High"] < data["High"].iloc[index-1] and 
                        row["Low"] < data["Low"].iloc[index-1] and 
                        row["Close"] < data["Close"].iloc[index-1] and 
                        row["Volume"] > data["前5均量"].iloc[index] and row["RSI"] > 50):
                        signals.append("📉 價格趨勢賣出(量)")
                    if (index > 0 and row["High"] > data["High"].iloc[index-1] and 
                        row["Low"] > data["Low"].iloc[index-1] and 
                        row["Close"] > data["Close"].iloc[index-1] and 
                        row["Volume Change %"] > 15 and row["RSI"] < 50):
                        signals.append("📈 價格趨勢買入(量%)")
                    if (index > 0 and row["High"] < data["High"].iloc[index-1] and 
                        row["Low"] < data["Low"].iloc[index-1] and 
                        row["Close"] < data["Close"].iloc[index-1] and 
                        row["Volume Change %"] > 15 and row["RSI"] > 50):
                        signals.append("📉 價格趨勢賣出(量%)")
                    if index > 0:
                        gap_pct = ((row["Open"] - data["Close"].iloc[index-1]) / data["Close"].iloc[index-1]) * 100
                        is_up_gap = gap_pct > GAP_THRESHOLD
                        is_down_gap = gap_pct < -GAP_THRESHOLD
                        if is_up_gap or is_down_gap:
                            trend = data["Close"].iloc[index-5:index].mean() if index >= 5 else 0
                            prev_trend = data["Close"].iloc[index-6:index-1].mean() if index >= 6 else trend
                            is_up_trend = row["Close"] > trend and trend > prev_trend
                            is_down_trend = row["Close"] < trend and trend < prev_trend
                            is_high_volume = row["Volume"] > data["前5均量"].iloc[index]
                            is_price_reversal = (index < len(data) - 1 and
                                                ((is_up_gap and data["Close"].iloc[index+1] < row["Close"]) or
                                                 (is_down_gap and data["Close"].iloc[index+1] > row["Close"])))
                            if is_up_gap:
                                if is_price_reversal and is_high_volume:
                                    signals.append("📈 衰竭跳空(上)")
                                elif is_up_trend and is_high_volume:
                                    signals.append("📈 持續跳空(上)")
                                elif row["High"] > data["High"].iloc[index-1:index].max() and is_high_volume:
                                    signals.append("📈 突破跳空(上)")
                                else:
                                    signals.append("📈 普通跳空(上)")
                            elif is_down_gap:
                                if is_price_reversal and is_high_volume:
                                    signals.append("📉 衰竭跳空(下)")
                                elif is_down_trend and is_high_volume:
                                    signals.append("📉 持續跳空(下)")
                                elif row["Low"] < data["Low"].iloc[index-1:index].min() and is_high_volume:
                                    signals.append("📉 突破跳空(下)")
                                else:
                                    signals.append("📉 普通跳空(下)")
                    if row['Continuous_Up'] >= CONTINUOUS_UP_THRESHOLD and row["RSI"] < 70:
                        signals.append("📈 連續向上買入")
                    if row['Continuous_Down'] >= CONTINUOUS_DOWN_THRESHOLD and row["RSI"] > 30:
                        signals.append("📉 連續向下賣出")
                    if pd.notna(row["SMA50"]):
                        if row["Close"] > row["SMA50"] and row["MACD"] > 0:
                            signals.append("📈 SMA50上升趨勢")
                        elif row["Close"] < row["SMA50"] and row["MACD"] < 0:
                            signals.append("📉 SMA50下降趨勢")
                    if pd.notna(row["SMA50"]) and pd.notna(row["SMA200"]):
                        if row["Close"] > row["SMA50"] and row["SMA50"] > row["SMA200"] and row["MACD"] > 0:
                            signals.append("📈 SMA50_200上升趨勢")
                        elif row["Close"] < row["SMA50"] and row["SMA50"] < row["SMA200"] and row["MACD"] < 0:
                            signals.append("📉 SMA50_200下降趨勢")
                    if index > 0 and row["Close"] > row["Open"] and row["Open"] > data["Close"].iloc[index-1] and row["RSI"] < 70:
                        signals.append("📈 新买入信号")
                    if index > 0 and row["Close"] < row["Open"] and row["Open"] < data["Close"].iloc[index-1] and row["RSI"] > 30:
                        signals.append("📉 新卖出信号")
                    if index > 0 and abs(row["Price Change %"]) > PRICE_CHANGE_THRESHOLD and abs(row["Volume Change %"]) > VOLUME_CHANGE_THRESHOLD and row["MACD"] > row["Signal"]:
                        signals.append("🔄 新转折点")
                    if len(signals) > 8:
                        signals.append(f"🔥 关键转折点 (信号数: {len(signals)})")
                    if index > 0 and row["RSI"] < 30 and row["MACD"] > 0 and data["MACD"].iloc[index-1] <= 0:
                        signals.append("📈 RSI-MACD Oversold Crossover")
                    if index > 0 and row["EMA5"] > row["EMA10"] and row["Close"] > row["SMA50"]:
                        signals.append("📈 EMA-SMA Uptrend Buy")
                    if index > 0 and row["Volume"] > data["前5均量"].iloc[index] and row["MACD"] > 0 and data["MACD"].iloc[index-1] <= 0:
                        signals.append("📈 Volume-MACD Buy")
                    if index > 0 and row["RSI"] > 70 and row["MACD"] < 0 and data["MACD"].iloc[index-1] >= 0:
                        signals.append("📉 RSI-MACD Overbought Crossover")
                    if index > 0 and row["EMA5"] < row["EMA10"] and row["Close"] < row["SMA50"]:
                        signals.append("📉 EMA-SMA Downtrend Sell")
                    if index > 0 and row["Volume"] > data["前5均量"].iloc[index] and row["MACD"] < 0 and data["MACD"].iloc[index-1] >= 0:
                        signals.append("📉 Volume-MACD Sell")
                    if (index > 0 and row["EMA10"] > row["EMA30"] and 
                        data["EMA10"].iloc[index-1] <= data["EMA30"].iloc[index-1]):
                        signals.append("📈 EMA10_30買入")
                    if (index > 0 and row["EMA10"] > row["EMA30"] and 
                        data["EMA10"].iloc[index-1] <= data["EMA30"].iloc[index-1] and 
                        row["EMA10"] > row["EMA40"]):
                        signals.append("📈 EMA10_30_40強烈買入")
                    if (index > 0 and row["EMA10"] < row["EMA30"] and 
                        data["EMA10"].iloc[index-1] >= data["EMA30"].iloc[index-1]):
                        signals.append("📉 EMA10_30賣出")
                    if (index > 0 and row["EMA10"] < row["EMA30"] and 
                        data["EMA10"].iloc[index-1] >= data["EMA30"].iloc[index-1] and 
                        row["EMA10"] < row["EMA40"]):
                        signals.append("📉 EMA10_30_40強烈賣出")
                    if (index > 0 and 
                        data["Close"].iloc[index-1] < data["Open"].iloc[index-1] and 
                        row["Close"] > row["Open"] and 
                        row["Open"] < data["Close"].iloc[index-1] and 
                        row["Close"] > data["Open"].iloc[index-1] and 
                        row["Volume"] > data["前5均量"].iloc[index] and 
                        row["RSI"] < 50):
                        signals.append("📈 看漲吞沒")
                    if (index > 0 and 
                        data["Close"].iloc[index-1] > data["Open"].iloc[index-1] and 
                        row["Close"] < row["Open"] and 
                        row["Open"] > data["Close"].iloc[index-1] and 
                        row["Close"] < data["Open"].iloc[index-1] and 
                        row["Volume"] > data["前5均量"].iloc[index] and 
                        row["RSI"] > 50):
                        signals.append("📉 看跌吞沒")
                    if (index > 0 and 
                        row["Close"] > data["Close"].iloc[index-1] and
                        abs(row["Close"] - row["Open"]) < (row["High"] - row["Low"]) * 0.3 and 
                        (min(row["Open"], row["Close"]) - row["Low"]) >= 2 * abs(row["Close"] - row["Open"]) and 
                        (row["High"] - max(row["Open"], row["Close"])) < (min(row["Open"], row["Close"]) - row["Low"]) and 
                        row["Volume"] > data["前5均量"].iloc[index] and 
                        row["RSI"] < 50):
                        signals.append("📈 錘頭線")
                    if (index > 0 and 
                        row["Close"] < data["Close"].iloc[index-1] and
                        abs(row["Close"] - row["Open"]) < (row["High"] - row["Low"]) * 0.3 and 
                        (min(row["Open"], row["Close"]) - row["Low"]) >= 2 * abs(row["Close"] - row["Open"]) and 
                        (row["High"] - max(row["Open"], row["Close"])) < (min(row["Open"], row["Close"]) - row["Low"]) and 
                        row["Volume"] > data["前5均量"].iloc[index] and 
                        row["RSI"] > 50):
                        signals.append("📉 上吊線")
                    if (index > 1 and 
                        data["Close"].iloc[index-2] < data["Open"].iloc[index-2] and
                        abs(data["Close"].iloc[index-1] - data["Open"].iloc[index-1]) < 0.3 * abs(data["Close"].iloc[index-2] - data["Open"].iloc[index-2]) and
                        row["Close"] > row["Open"] and
                        row["Close"] > (data["Open"].iloc[index-2] + data["Close"].iloc[index-2]) / 2 and
                        row["Volume"] > data["前5均量"].iloc[index] and 
                        row["RSI"] < 50):
                        signals.append("📈 早晨之星")
                    if (index > 1 and 
                        data["Close"].iloc[index-2] > data["Open"].iloc[index-2] and
                        abs(data["Close"].iloc[index-1] - data["Open"].iloc[index-1]) < 0.3 * abs(data["Close"].iloc[index-2] - data["Open"].iloc[index-2]) and
                        row["Close"] < row["Open"] and
                        row["Close"] < (data["Open"].iloc[index-2] + data["Close"].iloc[index-2]) / 2 and
                        row["Volume"] > data["前5均量"].iloc[index] and 
                        row["RSI"] > 50):
                        signals.append("📉 黃昏之星")

                    if index > 0 and row["High"] > data['High_Max'].iloc[index-1]:
                        signals.append("📈 BreakOut_5K")
                        
                    if index > 0 and row["Low"] < data['Low_Min'].iloc[index-1]:
                        signals.append("📉 BreakDown_5K")
                        
                        
                    # 新增：烏雲蓋頂
                    if (index > 0 and 
                        data["Close"].iloc[index-1] > data["Open"].iloc[index-1] and  # 前一日陽線
                        row["Open"] > data["Close"].iloc[index-1] and  # 當前開盤高於前日收盤
                        row["Close"] < row["Open"] and  # 當前為陰線
                        row["Close"] < (data["Open"].iloc[index-1] + data["Close"].iloc[index-1]) / 2 and  # 收盤低於前日K線中點
                        row["Volume"] > data["前5均量"].iloc[index]):  # 成交量放大
                        signals.append("📉 烏雲蓋頂")
                    # 新增：刺透形態
                    if (index > 0 and 
                        data["Close"].iloc[index-1] < data["Open"].iloc[index-1] and  # 前一日陰線
                        row["Open"] < data["Close"].iloc[index-1] and  # 當前開盤低於前日收盤
                        row["Close"] > row["Open"] and  # 當前為陽線
                        row["Close"] > (data["Open"].iloc[index-1] + data["Close"].iloc[index-1]) / 2 and  # 收盤高於前日K線中點
                        row["Volume"] > data["前5均量"].iloc[index]):  # 成交量放大
                        signals.append("📈 刺透形態")
                    # 新增：VWAP信号（作为主进出场基准）
                    if index > 0 and pd.notna(row["VWAP"]):
                        if row["Close"] > row["VWAP"] and data["Close"].iloc[index-1] <= data["VWAP"].iloc[index-1]:
                            signals.append("📈 VWAP買入")
                        elif row["Close"] < row["VWAP"] and data["Close"].iloc[index-1] >= data["VWAP"].iloc[index-1]:
                            signals.append("📉 VWAP賣出")
                    # 新增：MFI背离信号
                    if index >= MFI_DIVERGENCE_WINDOW and pd.notna(row["MFI"]):
                        if data['MFI_Bull_Div'].iloc[index]:
                            signals.append("📈 MFI牛背離買入")
                        if data['MFI_Bear_Div'].iloc[index]:
                            signals.append("📉 MFI熊背離賣出")
                    # 新增：OBV突破信号（确认突破量能）
                    if index > 0 and pd.notna(row["OBV"]):
                        if row["Close"] > data["Close"].iloc[index-1] and row["OBV"] > data['OBV_Roll_Max'].iloc[index-1]:
                            signals.append("📈 OBV突破買入")
                        elif row["Close"] < data["Close"].iloc[index-1] and row["OBV"] < data['OBV_Roll_Min'].iloc[index-1]:
                            signals.append("📉 OBV突破賣出")
                    # 新增：VIX 恐慌指数信号
                    if index > 0 and pd.notna(row["VIX"]):
                        vix_prev = data["VIX"].iloc[index-1]
                        if row["VIX"] > VIX_HIGH_THRESHOLD and row["VIX"] > vix_prev:
                            signals.append("📉 VIX恐慌賣出")
                        elif row["VIX"] < VIX_LOW_THRESHOLD and row["VIX"] < vix_prev:
                            signals.append("📈 VIX平靜買入")
                    # 新增：VIX 趨勢信號（EMA交叉）
                    if index > 0 and pd.notna(row["VIX_EMA_Fast"]) and pd.notna(row["VIX_EMA_Slow"]):
                        if row["VIX_EMA_Fast"] > row["VIX_EMA_Slow"] and data["VIX_EMA_Fast"].iloc[index-1] <= data["VIX_EMA_Slow"].iloc[index-1]:
                            signals.append("📉 VIX上升趨勢賣出")
                        elif row["VIX_EMA_Fast"] < row["VIX_EMA_Slow"] and data["VIX_EMA_Fast"].iloc[index-1] >= data["VIX_EMA_Slow"].iloc[index-1]:
                            signals.append("📈 VIX下降趨勢買入")
                    return ", ".join(signals) if signals else ""
                
                data["異動標記"] = [mark_signal(row, i) for i, row in data.iterrows()]

                # 性能优化：使用缓存函数计算K线形态
                data = compute_kline_patterns(data, BODY_RATIO_THRESHOLD, SHADOW_RATIO_THRESHOLD, DOJI_BODY_THRESHOLD)

                # 新增：综合解读（最后 5 根 K 线）（最小改动，添加VWAP/MFI/OBV/VIX提及）
                def generate_comprehensive_interpretation(data):
                    last_5 = data.tail(5)
                    if len(last_5) < 5:
                        return "數據不足，無法生成綜合解讀"
                    
                    patterns = last_5["K線形態"].value_counts()
                    volume_status = last_5["成交量標記"].value_counts()
                    bullish_count = len(last_5[last_5["K線形態"].isin(["錘子線", "大陽線", "看漲吞噬", "刺透形態", "早晨之星"])])
                    bearish_count = len(last_5[last_5["K線形態"].isin(["射擊之星", "大陰線", "看跌吞噬", "烏雲蓋頂", "黃昏之星"])])
                    neutral_count = len(last_5[last_5["K線形態"].isin(["十字星", "普通K線"])])
                    high_volume_count = len(last_5[last_5["成交量標記"] == "放量"])

                    vwap_trend = "多頭（價格>VWAP）" if last_5["Close"].iloc[-1] > last_5["VWAP"].iloc[-1] else "空頭（價格<VWAP）"
                    mfi_level = f"MFI={last_5['MFI'].iloc[-1]:.1f}（{'超賣背離機會' if last_5['MFI'].iloc[-1] < 20 else '超買背離風險' if last_5['MFI'].iloc[-1] > 80 else '中性'}）"
                    obv_trend = "OBV上漲確認量能" if last_5["OBV"].iloc[-1] > last_5["OBV"].iloc[0] else "OBV下跌警示量能不足"
                    vix_level = f"VIX={last_5['VIX'].iloc[-1]:.1f}（{'恐慌高位' if last_5['VIX'].iloc[-1] > VIX_HIGH_THRESHOLD else '平靜低位' if last_5['VIX'].iloc[-1] < VIX_LOW_THRESHOLD else '中性'}）"
                    vix_trend = "VIX趨勢上升（EMA Fast > Slow）" if last_5["VIX_EMA_Fast"].iloc[-1] > last_5["VIX_EMA_Slow"].iloc[-1] else "VIX趨勢下降（EMA Fast < Slow）"

                    if bullish_count >= 3 and high_volume_count >= 3:
                        return f"最近五日多方主導，出現多根看漲形態（如大陽線或看漲吞噬）且多伴隨放量，市場呈現強勢上漲趨勢，{vwap_trend}，{mfi_level}，{obv_trend}，{vix_level}，{vix_trend}，建議關注買入機會。"
                    elif bearish_count >= 3 and high_volume_count >= 3:
                        return f"最近五日空方主導，出現多根看跌形態（如大陰線或看跌吞噬）且多伴隨放量，市場呈現強勢下跌趨勢，{vwap_trend}，{mfi_level}，{obv_trend}，{vix_level}，{vix_trend}，建議注意賣出風險。"
                    elif neutral_count >= 3:
                        return f"最近五日多空交戰，型態以十字星或普通K線為主，成交量無明顯趨勢，市場處於盤整或方向不明階段，{vwap_trend}，{mfi_level}，{obv_trend}，{vix_level}，{vix_trend}。"
                    elif bullish_count >= 2 and bearish_count >= 2:
                        return f"最近五日多空激烈爭奪，看漲與看跌形態交替出現，成交量變化不一，市場方向不明，建議觀望，{vwap_trend}，{mfi_level}，{obv_trend}，{vix_level}，{vix_trend}。"
                    else:
                        return f"最近五日市場型態與成交量無明顯趨勢，建議持續觀察後續動向，{vwap_trend}，{mfi_level}，{obv_trend}，{vix_level}，{vix_trend}。"

                comprehensive_interpretation = generate_comprehensive_interpretation(data)

                # 当前资料
                current_price = data["Close"].iloc[-1]
                previous_close = stock.info.get("previousClose", current_price)
                price_change = current_price - previous_close
                price_pct_change = (price_change / previous_close) * 100 if previous_close else 0

                last_volume = data["Volume"].iloc[-1]
                prev_volume = data["Volume"].iloc[-2] if len(data) > 1 else last_volume
                volume_change = last_volume - prev_volume
                volume_pct_change = (volume_change / prev_volume) * 100 if prev_volume else 0

                # 检查 Low > High、High < Low、MACD、EMA、价格趋势及带成交量条件的价格趋势信号
                low_high_signal = len(data) > 1 and data["Low"].iloc[-1] > data["High"].iloc[-2]
                high_low_signal = len(data) > 1 and data["High"].iloc[-1] < data["Low"].iloc[-2]
                macd_buy_signal = len(data) > 1 and data["MACD"].iloc[-1] > 0 and data["MACD"].iloc[-2] <= 0
                macd_sell_signal = len(data) > 1 and data["MACD"].iloc[-1] <= 0 and data["MACD"].iloc[-2] > 0
                ema_buy_signal = (len(data) > 1 and 
                                 data["EMA5"].iloc[-1] > data["EMA10"].iloc[-1] and 
                                 data["EMA5"].iloc[-2] <= data["EMA10"].iloc[-2] and 
                                 data["Volume"].iloc[-1] > data["Volume"].iloc[-2])
                ema_sell_signal = (len(data) > 1 and 
                                  data["EMA5"].iloc[-1] < data["EMA10"].iloc[-1] and 
                                  data["EMA5"].iloc[-2] >= data["EMA10"].iloc[-2] and 
                                  data["Volume"].iloc[-1] > data["Volume"].iloc[-2])
                price_trend_buy_signal = (len(data) > 1 and 
                                         data["High"].iloc[-1] > data["High"].iloc[-2] and 
                                         data["Low"].iloc[-1] > data["Low"].iloc[-2] and 
                                         data["Close"].iloc[-1] > data["Close"].iloc[-2])
                price_trend_sell_signal = (len(data) > 1 and 
                                          data["High"].iloc[-1] < data["High"].iloc[-2] and 
                                          data["Low"].iloc[-1] < data["Low"].iloc[-2] and 
                                          data["Close"].iloc[-1] < data["Close"].iloc[-2])
                price_trend_vol_buy_signal = (len(data) > 1 and 
                                             data["High"].iloc[-1] > data["High"].iloc[-2] and 
                                             data["Low"].iloc[-1] > data["Low"].iloc[-2] and 
                                             data["Close"].iloc[-1] > data["Close"].iloc[-2] and 
                                             data["Volume"].iloc[-1] > data["前5均量"].iloc[-1])
                price_trend_vol_sell_signal = (len(data) > 1 and 
                                              data["High"].iloc[-1] < data["High"].iloc[-2] and 
                                              data["Low"].iloc[-1] < data["Low"].iloc[-2] and 
                                              data["Close"].iloc[-1] < data["Close"].iloc[-2] and 
                                              data["Volume"].iloc[-1] > data["前5均量"].iloc[-1])
                price_trend_vol_pct_buy_signal = (len(data) > 1 and 
                                                 data["High"].iloc[-1] > data["High"].iloc[-2] and 
                                                 data["Low"].iloc[-1] > data["Low"].iloc[-2] and 
                                                 data["Close"].iloc[-1] > data["Close"].iloc[-2] and 
                                                 data["Volume Change %"].iloc[-1] > 15)
                price_trend_vol_pct_sell_signal = (len(data) > 1 and 
                                                  data["High"].iloc[-1] < data["High"].iloc[-2] and 
                                                  data["Low"].iloc[-1] < data["Low"].iloc[-2] and 
                                                  data["Close"].iloc[-1] < data["Close"].iloc[-2] and 
                                                  data["Volume Change %"].iloc[-1] > 15)
                new_buy_signal = (len(data) > 1 and 
                                 data["Close"].iloc[-1] > data["Open"].iloc[-1] and 
                                 data["Open"].iloc[-1] > data["Close"].iloc[-2])
                new_sell_signal = (len(data) > 1 and 
                                  data["Close"].iloc[-1] < data["Open"].iloc[-1] and 
                                  data["Open"].iloc[-1] < data["Close"].iloc[-2])
                new_pivot_signal = (len(data) > 1 and 
                                   abs(data["Price Change %"].iloc[-1]) > PRICE_CHANGE_THRESHOLD and 
                                   abs(data["Volume Change %"].iloc[-1] ) > VOLUME_CHANGE_THRESHOLD)
                ema10_30_buy_signal = (len(data) > 1 and 
                                       data["EMA10"].iloc[-1] > data["EMA30"].iloc[-1] and 
                                       data["EMA10"].iloc[-2] <= data["EMA30"].iloc[-2])
                ema10_30_40_strong_buy_signal = (len(data) > 1 and 
                                                 data["EMA10"].iloc[-1] > data["EMA30"].iloc[-1] and 
                                                 data["EMA10"].iloc[-2] <= data["EMA30"].iloc[-2] and 
                                                 data["EMA10"].iloc[-1] > data["EMA40"].iloc[-1])
                ema10_30_sell_signal = (len(data) > 1 and 
                                        data["EMA10"].iloc[-1] < data["EMA30"].iloc[-1] and 
                                        data["EMA10"].iloc[-2] >= data["EMA30"].iloc[-2])
                ema10_30_40_strong_sell_signal = (len(data) > 1 and 
                                                  data["EMA10"].iloc[-1] < data["EMA30"].iloc[-1] and 
                                                  data["EMA10"].iloc[-2] >= data["EMA30"].iloc[-2] and 
                                                  data["EMA10"].iloc[-1] < data["EMA40"].iloc[-1])
                bullish_engulfing = (len(data) > 1 and 
                                     data["Close"].iloc[-2] < data["Open"].iloc[-2] and 
                                     data["Close"].iloc[-1] > data["Open"].iloc[-1] and 
                                     data["Open"].iloc[-1] < data["Close"].iloc[-2] and 
                                     data["Close"].iloc[-1] > data["Open"].iloc[-2] and 
                                     data["Volume"].iloc[-1] > data["前5均量"].iloc[-1] and 
                                     data["RSI"].iloc[-1] < 50)
                bearish_engulfing = (len(data) > 1 and 
                                     data["Close"].iloc[-2] > data["Open"].iloc[-2] and 
                                     data["Close"].iloc[-1] < data["Open"].iloc[-1] and 
                                     data["Open"].iloc[-1] > data["Close"].iloc[-2] and 
                                     data["Close"].iloc[-1] < data["Open"].iloc[-2] and 
                                     data["Volume"].iloc[-1] > data["前5均量"].iloc[-1] and 
                                     data["RSI"].iloc[-1] > 50)
                hammer = (len(data) > 1 and 
                          data["Close"].iloc[-1] > data["Close"].iloc[-2] and 
                          abs(data["Close"].iloc[-1] - data["Open"].iloc[-1]) < (data["High"].iloc[-1] - data["Low"].iloc[-1]) * 0.3 and 
                          (min(data["Open"].iloc[-1], data["Close"].iloc[-1]) - data["Low"].iloc[-1]) >= 2 * abs(data["Close"].iloc[-1] - data["Open"].iloc[-1]) and 
                          (data["High"].iloc[-1] - max(data["Open"].iloc[-1], data["Close"].iloc[-1])) < (min(data["Open"].iloc[-1], data["Close"].iloc[-1]) - data["Low"].iloc[-1]) and 
                          data["Volume"].iloc[-1] > data["前5均量"].iloc[-1] and 
                          data["RSI"].iloc[-1] < 50)
                hanging_man = (len(data) > 1 and 
                               data["Close"].iloc[-1] < data["Close"].iloc[-2] and 
                               abs(data["Close"].iloc[-1] - data["Open"].iloc[-1]) < (data["High"].iloc[-1] - data["Low"].iloc[-1]) * 0.3 and 
                               (min(data["Open"].iloc[-1], data["Close"].iloc[-1]) - data["Low"].iloc[-1]) >= 2 * abs(data["Close"].iloc[-1] - data["Open"].iloc[-1]) and 
                               (data["High"].iloc[-1] - max(data["Open"].iloc[-1], data["Close"].iloc[-1])) < (min(data["Open"].iloc[-1], data["Close"].iloc[-1]) - data["Low"].iloc[-1]) and 
                               data["Volume"].iloc[-1] > data["前5均量"].iloc[-1] and 
                               data["RSI"].iloc[-1] > 50)
                morning_star = (len(data) > 2 and 
                                data["Close"].iloc[-3] < data["Open"].iloc[-3] and 
                                abs(data["Close"].iloc[-2] - data["Open"].iloc[-2]) < 0.3 * abs(data["Close"].iloc[-3] - data["Open"].iloc[-3]) and 
                                data["Close"].iloc[-1] > data["Open"].iloc[-1] and 
                                data["Close"].iloc[-1] > (data["Open"].iloc[-3] + data["Close"].iloc[-3]) / 2 and 
                                data["Volume"].iloc[-1] > data["前5均量"].iloc[-1] and 
                                data["RSI"].iloc[-1] < 50)
                evening_star = (len(data) > 2 and 
                                data["Close"].iloc[-3] > data["Open"].iloc[-3] and 
                                abs(data["Close"].iloc[-2] - data["Open"].iloc[-2]) < 0.3 * abs(data["Close"].iloc[-3] - data["Open"].iloc[-3]) and 
                                data["Close"].iloc[-1] < data["Open"].iloc[-1] and 
                                data["Close"].iloc[-1] < (data["Open"].iloc[-3] + data["Close"].iloc[-3]) / 2 and 
                                data["Volume"].iloc[-1] > data["前5均量"].iloc[-1] and 
                                data["RSI"].iloc[-1] > 50)
                
                # 新增：VWAP、MFI、OBV 当前信号检测
                vwap_buy_signal = len(data) > 1 and pd.notna(data["VWAP"].iloc[-1]) and data["Close"].iloc[-1] > data["VWAP"].iloc[-1] and data["Close"].iloc[-2] <= data["VWAP"].iloc[-2]
                vwap_sell_signal = len(data) > 1 and pd.notna(data["VWAP"].iloc[-1]) and data["Close"].iloc[-1] < data["VWAP"].iloc[-1] and data["Close"].iloc[-2] >= data["VWAP"].iloc[-2]
                mfi_bull_divergence = len(data) > MFI_DIVERGENCE_WINDOW and data['MFI_Bull_Div'].iloc[-1]
                mfi_bear_divergence = len(data) > MFI_DIVERGENCE_WINDOW and data['MFI_Bear_Div'].iloc[-1]
                obv_breakout_buy = len(data) > 1 and data["Close"].iloc[-1] > data["Close"].iloc[-2] and data["OBV"].iloc[-1] > data['OBV_Roll_Max'].iloc[-2]
                obv_breakout_sell = len(data) > 1 and data["Close"].iloc[-1] < data["Close"].iloc[-2] and data["OBV"].iloc[-1] < data['OBV_Roll_Min'].iloc[-2]
                
                # 新增：VIX 当前信号检测
                vix_panic_sell = len(data) > 1 and pd.notna(data["VIX"].iloc[-1]) and data["VIX"].iloc[-1] > VIX_HIGH_THRESHOLD and data["VIX"].iloc[-1] > data["VIX"].iloc[-2]
                vix_calm_buy = len(data) > 1 and pd.notna(data["VIX"].iloc[-1]) and data["VIX"].iloc[-1] < VIX_LOW_THRESHOLD and data["VIX"].iloc[-1] < data["VIX"].iloc[-2]
                
                # 新增：VIX 趨勢当前信号检测
                vix_uptrend_sell = len(data) > 1 and pd.notna(data["VIX_EMA_Fast"].iloc[-1]) and data["VIX_EMA_Fast"].iloc[-1] > data["VIX_EMA_Slow"].iloc[-1] and data["VIX_EMA_Fast"].iloc[-2] <= data["VIX_EMA_Slow"].iloc[-2]
                vix_downtrend_buy = len(data) > 1 and pd.notna(data["VIX_EMA_Fast"].iloc[-1]) and data["VIX_EMA_Fast"].iloc[-1] < data["VIX_EMA_Slow"].iloc[-1] and data["VIX_EMA_Fast"].iloc[-2] >= data["VIX_EMA_Slow"].iloc[-2]
                
                # 跳空信号检测
                gap_common_up = False
                gap_common_down = False
                gap_breakaway_up = False
                gap_breakaway_down = False
                gap_runaway_up = False
                gap_runaway_down = False
                gap_exhaustion_up = False
                gap_exhaustion_down = False
                if len(data) > 1:
                    gap_pct = ((data["Open"].iloc[-1] - data["Close"].iloc[-2]) / data["Close"].iloc[-2]) * 100
                    is_up_gap = gap_pct > GAP_THRESHOLD
                    is_down_gap = gap_pct < -GAP_THRESHOLD
                    if is_up_gap or is_down_gap:
                        trend = data["Close"].iloc[-5:].mean() if len(data) >= 5 else 0
                        prev_trend = data["Close"].iloc[-6:-1].mean() if len(data) >= 6 else trend
                        is_up_trend = data["Close"].iloc[-1] > trend and trend > prev_trend
                        is_down_trend = data["Close"].iloc[-1] < trend and trend < prev_trend
                        is_high_volume = data["Volume"].iloc[-1] > data["前5均量"].iloc[-1]
                        is_price_reversal = (len(data) > 2 and
                                            ((is_up_gap and data["Close"].iloc[-1] < data["Close"].iloc[-2]) or
                                             (is_down_gap and data["Close"].iloc[-1] > data["Close"].iloc[-2])))
                        if is_up_gap:
                            if is_price_reversal and is_high_volume:
                                gap_exhaustion_up = True
                            elif is_up_trend and is_high_volume:
                                gap_runaway_up = True
                            elif data["High"].iloc[-1] > data["High"].iloc[-2:-1].max() and is_high_volume:
                                gap_breakaway_up = True
                            else:
                                gap_common_up = True
                        elif is_down_gap:
                            if is_price_reversal and is_high_volume:
                                gap_exhaustion_down = True
                            elif is_down_trend and is_high_volume:
                                gap_runaway_down = True
                            elif data["Low"].iloc[-1] < data["Low"].iloc[-2:-1].min() and is_high_volume:
                                gap_breakaway_down = True
                            else:
                                gap_common_down = True

                # 连续向上/向下信号检测
                continuous_up_buy_signal = data['Continuous_Up'].iloc[-1] >= CONTINUOUS_UP_THRESHOLD
                continuous_down_sell_signal = data['Continuous_Down'].iloc[-1] >= CONTINUOUS_DOWN_THRESHOLD

                # SMA趋势信号检测
                sma50_up_trend = False
                sma50_down_trend = False
                sma50_200_up_trend = False
                sma50_200_down_trend = False
                if pd.notna(data["SMA50"].iloc[-1]):
                    if data["Close"].iloc[-1] > data["SMA50"].iloc[-1]:
                        sma50_up_trend = True
                    elif data["Close"].iloc[-1] < data["SMA50"].iloc[-1]:
                        sma50_down_trend = True
                if pd.notna(data["SMA50"].iloc[-1]) and pd.notna(data["SMA200"].iloc[-1]):
                    if data["Close"].iloc[-1] > data["SMA50"].iloc[-1] and data["SMA50"].iloc[-1] > data["SMA200"].iloc[-1]:
                        sma50_200_up_trend = True
                    elif data["Close"].iloc[-1] < data["SMA50"].iloc[-1] and data["SMA50"].iloc[-1] < data["SMA200"].iloc[-1]:
                        sma50_200_down_trend = True

                # 显示当前资料
                st.metric(f"{ticker} 🟢 股價變動", f"${current_price:.2f}",
                          f"{price_change:.2f} ({price_pct_change:.2f}%)")
                st.metric(f"{ticker} 🔵 成交量變動", f"{last_volume:,}",
                          f"{volume_change:,} ({volume_pct_change:.2f}%)")

                # 新增：VIX 指标显示
                if pd.notna(data["VIX"].iloc[-1]):
                    st.metric(f"{ticker} ⚡ VIX 恐慌指數", f"{data['VIX'].iloc[-1]:.2f}",
                              f"{data['VIX Change %'].iloc[-1]:.2f}%" if pd.notna(data['VIX Change %'].iloc[-1]) else "N/A")

                # 计算并显示所有信号的成功率
                success_rates = calculate_signal_success_rate(data)
                st.subheader(f"📊 {ticker} 各信号成功率")
                success_data = []
                for signal, metrics in success_rates.items():
                    success_rate = metrics["success_rate"]
                    total_signals = metrics["total_signals"]
                    direction = metrics["direction"]
                    success_definition = "下一交易日的最低价低于当前最低价且收盘价低于当前收盘价" if direction == "down" else "下一交易日的最高价高于当前最高价且收盘价高于当前收盘价"
                    success_data.append({
                        "信号": signal,
                        "成功率 (%)": f"{success_rate:.2f}%",
                        "触发次数": total_signals,
                        "成功定义": success_definition
                    })
                    st.metric(f"{ticker} {signal} 成功率", 
                              f"{success_rate:.2f}%",
                              f"基于 {total_signals} 次信号 ({'下跌' if direction == 'down' else '上涨'})")
                    if total_signals > 0 and total_signals < 5:
                        st.warning(f"⚠️ {ticker} {signal} 样本量过少（{total_signals} 次），成功率可能不稳定")
                
                # 显示成功率表格
                if success_data:
                    st.dataframe(
                        pd.DataFrame(success_data),
                        use_container_width=True,
                        column_config={
                            "信号": st.column_config.TextColumn("信号", width="medium"),
                            "成功率 (%)": st.column_config.TextColumn("成功率 (%)", width="small"),
                            "触发次数": st.column_config.NumberColumn("触发次数", width="small"),
                            "成功定义": st.column_config.TextColumn("成功定义", width="large")
                        }
                    )

                # 新增：显示综合解读
                st.subheader(f"📝 {ticker} 綜合解讀")
                st.write(comprehensive_interpretation)

                # 异动提醒 + Email 推播（新增 or 新信号）
                if (abs(price_pct_change) >= PRICE_THRESHOLD and abs(volume_pct_change) >= VOLUME_THRESHOLD) or low_high_signal or high_low_signal or macd_buy_signal or macd_sell_signal or ema_buy_signal or ema_sell_signal or price_trend_buy_signal or price_trend_sell_signal or price_trend_vol_buy_signal or price_trend_vol_sell_signal or price_trend_vol_pct_buy_signal or price_trend_vol_pct_sell_signal or gap_common_up or gap_common_down or gap_breakaway_up or gap_breakaway_down or gap_runaway_up or gap_runaway_down or gap_exhaustion_up or gap_exhaustion_down or continuous_up_buy_signal or continuous_down_sell_signal or sma50_up_trend or sma50_down_trend or sma50_200_up_trend or sma50_200_down_trend or new_buy_signal or new_sell_signal or new_pivot_signal or ema10_30_buy_signal or ema10_30_40_strong_buy_signal or ema10_30_sell_signal or ema10_30_40_strong_sell_signal or bullish_engulfing or bearish_engulfing or hammer or hanging_man or morning_star or evening_star or vwap_buy_signal or vwap_sell_signal or mfi_bull_divergence or mfi_bear_divergence or obv_breakout_buy or obv_breakout_sell or vix_panic_sell or vix_calm_buy or vix_uptrend_sell or vix_downtrend_buy:
                    alert_msg = f"{ticker} 異動：價格 {price_pct_change:.2f}%、成交量 {volume_pct_change:.2f}%"
                    if low_high_signal:
                        alert_msg += "，當前最低價高於前一時段最高價"
                    if high_low_signal:
                        alert_msg += "，當前最高價低於前一時段最低價"
                    if macd_buy_signal:
                        alert_msg += "，MACD 買入訊號（MACD 線由負轉正）"
                    if macd_sell_signal:
                        alert_msg += "，MACD 賣出訊號（MACD 線由正轉負）"
                    if ema_buy_signal:
                        alert_msg += "，EMA 買入訊號（EMA5 上穿 EMA10，成交量放大）"
                    if ema_sell_signal:
                        alert_msg += "，EMA 賣出訊號（EMA5 下破 EMA10，成交量放大）"
                    if price_trend_buy_signal:
                        alert_msg += "，價格趨勢買入訊號（最高價、最低價、收盤價均上漲）"
                    if price_trend_sell_signal:
                        alert_msg += "，價格趨勢賣出訊號（最高價、最低價、收盤價均下跌）"
                    if price_trend_vol_buy_signal:
                        alert_msg += "，價格趨勢買入訊號（量）（最高價、最低價、收盤價均上漲且成交量放大）"
                    if price_trend_vol_sell_signal:
                        alert_msg += "，價格趨勢賣出訊號（量）（最高價、最低價、收盤價均下跌且成交量放大）"
                    if price_trend_vol_pct_buy_signal:
                        alert_msg += "，價格趨勢買入訊號（量%）（最高價、最低價、收盤價均上漲且成交量變化 > 15%）"
                    if price_trend_vol_pct_sell_signal:
                        alert_msg += "，價格趨勢賣出訊號（量%）（最高價、最低價、收盤價均下跌且成交量變化 > 15%）"
                    if gap_common_up:
                        alert_msg += "，普通跳空(上)（價格向上跳空，未伴隨明顯趨勢或成交量放大）"
                    if gap_common_down:
                        alert_msg += "，普通跳空(下)（價格向下跳空，未伴隨明顯趨勢或成交量放大）"
                    if gap_breakaway_up:
                        alert_msg += "，突破跳空(上)（價格向上跳空，突破前高且成交量放大）"
                    if gap_breakaway_down:
                        alert_msg += "，突破跳空(下)（價格向下跳空，跌破前低且成交量放大）"
                    if gap_runaway_up:
                        alert_msg += "，持續跳空(上)（價格向上跳空，處於上漲趨勢且成交量放大）"
                    if gap_runaway_down:
                        alert_msg += "，持續跳空(下)（價格向下跳空，處於下跌趨勢且成交量放大）"
                    if gap_exhaustion_up:
                        alert_msg += "，衰竭跳空(上)（價格向上跳空，趨勢末端且隨後價格下跌，成交量放大）"
                    if gap_exhaustion_down:
                        alert_msg += "，衰竭跳空(下)（價格向下跳空，趨勢末端且隨後價格上漲，成交量放大）"
                    if continuous_up_buy_signal:
                        alert_msg += f"，連續向上策略買入訊號（至少連續 {CONTINUOUS_UP_THRESHOLD} 根K線上漲）"
                    if continuous_down_sell_signal:
                        alert_msg += f"，連續向下策略賣出訊號（至少連續 {CONTINUOUS_DOWN_THRESHOLD} 根K線下跌）"
                    if sma50_up_trend:
                        alert_msg += "，SMA50 上升趨勢（當前價格高於 SMA50）"
                    if sma50_down_trend:
                        alert_msg += "，SMA50 下降趨勢（當前價格低於 SMA50）"
                    if sma50_200_up_trend:
                        alert_msg += "，SMA50_200 上升趨勢（當前價格高於 SMA50 且 SMA50 高於 SMA200）"
                    if sma50_200_down_trend:
                        alert_msg += "，SMA50_200 下降趨勢（當前價格低於 SMA50 且 SMA50 低於 SMA200）"
                    if new_buy_signal:
                        alert_msg += "，新买入信号（今日收盘价大于开盘价且今日开盘价大于前日收盘价）"
                    if new_sell_signal:
                        alert_msg += "，新卖出信号（今日收盘价小于开盘价且今日开盘价小于前日收盘价）"
                    if new_pivot_signal:
                        alert_msg += f"，新转折点（|Price Change %| > {PRICE_CHANGE_THRESHOLD}% 且 |Volume Change %| > {VOLUME_CHANGE_THRESHOLD}%）"
                    if ema10_30_buy_signal:
                        alert_msg += "，EMA10_30 買入訊號（EMA10 上穿 EMA30）"
                    if ema10_30_40_strong_buy_signal:
                        alert_msg += "，EMA10_30_40 強烈買入訊號（EMA10 上穿 EMA30 且高於 EMA40）"
                    if ema10_30_sell_signal:
                        alert_msg += "，EMA10_30 賣出訊號（EMA10 下破 EMA30）"
                    if ema10_30_40_strong_sell_signal:
                        alert_msg += "，EMA10_30_40 強烈賣出訊號（EMA10 下破 EMA30 且低於 EMA40）"
                    if bullish_engulfing:
                        alert_msg += "，看漲吞沒形態（當前K線完全包圍前一根看跌K線，成交量放大）"
                    if bearish_engulfing:
                        alert_msg += "，看跌吞沒形態（當前K線完全包圍前一根看漲K線，成交量放大）"
                    if hammer:
                        alert_msg += "，錘頭線（下影線較長，買方介入，預示反轉）"
                    if hanging_man:
                        alert_msg += "，上吊線（下影線較長，賣方介入，預示反轉）"
                    if morning_star:
                        alert_msg += "，早晨之星（下跌後出現小實體K線，隨後強烈看漲K線，預示反轉）"
                    if evening_star:
                        alert_msg += "，黃昏之星（上漲後出現小實體K線，隨後強烈看跌K線，預示反轉）"
                    # 新增：VWAP、MFI、OBV 描述
                    if vwap_buy_signal:
                        alert_msg += "，VWAP 買入訊號（價格上穿 VWAP，作為主進場基準）"
                    if vwap_sell_signal:
                        alert_msg += "，VWAP 賣出訊號（價格下破 VWAP，作為主出場基準）"
                    if mfi_bull_divergence:
                        alert_msg += "，MFI 牛背離買入（價格新低但 MFI 未新低，偵測超賣背離）"
                    if mfi_bear_divergence:
                        alert_msg += "，MFI 熊背離賣出（價格新高但 MFI 未新高，偵測超買背離）"
                    if obv_breakout_buy:
                        alert_msg += "，OBV 突破買入（OBV 新高確認價格上漲量能）"
                    if obv_breakout_sell:
                        alert_msg += "，OBV 突破賣出（OBV 新低確認價格下跌量能）"
                    # 新增：VIX 描述
                    if vix_panic_sell:
                        alert_msg += "，VIX 恐慌賣出（VIX > 30 且上升，市場恐慌加劇）"
                    if vix_calm_buy:
                        alert_msg += "，VIX 平靜買入（VIX < 20 且下降，市場穩定）"
                    # 新增：VIX 趨勢描述
                    if vix_uptrend_sell:
                        alert_msg += "，VIX 上升趨勢賣出（VIX EMA5 上穿 EMA10，恐慌增加）"
                    if vix_downtrend_buy:
                        alert_msg += "，VIX 下降趨勢買入（VIX EMA5 下破 EMA10，市場平靜）"
                    # 新增：加入最新K线形态到提醒
                    if data["K線形態"].iloc[-1] != "普通K線":
                        alert_msg += f"，最新K線形態：{data['K線形態'].iloc[-1]}（{data['單根解讀'].iloc[-1]}）"
                    st.warning(f"📣 {alert_msg}")
                    st.toast(f"📣 {alert_msg}")
                    send_email_alert(ticker, price_pct_change, volume_pct_change, low_high_signal, high_low_signal, 
                                    macd_buy_signal, macd_sell_signal, ema_buy_signal, ema_sell_signal, 
                                    price_trend_buy_signal, price_trend_sell_signal,
                                    price_trend_vol_buy_signal, price_trend_vol_sell_signal,
                                    price_trend_vol_pct_buy_signal, price_trend_vol_pct_sell_signal,
                                    gap_common_up, gap_common_down, gap_breakaway_up, gap_breakaway_down,
                                    gap_runaway_up, gap_runaway_down, gap_exhaustion_up, gap_exhaustion_down,
                                    continuous_up_buy_signal, continuous_down_sell_signal,
                                    sma50_up_trend, sma50_down_trend,
                                    sma50_200_up_trend, sma50_200_down_trend,
                                    new_buy_signal, new_sell_signal, new_pivot_signal,
                                    ema10_30_buy_signal, ema10_30_40_strong_buy_signal,
                                    ema10_30_sell_signal, ema10_30_40_strong_sell_signal,
                                    bullish_engulfing, bearish_engulfing, hammer, hanging_man,
                                    morning_star, evening_star,
                                    # 新增调用参数
                                    vwap_buy_signal, vwap_sell_signal,
                                    mfi_bull_divergence, mfi_bear_divergence,
                                    obv_breakout_buy, obv_breakout_sell,
                                    # 新增 VIX 参数
                                    vix_panic_sell, vix_calm_buy,
                                    # 新增 VIX 趨勢参数
                                    vix_uptrend_sell, vix_downtrend_buy)

                    # 修改：Telegram 發送邏輯（基於表格條件匹配）
                    if data["Close_N_High"].iloc[-1] >=HIGH_N_HIGH_THRESHOLD:
                            alertmsg = f"有機會再破新高,買入訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['High'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                            send_telegram_alert(alertmsg)
                    if data["High"].iloc[-1] > data['High_Max'].iloc[-1]:
                            alertmsg = f"破5K新高,買入訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['Close'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                            send_telegram_alert(alertmsg)
                    if data["Low"].iloc[-1] < data['Low_Min'].iloc[-1]:
                            alertmsg = f"穿5K新低,賣出訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['Close'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                            send_telegram_alert(alertmsg)
                    if len(data["異動標記"]) > 0:
                        K_signals = str(data["異動標記"].iloc[-1])  # 最新一根K线的信号字符串
                        K_signals_list = [s.strip() for s in K_signals.split(", ") if s.strip()]  # 拆分並過濾空
                        current_volume_mark = data["成交量標記"].iloc[-1]
                        current_kline_pattern = data["K線形態"].iloc[-1]
                        ###
                        # if data["Close_N_High"].iloc[-1] >=HIGH_N_HIGH_THRESHOLD:
                        #     alertmsg = f"有機會再破新高,買入訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['High'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                        #     send_telegram_alert(alertmsg)
                        # if data["High"].iloc[-1] > data['High_Max'].iloc[-1]:
                        #     alertmsg = f"破5K新高,買入訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['Close'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                        #     send_telegram_alert(alertmsg)
                        # if data["Low"].iloc[-1] < data['Low_Min'].iloc[-1]:
                        #     alertmsg = f"穿5K新低,賣出訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['Close'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                        #     send_telegram_alert(alertmsg)


                        matched_rank = None
                        for idx, row in telegram_conditions.iterrows():
                            required_signals = [s.strip() for s in str(row["異動標記"]).split(", ") if s.strip()]
                            required_volume = row["成交量標記"]
                            required_pattern = row["K線形態"]
                            
                            if (all(sig in K_signals_list for sig in required_signals) and
                                current_volume_mark == required_volume and
                                current_kline_pattern == required_pattern):
                                matched_rank = row["排名"]
                                break
                        
                        if matched_rank is not None:
                            alertmsg = f"1D BUY 趨勢反轉,買入訊號: {data['Datetime'].iloc[-1]} {ticker}:{selected_interval}:$ {data['Close'].iloc[-1].round(2)} *{data['異動標記'].iloc[-1]}*{data['成交量標記'].iloc[-1]}*{data['K線形態'].iloc[-1]}*{data['單根解讀'].iloc[-1]}* 匹配排名 {matched_rank} 條件"
                            send_telegram_alert(alertmsg)
                    ##########
                # 添加 K 线图（含 EMA）、成交量柱状图和 RSI 子图（新增 VWAP/MFI/OBV traces）
                st.subheader(f"📈 {ticker} K線圖與技術指標")
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                fig = make_subplots(rows=3, cols=1, shared_xaxes=True, 
                                    subplot_titles=(f"{ticker} K線與EMA/VWAP", "成交量/OBV", "RSI/MFI"),
                                    vertical_spacing=0.1, row_heights=[0.5, 0.2, 0.3])
                
                # 添加 K 线图
                fig.add_trace(go.Candlestick(x=data.tail(50)["Datetime"],
                                            open=data.tail(50)["Open"],
                                            high=data.tail(50)["High"],
                                            low=data.tail(50)["Low"],
                                            close=data.tail(50)["Close"],
                                            name="K線"), row=1, col=1)
                
                # 添加 EMA5、EMA10、EMA30 和 EMA40
                fig.add_trace(px.line(data.tail(50), x="Datetime", y="EMA5")["data"][0], row=1, col=1)
                fig.add_trace(px.line(data.tail(50), x="Datetime", y="EMA10")["data"][0], row=1, col=1)
                fig.add_trace(px.line(data.tail(50), x="Datetime", y="EMA30")["data"][0], row=1, col=1)
                fig.add_trace(px.line(data.tail(50), x="Datetime", y="EMA40")["data"][0], row=1, col=1)
                
                # 新增：VWAP 線（主圖）
                fig.add_trace(go.Scatter(x=data.tail(50)["Datetime"], y=data.tail(50)["VWAP"], 
                                         mode='lines', name='VWAP', line=dict(color='purple', width=2)), row=1, col=1)
                
                # 添加成交量柱状图
                fig.add_bar(x=data.tail(50)["Datetime"], y=data.tail(50)["Volume"], 
                           name="成交量", opacity=0.5, row=2, col=1)
                
                # 新增：OBV 線（成交量子圖，secondary_y）
                fig.add_trace(go.Scatter(x=data.tail(50)["Datetime"], y=data.tail(50)["OBV"], 
                                         mode='lines', name='OBV', yaxis="y2", line=dict(color='orange', width=2)), row=2, col=1)
                fig.add_hline(y=0, line_dash="dash", line_color="black", row=2, col=1)
                fig.update_layout(yaxis2=dict(overlaying="y", side="right", title="OBV"))
                
                # 添加 RSI 子图
                fig.add_trace(px.line(data.tail(50), x="Datetime", y="RSI")["data"][0], row=3, col=1)
                fig.add_hline(y=70, line_dash="dash", line_color="red", row=3, col=1)  # 超买线
                fig.add_hline(y=30, line_dash="dash", line_color="green", row=3, col=1)  # 超卖线
                
                # 新增：MFI 線（RSI子圖，secondary_y）
                fig.add_trace(go.Scatter(x=data.tail(50)["Datetime"], y=data.tail(50)["MFI"], 
                                         mode='lines', name='MFI', yaxis="y3", line=dict(color='brown', width=2)), row=3, col=1)
                fig.add_hline(y=80, line_dash="dash", line_color="red", row=3, col=1, yref="y3")  # MFI超买
                fig.add_hline(y=20, line_dash="dash", line_color="green", row=3, col=1, yref="y3")  # MFI超卖
                fig.update_layout(yaxis3=dict(overlaying="y", side="right", title="MFI", range=[0,100]))
                
                # 标记 EMA 买入/卖出信号、关键转折点、新买入信号、新卖出信号、新转折点及新EMA信号
                for i in range(1, len(data.tail(50))):
                    idx = -50 + i  # 调整索引以匹配 tail(50)
                    if (data["EMA5"].iloc[idx] > data["EMA10"].iloc[idx] and 
                        data["EMA5"].iloc[idx-1] <= data["EMA10"].iloc[idx-1]):
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 EMA買入", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    elif (data["EMA5"].iloc[idx] < data["EMA10"].iloc[idx] and 
                          data["EMA5"].iloc[idx-1] >= data["EMA10"].iloc[idx-1]):
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 EMA賣出", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "关键转折点" in data["異動標記"].iloc[idx]:
                        fig.add_scatter(x=[data["Datetime"].iloc[idx]], y=[data["Close"].iloc[idx]],
                                       mode="markers+text", marker=dict(symbol="star", size=12, color="yellow"),
                                       text=[f"🔥 转折点 ${data['Close'].iloc[idx]:.2f}"],
                                       textposition="top center", name="关键转折点", row=1, col=1)
                    if "新买入信号" in data["異動標記"].iloc[idx]:
                        fig.add_scatter(x=[data["Datetime"].iloc[idx]], y=[data["Close"].iloc[idx]],
                                       mode="markers+text", marker=dict(symbol="triangle-up", size=10, color="green"),
                                       text=[f"📈 新买入 ${data['Close'].iloc[idx]:.2f}"],
                                       textposition="bottom center", name="新买入信号", row=1, col=1)
                    if "新卖出信号" in data["異動標記"].iloc[idx]:
                        fig.add_scatter(x=[data["Datetime"].iloc[idx]], y=[data["Close"].iloc[idx]],
                                       mode="markers+text", marker=dict(symbol="triangle-down", size=10, color="red"),
                                       text=[f"📉 新卖出 ${data['Close'].iloc[idx]:.2f}"],
                                       textposition="top center", name="新卖出信号", row=1, col=1)
                    if "新转折点" in data["異動標記"].iloc[idx]:
                        fig.add_scatter(x=[data["Datetime"].iloc[idx]], y=[data["Close"].iloc[idx]],
                                       mode="markers+text", marker=dict(symbol="star", size=10, color="purple"),
                                       text=[f"🔄 新转折点 ${data['Close'].iloc[idx]:.2f}"],
                                       textposition="top center", name="新转折点", row=1, col=1)
                    if "EMA10_30買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 EMA10_30買入", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    if "EMA10_30_40強烈買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 EMA10_30_40強烈買入", showarrow=True, arrowhead=2, ax=20, ay=-50, row=1, col=1)
                    if "EMA10_30賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 EMA10_30賣出", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "EMA10_30_40強烈賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 EMA10_30_40強烈賣出", showarrow=True, arrowhead=2, ax=20, ay=50, row=1, col=1)
                    if "看漲吞沒" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 看漲吞沒", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    if "看跌吞沒" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 看跌吞沒", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "錘頭線" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 錘頭線", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    if "上吊線" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 上吊線", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "早晨之星" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 早晨之星", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    if "黃昏之星" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 黃昏之星", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    # 新增：标记新信号
                    if "📈 VWAP買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 VWAP買入", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    if "📉 VWAP賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 VWAP賣出", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "📈 MFI牛背離買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 MFI牛背離", showarrow=True, arrowhead=2, ax=20, ay=-30, row=3, col=1)
                    if "📉 MFI熊背離賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 MFI熊背離", showarrow=True, arrowhead=2, ax=20, ay=30, row=3, col=1)
                    if "📈 OBV突破買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 OBV突破", showarrow=True, arrowhead=2, ax=20, ay=-30, row=2, col=1)
                    if "📉 OBV突破賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 OBV突破", showarrow=True, arrowhead=2, ax=20, ay=30, row=2, col=1)
                    # 新增：VIX 标记
                    if "📉 VIX恐慌賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 VIX恐慌", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "📈 VIX平靜買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 VIX平靜", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                    # 新增：VIX 趨勢标记
                    if "📉 VIX上升趨勢賣出" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📉 VIX上升", showarrow=True, arrowhead=2, ax=20, ay=30, row=1, col=1)
                    if "📈 VIX下降趨勢買入" in data["異動標記"].iloc[idx]:
                        fig.add_annotation(x=data["Datetime"].iloc[idx], y=data["Close"].iloc[idx],
                                         text="📈 VIX下降", showarrow=True, arrowhead=2, ax=20, ay=-30, row=1, col=1)
                
                fig.update_layout(yaxis_title="價格", yaxis2_title="成交量", yaxis3_title="RSI", showlegend=True)
                st.plotly_chart(fig, use_container_width=True, key=f"chart_{ticker}_{timestamp}")

                # 合并显示五项指标前 X% 的范围到表格
                st.subheader(f"📊 {ticker} 前 {PERCENTILE_THRESHOLD}% 數據範圍")
                range_data = []
                
                # Price Change % 范围
                sorted_price_changes = data["Price Change %"].dropna().sort_values(ascending=False)
                if len(sorted_price_changes) > 0:
                    top_percent_count = max(1, int(len(sorted_price_changes) * PERCENTILE_THRESHOLD / 100))
                    top_percent = sorted_price_changes.head(top_percent_count)
                    range_data.append({
                        "指標": "Price Change %",
                        "範圍類型": "最高到最低",
                        "最大值": f"{top_percent.max():.2f}%",
                        "最小值": f"{top_percent.min():.2f}%"
                    })
                sorted_price_changes_asc = data["Price Change %"].dropna().sort_values(ascending=True)
                if len(sorted_price_changes_asc) > 0:
                    bottom_percent_count = max(1, int(len(sorted_price_changes_asc) * PERCENTILE_THRESHOLD / 100))
                    bottom_percent = sorted_price_changes_asc.head(bottom_percent_count)
                    range_data.append({
                        "指標": "Price Change %",
                        "範圍類型": "最低到最高",
                        "最大值": f"{bottom_percent.max():.2f}%",
                        "最小值": f"{bottom_percent.min():.2f}%"
                    })

                # Volume Change % 范围
                sorted_volume_changes = data["Volume Change %"].dropna().sort_values(ascending=False)
                if len(sorted_volume_changes) > 0:
                    top_volume_percent_count = max(1, int(len(sorted_volume_changes) * PERCENTILE_THRESHOLD / 100))
                    top_volume_percent = sorted_volume_changes.head(top_volume_percent_count)
                    range_data.append({
                        "指標": "Volume Change %",
                        "範圍類型": "最高到最低",
                        "最大值": f"{top_volume_percent.max():.2f}%",
                        "最小值": f"{top_volume_percent.min():.2f}%"
                    })
                sorted_volume_changes_asc = data["Volume Change %"].dropna().sort_values(ascending=True)
                if len(sorted_volume_changes_asc) > 0:
                    bottom_volume_percent_count = max(1, int(len(sorted_volume_changes_asc) * PERCENTILE_THRESHOLD / 100))
                    bottom_volume_percent = sorted_volume_changes_asc.head(bottom_volume_percent_count)
                    range_data.append({
                        "指標": "Volume Change %",
                        "範圍類型": "最低到最高",
                        "最大值": f"{bottom_volume_percent.max():.2f}%",
                        "最小值": f"{bottom_volume_percent.min():.2f}%"
                    })

                # Volume 范围
                sorted_volumes = data["Volume"].dropna().sort_values(ascending=False)
                if len(sorted_volumes) > 0:
                    top_volume_abs_count = max(1, int(len(sorted_volumes) * PERCENTILE_THRESHOLD / 100))
                    top_volume_abs = sorted_volumes.head(top_volume_abs_count)
                    range_data.append({
                        "指標": "Volume",
                        "範圍類型": "最高到最低",
                        "最大值": f"{int(top_volume_abs.max()):,}",
                        "最小值": f"{int(top_volume_abs.min()):,}"
                    })
                sorted_volumes_asc = data["Volume"].dropna().sort_values(ascending=True)
                if len(sorted_volumes_asc) > 0:
                    bottom_volume_abs_count = max(1, int(len(sorted_volumes_asc) * PERCENTILE_THRESHOLD / 100))
                    bottom_volume_abs = sorted_volumes_asc.head(bottom_volume_abs_count)
                    range_data.append({
                        "指標": "Volume",
                        "範圍類型": "最低到最高",
                        "最大值": f"{int(bottom_volume_abs.max()):,}",
                        "最小值": f"{int(bottom_volume_abs.min()):,}"
                    })

                # 📈 股價漲跌幅 (%) 范围
                sorted_price_change_abs = data["📈 股價漲跌幅 (%)"].dropna().sort_values(ascending=False)
                if len(sorted_price_change_abs) > 0:
                    top_price_change_abs_count = max(1, int(len(sorted_price_change_abs) * PERCENTILE_THRESHOLD / 100))
                    top_price_change_abs = sorted_price_change_abs.head(top_price_change_abs_count)
                    range_data.append({
                        "指標": "📈 股價漲跌幅 (%)",
                        "範圍類型": "最高到最低",
                        "最大值": f"{top_price_change_abs.max():.2f}%",
                        "最小值": f"{top_price_change_abs.min():.2f}%"
                    })
                sorted_price_change_abs_asc = data["📈 股價漲跌幅 (%)"].dropna().sort_values(ascending=True)
                if len(sorted_price_change_abs_asc) > 0:
                    bottom_price_change_abs_count = max(1, int(len(sorted_price_change_abs_asc) * PERCENTILE_THRESHOLD / 100))
                    bottom_price_change_abs = sorted_price_change_abs_asc.head(bottom_price_change_abs_count)
                    range_data.append({
                        "指標": "📈 股價漲跌幅 (%)",
                        "範圍類型": "最低到最高",
                        "最大值": f"{bottom_price_change_abs.max():.2f}%",
                        "最小值": f"{bottom_price_change_abs.min():.2f}%"
                    })

                # 📊 成交量變動幅 (%) 范围
                sorted_volume_change_abs = data["📊 成交量變動幅 (%)"].dropna().sort_values(ascending=False)
                if len(sorted_volume_change_abs) > 0:
                    top_volume_change_abs_count = max(1, int(len(sorted_volume_change_abs) * PERCENTILE_THRESHOLD / 100))
                    top_volume_change_abs = sorted_volume_change_abs.head(top_volume_change_abs_count)
                    range_data.append({
                        "指標": "📊 成交量變動幅 (%)",
                        "範圍類型": "最高到最低",
                        "最大值": f"{top_volume_change_abs.max():.2f}%",
                        "最小值": f"{top_volume_change_abs.min():.2f}%"
                    })
                sorted_volume_change_abs_asc = data["📊 成交量變動幅 (%)"].dropna().sort_values(ascending=True)
                if len(sorted_volume_change_abs_asc) > 0:
                    bottom_volume_change_abs_count = max(1, int(len(sorted_volume_change_abs_asc) * PERCENTILE_THRESHOLD / 100))
                    bottom_volume_change_abs = sorted_volume_change_abs_asc.head(bottom_volume_change_abs_count)
                    range_data.append({
                        "指標": "📊 成交量變動幅 (%)",
                        "範圍類型": "最低到最高",
                        "最大值": f"{bottom_volume_change_abs.max():.2f}%",
                        "最小值": f"{bottom_volume_change_abs.min():.2f}%"
                    })

                # 创建并显示合并表格
                if range_data:
                    range_df = pd.DataFrame(range_data)
                    st.dataframe(
                        range_df,
                        use_container_width=True,
                        column_config={
                            "指標": st.column_config.TextColumn("指標", width="medium"),
                            "範圍類型": st.column_config.TextColumn("範圍類型", width="medium"),
                            "最大值": st.column_config.TextColumn("最大值", width="small"),
                            "最小值": st.column_config.TextColumn("最小值", width="small")
                        }
                    )
                else:
                    st.write("無有效數據範圍可顯示")

                # 显示含异动标记的历史资料（新增列：VWAP, MFI, OBV, VIX, VIX_EMA_Fast, VIX_EMA_Slow）
                st.subheader(f"📋 歷史資料：{ticker}")
                display_data = data[["Datetime","Open","Low","High", "Close", "Volume", "Price Change %", 
                                     "Volume Change %", "📈 股價漲跌幅 (%)", 
                                     "📊 成交量變動幅 (%)","Close_Difference", "異動標記",
                                     "成交量標記", "K線形態", "單根解讀", "VWAP", "MFI", "OBV", "VIX", "VIX_EMA_Fast", "VIX_EMA_Slow"]].tail(15)
                if not display_data.empty:
                    st.dataframe(
                        display_data,
                        height=600,
                        use_container_width=True,
                        column_config={
                            "異動標記": st.column_config.TextColumn(width="large"),
                            "單根解讀": st.column_config.TextColumn(width="large")
                        }
                    )
                else:
                    st.warning(f"⚠️ {ticker} 歷史數據表無內容可顯示")

                # 添加下载按钮
                csv = data.to_csv(index=False)
                st.download_button(
                    label=f"📥 下載 {ticker} 數據 (CSV)",
                    data=csv,
                    file_name=f"{ticker}_數據_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                )

            except Exception as e:
                st.warning(f"⚠️ 無法取得 {ticker} 的資料：{e}，將跳過此股票")
                continue

        st.markdown("---")
        st.info("📡 頁面將在 5 分鐘後自動刷新...")

    time.sleep(REFRESH_INTERVAL)
    placeholder.empty()
