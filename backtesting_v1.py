import streamlit as st
import pandas as pd
import ccxt
import backtrader as bt
from datetime import datetime, timezone
import plotly.graph_objects as go
import matplotlib

matplotlib.use('Agg')

# --- 1. Класс Стратегии (с новым кастомным логом) ---
class DcaGridStrategyFIFO(bt.Strategy):
    params = (
        ('initial_order_size', 100.0), ('safety_order_size', 100.0),
        ('price_step_percent', 2.0), ('price_step_multiplier', 1.5),
        ('safety_orders_count', 10), ('take_profit_percent', 2.0),
        ('volume_multiplier', 1.0),
        ('direction', 'Long'), ('is_futures', False), ('leverage', 1),
    )

    def __init__(self):
        self.open_orders_queue = []
        self.safety_orders_placed = 0
        # ИЗМЕНЕНИЕ 2: Наш собственный лог для FIFO
        self.completed_cycles = []

    def notify_order(self, order):
        if order.status != order.Completed: return

        # Определяем, является ли ордер закрывающим (Take Profit)
        is_closing_trade = (self.p.direction == 'Long' and order.issell()) or \
                           (self.p.direction == 'Short' and order.isbuy())

        if is_closing_trade:
            if self.open_orders_queue:
                closed_order = self.open_orders_queue.pop(0)
                # Логируем завершенный цикл
                pnl = (order.executed.price - closed_order['price']) * closed_order['size']
                if self.p.direction == 'Short': pnl = -pnl # Инвертируем PnL для шорта
                
                self.completed_cycles.append({
                    "Дата закрытия": bt.num2date(order.executed.dt).strftime('%Y-%m-%d %H:%M'),
                    "Профит ($)": pnl,
                    "Длительность (свечей)": self.data.buflen() - closed_order['bar_opened'],
                })
        else: # Ордер на открытие или усреднение
            self.open_orders_queue.append({
                'price': order.executed.price,
                'size': order.executed.size,
                'bar_opened': self.data.buflen() # Запоминаем номер свечи, на которой открыт ордер
            })

    def next(self):
        # Если нет открытых позиций, начинаем новый торговый цикл
        if not self.position:
            self.safety_orders_placed = 0
            self.open_orders_queue = []
            self.start_new_cycle()
            return
            
        # Логика Take Profit по FIFO
        if self.open_orders_queue:
            oldest_order = self.open_orders_queue[0]
            if self.p.direction == 'Long':
                take_profit_price = oldest_order['price'] * (1 + self.p.take_profit_percent / 100)
                if self.data.close[0] >= take_profit_price: self.sell(size=oldest_order['size'])
            else: # Short
                take_profit_price = oldest_order['price'] * (1 - self.p.take_profit_percent / 100)
                if self.data.close[0] <= take_profit_price: self.buy(size=oldest_order['size'])

        # Логика страховочных ордеров
        if self.safety_orders_placed < self.p.safety_orders_count and self.open_orders_queue:
            last_order_price = self.open_orders_queue[-1]['price']
            step = self.p.price_step_percent / 100.0 * (self.p.price_step_multiplier ** self.safety_orders_placed)
            if self.p.direction == 'Long':
                next_safety_price = last_order_price * (1 - step)
                if self.data.close[0] <= next_safety_price: self.place_safety_order()
            else: # Short
                next_safety_price = last_order_price * (1 + step)
                if self.data.close[0] >= next_safety_price: self.place_safety_order()
    
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
    except Exception as e: st.error(f"Ошибка: {e}"); return None

st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.title("📈 Гибридный DCA/Grid Бэктестер (FIFO)")

with st.sidebar:
    st.header("⚙️ Параметры бэктеста")
    direction = st.radio("Направление", ["Long", "Short"])
    # ИСПРАВЛЕНИЕ 1: Возвращаем дату начала и конца
    start_date = st.date_input("Дата начала", datetime(2023, 1, 1))
    end_date = st.date_input("Дата окончания", datetime.now())
    initial_cash = st.number_input("Начальный капитал", value=10000.0)

    st.header("🛠️ Параметры стратегии")
    initial_order_size = st.number_input("Начальный ордер ($)", value=100.0)
    safety_order_size = st.number_input("Страховочный ордер ($)", value=100.0)
    volume_multiplier = st.number_input("Множитель суммы", min_value=1.0, value=1.0, format="%.2f")
    safety_orders_count = st.number_input("Макс. кол-во СО", min_value=1, value=20)
    price_step_percent = st.number_input("Шаг цены (%)", min_value=0.01, value=2.0, format="%.2f")
    price_step_multiplier = st.number_input("Множитель шага цены", min_value=1.0, value=1.5, format="%.2f")
    take_profit_percent = st.number_input("Take profit (%)", min_value=0.01, value=2.0, format="%.2f")

if st.sidebar.button("🚀 Запустить бэктест"):
    start_datetime = datetime.combine(start_date, datetime.min.time())
    end_datetime = datetime.combine(end_date, datetime.max.time())
    with st.spinner(f"Загружаем данные..."): data_df = fetch_data("okx", "BTC-USDT", "1h", start_datetime)
    if data_df is not None and not data_df.empty:
        data_df = data_df.loc[start_datetime:end_datetime]
        st.success("Данные загружены.")
        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=data_df))
        
        strategy_params = {
            'initial_order_size': initial_order_size, 'safety_order_size': safety_order_size,
            'safety_orders_count': safety_orders_count, 'price_step_percent': price_step_percent,
            'price_step_multiplier': price_step_multiplier, 'take_profit_percent': take_profit_percent,
            'volume_multiplier': volume_multiplier, 'direction': direction
        }
        cerebro.addstrategy(DcaGridStrategyFIFO, **strategy_params)
        
        cerebro.broker.set_cash(initial_cash)
        cerebro.broker.setcommission(commission=0.0006)
        
        start_value = cerebro.broker.getvalue()
        results = cerebro.run()
        end_value = cerebro.broker.getvalue()
        
        st.header("📊 Результаты")
        pnl = end_value - start_value
        pnl_percent = (pnl / start_value) * 100 if start_value > 0 else 0
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Начальный капитал", f"${start_value:,.2f}")
        col2.metric("Конечный капитал", f"${end_value:,.2f}", f"{pnl:,.2f} $")
        col3.metric("Прибыль/убыток (%)", f"{pnl_percent:.2f}%")

        # ИЗМЕНЕНИЕ 2: Отображаем наш кастомный лог
        st.header("📋 Завершенные торговые циклы (FIFO)")
        trade_log = results[0].completed_cycles
        if trade_log:
            log_df = pd.DataFrame(trade_log)
            st.dataframe(log_df.style.format({"Профит ($)": "${:,.2f}"}))
        else:
            st.info("За весь период не было завершено ни одного торгового цикла.")
