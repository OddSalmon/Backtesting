import streamlit as st
import pandas as pd
import ccxt
from datetime import datetime, timezone

# --- 1. Новый, быстрый движок для бэктеста ---
def run_fast_backtest(data, params):
    # Извлекаем параметры для удобства
    direction = params['direction']
    initial_order_size = params['initial_order_size']
    safety_order_size = params['safety_order_size']
    volume_multiplier = params['volume_multiplier']
    safety_orders_count = params['safety_orders_count']
    price_step_percent = params['price_step_percent'] / 100.0
    price_step_multiplier = params['price_step_multiplier']
    take_profit_percent = params['take_profit_percent'] / 100.0

    # Списки для отслеживания состояния
    open_orders = []
    completed_cycles = []
    cash = params['initial_cash']
    
    # Симуляция по дням
    for index, row in data.iterrows():
        day_low, day_high = row['low'], row['high']

        # --- Логика Take Profit (FIFO) ---
        if open_orders:
            oldest_order = open_orders[0]
            if direction == 'Long':
                tp_price = oldest_order['price'] * (1 + take_profit_percent)
                if day_high >= tp_price:
                    pnl = (tp_price - oldest_order['price']) * oldest_order['size_coin']
                    cash += oldest_order['size_usd'] + pnl
                    completed_cycles.append({'date': index.date(), 'pnl': pnl})
                    open_orders.pop(0)
            else: # Short
                tp_price = oldest_order['price'] * (1 - take_profit_percent)
                if day_low <= tp_price:
                    pnl = (oldest_order['price'] - tp_price) * oldest_order['size_coin']
                    cash += oldest_order['size_usd'] + pnl
                    completed_cycles.append({'date': index.date(), 'pnl': pnl})
                    open_orders.pop(0)
        
        # --- Логика входа и страховочных ордеров ---
        if not open_orders: # Если нет открытых позиций, делаем начальный ордер
            entry_price = row['open'] # Входим по цене открытия дня
            size_coin = initial_order_size / entry_price
            open_orders.append({'price': entry_price, 'size_coin': size_coin, 'size_usd': initial_order_size, 'so_level': 0})
            cash -= initial_order_size
        else: # Если уже есть позиция, проверяем страховочные ордера
            if len(open_orders) <= safety_orders_count:
                last_order_price = open_orders[-1]['price']
                current_so_level = open_orders[-1]['so_level']
                step = price_step_percent * (price_step_multiplier ** current_so_level)

                if direction == 'Long':
                    so_price = last_order_price * (1 - step)
                    if day_low <= so_price:
                        so_size_usd = safety_order_size * (volume_multiplier ** current_so_level)
                        so_size_coin = so_size_usd / so_price
                        open_orders.append({'price': so_price, 'size_coin': so_size_coin, 'size_usd': so_size_usd, 'so_level': current_so_level + 1})
                        cash -= so_size_usd
                else: # Short
                    so_price = last_order_price * (1 + step)
                    if day_high >= so_price:
                        so_size_usd = safety_order_size * (volume_multiplier ** current_so_level)
                        so_size_coin = so_size_usd / so_price
                        open_orders.append({'price': so_price, 'size_coin': so_size_coin, 'size_usd': so_size_usd, 'so_level': current_so_level + 1})
                        cash -= so_size_usd

    # Рассчитываем итоговую стоимость открытых позиций
    final_open_positions_value = sum([order['size_coin'] * data['close'][-1] for order in open_orders])
    final_cash = cash + final_open_positions_value
    
    return final_cash, completed_cycles


# --- 2. Функции и UI ---
@st.cache_data
def fetch_data(exchange_name, symbol, timeframe, start_date):
    try:
        exchange = getattr(ccxt, exchange_name)(); since = int(start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=2000)
        if not ohlcv: return None
        df = pd.DataFrame(ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms'); df.set_index('datetime', inplace=True); return df
    except Exception as e: st.error(f"Ошибка загрузки данных: {e}"); return None

st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.title("⚡️ Сверхбыстрый бэктестер для сеточной DCA-стратегии (FIFO)")

with st.sidebar:
    st.header("⚙️ Параметры бэктеста")
    direction = st.radio("Направление", ["Long", "Short"])
    exchange = st.selectbox("Биржа", ["okx", "bybit", "binance", "bitget"])
    symbol_ccxt = st.text_input("Торговая пара (тикер CCXT)", "BTC/USDT")
    start_date = st.date_input("Дата начала", datetime(2023, 1, 1))
    end_date = st.date_input("Дата окончания", datetime.now())
    initial_cash = st.number_input("Начальный капитал", value=10000.0)

    st.header("🛠️ Параметры стратегии")
    initial_order_size = st.number_input("Начальный ордер ($)", value=100.0)
    safety_order_size = st.number_input("Страховочный ордер ($)", value=100.0)
    volume_multiplier = st.number_input("Множитель суммы", min_value=1.0, value=1.0, format="%.2f")
    safety_orders_count = st.number_input("Макс. кол-во СО", min_value=1, value=20)
    price_step_percent = st.number_input("Шаг цены (%)", min_value=0.01, value=2.0, format="%.2f")
    price_step_multiplier = st.number_input("Множитель шага цены", min_value=1.0, value=1.1, format="%.2f")
    take_profit_percent = st.number_input("Take profit (%)", min_value=0.01, value=1.0, format="%.2f")

if st.sidebar.button("🚀 Запустить бэктест"):
    start_datetime = datetime.combine(start_date, datetime.min.time())
    end_datetime = datetime.combine(end_date, datetime.max.time())
    
    params = {
        'direction': direction, 'initial_cash': initial_cash, 'initial_order_size': initial_order_size,
        'safety_order_size': safety_order_size, 'volume_multiplier': volume_multiplier,
        'safety_orders_count': safety_orders_count, 'price_step_percent': price_step_percent,
        'price_step_multiplier': price_step_multiplier, 'take_profit_percent': take_profit_percent,
    }

    with st.spinner(f"Загружаем дневные данные для {symbol_ccxt} с {exchange}..."):
        # Всегда загружаем дневные свечи для скорости
        data_df = fetch_data(exchange, symbol_ccxt, '1d', start_datetime)

    if data_df is not None and not data_df.empty:
        data_df = data_df.loc[start_datetime:end_datetime]
        st.success(f"Данные с {start_datetime.date()} по {end_datetime.date()} загружены.")
        
        final_cash, completed_cycles = run_fast_backtest(data_df, params)
        
        st.header(f"📊 Результаты для {symbol_ccxt} ({exchange})")
        pnl = final_cash - initial_cash
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Начальный капитал", f"${initial_cash:,.2f}")
        col2.metric("Конечный капитал", f"${final_cash:,.2f}", f"{pnl:,.2f} $")
        col3.metric("Завершено циклов", len(completed_cycles))

        st.header("📋 Завершенные торговые циклы (FIFO)")
        if completed_cycles:
            log_df = pd.DataFrame(completed_cycles)
            st.dataframe(log_df.style.format({"pnl": "${:,.2f}"}))
            total_pnl = log_df['pnl'].sum()
            st.metric("Суммарный зафиксированный профит", f"${total_pnl:,.2f}")
        else:
            st.info("За весь период не было завершено ни одного прибыльного цикла.")
