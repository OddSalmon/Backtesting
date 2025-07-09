import streamlit as st
import pandas as pd
import ccxt
import backtrader as bt
from datetime import datetime, timezone
import plotly.graph_objects as go
import matplotlib

matplotlib.use('Agg')

# --- 1. Класс Стратегии (без изменений в логике, только в коде) ---
class DcaGridStrategyFIFO(bt.Strategy):
    params = (
        ('initial_order_size', 100.0), ('safety_order_size', 100.0),
        ('price_step_percent', 2.0), ('price_step_multiplier', 1.5),
        ('safety_orders_count', 10), ('take_profit_percent', 2.0),
        ('volume_multiplier', 1.0),
        ('direction', 'Long'), ('is_futures', False), ('leverage', 1),
    )
    def __init__(self):
        self.open_orders_queue = []; self.safety_orders_placed = 0; self.liquidated = False

    def notify_order(self, order):
        if order.status != order.Completed: return
        is_closing_trade = (self.p.direction == 'Long' and order.issell()) or (self.p.direction == 'Short' and order.isbuy())
        if is_closing_trade:
            if self.open_orders_queue: self.open_orders_queue.pop(0)
        else: self.open_orders_queue.append({'price': order.executed.price, 'size': order.executed.size})

    def next(self):
        if self.liquidated: return
        if not self.position:
            self.safety_orders_placed = 0; self.open_orders_queue = []
            self.start_new_cycle(); return
        if self.open_orders_queue:
            oldest_order = self.open_orders_queue[0]
            if self.p.direction == 'Long':
                tp_price = oldest_order['price'] * (1 + self.p.take_profit_percent / 100)
                if self.data.close[0] >= tp_price: self.sell(size=oldest_order['size'])
            else:
                tp_price = oldest_order['price'] * (1 - self.p.take_profit_percent / 100)
                if self.data.close[0] <= tp_price: self.buy(size=oldest_order['size'])
        if self.safety_orders_placed < self.p.safety_orders_count and self.open_orders_queue:
            last_price = self.open_orders_queue[-1]['price']
            step = self.p.price_step_percent / 100.0 * (self.p.price_step_multiplier ** self.safety_orders_placed)
            if self.p.direction == 'Long':
                so_price = last_price * (1 - step)
                if self.data.close[0] <= so_price: self.place_safety_order()
            else:
                so_price = last_price * (1 + step)
                if self.data.close[0] >= so_price: self.place_safety_order()

    def place_safety_order(self):
        size_mult = self.p.volume_multiplier ** self.safety_orders_placed
        order_size = (self.p.safety_order_size * size_mult) / self.data.close[0]
        if self.p.direction == 'Long': self.buy(size=order_size)
        else: self.sell(size=order_size)
        self.safety_orders_placed += 1
    
    def start_new_cycle(self):
        size = self.p.initial_order_size / self.data.close[0]
        if self.p.direction == 'Long': self.buy(size=size)
        else: self.sell(size=size)

