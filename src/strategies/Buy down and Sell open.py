from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from datetime import datetime
from typing import Optional
import threading, time, pytz

# 配置合约参数（请按需修改）
TRADING_SYMBOL = "MNQ"
CONTRACT_MONTH = "202412"  # 请根据实际前月合约更新，例如 '202412'
EXCHANGE = "CME"  # 也可使用 "GLOBEX"（视券商/账户设置而定）
CURRENCY = "USD"
TRADING_CLASS = "MNQ"


def build_mnq_contract() -> Contract:
    """构建MNQ期货合约"""
    c = Contract()
    c.symbol = TRADING_SYMBOL
    c.secType = "FUT"
    c.exchange = EXCHANGE
    c.currency = CURRENCY
    c.lastTradeDateOrContractMonth = CONTRACT_MONTH
    c.tradingClass = TRADING_CLASS
    return c


# 策略逻辑类
class BuyDownSellOpenStrategy:
    def __init__(self):
        self.trades_today = 0       # 今日已执行买入次数
        self.position = False       # 当前是否持有头寸
        self.last_trade_day = None  # 上次交易日期（用于每日重置）

    def initialize(self):
        print("策略初始化完成。")

    def check_new_day(self):
        today = datetime.now(pytz.timezone("US/Eastern")).date()
        if self.last_trade_day != today:
            self.trades_today = 0
            self.last_trade_day = today

    def on_tick(self, current_time: datetime, ib_app):
        # 每次收到“时间更新”时调用，尝试在指定时间点下单
        self.check_new_day()
        est_time = current_time.astimezone(pytz.timezone("US/Eastern"))
        hour, minute = est_time.hour, est_time.minute

        # 收盘前1分钟（美股16:00收盘，因此判定 15:59）买入（每次1手，最多3次）
        if hour == 15 and minute == 59:
            if not self.position and self.trades_today < 3:
                # 构造市价单（BUY 1）
                order = Order()
                order.action = "BUY"
                order.orderType = "MKT"
                order.totalQuantity = 1

                try:
                    if ib_app.nextOrderId is None:
                        print("等待获取下一个可用的 OrderId...")
                    else:
                        contract = build_mnq_contract()
                        ib_app.placeOrder(ib_app.nextOrderId, contract, order)
                        print(f"{est_time} 买入下单，OrderId={ib_app.nextOrderId}")
                        ib_app.nextOrderId += 1
                        self.trades_today += 1
                        self.position = True
                except Exception as e:
                    print(f"买入下单失败: {e}")

        # 开盘后1分钟（9:31），卖出持仓（有仓位时）
        if hour == 9 and minute == 31:
            if self.position:
                order = Order()
                order.action = "SELL"
                order.orderType = "MKT"
                order.totalQuantity = 1

                try:
                    if ib_app.nextOrderId is None:
                        print("等待获取下一个可用的 OrderId...")
                    else:
                        contract = build_mnq_contract()
                        ib_app.placeOrder(ib_app.nextOrderId, contract, order)
                        print(f"{est_time} 卖出下单，OrderId={ib_app.nextOrderId}")
                        ib_app.nextOrderId += 1
                        self.position = False
                except Exception as e:
                    print(f"卖出下单失败: {e}")


# IB 客户端应用（整合策略逻辑）
class IBApp(EWrapper, EClient):
    def __init__(self, strategy):
        EClient.__init__(self, self)
        self.strategy = strategy
        self.nextOrderId: Optional[int] = None
        self.last_server_time: Optional[datetime] = None

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.nextOrderId = orderId
        print(f"获取到下单ID：{orderId}")

    def currentTime(self, time_int: int):
        # 服务器返回Unix时间戳（秒）
        try:
            self.last_server_time = datetime.fromtimestamp(time_int, tz=pytz.utc)
        except Exception as e:
            print(f"解析服务器时间失败: {e}")

    def get_current_time(self) -> datetime:
        # 优先使用服务器时间，其次使用本地UTC时间
        return self.last_server_time or datetime.now(pytz.utc)

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, *args):
        super().orderStatus(orderId, status, filled, remaining, avgFillPrice, *args)
        print(f"订单状态更新：OrderId={orderId}, 状态={status}, 成交量={filled}, 均价={avgFillPrice}")

    def openOrder(self, orderId, contract, order, orderState):
        super().openOrder(orderId, contract, order, orderState)
        print(f"打开订单：OrderId={orderId}, 合约={contract.symbol}, {order.action} {order.totalQuantity} 手")

    def error(self, reqId, errorCode, errorString):
        print(f"错误 {reqId}: {errorCode} {errorString}")


def run_loop(app):
    app.run()


if __name__ == "__main__":
    # 创建策略和 IB 应用
    strategy = BuyDownSellOpenStrategy()
    strategy.initialize()

    app = IBApp(strategy)
    app.connect("127.0.0.1", 4002, clientId=1)  # 连接 TWS/IB Gateway（按需修改端口）
    # 启动后台线程运行 IB 消息循环
    api_thread = threading.Thread(target=run_loop, args=(app,), daemon=True)
    api_thread.start()

    # 等待获取到下单ID
    while app.nextOrderId is None:
        time.sleep(0.1)

    print("开始执行策略 ...")
    try:
        # 主循环：每秒检查一次当前时间（优先使用IB服务器时间）
        while True:
            try:
                app.reqCurrentTime()
            except Exception:
                pass
            current_time = app.get_current_time()
            strategy.on_tick(current_time, app)
            time.sleep(1)
    except KeyboardInterrupt:
        app.disconnect()
        print("策略终止，已断开连接。")
