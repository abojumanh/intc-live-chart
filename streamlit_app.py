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

from datetime import datetime, time as dtime

import pandas as pd
import requests
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pytz
import streamlit as st
from streamlit_autorefresh import st_autorefresh

NY_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
OPENING_RANGE_MINUTES = 15
REFRESH_SECONDS = 30

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
    "INTC", "NVDA", "XOM", "CVX", "GILD", "NEM", "FCX", "SLB",
    "PEP", "KO", "MRK", "OXY", "QCOM", "CSCO", "PG", "HAL",
    "DVN", "EOG", "CF", "DD",
]
CUSTOM_OPTION = "سهم آخر (اكتبه يدوياً)"

# حالة "التسليح" لكل سهم — محفوظة في ذاكرة الجلسة (session_state)،
# تبقى ثابتة طوال الجلسة ولا تُعاد للصفر إلا عند إغلاق التبويب فعلياً
if "armed" not in st.session_state:
    st.session_state.armed = {}

col1, col2 = st.columns([2, 2])

with col1:
    options = WATCHLIST + [CUSTOM_OPTION]
    choice = st.selectbox("اختر من قائمتك", options)

with col2:
    if choice == CUSTOM_OPTION:
        typed = st.text_input("أو اكتب رمز سهم آخر", value="")
        symbol = typed.upper().strip() or "INTC"
    else:
        symbol = choice

ntfy_topic = st.text_input(
    "قناة إشعارات ntfy (اختياري — اتركها فارغة لتعطيل الإشعارات)",
    value="",
).strip()

watch_all = st.checkbox(
    "راقب كل أسهم القائمة معاً وأرسل تنبيه لأي اختراق (يحتاج قناة ntfy أعلاه)",
    value=False,
)


def fetch_and_prepare(sym: str):
    ticker = yf.Ticker(sym)
    data = ticker.history(period="1d", interval="1m")
    if data.empty:
        return None, None, None

    data.index = data.index.tz_convert(NY_TZ)
    today_ny = datetime.now(NY_TZ).date()
    open_start = NY_TZ.localize(datetime.combine(today_ny, MARKET_OPEN))
    open_end = open_start + pd.Timedelta(minutes=OPENING_RANGE_MINUTES)

    session = data[data.index >= open_start]
    if session.empty:
        return None, None, None

    opening_window = session[session.index < open_end]
    if opening_window.empty:
        opening_window = session.iloc[:15]

    range_high = float(opening_window["High"].max())
    range_low = float(opening_window["Low"].min())
    return session, range_high, range_low


def send_ntfy_alert(topic: str, title: str, message: str) -> bool:
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8")},
            timeout=5,
        )
        return True
    except Exception:
        return False


def check_breakout_and_notify(sym: str, last: float, entry: float, stop: float, target: float, topic: str):
    """يرسل تنبيهاً مرة واحدة بالضبط عند لحظة الاختراق، ولا يكرره
    إلا بعد ما يرجع السعر تحت مستوى الدخول ثم يخترق من جديد."""
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
            return True
    return False


# مراقبة كل أسهم القائمة معاً (وضع اختياري)
if ntfy_topic and watch_all:
    sent_now = []
    for wsym in WATCHLIST:
        w_session, w_high, w_low = fetch_and_prepare(wsym)
        if w_session is None:
            continue
        w_stop = (w_high + w_low) / 2
        w_entry = w_high
        w_target = w_entry + 2 * (w_entry - w_stop)
        w_last = float(w_session["Close"].iloc[-1])
        if check_breakout_and_notify(wsym, w_last, w_entry, w_stop, w_target, ntfy_topic):
            sent_now.append(wsym)
    if sent_now:
        st.success(f"✅ تم إرسال تنبيه اختراق لجوالك عن: {', '.join(sent_now)}")

session, range_high, range_low = fetch_and_prepare(symbol)

if session is None:
    st.warning("لا توجد بيانات بعد. تأكد أن السوق مفتوح أو أن رمز السهم صحيح.")
    st.stop()

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

