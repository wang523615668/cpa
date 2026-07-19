# Grok / CPA 额度说明（2026-07-15 实测）

## 额度会不会重置？

**会。不是永久死号。**

xAI 免费 Build（`grok-4.5-build-free`）额度是 **滚动 24 小时窗口**：

```
code: subscription:free-usage-exhausted
"Usage resets over a rolling 24-hour window
 — tokens (actual/limit): 2135162/2000000"
```

| 项 | 实测值 |
|---|---|
| 窗口 | **rolling 24h**（按过去 24h token 累计，不是固定每天 0 点） |
| 上限 | 约 **2,000,000 tokens / 窗** |
| 触顶后 | chat 429，但 **refresh_token 仍有效**，`/models` 仍能列 `grok-4.5` |
| 恢复方式 | **等窗口滑过去**（大约再过十几～24 小时，视消耗曲线） |
| 不能做什么 | 没有本地「一键重置额度」API；删号重注册才是新额度 |

## 两类「失效」别混

| 类型 | 错误 | 处理 |
|---|---|---|
| **额度耗尽** | `free-usage-exhausted` / spending-limit | 临时隔离 `disabled=true`，**约 20h 后自动 revive 试活**，勿删 |
| **权限拒绝** | `permission-denied` + unavailable | 批量限流，**勿删**；CPA 会标 unavailable 跳过 |

## 池子策略

- 扫描：`cpa/monitor/cpa_auth_scan.py`
- health 每 30 分钟：`scan --quarantine --revive` + chat probe
- 闲时单号补池：`register_one_then_kill.sh`（注册 1 个就杀浏览器）
- 目标池 100，底线 30（按 **usable** 计，不含 spending/unavailable）

## 重度用量粗估

单号约 200 万 token/24h；Hermes 长上下文+工具一次可能数万 token。  
重度日用建议常备 **可用号 60–100** 做轮转。
