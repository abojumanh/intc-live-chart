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

# قائمة قنوات ntfy المحفوظة — اختر منها مباشرة، أو اترك بدون إشعارات،
# أو اكتب قناة جديدة يدوياً
NTFY_CHANNELS = ["بدون إشعارات (تعطيل)", "safar-nvda-alerts-9284", "قناة أخرى (اكتبها يدوياً)"]

ntfy_choice = st.selectbox("قناة إشعارات ntfy", NTFY_CHANNELS)

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


def check_stop_proximity_and_notify(sym: str, last: float, entry: float, stop: float, topic: str):
    """ينبّه لمرة واحدة لما السعر يقترب من وقف الخسارة (يصل لمنتصف
    المسافة بين الدخول والوقف)، حتى تنتبه قبل ما يُضرب فعلياً."""
    halfway = entry - (entry - stop) / 2
    key = f"stopwarn_{sym}"
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


# سجل بسيط لكل اختراق أو تنبيه صار خلال الجلسة
if "trade_log" not in st.session_state:
    st.session_state.trade_log = []


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
        check_stop_proximity_and_notify(wsym, w_last, w_entry, w_stop, ntfy_topic)
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
