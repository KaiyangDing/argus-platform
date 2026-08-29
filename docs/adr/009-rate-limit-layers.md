# ADR-009：限流分三层，HTTP 层钉 fastapi-limiter 0.1.6（Redis 计数）

日期：2026-08-29（P3.2）
状态：已接受

## 背景

P3 验收要求 per-user 的 HTTP 限流（Redis 计数）。平台的请求分三种形态：
瞬时写请求（登录/上传/发起）、长挂连接（SSE 流）、烧钱操作（研究/追问）。
一把闸管不了三种形态。

## 决策

三层各司其职：
1. **HTTP 频率闸**（fastapi-limiter 0.1.6，Redis lua 原子计数）：登录后
   per-user（跨设备共享额度），未登录退化 per-IP；只挂写路径与烧钱路径
   + healthz，读路径不设闸；
2. **SSE 并发闸**（Redis INCR/DECR + TTL 兜底，app/limits.py SseGate）：
   限「同时在线的流」数量——流式连接一挂几分钟，按请求数限不住它；
   研究进度流与对话流共享一个池；
3. **业务配额闸**（预算/任务槽，ADR-008）：走 PG，管钱不管频率。

版本钉死 0.1.6：0.2.0 重写换成 pyrate-limiter 进程内桶，Redis 退成自装
插件——进程内计数在多副本部署下各数各的，限流形同虚设；0.1.6 的 lua
计数天然跨进程一致，正是「per-user，Redis 计数」的字面实现。

只用库的核心（init 生命周期 + lua 原子计数脚本），不用它的 RateLimiter
依赖：其 __call__ 按 route_index 扫 app.routes 拼计数键，FastAPI 0.141 的
include_router 产物 _IncludedRouter 无 .path，一碰即崩（0.2.0 先取
route.path 再 hasattr，同病；测试抓获）。且 route 序号键随路由增删漂移，
部署一次计数器全体清零。自己的依赖用显式 scope 键（register/chat/...）：
稳定、可读、语义与库版一致——适配层十行，计数核心仍是库的。

限流基础设施不可用（未 init / Redis 挂）时 **fail-open**：限流是防滥用
的护栏，不是核心功能的前置依赖，不该连坐把登录拦死；副作用是全部 API
测试免 init 直跑（ASGITransport 不执行 lifespan），限流行为由专项测试
在真 Redis 上覆盖。

## 阈值（怎么定的）

- register 5/min、login 10/min、refresh 30/min（per-IP）：人手速之上、
  脚本之下；
- upload 20/min：「拖一批年报进来」的真实峰值；
- research 10/min、chat 20/min：真实约束在业务配额（ADR-008），这里只防
  连点把 429 打成日志洪水；诚实使用一轮 ≥3s，打不满 20/min；
- healthz 60/min：每次真连三个依赖，不设闸就是压测放大器；
- SSE 并发 5/user：前端峰值 2（研究进度 + 对话）的 2.5 倍余量；
  TTL 1800s 是崩溃连接槽位的自愈上限。

## 后果

- SSE 闸计连接不计计算：对话断线后图仍在后台跑完（ADR-010），槽位随
  连接断开即还——「并发烧钱」由配额闸管，这里只管连接数；
- 读路径（列表/轮询/详情）无闸：承载力由压测（P3.3/P3.6）验证，读闸
  会先把自己前端的轮询打死；
- SseGate 负漂移自愈取「宁可短暂多放，不永久少放」（DECR 到负即删键）。
