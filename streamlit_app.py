"""
شاشة الأسهم الحية — نسخة سحابية (Streamlit)
==========================================
تعمل على خادم مجاني (Streamlit Cloud) بدل جهازك الشخصي،
تبقى تعمل حتى لو أغلقت اللابتوب، وتفتحها من رابط واحد
على جوالك من أي مكان.

هذه النسخة تستخدم تحديثاً داخلياً حقيقياً (autorefresh) بدل
إعادة تحميل الصفحة بالكامل، لذا حالة كل شيء (السهم المختار،
وضع مراقبة كل الأسهم، وأي تنبيه أُرسل) تبقى محفوظة بدقة تامة،
ولا تُرسل أي تنبيهات مكررة.
"""

from datetime import datetime, time as dtime, timedelta
import json
import base64

import pandas as pd
import feedparser
import requests
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pytz
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from tradingview_ta import TA_Handler, Interval

NY_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
OPENING_RANGE_MINUTES = 15
REFRESH_SECONDS = 30

# الأسهم المتوافقة مع الشريعة، مع بورصتها الصحيحة (لازمة لتحليل تريدنج فيو)
EXCHANGE_MAP = {
    "INTC": "NASDAQ",
    "AEYE": "NASDAQ", "MODD": "NASDAQ", "HTFL": "NASDAQ", "RCMT": "NASDAQ",
    "IMDX": "NASDAQ", "LPTH": "NASDAQ", "ELMT": "NASDAQ", "CPRT": "NASDAQ",
    "IOVA": "NASDAQ",
    "BW": "NYSE",
    "REA": "AMEX", "BUDA": "AMEX",
}

TECHNICAL_LABELS = {
    "STRONG_BUY": ("شراء قوي 🟢🟢", "#0a8a3f"),
    "BUY": ("شراء 🟢", "#22c55e"),
    "NEUTRAL": ("محايد ⚪", "#9e9e9e"),
    "SELL": ("بيع 🔴", "#ef4444"),
    "STRONG_SELL": ("بيع قوي 🔴🔴", "#b91c1c"),
}

st.set_page_config(page_title="شاشة حية", layout="wide")

# تحديث داخلي حقيقي كل 30 ثانية — بلا إعادة تحميل الصفحة بالكامل،
# وبلا فقدان أي حالة محفوظة (السهم، التنبيهات المُرسلة، إلخ)
st_autorefresh(interval=REFRESH_SECONDS * 1000, key="auto_refresh")

# اتجاه الكتابة من اليمين لليسار
st.markdown(
    """
    <style>
    body, .stApp { direction: rtl; text-align: right; font-family: Tahoma, Arial; }
    </style>
    """,
    unsafe_allow_html=True,
)

# قائمة أسهمك المفضلة (الشريعة متوافقة)
WATCHLIST = [
    "INTC",
    "AEYE", "MODD", "HTFL", "RCMT", "IMDX", "LPTH",
    "REA", "BW", "ELMT", "BUDA", "CPRT", "IOVA",
]
CUSTOM_OPTION = "سهم آخر (اكتبه يدوياً)"

# كلمات مفتاحية إنجليزية تدل على خبر إيجابي محتمل (عناوين الأخبار تجي إنجليزي دايماً)
POSITIVE_NEWS_KEYWORDS = [
    "beats", "surge", "approval", "upgrade", "raises guidance",
    "strong buy", "record revenue", "contract win",
]

# حالة "التسليح" لكل سهم — محفوظة في ذاكرة الجلسة (session_state)،
# تبقى ثابتة طوال الجلسة ولا تُعاد للصفر إلا عند إغلاق التبويب فعلياً
if "armed" not in st.session_state:
    st.session_state.armed = {}

# قائمة قنوات ntfy المحفوظة — اختر منها مباشرة، أو اترك بدون إشعارات،
# أو اكتب قناة جديدة يدوياً
NTFY_CHANNELS = ["بدون إشعارات (تعطيل)", "safar-nvda-alerts-9284", "قناة أخرى (اكتبها يدوياً)"]

ntfy_choice = st.selectbox("قناة إشعارات ntfy", NTFY_CHANNELS, index=1)

if ntfy_choice == "قناة أخرى (اكتبها يدوياً)":
    ntfy_topic = st.text_input("اكتب اسم القناة", value="").strip()
elif ntfy_choice == "بدون إشعارات (تعطيل)":
    ntfy_topic = ""
else:
    ntfy_topic = ntfy_choice

watch_all = st.checkbox(
    "راقب كل أسهم القائمة معاً وأرسل تنبيه لأي اختراق (يحتاج قناة ntfy أعلاه)",
    value=False,
)

watch_ipos = st.checkbox(
    "🆕 فعّل تنبيه اكتتابات السوق الأمريكي الجديدة (يحتاج قناة ntfy أعلاه)",
    value=False,
)

watch_stock_news = st.checkbox(
    "📰 فعّل تنبيه الأخبار الإيجابية لأسهم القائمة (يحتاج قناة ntfy أعلاه)",
    value=False,
)

# نفحص الاكتتابات والأخبار كل 5 دقائق بس (مو كل 30 ثانية)، حتى ما
# نتجاوز الحد المجاني لطلبات API
_now_ts = datetime.now(NY_TZ).timestamp()
if ntfy_topic and watch_ipos and (_now_ts - st.session_state.last_ipo_check > 300):
    check_new_ipos_and_notify(ntfy_topic)
    st.session_state.last_ipo_check = _now_ts

if ntfy_topic and watch_stock_news and (_now_ts - st.session_state.last_news_check > 300):
    check_stock_news_and_notify(ntfy_topic)
    st.session_state.last_news_check = _now_ts


