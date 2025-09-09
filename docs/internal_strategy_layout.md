# Internal Strategy Layout and API

The web interface now provides a dedicated layout when viewing **internal** strategies.

## Tabs

Internal strategies display three tabs:

1. **Summary** – shows description, file path, schedule and SL/TP settings.
2. **Orders & PnL** – renders the main orders table for quick review of fills and profit/loss.
3. **Visual Metrics** – reuses existing performance charts.

The previous *API Examples* tab is hidden for internal strategies.

## Backend API

A new endpoint consolidates internal strategy information:

```
GET /api/strategies/{name}/internal-info
```

Returns strategy details including schedule and any associated risk rules
(SL/TP values). The response structure matches other strategy endpoints and
adds a `risk_rules` field keyed by symbol.

This data is used by the frontend to populate the Summary tab.