# --- 2. Функции и UI ---
@st.cache_data
def fetch_data(exchange_name, symbol, timeframe, start_date):
    try:
        exchange = getattr(ccxt, exchange_name)(); since = int(start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
        all_ohlcv = [];
        while True:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv: break
            all_ohlcv.extend(ohlcv); since = ohlcv[-1][0] + 1
        df = pd.DataFrame(all_ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms'); df.set_index('datetime', inplace=True); return df
    except Exception as e: st.error(f"Ошибка загрузки данных: {e}"); return None

st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.title("📈 Гибридный DCA/Grid Бэктестер (FIFO)")

with st.sidebar:
    st.header("⚙️ Параметры бэктеста")
    direction = st.radio("Направление", ["Long", "Short"])
    exchange = st.selectbox("Биржа", ["okx", "bitmex", "bybit", "binance"])
    instrument = st.selectbox("Инструмент", ["Spot", "Futures"])
    symbol_ccxt = st.text_input("Торговая пара (тикер CCXT)", "BTC/USDT")
    timeframe = st.selectbox("Таймфрейм", ['1m', '5m', '15m', '30m', '1h', '4h', '1d'])
    start_date = st.date_input("Дата начала", datetime(2024, 1, 1))
    end_date = st.date_input("Дата окончания", datetime.now()) # ИСПРАВЛЕНИЕ: Дата конца используется
    initial_cash = st.number_input("Начальный капитал", value=10000.0)

    st.header("🛠️ Параметры стратегии")
    initial_order_size = st.number_input("Начальный ордер ($)", value=100.0)
    safety_order_size = st.number_input("Страховочный ордер ($)", value=100.0)
    volume_multiplier = st.number_input("Множитель суммы", min_value=1.0, value=1.0, format="%.2f")
    safety_orders_count = st.number_input("Макс. кол-во СО", min_value=1, value=20)
    price_step_percent = st.number_input("Шаг цены (%)", min_value=0.01, value=1.0, format="%.2f")
    price_step_multiplier = st.number_input("Множитель шага цены", min_value=1.0, value=1.1, format="%.2f")
    take_profit_percent = st.number_input("Take profit (%)", min_value=0.01, value=1.0, format="%.2f")
    
    st.header("💰 Комиссии и плечо")
    is_futures = (instrument == "Futures")
    commission = st.number_input("Комиссия (%)", value=0.06, format="%.4f") / 100.0
    leverage = 1
    if is_futures:
        leverage = st.slider("Плечо (Leverage)", 1, 100, 10)

# --- Основной блок ---
if st.sidebar.button("🚀 Запустить бэктест"):
    start_datetime = datetime.combine(start_date, datetime.min.time())
    end_datetime = datetime.combine(end_date, datetime.max.time())
    
    # ИСПРАВЛЕНИЕ: Используем выбранные в UI параметры
    with st.spinner(f"Загружаем {timeframe} данные для {symbol_ccxt} с {exchange}..."):
        data_df = fetch_data(exchange, symbol_ccxt, timeframe, start_datetime)

    if data_df is not None and not data_df.empty:
        data_df = data_df.loc[start_datetime:end_datetime]
        st.success(f"Данные с {start_datetime.date()} по {end_datetime.date()} загружены.")
        
        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=data_df))
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade_analyzer')
        
        strategy_params = {'initial_order_size': initial_order_size, 'safety_order_size': safety_order_size, 'safety_orders_count': safety_orders_count, 'price_step_percent': price_step_percent, 'price_step_multiplier': price_step_multiplier, 'take_profit_percent': take_profit_percent, 'volume_multiplier': volume_multiplier, 'direction': direction, 'is_futures': is_futures, 'leverage': leverage}
        cerebro.addstrategy(DcaGridStrategyFIFO, **strategy_params)
        
        cerebro.broker.set_cash(initial_cash)
        # ИСПРАВЛЕНИЕ: Учет плеча и комиссии
        cerebro.broker.setcommission(commission=commission, leverage=leverage if is_futures else 1)
        
        start_value = cerebro.broker.getvalue()
        results = cerebro.run()
        end_value = cerebro.broker.getvalue()
        
        st.header(f"📊 Результаты для {symbol_ccxt} ({exchange})")
        pnl = end_value - start_value
        trade_analysis = results[0].analyzers.trade_analyzer.get_analysis()
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Начальный капитал", f"${start_value:,.2f}")
        col2.metric("Конечный капитал", f"${end_value:,.2f}", f"{pnl:,.2f} $")
        
        total_trades = 0
        if trade_analysis and 'total' in trade_analysis:
            total_trades = trade_analysis.total.total
        col3.metric("Завершено циклов", total_trades)

        st.header("📋 Завершенные торговые циклы (FIFO)")
        if total_trades > 0:
            log_data = []
            for t in trade_analysis.trades:
                log_data.append({'Profit ($)': t.pnlcomm, 'Duration (bars)': t.barlen})
            st.dataframe(pd.DataFrame(log_data))
        else:
            st.info("За весь период не было завершено ни одного торгового цикла.")