def fetch_raw_data(sym: str):
    """يجيب آخر 5 أيام تداول كاملة (بيانات دقيقة بدقيقة) دفعة واحدة،
    حتى نقدر نختار أي يوم منها للمراجعة، مو بس اليوم الحالي."""
    ticker = yf.Ticker(sym)
    data = ticker.history(period="5d", interval="1m")
    if data.empty:
        return None
    data.index = data.index.tz_convert(NY_TZ)
    return data


def available_trading_dates(data: pd.DataFrame):
    """قائمة أيام التداول المتوفرة فعلياً في البيانات، من الأحدث للأقدم."""
    if data is None or data.empty:
        return []
    dates = sorted(set(data.index.date), reverse=True)
    return dates


def extract_session_for_date(data: pd.DataFrame, target_date):
    """يستخرج جلسة يوم تداول محدد من البيانات، ويحسب نطاق الافتتاح له."""
    day_start = NY_TZ.localize(datetime.combine(target_date, MARKET_OPEN))
    day_end = day_start + pd.Timedelta(hours=7)
    session = data[(data.index >= day_start) & (data.index < day_end)]
    if session.empty:
        return None, None, None

    open_end = day_start + pd.Timedelta(minutes=OPENING_RANGE_MINUTES)
    opening_window = session[session.index < open_end]
    if opening_window.empty:
        opening_window = session.iloc[:15]

    range_high = float(opening_window["High"].max())
    range_low = float(opening_window["Low"].min())
    return session, range_high, range_low


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """يجمع شموع الدقيقة الواحدة إلى شموع أكبر (5 دقائق، 15 دقيقة، إلخ)."""
    out = pd.DataFrame({
        "Open": df["Open"].resample(rule).first(),
        "High": df["High"].resample(rule).max(),
        "Low": df["Low"].resample(rule).min(),
        "Close": df["Close"].resample(rule).last(),
        "Volume": df["Volume"].resample(rule).sum(),
    }).dropna()
    return out


def fetch_and_prepare(sym: str):
    """نسخة مبسّطة تُستخدم لمراقبة كل الأسهم معاً (تاخذ دائماً آخر
    يوم تداول متوفر تلقائياً، بدون خيار اختيار تاريخ)."""
    data = fetch_raw_data(sym)
    if data is None:
        return None, None, None, False

    today_ny = datetime.now(NY_TZ).date()
    session, range_high, range_low = extract_session_for_date(data, today_ny)
    is_last_trading_day = False

    if session is None:
        dates = available_trading_dates(data)
        if not dates:
            return None, None, None, False
        session, range_high, range_low = extract_session_for_date(data, dates[0])
        is_last_trading_day = True
        if session is None:
            return None, None, None, False

    return session, range_high, range_low, is_last_trading_day


@st.cache_data(ttl=120)
def get_technical_outlook(sym: str):
    """يجيب تقييماً فنياً جاهزاً من تريدنج فيو (شراء/بيع/محايد)
    مع عدد المؤشرات المؤيدة لكل اتجاه. محفوظ مؤقتاً لمدة دقيقتين
    (مو كل 30 ثانية زي السعر)، لأن التحليل الفني لا يتغير بهذا التكرار،
    وتجنباً لأي ضغط غير ضروري على المصدر."""
    exchange = EXCHANGE_MAP.get(sym, "NASDAQ")
    try:
        handler = TA_Handler(
            symbol=sym,
            screener="america",
            exchange=exchange,
            interval=Interval.INTERVAL_15_MINUTES,
        )
        summary = handler.get_analysis().summary
        return {
            "توصية": summary.get("RECOMMENDATION", "غير متاح"),
            "شراء": summary.get("BUY", 0),
            "بيع": summary.get("SELL", 0),
            "محايد": summary.get("NEUTRAL", 0),
        }
    except Exception:
        return None


# نجيب بيانات كل الأسهم مرة واحدة فقط (مو مرتين) لو "راقب كل الأسهم"
# مفعّلة — نفس البيانات تُستخدم للوحة الألوان ولفحص الاختراقات معاً،
# لتفادي مضاعفة عدد الطلبات لياهو فاينانس بلا داعٍ
watch_data = {}
if watch_all:
    for wsym in WATCHLIST:
        w_session, w_high, w_low, _ = fetch_and_prepare(wsym)
        if w_session is None:
            continue
        w_open = float(w_session["Open"].iloc[0])
        w_last = float(w_session["Close"].iloc[-1])
        watch_data[wsym] = {
            "session": w_session, "high": w_high, "low": w_low,
            "open": w_open, "last": w_last,
        }

# السهم المختار حالياً — محفوظ في ذاكرة الجلسة، يبقى ثابتاً حتى
# يُغيَّر بضغطة على لوحة الألوان أو من القائمة العادية
if "selected_symbol" not in st.session_state:
    st.session_state.selected_symbol = "INTC"

