import sys
import requests
import numpy as np
import threading
import time
import telebot
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
import logging
import random
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QWidget, QTableWidget, QTableWidgetItem, QLabel, 
                             QPushButton, QLineEdit, QSpinBox, QDoubleSpinBox, 
                             QTextEdit, QSplitter, QComboBox, QDialog, QFormLayout)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon
import winsound

# Configuration Telegram bot notifications, replace with
# your own token and chat ID
TOKEN = ''
CHAT_ID = ''
GROUP_INTERVAL = 150

# Thresholds (now as global variables)
LARGE_WALL_THRESHOLD = 300
CANCELLATION_THRESHOLD = 80
NOTIFICATION_COOLDOWN = 3600  # 1 hour in seconds

default_settings = {
    "BTCUSDT": {
        "group_interval": 100,
        "large_wall_threshold": 150,
        "cancellation_threshold": 80
    },
    "ETHUSDT": {
        "group_interval": 10,
        "large_wall_threshold": 2500,
        "cancellation_threshold": 500
    },
    # Add more pairs as needed
}

# Initialize the Telegram bot
bot = telebot.TeleBot(TOKEN)

# Global variables
is_running = threading.Event()
is_running.set()  # Start in running state
last_notified_walls = {'bids': {}, 'asks': {}}
current_symbol = 'BTCUSDT'

# Lock for thread-safe operations
lock = threading.Lock()

# ... (keep all the existing functions like group_orders, fetch_order_book, etc.)
def group_orders(orders, interval=GROUP_INTERVAL):
    grouped = {}
    for price, amount in orders:
        price = float(price)
        amount = float(amount)
        group_price = round(price / interval) * interval
        if group_price in grouped:
            grouped[group_price] += amount
        else:
            grouped[group_price] = amount
    
    return sorted(grouped.items(), key=lambda x: x[0])

def update_pair(self, new_pair):
    global current_symbol, GROUP_INTERVAL
    if new_pair != current_symbol:
        current_symbol = new_pair
        send_telegram_notification(f"Switching to {current_symbol} order book.")
        self.log_output.append(f"Switching to {current_symbol} order book.")
        
        # Update the group interval input value to the default value
        self.group_interval_input.setValue(GROUP_INTERVAL)
        self.update_group_interval(GROUP_INTERVAL)
        
        self.update_order_book(None)  # Clear the order book table

def update_group_interval(self, value):
    global GROUP_INTERVAL
    GROUP_INTERVAL = value
    send_telegram_notification(f"Group interval set to {value}")
    self.log_output.append(f"Group interval set to {value}")
    self.update_order_book(None)  # Trigger a new update of the order book

def update_order_book_thread(self):
    previous_order_book = None
    while True:
        if is_running.is_set():
            try:
                current_order_book = fetch_order_book(symbol=current_symbol, limit=5000, group_interval=GROUP_INTERVAL)
                if current_order_book is None:
                    raise ValueError("Failed to fetch order book data")
                
                bids, asks = analyze_order_book(current_order_book)
                if bids is None or asks is None:
                    raise ValueError("Failed to analyze order book data")

                if previous_order_book:
                    detect_cancellations(previous_order_book, current_order_book)

                bids = sorted(bids, key=lambda x: x[0], reverse=True)
                asks = sorted(asks, key=lambda x: x[0])

                self.update_signal.emit((bids, asks))

                previous_order_book = current_order_book

            except Exception as e:
                logging.error(f"Error in update_order_book: {e}")
                self.log_output.append(f"Error: {e}")
                self.update_signal.emit(None)

        time.sleep(random.uniform(2, 3))

def fetch_order_book(symbol='BTCUSDT', limit=5000, group_interval=GROUP_INTERVAL):
    url = f"https://api.binance.com/api/v3/depth"
    params = {'symbol': symbol, 'limit': limit}
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        order_book = response.json()
        
        if 'bids' not in order_book or 'asks' not in order_book:
            raise ValueError("Invalid order book data: missing 'bids' or 'asks'")
        
        bids = group_orders(order_book['bids'], group_interval)
        asks = group_orders(order_book['asks'], group_interval)
        
        return {'bids': bids, 'asks': asks}
    except requests.RequestException as e:
        logging.error(f"Error fetching order book: {e}")
        return None
    except ValueError as e:
        logging.error(f"Error processing order book data: {e}")
        return None