# خيار الخطوط المتقاطعة (Crosshair) — تتبع إصبعك/مؤشرك على الشارت
# لقراءة السعر والوقت بدقة عند أي نقطة تلمسها
show_crosshair = st.checkbox("إظهار الخطوط المتقاطعة (Crosshair)", value=True)

fig = make_subplots(
    rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
    vertical_spacing=0.03,
)

fig.add_trace(go.Candlestick(
    x=session.index, open=session["Open"], high=session["High"],
    low=session["Low"], close=session["Close"], name=symbol,
), row=1, col=1)

fig.add_hline(y=entry_price, line_dash="dash", line_color="red", row=1, col=1)
fig.add_annotation(
    x=session.index[len(session) // 2], y=entry_price,
    text=f"⬆️ دخول عند: {entry_price:.2f}$", showarrow=False,
    bgcolor="#0a8a3f", font=dict(color="white", size=12), yshift=12, row=1, col=1,
)

fig.add_hline(y=range_low, line_dash="dash", line_color="green", row=1, col=1)
fig.add_annotation(
    x=session.index[-1], y=range_low,
    text=f"$دعم: {range_low:.2f}", showarrow=False,
    font=dict(color="gray", size=11), yshift=-12, xanchor="right", row=1, col=1,
)

fig.add_hline(y=stop_loss, line_dash="dot", line_color="#d0332f", row=1, col=1)
fig.add_annotation(
    x=session.index[len(session) // 2], y=stop_loss,
    text=f"⛔ توقف عند: {stop_loss:.2f}$", showarrow=False,
    bgcolor="#d0332f", font=dict(color="white", size=12), yshift=-12, row=1, col=1,
)

fig.add_annotation(
    x=session.index[0], y=open_price,
    text=f"$افتتاح: {open_price:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11), ax=-40, ay=-20, row=1, col=1,
)

idx_high = session["High"].idxmax()
fig.add_annotation(
    x=idx_high, y=day_high,
    text=f"$أعلى سعر: {day_high:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11), ax=0, ay=-30, row=1, col=1,
)

idx_low = session["Low"].idxmin()
fig.add_annotation(
    x=idx_low, y=day_low,
    text=f"$أدنى سعر: {day_low:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11), ax=0, ay=30, row=1, col=1,
)

fig.add_annotation(
    x=session.index[-1], y=last_price,
    text=f"$إغلاق حالي: {last_price:.2f}", showarrow=True, arrowhead=2,
    font=dict(size=11, color=price_color), ax=40, ay=-20, row=1, col=1,
)

colors = ["green" if c >= o else "red" for o, c in zip(session["Open"], session["Close"])]
fig.add_trace(go.Bar(x=session.index, y=session["Volume"], marker_color=colors, name="الحجم"), row=2, col=1)

fig.update_layout(
    xaxis_rangeslider_visible=False,
    height=650,
    template="plotly_white",
    # يحافظ على أي تكبير/تحريك سويته يدوياً حتى بعد كل تحديث تلقائي،
    # فلا "يختفي" أو يرجع للوضع الافتراضي كل 30 ثانية
    uirevision="keep_zoom",
    hovermode="x",
    # التحريك بالسحب بدل "تكبير بالسحب" — يمنع مشكلة اختفاء الشارت
    # على الجوال عند لمسه وسحبه (كان يكبّر على نطاق ضيق جداً وفارغ)
    dragmode="pan",
)

if show_crosshair:
    fig.update_xaxes(
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikedash="solid", spikecolor="gray", spikethickness=1,
        row=1, col=1,
    )
    fig.update_yaxes(
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikedash="solid", spikecolor="gray", spikethickness=1,
        row=1, col=1,
    )

st.plotly_chart(
    fig,
    use_container_width=True,
    config={
        "scrollZoom": True,       # يسمح بالتكبير بإصبعين (pinch) بأمان
        "displaylogo": False,
        "modeBarButtonsToAdd": ["resetScale2d"],
    },
)
st.caption("💡 اسحب إصبعك للتحريك، وبإصبعين للتكبير/التصغير. اضغط ضغطتين متتاليتين لإعادة الشارت لوضعه الطبيعي.")