if watch_all:
    # لوحة ألوان تفاعلية: كل سهم زر حقيقي بخلفية خضراء (صاعد) أو
    # حمراء (هابط) اليوم — اضغط على أي مربع ليصير هو السهم المعروض
    # في الشارت والتحليل تحت مباشرة
    st.markdown("###### 🎨 لوحة الأسهم (اضغط على أي سهم لعرضه)")
    board_cols = st.columns(4)
    style_blocks = []
    TA_ICON = {
        "STRONG_BUY": "⬆️⬆️", "BUY": "⬆️", "NEUTRAL": "➡️",
        "SELL": "⬇️", "STRONG_SELL": "⬇️⬇️",
    }
    for i, wsym in enumerate(WATCHLIST):
        with board_cols[i % 4]:
            tile_key = f"tile_{wsym}"
            ta = get_technical_outlook(wsym)
            ta_icon = TA_ICON.get(ta["توصية"], "") if ta else ""
            # عدد المؤشرات الفنية المؤيدة للشراء مقابل البيع (من تريدنج
            # فيو) — أقرب معلومة متاحة مجاناً لـ"عدد الصفقات شراء/بيع"،
            # فهي أصوات مؤشرات فنية، وليست صفقات فعلية منفذة في السوق
            ta_counts = f"🟢{ta['شراء']} 🔴{ta['بيع']}" if ta else ""
            if wsym in watch_data:
                d = watch_data[wsym]
                chg_pct = ((d["last"] - d["open"]) / d["open"]) * 100 if d["open"] else 0
                bg = "#0a8a3f" if chg_pct >= 0 else "#d0332f"
                label = f"{wsym} {ta_icon}\n{d['last']:.2f}$  {chg_pct:+.1f}%\n{ta_counts}"
            else:
                bg = "#9e9e9e"
                label = f"{wsym} {ta_icon}\n—\n{ta_counts}"

            with st.container(key=tile_key):
                if st.button(label, key=f"btn_{wsym}", use_container_width=True):
                    st.session_state.selected_symbol = wsym

            style_blocks.append(
                f".st-key-{tile_key} button {{background-color:{bg} !important; "
                f"color:white !important; border:none !important; white-space:pre-line;}}"
            )

    st.markdown(f"<style>{''.join(style_blocks)}</style>", unsafe_allow_html=True)
    st.caption(
        "⬆️ = التحليل الفني يميل للشراء — ⬇️ = يميل للبيع — ➡️ = محايد\n\n"
        "🟢/🔴 = عدد المؤشرات الفنية المؤيدة للشراء/البيع (من تريدنج فيو) — "
        "هذي أصوات مؤشرات تحليلية، وليست عدد صفقات فعلية منفذة في السوق"
    )

    custom_typed = st.text_input("أو اكتب رمز سهم آخر غير موجود في اللوحة", value="")
    if custom_typed.strip():
        st.session_state.selected_symbol = custom_typed.upper().strip()

    symbol = st.session_state.selected_symbol
else:
    col1, col2 = st.columns([2, 2])
    with col1:
        options = WATCHLIST + [CUSTOM_OPTION]
        default_index = options.index(st.session_state.selected_symbol) if st.session_state.selected_symbol in WATCHLIST else 0
        choice = st.selectbox("اختر من قائمتك", options, index=default_index)
    with col2:
        if choice == CUSTOM_OPTION:
            typed = st.text_input("أو اكتب رمز سهم آخر", value="")
            symbol = typed.upper().strip() or "INTC"
        else:
            symbol = choice
    st.session_state.selected_symbol = symbol




def send_ntfy_alert(topic: str, title: str, message: str, click_url: str = "") -> bool:
    try:
        headers = {"Title": title.encode("utf-8")}
        if click_url:
            headers["Click"] = click_url
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
        return True
    except Exception:
        return False



def check_new_ipos_and_notify(topic: str):
    """يفحص اكتتابات السوق الأمريكي الجديدة (عبر Finnhub) ويرسل تنبيه
    لكل اكتتاب جديد لم يُرسل عنه تنبيه من قبل. عند الضغط على التنبيه
    يفتح رابط بحث جاهز عن خبر الاكتتاب."""
    if not topic:
        return
    try:
        api_key = st.secrets.get("finnhub_api_key", "")
        if not api_key:
            return
        today = datetime.now(NY_TZ).date()
        from_date = today.isoformat()
        to_date = (today + timedelta(days=7)).isoformat()
        url = (
            f"https://finnhub.io/api/v1/calendar/ipo"
            f"?from={from_date}&to={to_date}&token={api_key}"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return
        ipos = resp.json().get("ipoCalendar", [])
        for ipo in ipos:
            symbol = ipo.get("symbol", "")
            name = ipo.get("name", "")
            ipo_date = ipo.get("date", "")
            if not symbol or symbol in st.session_state.seen_ipos:
                continue
            st.session_state.seen_ipos.add(symbol)
            search_query = f"{name} {symbol} IPO".replace(" ", "+")
            click_url = f"https://www.google.com/search?q={search_query}&tbm=nws"
            send_ntfy_alert(
                topic,
                f"🆕 اكتتاب جديد: {symbol}",
                f"{name} — تاريخ الاكتتاب المتوقع: {ipo_date}",
                click_url=click_url,
            )
    except Exception:
        pass


def check_stock_news_and_notify(topic: str):
    """يفحص أخبار كل سهم من قائمة المراقبة عبر RSS، ويرسل تنبيه فقط
    للأخبار اللي عنوانها يحتوي كلمة مفتاحية إيجابية. عند الضغط على
    التنبيه يفتح رابط الخبر الأصلي مباشرة."""
    if not topic:
        return
    for sym in WATCHLIST:
        try:
            rss_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:5]:
                link = entry.get("link", "")
                title = entry.get("title", "")
                if not link or link in st.session_state.seen_news_links:
                    continue
                title_lower = title.lower()
                matched_keyword = next(
                    (kw for kw in POSITIVE_NEWS_KEYWORDS if kw in title_lower), None
                )
                st.session_state.seen_news_links.add(link)
                if matched_keyword:
                    send_ntfy_alert(
                        topic,
                        f"📰 خبر إيجابي عن {sym}",
                        title,
                        click_url=link,
                    )
        except Exception:
            continue



    """يرسل تنبيهاً مرة واحدة بالضبط عند لحظة الاختراق، ولا يكرره
    إلا بعد ما يرجع السعر تحت مستوى الدخول ثم يخترق من جديد.
    يسجّل أيضاً كل اختراق فعلي في سجل الصفقات."""
    was_armed = st.session_state.armed.get(sym, True)
    if last < entry:
        st.session_state.armed[sym] = True
        return False
    if last >= entry and was_armed:
        msg = (
            f"السعر الحالي: {last:.2f}$\n"
            f"وقف الخسارة المقترح: {stop:.2f}$\n"
            f"الهدف المقترح: {target:.2f}$\n"
            f"⚠️ هذا تنبيه للمراجعة فقط، القرار بيدك"
        )
        if send_ntfy_alert(topic, f"اختراق صاعد ⬆️ {sym}", msg):
            st.session_state.armed[sym] = False
            st.session_state.trade_log.insert(0, {
                "الوقت": datetime.now(NY_TZ).strftime("%H:%M:%S"),
                "السهم": sym,
                "النوع": "اختراق صاعد ⬆️",
                "السعر": f"{last:.2f}$",
            })
            return True
    return False