def analyze_order_book(order_book, self):
    if order_book is None:
        logging.warning("Cannot analyze order book: No data available")
        return None, None
    
    bids = np.array(order_book['bids'], dtype=float)
    asks = np.array(order_book['asks'], dtype=float)
    
    total_bid_volume = np.sum(bids[:, 1])
    total_ask_volume = np.sum(asks[:, 1])
    
    best_bid = bids[-1, 0]
    best_ask = asks[0, 0]
    
    spread = best_ask - best_bid
    mid_price = (best_ask + best_bid) / 2
    
    # Create a formatted string with the analysis results
    analysis_result = (
        f"Best Bid: {best_bid}, Best Ask: {best_ask}\n"
        f"Spread: {spread}, Mid Price: {mid_price}\n"
        f"Total Bid Volume: {total_bid_volume}, Total Ask Volume: {total_ask_volume}"
    )
    
    # Emit a signal to update the GUI log
    self.log_signal.emit(analysis_result)
    
    detect_large_walls(bids, asks)
    
    return bids, asks

def detect_large_walls(bids, asks):
    current_time = time.time()
    
    def check_and_notify(orders, side):
        for price, amount in orders:
            with lock:
                if amount > LARGE_WALL_THRESHOLD:
                    price_key = f"{price:.2f}"
                    if price_key in last_notified_walls[side]:
                        last_time, last_amount = last_notified_walls[side][price_key]
                        if current_time - last_time < NOTIFICATION_COOLDOWN and abs(amount - last_amount) / last_amount < 0.1:
                            continue  # Skip notification if it's too soon and the change is less than 10%
                    
                    last_notified_walls[side][price_key] = (current_time, amount)
                    send_telegram_notification(f"Large {side} wall detected: {amount:.2f} coins at ${float(price):.2f}")
                    break  # Only notify about the first wall found
    
    check_and_notify(bids, 'bids')
    check_and_notify(asks, 'asks')

def play_notification_sound():
    winsound.PlaySound("SystemHand", winsound.SND_ALIAS)

def send_telegram_notification(message):
    try:
        bot.send_message(CHAT_ID, message)
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")
    

def detect_cancellations(previous_book, current_book):
    def compare_orders(prev, curr, side):
        prev_df = pd.DataFrame(prev, columns=['price', 'amount'])
        curr_df = pd.DataFrame(curr, columns=['price', 'amount'])
        
        merged = prev_df.merge(curr_df, on='price', how='outer', suffixes=('_prev', '_curr'))
        merged = merged.fillna(0)
        
        with lock:
            cancellations = merged[merged['amount_prev'] - merged['amount_curr'] > CANCELLATION_THRESHOLD]
        
        for _, row in cancellations.iterrows():
            cancelled_amount = row['amount_prev'] - row['amount_curr']
            color_square = '🟥' if side == 'ask' else '🟩'  # Red square for ask, green for bid
            send_telegram_notification(f"{color_square} Large {side} spoofing detected: {cancelled_amount:.4f} coins at ${row['price']}")

    compare_orders(previous_book['bids'], current_book['bids'], 'bid')
    compare_orders(previous_book['asks'], current_book['asks'], 'ask')

def send_current_state(order_book):
    if order_book is None:
        send_telegram_notification("No order book data available.")
        return
    
    bids = order_book['bids']
    asks = order_book['asks']
    current_price = (float(bids[0][0]) + float(asks[0][0])) / 2
    
    message = f"Current Price: ${current_price:.2f}\n\n"
    message += "Top 5 Bids:\n"
    for price, amount in bids[:5]:
        message += f"${float(price):.2f}: {float(amount):.4f} BTC\n"
    
    message += "\nTop 5 Asks:\n"
    for price, amount in asks[:5]:
        message += f"${float(price):.2f}: {float(amount):.4f} BTC\n"
    
    send_telegram_notification(message)

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(300, 250)  # Set a fixed size for the dialog

        self.setStyleSheet("""
            QWidget {
                background-color: #161A1E;
                color: white;
            }
            QPushButton {
                background-color: #FCD535;
                color: black;
                border: none;
                padding: 5px;
                border-radius: 5px;
                min-width: 20px;
                min-height: 20px;
            }
            QPushButton:hover {
                background-color: #FFE14D;
            }
            QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #2B2B2B;
                color: white;
                border: 1px solid #3A3A3A;
            }
        """)
        self.setup_ui()

    def setup_ui(self):
        layout = QFormLayout(self)

        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        self.pair_dropdown = QComboBox()
        self.pair_dropdown.addItems(['BTCUSDT', 'ETHUSDT', 'XRPUSDT'])
        self.pair_dropdown.currentTextChanged.connect(self.load_pair_settings)
        layout.addRow("Trading Pair:", self.pair_dropdown)

        self.group_interval_input = QSpinBox()
        self.group_interval_input.setRange(10, 1000)
        layout.addRow("Group by:", self.group_interval_input)

        self.large_wall_input = QDoubleSpinBox()
        self.large_wall_input.setRange(0, 1000000)
        layout.addRow("Big orders alert:", self.large_wall_input)

        self.cancellation_input = QDoubleSpinBox()
        self.cancellation_input.setRange(0, 1000000)
        layout.addRow("Spoofing detection:", self.cancellation_input)

        self.cooldown_input = QSpinBox()
        self.cooldown_input.setRange(0, 86400)
        layout.addRow("Notification Cooldown (s):", self.cooldown_input)

        save_button = QPushButton("Save")
        save_button.clicked.connect(self.save_settings)
        layout.addRow(save_button)

        self.load_pair_settings(self.pair_dropdown.currentText())

    def load_pair_settings(self, pair):
        if pair in default_settings:
            settings = default_settings[pair]
            self.group_interval_input.setValue(settings["group_interval"])
            self.large_wall_input.setValue(settings["large_wall_threshold"])
            self.cancellation_input.setValue(settings["cancellation_threshold"])
        self.cooldown_input.setValue(NOTIFICATION_COOLDOWN)

    def save_settings(self):
        global GROUP_INTERVAL, LARGE_WALL_THRESHOLD, CANCELLATION_THRESHOLD, NOTIFICATION_COOLDOWN, current_symbol
        current_symbol = self.pair_dropdown.currentText()
        GROUP_INTERVAL = self.group_interval_input.value()
        LARGE_WALL_THRESHOLD = self.large_wall_input.value()
        CANCELLATION_THRESHOLD = self.cancellation_input.value()
        NOTIFICATION_COOLDOWN = self.cooldown_input.value()

        default_settings[current_symbol] = {
            "group_interval": GROUP_INTERVAL,
            "large_wall_threshold": LARGE_WALL_THRESHOLD,
            "cancellation_threshold": CANCELLATION_THRESHOLD
        }

        self.accept()

