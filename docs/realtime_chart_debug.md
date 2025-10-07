# 实时按钮导致 `/chart_data` 503 的排查步骤

下面的步骤可以帮助你定位“在监控弹窗中点击实时按钮后，`/chart_data` 接口返回 `503 {"detail":"Market data service unavailable"}`”的问题。每一步都尽量给出要观察的现象和可能的下一步措施。

## 1. 前端触发流程确认

1. **确认按钮绑定的逻辑是否执行**  
   打开浏览器 DevTools 的 Console，在点击实时按钮前输入 `window.monitoringManager`（或你暴露的管理实例）查看是否存在。点击按钮时关注是否出现 `"[monitor] subscribe realtime"` 的日志 —— 这是 `subscribeMonitorRealtime` 中的调试输出，说明点击事件确实命中该分支。【F:static/js/monitoring/manager.js†L1847-L1889】【F:static/js/monitoring/manager.js†L2429-L2457】

2. **检查按钮点击后发起的 HTTP 请求**  
   在 Network 面板中勾选“Preserve log”，点击实时按钮后定位到 `POST /chart_data`，确认请求体内是否带上了 `symbol`、`duration`、`bar_size` 以及 `use_cache=false`、`force_refresh=true` 等字段。这些字段来自 `loadMonitorChart`，实时模式会强制绕过缓存，触发服务端的强制刷新路径。【F:static/js/monitoring/manager.js†L2759-L2823】

3. **观察 WebSocket 订阅是否成功**  
   同时在 Network -> WS 中查看监控页面使用的 WebSocket 连接，确认是否向服务器发送了 `{type: "subscribe_realtime", symbol: "..."}` 消息。如果没有发送或者服务端立即返回错误，需要先解决实时订阅失败的问题。【F:static/js/monitoring/manager.js†L1873-L1885】【F:src/routes/websocket.py†L2200-L2232】

## 2. 后端 API 行为核对

4. **查看 FastAPI 日志中 `/chart_data` 的处理结果**  
   后端的 `/chart_data` 路由会调用 `_fetch_chart_data_payload`，先尝试内存缓存，再转发到 market-data 服务。如果缓存被跳过（`force_refresh=true`），任何下游的异常都会透传为 503。确认日志里是否有 "Market data service unavailable" 或者更底层的异常堆栈。【F:src/routes/market_data.py†L187-L265】

5. **确认转发到 market-data 服务的 payload**  
   如果需要更精确的信息，可临时在 `_fetch_chart_data_payload` 中打日志（或使用现有的调试工具）输出转发的参数，验证 `symbol`、`duration` 等是否符合预期，尤其注意是否因为 realtime 切换造成了空 symbol。

6. **单独调用 market-data 服务**  
   使用 `curl` 或 `httpie` 直接请求 market-data 微服务的 `/chart_data`，带上与前端一致的 JSON，确认服务是否正常。例如：
   ```bash
   curl -X POST "http://<market-service-host>:<port>/chart_data" \
        -H 'Content-Type: application/json' \
        -d '{"symbol":"AAPL","duration":"30 M","bar_size":"1 min","force_refresh":true,"use_cache":false}'
   ```
   如果这里也返回 503，说明问题在 microservice 或其依赖。

## 3. market-data 微服务内部检查

7. **检查 Redis / 缓存依赖是否可用**  
   `_load_series` 在最开始会通过 `_ensure_redis` 获取 Redis；失败时会抛出 503（"Market data cache unavailable"），FastAPI 网关会把它包装成 “Market data service unavailable”。确认 Redis 实例是否可连接，或者服务日志里是否有“Failed to acquire Redis client for chart data”之类的信息。【F:services/market_data/main.py†L1954-L1964】【F:services/market_data/main.py†L2117-L2157】

8. **确认行情数据聚合流程是否正常**  
   `_load_series` 会根据 `bar_size` 推导目标 interval，遍历候选粒度，必要时回落到 IB 数据。查看日志中是否有大量的 coverage gap 警告、IB fallback 失败等提示，排查数据源是否缺失或缓存未命中导致实时强制刷新时拿不到数据。【F:services/market_data/main.py†L2139-L2245】

9. **检查 `ServiceClient` 的请求情况**  
   如果 market-data 服务本身正常，可能是 API 层与 microservice 之间的网络问题。`ServiceClient.request` 会在超时或连接错误时抛出 `ServiceUnavailableError`，从而让 API 返回 503。调高日志级别或开启 httpx 的调试，确认是否存在超时、DNS 错误等网络异常。【F:src/routes/service_clients.py†L71-L167】

## 4. WebSocket 与实时数据交互

10. **确认实时订阅不会频繁切换 Symbol**  
    如果点击实时按钮的同时切换了监控的合约，前端会调用 `unsubscribeMonitorRealtime` 并触发一次强制刷新。反复切换可能让 service 在短时间内处理多个强制刷新请求，导致积压或被限流。利用前端日志确认在点击实时按钮后 `this.monitorRealtimeSymbol` 是否稳定。【F:static/js/monitoring/manager.js†L1853-L1916】

11. **观察服务器端实时 hub 的行为**  
    WebSocket 路由会记录订阅失败并切换到轮询模式。如果看到 “Falling back to polling realtime feed” 的日志，说明订阅未成功，实时刷新后端可能仍依赖历史接口补数据。需要排查 `_REALTIME_PRICE_HUB.subscribe` 对应的依赖是否健康。【F:src/routes/websocket.py†L2207-L2221】

## 5. 综合判断

- 如果 market-data 服务直接返回 503：重点检查 Redis、数据源或服务自身状态。
- 如果只有 API 层返回 503，而直接访问微服务正常：排查 API 与微服务之间的网络、认证、或者 `force_refresh` 分支造成的 payload 异常。
- 如果 503 只在点击实时按钮后出现：说明常规缓存路径正常，而强制刷新触发的问题，多数与下游依赖（Redis、外部行情、IB 回退）相关，需要重点检查这些组件。

按照上述步骤逐层定位，可以明确是哪一层在实时模式下出了问题，再对症处理。
