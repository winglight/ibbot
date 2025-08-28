import asyncio
from ib_insync import IB, Stock, util

async def main():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4002, clientId=1)

    contract = Stock('AAPL', exchange='SMART', currency='USD')
    await ib.qualifyContractsAsync(contract)

    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime='',
        durationStr='1 D',
        barSizeSetting='5 mins',
        whatToShow='TRADES',
        useRTH=True
    )

    print(util.df(bars).head())
    await ib.disconnectAsync()

if __name__ == '__main__':
    asyncio.run(main())   # Python 3.7+
