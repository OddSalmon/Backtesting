import streamlit as st
import pandas as pd
import ccxt
import backtrader as bt
from datetime import datetime, timezone
import plotly.graph_objects as go
import matplotlib

# Решаем проблему с графиком в серверной среде
matplotlib.use('Agg')

# --- 1. Класс Стратегии (без изменений) ---
class DcaGridStrategy(bt.Strategy):
    params = (
        ('initial_order_size', 100.0), ('safety_order_size', 100.0),
        ('price_step_percent', 2.0), ('price_step_multiplier', 1.5),
        ('safety_orders_count', 10), ('take_profit_percent', 2.0),
        ('direction', 'Long'), ('is_futures', False), ('leverage', 1),
    )

    def __init__(self):
        self.entry_price = 0; self.total_cost = 0; self.total_size = 0
        self.take_profit_price = 0; self.liquidation_price = 0
        self.safety_orders_placed = 0
        self.trades = []; self.liquidated = False

    def notify_order(self, order):
        if order.status == order.Completed:
            trade_info = {'dt': bt.num2date(order.executed.dt), 'price': order.executed.price, 'size': abs(order.executed.size)}
            if order.isbuy():
                trade_info['type'] = 'buy'
                if self.p.direction == 'Short' and self.position.size < 0: self.reset_cycle()
                else: self.total_cost += order.executed.value; self.total_size += order.executed.size
            elif order.issell():
                trade_info['type'] = 'sell'
                if self.p.direction == 'Long' and self.position.size > 0: self.reset_cycle()
                else: self.total_cost += abs(order.executed.value); self.total_size += abs(order.executed.size)
            self.trades.append(trade_info)
            if self.total_size > 0:
                self.entry_price = self.total_cost / self.total_size
                if self.p.direction == 'Long':
                    self.take_profit_price = self.entry_price * (1 + self.p.take_profit_percent / 100)
                    if self.p.is_futures and self.p.leverage > 1: self.liquidation_price = self.entry_price * (1 - (0.99 / self.p.leverage))
                else: # Short
                    self.take_profit_price = self.entry_price * (1 - self.p.take_profit_percent / 100)
                    if self.p.is_futures and self.p.leverage > 1: self.liquidation_price = self.entry_price * (1 + (0.99 / self.p.leverage))
    
    def next(self):
        if self.liquidated: return
        if self.p.is_futures and self.position and self.liquidation_price > 0:
            is_liquidated = (self.p.direction == 'Long' and self.data.close[0] <= self.liquidation_price) or \
                            (self.p.direction == 'Short' and self.data.close[0] >= self.liquidation_price)
            if is_liquidated:
                self.log(f'!!! LIQUIDATION at Price: {self.data.close[0]:.2f} !!!'); self.close(); self.liquidated = True; return
        if not self.position: self.start_new_cycle(); return
        if self.position:
            tp_hit = (self.p.direction == 'Long' and self.data.close[0] >= self.take_profit_price) or \
                     (self.p.direction == 'Short' and self.data.close[0] <= self.take_profit_price and self.take_profit_price > 0)
            if tp_hit: self.close()
        if self.safety_orders_placed < self.p.safety_orders_count:
            step = self.p.price_step_percent / 100.0 * (self.p.price_step_multiplier ** self.safety_orders_placed)
            if self.p.direction == 'Long':
                next_safety_price = self.entry_price * (1 - step)
                if self.data.close[0] <= next_safety_price: self.buy(size=self.p.safety_order_size / self.data.close[0]); self.safety_orders_placed += 1
            else: # Short
                next_safety_price = self.entry_price * (1 + step)
                if self.data.close[0] >= next_safety_price: self.sell(size=self.p.safety_order_size / self.data.close[0]); self.safety_orders_placed += 1
    
    def start_new_cycle(self):
        size = self.p.initial_order_size / self.data.close[0]
        if self.p.direction == 'Long': self.buy(size=size)
        else: self.sell(size=size)

    def reset_cycle(self):
        self.total_cost = 0; self.total_size = 0; self.safety_orders_placed = 0; self.take_profit_price = 0

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        log_container.write(f'{dt.isoformat()} - {txt}')

