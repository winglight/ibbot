try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
except ModuleNotFoundError:
    import pytest
    pytest.skip("ibapi 未安装，跳过示例模块", allow_module_level=True)
import threading
import time
import datetime

class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.start_time = time.time()
        self.market_ready = False

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        print(f"ERROR {reqId} {errorCode} {errorString}")
        # 检测2104+usfuture，代表CME行情场连好了
        if errorCode == 2104 and "usfuture" in errorString:
            self.market_ready = True

    def tickPrice(self, reqId, tickType, price, attrib):
        if tickType == 4 and price > 0:  # LAST price
            print(f"[{time.time()-self.start_time:.3f}s] ReqId {reqId} LAST price: {price}")

    def tickString(self, reqId, tickType, value):
        if tickType == 45:  # last timestamp
            quote_time = datetime.datetime.fromtimestamp(int(value))
            now = datetime.datetime.utcnow()
            diff_sec = (now - quote_time).total_seconds()
            if diff_sec > 10:
                print(f"⚠️ ReqId {reqId} 延迟 {diff_sec/60:.1f} 分钟（可能无实时权限）")
            else:
                print(f"✅ ReqId {reqId} 实时行情")

    def nextValidId(self, orderId):
        print("Connected. Waiting for usfuture market data farm...")
        # 开一个线程等待CME行情连好
        threading.Thread(target=self.wait_and_request).start()

    def wait_and_request(self):
        # 最多等10秒
        for _ in range(20):
            if self.market_ready:
                print("usfuture ready, requesting MNQ & MES...")
                self.reqMktData(1, self.mnq_contract(), "", True, False, [])
                self.reqMktData(2, self.mes_contract(), "", True, False, [])
                return
            time.sleep(0.5)
        print("⚠️ usfuture 未连接，无法请求实时期货行情")

    def mnq_contract(self):
        c = Contract()
        c.symbol = "MNQ"
        c.secType = "FUT"
        c.exchange = "CME"
        c.currency = "USD"
        c.lastTradeDateOrContractMonth = "202509"
        return c

    def mes_contract(self):
        c = Contract()
        c.symbol = "MES"
        c.secType = "FUT"
        c.exchange = "CME"
        c.currency = "USD"
        c.lastTradeDateOrContractMonth = "202509"
        return c

def run_loop(app):
    app.run()

if __name__ == "__main__":
    app = IBApp()
    app.connect("127.0.0.1", 4001, clientId=2)

    api_thread = threading.Thread(target=run_loop, args=(app,), daemon=True)
    api_thread.start()

    time.sleep(8)
    app.disconnect()