def check_stop_proximity_and_notify(sym: str, last: float, entry: float, stop: float, topic: str, key_id: str = None):
    """ينبّه لمرة واحدة لما السعر يقترب من وقف الخسارة (يصل لمنتصف
    المسافة بين الدخول والوقف)، حتى تنتبه قبل ما يُضرب فعلياً."""
    key_id = key_id or sym
    halfway = entry - (entry - stop) / 2
    key = f"stopwarn_{key_id}"
    already_warned = st.session_state.armed.get(key, False)
    if last > halfway:
        st.session_state.armed[key] = False
        return False
    if last <= halfway and not already_warned:
        msg = f"السعر الحالي: {last:.2f}$ — اقترب من وقف الخسارة ({stop:.2f}$)"
        if send_ntfy_alert(topic, f"⚠️ اقتراب من وقف الخسارة {sym}", msg):
            st.session_state.armed[key] = True
            st.session_state.trade_log.insert(0, {
                "الوقت": datetime.now(NY_TZ).strftime("%H:%M:%S"),
                "السهم": sym,
                "النوع": "⚠️ اقتراب من الوقف",
                "السعر": f"{last:.2f}$",
            })
            return True
    return False


def check_target_proximity_and_notify(trade_id: str, sym: str, last: float, entry: float, target: float, topic: str):
    """ينبّه لمرة واحدة لما السعر يقترب من الهدف (يصل لمنتصف
    المسافة بين الدخول والهدف) — نفس منطق اقتراب الوقف، بالاتجاه المعاكس."""
    halfway = entry + (target - entry) / 2
    key = f"targetwarn_{trade_id}"
    already_warned = st.session_state.armed.get(key, False)
    if last < halfway:
        st.session_state.armed[key] = False
        return False
    if last >= halfway and not already_warned:
        msg = f"السعر الحالي: {last:.2f}$ — اقترب من الهدف ({target:.2f}$)"
        if send_ntfy_alert(topic, f"🎯 اقتراب من الهدف {sym}", msg):
            st.session_state.armed[key] = True
            st.session_state.trade_log.insert(0, {
                "الوقت": datetime.now(NY_TZ).strftime("%H:%M:%S"),
                "السهم": sym, "النوع": "🎯 اقتراب من الهدف", "السعر": f"{last:.2f}$",
            })
            return True
    return False


def check_candle_alert(trade_id: str, sym: str, candle_df: pd.DataFrame, topic: str):
    """ينبّه عند بداية كل شمعة جديدة (وملخص إغلاق الشمعة السابقة)،
    بنفس حجم الشمعة المختار حالياً في الشارت الرئيسي."""
    if candle_df is None or candle_df.empty:
        return
    last_row = candle_df.iloc[-1]
    last_ts = candle_df.index[-1]
    key = f"candle_ts_{trade_id}"
    prev_ts = st.session_state.armed.get(key)

    if prev_ts is None:
        st.session_state.armed[key] = last_ts
        return

    if last_ts != prev_ts:
        # الشمعة السابقة أُغلقت الآن — نرسل ملخصها
        if prev_ts in candle_df.index:
            prev_row = candle_df.loc[prev_ts]
            msg_close = (
                f"إغلاق الشمعة — فتح: {prev_row['Open']:.2f}$ / إغلاق: {prev_row['Close']:.2f}$ / "
                f"أعلى: {prev_row['High']:.2f}$ / أدنى: {prev_row['Low']:.2f}$"
            )
            send_ntfy_alert(topic, f"🕯️ إغلاق شمعة {sym}", msg_close)

        # وشمعة جديدة بدأت
        msg_open = f"بدأت شمعة جديدة — سعر الافتتاح: {last_row['Open']:.2f}$"
        send_ntfy_alert(topic, f"🕯️ افتتاح شمعة {sym}", msg_open)

        st.session_state.trade_log.insert(0, {
            "الوقت": datetime.now(NY_TZ).strftime("%H:%M:%S"),
            "السهم": sym, "النوع": "🕯️ شمعة جديدة", "السعر": f"{last_row['Open']:.2f}$",
        })
        st.session_state.armed[key] = last_ts


# سجل بسيط لكل اختراق أو تنبيه صار خلال الجلسة
if "trade_log" not in st.session_state:
    st.session_state.trade_log = []

# صفقاتي المفتوحة — قائمة صفقات فعلية سجّلها المستخدم، كل واحدة
# لها تنبيهات مستقلة (هدف، وقف، وشمعة اختيارية)
# تُحفظ بشكل دائم في ملف trades.json داخل مستودع GitHub، حتى تبقى
# موجودة حتى لو التطبيق أعاد التشغيل أو نام لعدم الاستخدام
GITHUB_REPO = "abojumanh/intc-live-chart"
TRADES_FILE_PATH = "trades.json"


