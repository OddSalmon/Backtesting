import streamlit as st
import pandas as pd
import ccxt
import backtrader as bt
from datetime import datetime, timezone
import plotly.graph_objects as go
import matplotlib

matplotlib.use('Agg')

# --- 1. НОВАЯ ГИБРИДНАЯ СТРАТЕГИЯ (FIFO) ---
class DcaGridStrategyFIFO(bt.Strategy):
    params = (
        ('initial_order_size', 100.0), ('safety_order_size', 100.0),
        ('price_step_percent', 2.0), ('price_step_multiplier', 1.5),
        ('safety_orders_count', 10), ('take_profit_percent', 2.0),
        ('volume_multiplier', 1.0),
        ('direction', 'Long'), ('is_futures', False), ('leverage', 1),
    )

    def __init__(self):
        # Очередь из наших открытых ордеров. Храним {'price': цена, 'size': размер}
        self.open_orders_queue = []
        self.safety_orders_placed = 0
        self.liquidated = False

    def notify_order(self, order):
        if order.status != order.Completed: return

        # Определяем, является ли ордер закрывающим (Take Profit)
        is_closing_trade = (self.p.direction == 'Long' and order.issell()) or \
                           (self.p.direction == 'Short' and order.isbuy())

        if is_closing_trade:
            # Если это тейк-профит, удаляем самый старый ордер из нашей очереди
            if self.open_orders_queue:
                self.open_orders_queue.pop(0)
        else: # Ордер на открытие или усреднение
            self.open_orders_queue.append({'price': order.executed.price, 'size': order.executed.size})

    def next(self):
        if self.liquidated: return
        
        # Если нет открытых позиций, начинаем новый торговый цикл
        if not self.position:
            self.safety_orders_placed = 0
            self.open_orders_queue = []
            self.start_new_cycle()
            return
            
        # --- Логика Take Profit по FIFO ---
        if self.open_orders_queue:
            oldest_order = self.open_orders_queue[0]
            
            if self.p.direction == 'Long':
                take_profit_price = oldest_order['price'] * (1 + self.p.take_profit_percent / 100)
                if self.data.close[0] >= take_profit_price:
                    self.sell(size=oldest_order['size']) # Продаем только размер самого старого ордера
            else: # Short
                take_profit_price = oldest_order['price'] * (1 - self.p.take_profit_percent / 100)
                if self.data.close[0] <= take_profit_price:
                    self.buy(size=oldest_order['size']) # Откупаем только размер самого старого ордера

        # --- Логика страховочных ордеров ---
        if self.safety_orders_placed < self.p.safety_orders_count and self.open_orders_queue:
            last_order_price = self.open_orders_queue[-1]['price']
            step = self.p.price_step_percent / 100.0 * (self.p.price_step_multiplier ** self.safety_orders_placed)

            if self.p.direction == 'Long':
                next_safety_price = last_order_price * (1 - step)
                if self.data.close[0] <= next_safety_price:
                    self.place_safety_order()
            else: # Short
                next_safety_price = last_order_price * (1 + step)
                if self.data.close[0] >= next_safety_price:
                    self.place_safety_order()
    
    def place_safety_order(self):
        size_multiplier = self.p.volume_multiplier ** self.safety_orders_placed
        order_size = (self.p.safety_order_size * size_multiplier) / self.data.close[0]
        if self.p.direction == 'Long': self.buy(size=order_size)
        else: self.sell(size=order_size)
        self.safety_orders_placed += 1
    
    def start_new_cycle(self):
        size = self.p.initial_order_size / self.data.close[0]
        if self.p.direction == 'Long': self.buy(size=size)
        else: self.sell(size=size)

# --- 2. Функции и UI (без изменений, но с важными исправлениями) ---
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
    except Exception as e: st.error(f"Ошибка: {e}"); return None

def create_trade_log_df(analysis):
    trades_data = []
    if 'trades' not in analysis: return pd.DataFrame()
    for trade_list in analysis.trades:
        for trade in trade_list:
            trades_data.append({
                "Дата закрытия": trade.dt_close.strftime('%Y-%m-%d %H:%M'),
                "Профит ($)": trade.pnl,
                "Профит (%)": trade.pnlcomm_perc * 100 if trade.pnlcomm_perc is not None else 0,
                "Длительность (часы)": trade.barlen,
            })
    return pd.DataFrame(trades_data)

st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.title("📈 Гибридный DCA/Grid Бэктестер (FIFO)")

with st.sidebar:
    st.header("⚙️ Параметры бэктеста")
    direction = st.radio("Направление", ["Long", "Short"])
    initial_cash = st.number_input("Начальный капитал", value=10000.0)

    st.header("🛠️ Параметры стратегии")
    initial_order_size = st.number_input("Начальный ордер ($)", value=100.0)
    safety_order_size = st.number_input("Страховочный ордер ($)", value=100.0)
    safety_orders_count = st.number_input("Max trigger number", value=10, min_value=1)
    price_step_percent = st.number_input("Grid step (%)", value=2.0, min_value=0.1, format="%.2f")
    price_step_multiplier = st.number_input("Grid step ratio (%)", value=1.5, min_value=0.1, format="%.2f")
    volume_multiplier = st.number_input("Volume multiplier (Множитель суммы)", value=1.0, min_value=1.0, format="%.2f")
    take_profit_percent = st.number_input("Take Profit (%)", value=2.0, min_value=0.1, format="%.2f")

# --- Основная часть ---
if st.sidebar.button("🚀 Запустить бэктест"):
    start_datetime = datetime.combine(st.sidebar.date_input("Дата начала", datetime(2023, 1, 1)), datetime.min.time())
    with st.spinner(f"Загружаем данные..."): data_df = fetch_data("okx", "BTC-USDT", "1h", start_datetime)
    if data_df is not None and not data_df.empty:
        st.success("Данные загружены.")
        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=data_df))
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade_analyzer')
        
        strategy_params = {'initial_order_size': initial_order_size, 'safety_order_size': safety_order_size, 'safety_orders_count': safety_orders_count, 'price_step_percent': price_step_percent, 'price_step_multiplier': price_step_multiplier, 'take_profit_percent': take_profit_percent, 'volume_multiplier': volume_multiplier, 'direction': direction}
        cerebro.addstrategy(DcaGridStrategyFIFO, **strategy_params)
        
        cerebro.broker.set_cash(initial_cash)
        cerebro.broker.setcommission(commission=0.0006)
        
        start_value = cerebro.broker.getvalue()
        results = cerebro.run()
        end_value = cerebro.broker.getvalue()
        
        st.header("📊 Результаты")
        pnl = end_value - start_value
        trade_analysis = results[0].analyzers.trade_analyzer.get_analysis()
        
        col1, col2 = st.columns(2)
        col1.metric("Начальный капитал", f"${start_value:,.2f}")
        col2.metric("Конечный капитал", f"${end_value:,.2f}", f"{pnl:,.2f} $")

        st.header("📋 Завершенные торговые циклы (FIFO)")
        if trade_analysis and 'total' in trade_analysis and trade_analysis.total.total > 0:
            log_df = create_trade_log_df(trade_analysis)
            st.dataframe(log_df.style.format({"Профит ($)": "${:,.2f}", "Профит (%)": "{:.2f}%"}))
        else:
            st.info("За весь период не было завершено ни одного торгового цикла.")