# --- 2. Функции и Пресеты ---
@st.cache_data
def fetch_data(exchange_name, symbol, timeframe, start_date):
    try:
        exchange = getattr(ccxt, exchange_name)(); since = int(start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
        all_ohlcv = []
        while True:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            
        df = pd.DataFrame(all_ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms'); df.set_index('datetime', inplace=True); return df
    except Exception as e:
        st.error(f"Ошибка: {e}"); return None

# Функция графика остается в коде, но мы не будем ее вызывать
def plot_interactive_chart(data_df, trades, show_trades=False):
    fig = go.Figure(data=[go.Candlestick(x=data_df.index, open=data_df['open'], high=data_df['high'], low=data_df['low'], close=data_df['close'], name='Цена')])
    if show_trades:
        buys = [t for t in trades if t['type'] == 'buy']; sells = [t for t in trades if t['type'] == 'sell']
        fig.add_trace(go.Scatter(x=[t['dt'] for t in buys], y=[t['price'] for t in buys], mode='markers', name='Покупки', marker=dict(color='cyan', size=10, symbol='triangle-up')))
        fig.add_trace(go.Scatter(x=[t['dt'] for t in sells], y=[t['price'] for t in sells], mode='markers', name='Продажи', marker=dict(color='magenta', size=10, symbol='triangle-down')))
    fig.update_layout(title='Интерактивный график цены и сделок', xaxis_rangeslider_visible=True, template='plotly_dark')
    return fig

PRESETS = {
    "okx": {
        "Spot": {"BTC/USDT": "BTC-USDT", "ETH/USDT": "ETH-USDT", "SOL/USDT": "SOL-USDT", "LTC/USDT": "LTC-USDT", "XRP/USDT": "XRP-USDT", "DOGE/USDT": "DOGE-USDT", "TON/USDT": "TON-USDT"},
        "Futures": {"BTC/USDT": "BTC-USDT-SWAP", "ETH/USDT": "ETH-USDT-SWAP", "SOL/USDT": "SOL-USDT-SWAP", "LTC/USDT":"LTC-USDT-SWAP", "XRP/USDT": "XRP-USDT-SWAP", "LINK/USDT": "LINK-USDT-SWAP", "DOGE/USDT": "DOGE-USDT-SWAP"}
    },
    "bitmex": {
        "Futures": {"XBT/USDT": "XBTUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT", "LINK/USDT": "LINKUSDT", "XRP/USDT": "XRPUSDT", "LTC/USDT": "LTCUSDT"}
    }
}

# --- 3. Интерфейс Streamlit ---
st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.title("📈 Продвинутый бэктестер для сеточной DCA-стратегии")

with st.sidebar:
    st.header("⚙️ Параметры бэктеста")
    direction = st.radio("Направление", ["Long", "Short"])
    exchange = st.selectbox("Биржа", list(PRESETS.keys()))
    instrument = st.selectbox("Инструмент", list(PRESETS[exchange].keys()))
    
    available_pairs = list(PRESETS[exchange][instrument].keys()) if instrument in PRESETS[exchange] else []
    symbol_display = st.selectbox("Торговая пара", available_pairs) if available_pairs else st.text_input("Ввод тикера CCXT", "BTC-USDT-SWAP")
    symbol_ccxt = PRESETS.get(exchange, {}).get(instrument, {}).get(symbol_display, symbol_display)

    timeframe = st.selectbox("Таймфрейм", ['1h', '4h', '1d'])
    start_date = st.date_input("Дата начала", datetime(2023, 1, 1))
    end_date = st.date_input("Дата окончания", datetime.now())
    initial_cash = st.number_input("Начальный капитал", value=10000.0)

    st.header("🛠️ Параметры стратегии")
    initial_order_size = st.number_input("Начальный ордер ($)", value=100.0)
    safety_order_size = st.number_input("Страховочный ордер ($)", value=100.0)
    safety_orders_count = st.number_input("Max trigger number", value=10, min_value=1)
    price_step_percent = st.number_input("Grid step (%)", value=2.0, min_value=0.1, format="%.2f")
    price_step_multiplier = st.number_input("Grid step ratio (%)", value=1.5, min_value=0.1, format="%.2f")
    take_profit_percent = st.number_input("Take Profit (%)", value=2.0, min_value=0.1, format="%.2f")

    steps = [price_step_percent * (price_step_multiplier ** i) for i in range(safety_orders_count)]
    theoretical_range = sum(steps)
    st.metric("Заданный Trading Range (%)", f"{theoretical_range:.2f}%")

    st.header("💰 Комиссии и плечо")
    use_commission = st.checkbox("Учитывать комиссию", value=True)
    commission = st.number_input("Комиссия (%)", value=0.06, format="%.4f", disabled=not use_commission) / 100.0
        
    is_futures = (instrument == "Futures")
    leverage = 1
    if is_futures:
        use_leverage = st.checkbox("Использовать плечо", value=True)
        leverage = st.slider("Плечо (Leverage)", 1, 100, 10, disabled=not use_leverage)

if st.sidebar.button("🚀 Запустить бэктест"):
    start_datetime = datetime.combine(start_date, datetime.min.time())
    end_datetime = datetime.combine(end_date, datetime.max.time())
    
    with st.spinner(f"Загружаем данные..."): data_df = fetch_data(exchange, symbol_ccxt, timeframe, start_datetime)

    if data_df is not None and not data_df.empty:
        data_df = data_df.loc[start_datetime:end_datetime]
        st.success("Данные загружены.")
        
        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=data_df))
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
        
        strategy_params = {'initial_order_size': initial_order_size, 'safety_order_size': safety_order_size, 'safety_orders_count': safety_orders_count, 'price_step_percent': price_step_percent, 'price_step_multiplier': price_step_multiplier, 'take_profit_percent': take_profit_percent, 'direction': direction, 'is_futures': is_futures, 'leverage': leverage}
        cerebro.addstrategy(DcaGridStrategy, **strategy_params)
        
        cerebro.broker.set_cash(initial_cash)
        cerebro.broker.setcommission(commission=commission if use_commission else 0.0, leverage=leverage if is_futures and use_leverage else 1)
        
        log_container = st.expander("Показать/скрыть лог сделок", expanded=False)
        start_value = cerebro.broker.getvalue()
        results = cerebro.run()
        end_value = cerebro.broker.getvalue()

        if results[0].liquidated and end_value < initial_cash * 0.1:
            st.markdown("""<div style='background-color:#FF4B4B;padding:20px;border-radius:10px;text-align:center;'><h1 style='color:white;margin:0;'>🚨 БОТ ЛИКВИДИРОВАН 🚨</h1></div>""", unsafe_allow_html=True)

        st.header("📊 Результаты")
        pnl = end_value - start_value
        pnl_percent = (pnl / start_value) * 100 if start_value > 0 else 0
        max_drawdown = results[0].analyzers.drawdown.get_analysis()['max']['drawdown']
        total_trades = len(results[0].trades)
        
        # --- ИЗМЕНЕНИЕ: Новая структура вывода результатов ---
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Начальный капитал", f"${start_value:,.2f}")
        col2.metric("Конечный капитал", f"${end_value:,.2f}", f"{pnl:,.2f} $")
        col3.metric("Прибыль/убыток (%)", f"{pnl_percent:.2f}%")
        col4.metric("Количество сделок", total_trades)

        st.metric("Макс. просадка в тесте (%)", f"{max_drawdown:.2f}%")

        # --- График остается закомментированным ---
        # st.subheader("Интерактивный график")
        # show_trades_on_chart = st.checkbox("Показать все сделки на графике", value=True)
        # fig = plot_interactive_chart(data_df, results[0].trades, show_trades=show_trades_on_chart)
        # st.plotly_chart(fig, use_container_width=True)