def load_trades_from_github():
    try:
        token = st.secrets["github_token"]
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{TRADES_FILE_PATH}"
        headers = {"Authorization": f"token {token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
            data = json.loads(content)
            return data.get("trades", []), data.get("counter", 0)
        return [], 0
    except Exception:
        return [], 0


def save_trades_to_github(trades, counter):
    try:
        token = st.secrets["github_token"]
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{TRADES_FILE_PATH}"
        headers = {"Authorization": f"token {token}"}
        get_resp = requests.get(url, headers=headers, timeout=10)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None
        content_str = json.dumps({"trades": trades, "counter": counter}, ensure_ascii=False)
        content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
        payload = {"message": "تحديث الصفقات", "content": content_b64}
        if sha:
            payload["sha"] = sha
        put_resp = requests.put(url, headers=headers, json=payload, timeout=10)
        if put_resp.status_code not in (200, 201):
            st.error(f"⚠️ فشل الحفظ على GitHub — كود: {put_resp.status_code} — {put_resp.text[:300]}")
    except Exception as e:
        st.error(f"⚠️ خطأ أثناء الحفظ: {e}")


if "my_trades" not in st.session_state:
    loaded_trades, loaded_counter = load_trades_from_github()
    st.session_state.my_trades = loaded_trades
    st.session_state.trade_id_counter = loaded_counter

# رموز الاكتتابات اللي سبق أرسلنا تنبيه عنها (عشان ما نكرر نفس التنبيه)
if "seen_ipos" not in st.session_state:
    st.session_state.seen_ipos = set()
if "last_ipo_check" not in st.session_state:
    st.session_state.last_ipo_check = 0

# روابط الأخبار اللي سبق أرسلنا تنبيه عنها
if "seen_news_links" not in st.session_state:
    st.session_state.seen_news_links = set()
if "last_news_check" not in st.session_state:
    st.session_state.last_news_check = 0



# مراقبة كل أسهم القائمة معاً (وضع اختياري) — نستخدم watch_data
# الجاهزة أعلاه (نفس البيانات المستخدمة للوحة الألوان)، بلا أي
# طلب إضافي مكرر لياهو فاينانس
if ntfy_topic and watch_all:
    sent_now = []
    for wsym, d in watch_data.items():
        w_stop = (d["high"] + d["low"]) / 2
        w_entry = d["high"]
        w_target = w_entry + 2 * (w_entry - w_stop)
        w_last = d["last"]
        if check_breakout_and_notify(wsym, w_last, w_entry, w_stop, w_target, ntfy_topic):
            sent_now.append(wsym)
        check_stop_proximity_and_notify(wsym, w_last, w_entry, w_stop, ntfy_topic)
    if sent_now:
        st.success(f"✅ تم إرسال تنبيه اختراق لجوالك عن: {', '.join(sent_now)}")

# ============ صفقاتي المفتوحة ============
st.markdown("### 💼 صفقاتي المفتوحة")

with st.expander("➕ سجّل صفقة جديدة"):
    tcol1, tcol2, tcol3 = st.columns(3)
    with tcol1:
        trade_options = WATCHLIST + [CUSTOM_OPTION]
        trade_choice = st.selectbox("السهم", trade_options, key="new_trade_symbol")
        if trade_choice == CUSTOM_OPTION:
            trade_symbol = st.text_input("اكتب رمز السهم", key="new_trade_symbol_custom").upper().strip()
        else:
            trade_symbol = trade_choice
    with tcol2:
        trade_entry = st.number_input("سعر الدخول ($)", min_value=0.0, value=0.0, step=0.01, format="%.2f", key="new_trade_entry")
        trade_qty = st.number_input("الكمية (عدد الأسهم)", min_value=0.0, value=0.0, step=1.0, key="new_trade_qty")
    with tcol3:
        trade_stop = st.number_input("وقف الخسارة ($) — اتركه 0 للحساب التلقائي", min_value=0.0, value=0.0, step=0.01, format="%.2f", key="new_trade_stop")
        trade_target = st.number_input("الهدف ($) — اتركه 0 للحساب التلقائي", min_value=0.0, value=0.0, step=0.01, format="%.2f", key="new_trade_target")

    candle_alert_enabled = st.checkbox("🕯️ فعّل تنبيه فتح/إغلاق الشمعة لهذه الصفقة", value=False, key="new_trade_candle")

    if st.button("إضافة الصفقة", key="add_trade_btn"):
        if trade_symbol and trade_entry > 0 and trade_qty > 0:
            st.session_state.trade_id_counter += 1
            st.session_state.my_trades.append({
                "id": st.session_state.trade_id_counter,
                "symbol": trade_symbol,
                "entry": trade_entry,
                "qty": trade_qty,
                "stop": trade_stop if trade_stop > 0 else None,
                "target": trade_target if trade_target > 0 else None,
                "candle_alerts": candle_alert_enabled,
            })
            save_trades_to_github(st.session_state.my_trades, st.session_state.trade_id_counter)
            st.success(f"✅ تمت إضافة صفقة {trade_symbol}")
        else:
            st.warning("عبّئ السهم وسعر الدخول والكمية على الأقل")

if not st.session_state.my_trades:
    st.caption("لا توجد صفقات مسجّلة بعد. اضغط أعلاه لإضافة صفقتك الأولى.")
else:
    trades_to_remove = []
    for trade in st.session_state.my_trades:
        t_session, t_high, t_low, _ = fetch_and_prepare(trade["symbol"])
        if t_session is None:
            st.warning(f"⚠️ لا توجد بيانات حالياً لسهم {trade['symbol']}")
            continue

        t_last = float(t_session["Close"].iloc[-1])
        t_stop = trade["stop"] or ((t_high + t_low) / 2)
        t_target = trade["target"] or (trade["entry"] + 2 * (trade["entry"] - t_stop))
        pnl = (t_last - trade["entry"]) * trade["qty"]
        pnl_pct = ((t_last - trade["entry"]) / trade["entry"]) * 100 if trade["entry"] else 0
        pnl_color = "#0a8a3f" if pnl >= 0 else "#d0332f"

        tc1, tc2 = st.columns([4, 1])
        with tc1:
            st.markdown(
                f"""
                <div style="border:1px solid #ddd; border-radius:10px; padding:10px 14px; margin-bottom:6px;">
                    <b>{trade['symbol']}</b> — دخول: {trade['entry']:.2f}$ × {trade['qty']:.0f} سهم
                    &nbsp;|&nbsp; السعر الحالي: {t_last:.2f}$
                    &nbsp;|&nbsp; الوقف: {t_stop:.2f}$ &nbsp;|&nbsp; الهدف: {t_target:.2f}$<br>
                    <span style="color:{pnl_color}; font-weight:bold;">
                        {'+' if pnl >= 0 else ''}{pnl:.2f}$ ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)
                    </span>
                    {' — 🕯️ تنبيه الشمعة مفعّل' if trade['candle_alerts'] else ''}
                </div>
                """,
                unsafe_allow_html=True,
            )
        with tc2:
            if st.button("🗑️ بيعتها", key=f"remove_trade_{trade['id']}"):
                trades_to_remove.append(trade["id"])

        if ntfy_topic:
            check_target_proximity_and_notify(str(trade["id"]), trade["symbol"], t_last, trade["entry"], t_target, ntfy_topic)
            check_stop_proximity_and_notify(trade["symbol"], t_last, trade["entry"], t_stop, ntfy_topic, key_id=str(trade["id"]))

    if trades_to_remove:
        st.session_state.my_trades = [t for t in st.session_state.my_trades if t["id"] not in trades_to_remove]
        save_trades_to_github(st.session_state.my_trades, st.session_state.trade_id_counter)
        st.rerun()

raw_data = fetch_raw_data(symbol)

if raw_data is None:
    st.warning("لا توجد بيانات متاحة لهذا السهم. تحقق من صحة الرمز.")
    st.stop()

trading_dates = available_trading_dates(raw_data)
today_ny = datetime.now(NY_TZ).date()

# قائمة اختيار يوم التداول للمراجعة — مفيدة خصوصاً وقت إغلاق السوق
# لتحليل عدة أيام سابقة قبل الافتتاح القادم، مو بس آخر يوم تلقائياً
date_labels = []
for d in trading_dates:
    label = d.strftime("%Y-%m-%d")
    if d == today_ny:
        label += " (اليوم)"
    date_labels.append(label)

default_idx = 0  # الأحدث دائماً أول عنصر بما إن القائمة مرتبة تنازلياً
selected_label = st.selectbox("يوم التداول المعروض للتحليل", date_labels, index=default_idx)
selected_date = trading_dates[date_labels.index(selected_label)]

session, range_high, range_low = extract_session_for_date(raw_data, selected_date)
is_last_trading_day = selected_date != today_ny

if session is None:
    st.warning("لا توجد بيانات لهذا اليوم تحديداً. جرّب يوماً آخر من القائمة.")
    st.stop()

if is_last_trading_day:
    st.info(
        f"📅 تعرض حالياً شارت يوم **{selected_date.strftime('%Y-%m-%d')}** للمراجعة والتحليل. "
        "المستويات المحسوبة (دخول/وقف/هدف) خاصة بذلك اليوم فقط."
    )

stop_loss = (range_high + range_low) / 2
entry_price = range_high
target_price = entry_price + 2 * (entry_price - stop_loss)
open_price = float(session["Open"].iloc[0])
last_price = float(session["Close"].iloc[-1])
day_high = float(session["High"].max())
day_low = float(session["Low"].min())
change = last_price - open_price
change_pct = (change / open_price) * 100 if open_price else 0
last_update = datetime.now(NY_TZ).strftime("%H:%M:%S")

if change >= 0:
    price_color = "#0a8a3f"
    arrow = "▲"
    sign = "+"
else:
    price_color = "#d0332f"
    arrow = "▼"
    sign = ""

# تنبيه السهم المختار فقط (يعمل دائماً بغض النظر عن وضع "راقب كل الأسهم")
if ntfy_topic and not watch_all:
    if check_breakout_and_notify(symbol, last_price, entry_price, stop_loss, target_price, ntfy_topic):
        st.success(f"✅ تم إرسال إشعار الاختراق لجوالك عبر قناة {ntfy_topic}")
    check_stop_proximity_and_notify(symbol, last_price, entry_price, stop_loss, ntfy_topic)

st.markdown(
    f"<h2>شاشة {symbol} الحية — تتحدث كل {REFRESH_SECONDS} ثانية تلقائياً</h2>",
    unsafe_allow_html=True,
)
st.markdown(
    f"""
    <div style="font-size:64px; font-weight:bold; color:{price_color}; line-height:1.1;">
        {last_price:.2f}$ <span style="font-size:36px;">{arrow}</span>
    </div>
    <div style="font-size:20px; color:{price_color}; margin-top:4px;">
        {sign}{change:.2f} ({sign}{change_pct:.2f}%)
    </div>
    <div style="font-size:15px; color:gray; margin-top:6px;">آخر تحديث: {last_update} (EDT)</div>
    """,
    unsafe_allow_html=True,
)

# بطاقة التحليل الفني (تريدنج فيو) — تقييم جاهز شراء/بيع/محايد
outlook = get_technical_outlook(symbol)
if outlook:
    label, badge_color = TECHNICAL_LABELS.get(outlook["توصية"], (outlook["توصية"], "#9e9e9e"))
    st.markdown(
        f"""
        <div style="background:{badge_color}; color:white; border-radius:10px;
                    padding:10px 16px; margin-top:8px; display:inline-block;">
            <b>📊 التحليل الفني: {label}</b><br>
            <span style="font-size:13px;">
                مؤيدون للشراء: {outlook['شراء']} — مؤيدون للبيع: {outlook['بيع']} — محايد: {outlook['محايد']}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("💡 تحليل فني تلقائي من تريدنج فيو، يتحدث كل دقيقتين تقريباً — للمراجعة فقط، وليس توصية استثمارية")
else:
    st.caption("📊 التحليل الفني غير متاح حالياً لهذا السهم")

# نسبة المخاطرة إلى العائد المتوقعة بناءً على مستويات الدخول
# والوقف والهدف المحسوبة تلقائياً
risk_amount = entry_price - stop_loss
reward_amount = target_price - entry_price
rr_ratio = reward_amount / risk_amount if risk_amount > 0 else 0
st.info(f"⚖️ نسبة المخاطرة إلى العائد: **1 : {rr_ratio:.1f}** — (مخاطرة {risk_amount:.2f}$ مقابل عائد محتمل {reward_amount:.2f}$)")

# حاسبة صفقتك الفعلية — أدخل الكمية وسعر دخولك الحقيقي لترى ربحك/خسارتك الحية
with st.expander("🧮 حاسبة صفقتي (اختياري)"):
    calc_col1, calc_col2 = st.columns(2)
    with calc_col1:
        my_shares = st.number_input("عدد الأسهم اللي اشتريتها", min_value=0.0, value=0.0, step=1.0)
    with calc_col2:
        my_entry = st.number_input("سعر دخولك الفعلي ($)", min_value=0.0, value=0.0, step=0.01, format="%.2f")

    if my_shares > 0 and my_entry > 0:
        pnl_dollars = (last_price - my_entry) * my_shares
        pnl_pct = ((last_price - my_entry) / my_entry) * 100
        pnl_color = "#0a8a3f" if pnl_dollars >= 0 else "#d0332f"
        pnl_sign = "+" if pnl_dollars >= 0 else ""
        st.markdown(
            f"""
            <div style="font-size:32px; font-weight:bold; color:{pnl_color};">
                {pnl_sign}{pnl_dollars:.2f}$ ({pnl_sign}{pnl_pct:.2f}%)
            </div>
            <div style="font-size:14px; color:gray;">
                القيمة الحالية: {(last_price * my_shares):.2f}$ — التكلفة الأصلية: {(my_entry * my_shares):.2f}$
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.caption("أدخل الكمية وسعر الدخول لحساب ربحك أو خسارتك الحالية تلقائياً")

col_a, col_b = st.columns(2)

with col_a:
    # حجم الشمعة نفسها — كل شمعة تلخّص هذه المدة من الحركة، بدل
    # شمعة كل دقيقة واحدة فقط. هذا يقلل عدد الشموع ويجعل الشارت
    # أوضح بكثير، تماماً مثل تطبيقات التداول الاحترافية
    candle_choice = st.selectbox(
        "حجم الشمعة",
        ["1 دقيقة", "5 دقائق", "15 دقيقة", "30 دقيقة", "60 دقيقة"],
        index=1,
    )
    candle_rule = {
        "1 دقيقة": "1min", "5 دقائق": "5min", "15 دقيقة": "15min",
        "30 دقيقة": "30min", "60 دقيقة": "60min",
    }[candle_choice]

# تنبيهات فتح/إغلاق الشمعة للصفقات المفعّلة — تتبع نفس حجم الشمعة
# المختار هنا بالضبط، لكل صفقة على حدة
if ntfy_topic:
    for trade in st.session_state.my_trades:
        if not trade.get("candle_alerts"):
            continue
        t_raw = fetch_raw_data(trade["symbol"])
        if t_raw is None:
            continue
        t_dates = available_trading_dates(t_raw)
        if not t_dates:
            continue
        t_session_1m, _, _ = extract_session_for_date(t_raw, t_dates[0])
        if t_session_1m is None or t_session_1m.empty:
            continue
        t_candles = resample_ohlc(t_session_1m, candle_rule) if candle_rule != "1min" else t_session_1m
        check_candle_alert(str(trade["id"]), trade["symbol"], t_candles, ntfy_topic)

with col_b:
    # التكبير على الجوال بلمسة الإصبع غير موثوق دائماً على كل المتصفحات،
    # لذا نضيف تحكماً مباشراً وأكيداً بالمدة الزمنية المعروضة — أسهل
    # وأوضح من محاولة السحب بإصبعين
    window_choice = st.selectbox(
        "المدة الزمنية المعروضة",
        ["آخر 30 دقيقة", "آخر ساعة", "آخر ساعتين", "آخر 4 ساعات", "اليوم كامل"],
        index=4,
    )
    window_minutes = {
        "آخر 30 دقيقة": 30, "آخر ساعة": 60, "آخر ساعتين": 120,
        "آخر 4 ساعات": 240, "اليوم كامل": None,
    }[window_choice]

col_c, col_d = st.columns(2)

with col_c:
    # المتوسط المتحرك البسيط (SMA) — خط يلخّص اتجاه السعر العام،
    # لكنه مؤشر "متأخر" (يتبع السعر بعد حدوثه)، أنسب للمدى المتوسط
    # أكثر من مضاربة اليوم الواحد اللي تعتمد استراتيجيتك عليها
    show_sma = st.checkbox("إظهار المتوسط المتحرك (SMA)", value=False)

with col_d:
    sma_period = st.number_input(
        "عدد الشموع في المتوسط", min_value=2, max_value=100, value=20, step=1,
        disabled=not show_sma,
    )

# VWAP (متوسط السعر المرجّح بالحجم) — أنسب بكثير من SMA لاستراتيجية
# اختراق نطاق الافتتاح، لأنه يعكس "السعر العادل" الحقيقي لليوم بناءً
# على أين تم تداول أغلب الكميات، ويبدأ من أول دقيقة في السوق
show_vwap = st.checkbox("إظهار VWAP (متوسط السعر المرجّح بالحجم)", value=True)


resampled = resample_ohlc(session, candle_rule) if candle_rule != "1min" else session

# نحسب المؤشرات على كامل بيانات اليوم أولاً (قبل قص المدة المعروضة)،
# حتى تكون صحيحة من أول نقطة تظهر على الشارت ولا تبدأ بفراغ في
# بداية النافذة الزمنية المختارة
if show_sma:
    resampled["SMA"] = resampled["Close"].rolling(window=int(sma_period)).mean()

if show_vwap:
    typical_price = (resampled["High"] + resampled["Low"] + resampled["Close"]) / 3
    resampled["VWAP"] = (typical_price * resampled["Volume"]).cumsum() / resampled["Volume"].cumsum()

if window_minutes is not None:
    cutoff = resampled.index[-1] - pd.Timedelta(minutes=window_minutes)
    chart_session = resampled[resampled.index >= cutoff]
    if chart_session.empty:
        chart_session = resampled
else:
    chart_session = resampled

fig = make_subplots(
    rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
    vertical_spacing=0.03,
)

fig.add_trace(go.Candlestick(
    x=chart_session.index, open=chart_session["Open"], high=chart_session["High"],
    low=chart_session["Low"], close=chart_session["Close"], name=symbol,
), row=1, col=1)

if show_sma and "SMA" in chart_session.columns:
    fig.add_trace(go.Scatter(
        x=chart_session.index, y=chart_session["SMA"],
        mode="lines", name=f"SMA {int(sma_period)}",
        line=dict(color="#FF8C00", width=2),
    ), row=1, col=1)

if show_vwap and "VWAP" in chart_session.columns:
    fig.add_trace(go.Scatter(
        x=chart_session.index, y=chart_session["VWAP"],
        mode="lines", name="VWAP",
        line=dict(color="#8E24AA", width=2, dash="dot"),
    ), row=1, col=1)

fig.add_hline(y=entry_price, line_dash="dash", line_color="red", row=1, col=1)
fig.add_annotation(
    x=chart_session.index[len(chart_session) // 2], y=entry_price,
    text=f"⬆️ دخول عند: {entry_price:.2f}$", showarrow=False,
    bgcolor="#0a8a3f", font=dict(color="white", size=12), yshift=12, row=1, col=1,
)

fig.add_hline(y=range_low, line_dash="dash", line_color="green", row=1, col=1)
fig.add_annotation(
    x=chart_session.index[-1], y=range_low,
    text=f"$دعم: {range_low:.2f}", showarrow=False,
    font=dict(color="gray", size=11), yshift=-12, xanchor="right", row=1, col=1,
)

fig.add_hline(y=stop_loss, line_dash="dot", line_color="#d0332f", row=1, col=1)
fig.add_annotation(
    x=chart_session.index[len(chart_session) // 2], y=stop_loss,
    text=f"⛔ توقف عند: {stop_loss:.2f}$", showarrow=False,
    bgcolor="#d0332f", font=dict(color="white", size=12), yshift=-12, row=1, col=1,
)

# تسميات الافتتاح/الأعلى/الأدنى تُحسب من المدة المعروضة حالياً على
# الشارت (لا من اليوم كامل دائماً)، حتى تبقى ضمن نطاق الشارت المرئي
chart_open = float(chart_session["Open"].iloc[0])
chart_high = float(chart_session["High"].max())
chart_low = float(chart_session["Low"].min())

fig.add_annotation(
    x=chart_session.index[0], y=chart_open,
    text=f"$افتتاح: {chart_open:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11), ax=-40, ay=-20, row=1, col=1,
)

idx_high = chart_session["High"].idxmax()
fig.add_annotation(
    x=idx_high, y=chart_high,
    text=f"$أعلى سعر: {chart_high:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11), ax=0, ay=-30, row=1, col=1,
)

idx_low = chart_session["Low"].idxmin()
fig.add_annotation(
    x=idx_low, y=chart_low,
    text=f"$أدنى سعر: {chart_low:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11), ax=0, ay=30, row=1, col=1,
)

fig.add_annotation(
    x=chart_session.index[-1], y=last_price,
    text=f"$إغلاق حالي: {last_price:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11, color=price_color), ax=40, ay=-20, row=1, col=1,
)

colors = ["green" if c >= o else "red" for o, c in zip(chart_session["Open"], chart_session["Close"])]
fig.add_trace(go.Bar(x=chart_session.index, y=chart_session["Volume"], marker_color=colors, name="الحجم"), row=2, col=1)

fig.update_layout(
    xaxis_rangeslider_visible=False,
    height=650,
    template="plotly_white",
    margin=dict(l=10, r=10, t=20, b=10),
    font=dict(size=13),
    # يحافظ على أي تكبير/تحريك سويته يدوياً حتى بعد كل تحديث تلقائي،
    # فلا "يختفي" أو يرجع للوضع الافتراضي كل 30 ثانية
    uirevision="keep_zoom",
    hovermode="x",
    # التحريك بالسحب بدل "تكبير بالسحب" — يمنع مشكلة اختفاء الشارت
    # على الجوال عند لمسه وسحبه (كان يكبّر على نطاق ضيق جداً وفارغ)
    dragmode="pan",
    hoverlabel=dict(bgcolor="white", font_size=14, font_color="black", bordercolor="#1E88E5"),
)

st.plotly_chart(
    fig,
    use_container_width=True,
    config={
        "scrollZoom": True,
        "displaylogo": False,
        "displayModeBar": True,
        "modeBarButtonsToAdd": ["zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d"],
    },
)
st.caption(
    "💡 الطريقة الأضمن للتكبير على الجوال: استخدم قائمة \"المدة الزمنية المعروضة\" أعلاه. "
    "بديلاً، جرّب أزرار + و − أعلى يمين الشارت، أو السحب بإصبعين."
)

# سجل الصفقات — كل اختراق أو تنبيه اقتراب من الوقف صار خلال الجلسة
st.markdown("### 📋 سجل التنبيهات (هذه الجلسة)")
if st.session_state.trade_log:
    st.dataframe(
        pd.DataFrame(st.session_state.trade_log),
        use_container_width=True,
        hide_index=True,
    )
    if st.button("🗑️ مسح السجل"):
        st.session_state.trade_log = []
        st.rerun()
else:
    st.caption("لا توجد تنبيهات بعد خلال هذه الجلسة.")