class OrderBookGUI(QMainWindow):
    update_signal = pyqtSignal(object)
    log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cryptocurrency Order Book")
        self.setFixedSize(300, 800)  # Set initial size to 200x800
        self.setup_ui()
        self.update_signal.connect(self.update_order_book)
        self.log_signal.connect(self.update_log)
        self.start_update_thread()

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #161A1E;
                color: white;
            }
            QPushButton {
                background-color: #FCD535;
                color: black;
                border: none;
                padding: 5px;
                border-radius: 5px;
                min-width: 20px;
                min-height: 20px;
            }
            QPushButton:hover {
                background-color: #FFE14D;
            }
            QTableWidget {
                gridline-color: #2B2B2B;
            }
            QHeaderView::section {
                background-color: #2B2B2B;
                color: white;
            }
            QTableWidget QTableCornerButton::section {
                background-color: #2B2B2B;
            }
        """)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Left side (Order book)
        left_widget = QWidget()
        self.left_widget = left_widget  # Add this line
        left_layout = QVBoxLayout(left_widget)
            
        # Order book table
        self.order_book_table = QTableWidget()
        self.order_book_table.setColumnCount(2)
        self.order_book_table.setHorizontalHeaderLabels(["Price", "Amount"])
        self.order_book_table.horizontalHeader().setStretchLastSection(True)
        left_layout.addWidget(self.order_book_table)

        # Current price label
        self.current_price_label = QLabel("Current Price: N/A")
        self.current_price_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_price_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        left_layout.addWidget(self.current_price_label)

        # Control panel
        control_layout = QHBoxLayout()
        left_layout.addLayout(control_layout)

        # Start/Stop button
        self.start_stop_button = QPushButton("Stop")
        self.start_stop_button.clicked.connect(self.toggle_updates)
        control_layout.addWidget(self.start_stop_button)

        # Settings button
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings)
        control_layout.addWidget(self.settings_button)

        # Toggle log button
        self.toggle_log_button = QPushButton("Hide Log")
        self.toggle_log_button.clicked.connect(self.toggle_log_output)
        control_layout.addWidget(self.toggle_log_button)

        # Right side (Log output)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumWidth(400)  # Set a minimum width for the log output
        self.log_output.hide()  # Hide the log output by default

        # Create a splitter
        
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left_widget)
        self.splitter.addWidget(self.log_output)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([300, 0])  # Initially, only show the order book

        main_layout.addWidget(self.splitter)

        # Hide the log output initially
        self.log_output.hide()

        # Update the toggle log button text
        self.toggle_log_button.setText("Show Log")


    def set_column_widths(self):
        self.order_book_table.setColumnWidth(0, 95)  # Price column
        self.order_book_table.setColumnWidth(1, 95)  # Amount column

    def toggle_log_output(self):
        if self.log_output.isVisible():
            self.log_output.hide()
            self.toggle_log_button.setText("Show Log")
            self.setFixedSize(300, 800)
            self.splitter.setSizes([300, 0])
        else:
            self.log_output.show()
            self.toggle_log_button.setText("Hide Log")
            self.setFixedSize(600, 800)
            self.splitter.setSizes([300, 300])
    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec():
            self.update_order_book(None)  # Trigger a new update of the order book

    def start_update_thread(self):
        self.update_thread = threading.Thread(target=self.update_order_book_thread, daemon=True)
        self.update_thread.start()

    def update_order_book_thread(self):
        previous_order_book = None
        while True:
            if is_running.is_set():
                try:
                    current_order_book = fetch_order_book(symbol=current_symbol, limit=5000, group_interval=GROUP_INTERVAL)
                    if current_order_book is None:
                        raise ValueError("Failed to fetch order book data")
                    
                    bids, asks = analyze_order_book(current_order_book, self)
                    if bids is None or asks is None:
                        raise ValueError("Failed to analyze order book data")

                    if previous_order_book:
                        detect_cancellations(previous_order_book, current_order_book)

                    bids = sorted(bids, key=lambda x: x[0], reverse=True)
                    asks = sorted(asks, key=lambda x: x[0])

                    self.update_signal.emit((bids, asks))

                    previous_order_book = current_order_book

                except Exception as e:
                    logging.error(f"Error in update_order_book: {e}")
                    self.log_output.append(f"Error: {e}")
                    self.update_signal.emit(None)

            time.sleep(random.uniform(2, 3))

    def update_log(self, message):
        self.log_output.append(message)

    def update_order_book(self, data):
        if data is None:
            self.show_error("Error updating order book. Retrying...")
            return

        bids, asks = data
        self.order_book_table.setRowCount(len(bids) + len(asks) + 1)  # +1 for the separator row

        current_price = (bids[0][0] + asks[0][0]) / 2
        self.current_price_label.setText(f"Current Price: ${current_price:.2f}")

        max_volume = max(max(bid[1] for bid in bids), max(ask[1] for ask in asks))
        
        bid_cmap = LinearSegmentedColormap.from_list("bid_cmap", ["#1a2636", "#39a789", "#2EBD85"])
        ask_cmap = LinearSegmentedColormap.from_list("ask_cmap", ["#1a2636", "#F25C54", "#F6465D"])

        for i, ask in enumerate(reversed(asks)):
            color = self.get_color_for_volume(ask[1], max_volume, ask_cmap)
            self.set_table_item(i, 0, f"{ask[0]:.2f}", QColor("#F6465D"), color)
            self.set_table_item(i, 1, f"{ask[1]:.4f}", QColor("#F6465D"), color)

        separator_row = len(asks)
        self.set_table_item(separator_row, 0, "--------", QColor("white"), QColor("#161A1E"))
        self.set_table_item(separator_row, 1, "--------", QColor("white"), QColor("#161A1E"))

        for i, bid in enumerate(bids):
            row = i + separator_row + 1
            color = self.get_color_for_volume(bid[1], max_volume, bid_cmap)
            self.set_table_item(row, 0, f"{bid[0]:.2f}", QColor("#2EBD85"), color)
            self.set_table_item(row, 1, f"{bid[1]:.4f}", QColor("#2EBD85"), color)

    def get_color_for_volume(self, volume, max_volume, cmap):
        normalized_volume = volume / max_volume
        rgba = cmap(normalized_volume)
        return QColor.fromRgbF(rgba[0], rgba[1], rgba[2], rgba[3])

    def set_table_item(self, row, col, value, text_color, bg_color):
        item = QTableWidgetItem(value)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(text_color)
        item.setBackground(bg_color)
        self.order_book_table.setItem(row, col, item)

    def show_error(self, message):
        self.order_book_table.setRowCount(1)
        self.set_table_item(0, 0, "Error", QColor("white"), QColor("red"))
        self.set_table_item(0, 1, message, QColor("white"), QColor("red"))
        self.current_price_label.setText("Error: No data available")
        self.log_output.append(message)

    def toggle_updates(self):
        if is_running.is_set():
            is_running.clear()
            self.start_stop_button.setText("Start")
            send_telegram_notification("Order book updates stopped.")
            self.log_output.append("Order book updates stopped.")
        else:
            is_running.set()
            self.start_stop_button.setText("Stop")
            send_telegram_notification("Order book updates resumed.")
            self.log_output.append("Order book updates resumed.")
            current_order_book = fetch_order_book()
            send_current_state(current_order_book)

if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    
    # Start the Telegram bot in a separate thread
    bot_thread = threading.Thread(target=bot.polling, daemon=True)
    bot_thread.start()
    
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(r'C:\Users\danie\liquidation\icon.png'))
    window = OrderBookGUI()
    window.show()
    sys.exit(app.exec())