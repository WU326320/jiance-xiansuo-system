# GitHub 电商采集项目调研（2026-11-05）

数据来源：GitHub Search API（`电商 爬虫`、`淘宝 京东 爬虫`、`1688 采集`、`taobao spider` 等按 stars 排序）+ 各仓库 `/commits?per_page=1` 首条。星数、日期均为本次实测，非 README 自述。

| 项目 | 链接 | 星数 | 最后提交 | 平台 | 可借鉴点 | 许可证 |
|---|---|---|---|---|---|---|
| NanmiCoder/MediaCrawler | github.com/NanmiCoder/MediaCrawler | 70,209 | 2026-11-05 | 小红书/抖音/快手/B站/微博/贴吧/知乎（无电商） | CDP 接管已登录 Chrome、JS 表达式取签名、登录态缓存、平台分模块 | 非商业学习许可 1.1 |
| DropsDevopsOrg/ECommerceCrawlers | github.com/DropsDevopsOrg/ECommerceCrawlers | 5,765 | 2024-05-22 | 淘宝/天猫/闲鱼等 | TaobaoCrawler 用 mitmproxy 抓包绕签名、商品字段与翻页套路 | MIT |
| ShilongLee/Crawler | github.com/ShilongLee/Crawler | 1,475 | 2026-10-19 | 淘宝/京东 + 抖音等 | HTTP API 服务化、按平台 add_account、IP 代理池、Docker 一键 | 非商业使用许可 |
| cv-cat/catbus | github.com/cv-cat/catbus | 572 | 2026-11-04 | 淘宝/京东/闲鱼等 12 平台 | 统一命令与 JSON 信封、`RISK_CONTROL` 显式报错即停、登录态按平台/账号隔离 0700 | MIT |
| zhangjiancong/MarketSpider | github.com/zhangjiancong/MarketSpider | 446 | 2025-05-26 | 淘宝/京东/1688 | 关键词→链接/价格/名称/店铺字段模型、GetCookie 取登录态、Tkinter 状态监控 | MIT |

未入选：lxSpider（1,977，GPL-3.0）、python-spider（19,846）等属案例合集/教程，非可复用框架，且 GPL 不适合产品化。

## 推荐

**基于什么**：以 MarketSpider 的字段与任务模型为骨架，借 MediaCrawler 的登录态方案和 catbus 的风控停机语义，自行实现采集层；不整体 fork——前两者与 ShilongLee/Crawler 均为非商业许可，若监测服务对外收费，只借鉴思路、不复制代码。

**借鉴哪部分**：
1. MarketSpider：搜索词→商品 ID/链接/店铺/价格/销量的字段定义与翻页、登录 Cookie 获取流程；其 Selenium 层已停更近一年，不直接用。
2. MediaCrawler：`ENABLE_CDP_MODE` 连接本机已登录 Chrome，复用 Cookie/扩展，比全新 Playwright 上下文更不易触发风控；登录态按平台隔离目录。
3. catbus：把验证码/风控当一等错误（`RISK_CONTROL`），不自动重试、不自动打码，任务置 paused 并告警；这正合“遇验证码就停”。

**要装的依赖**：Python 3.11；`scrapling[fetchers]==0.5.1`（会带入 playwright 1.62、patchright、curl_cffi）；`playwright install chromium` 或直接用本机 Chrome；可选 mitmproxy（调试淘宝签名）、Node 20（仅当复用 MediaCrawler 的 JS 取签名）。存储用标准库 SQLite 即可。

**是否仍用 Scrapling**：用。0.5.1 的 `StealthySession(cdp_url=...)` 可接管已登录 Chrome，`StealthyFetcher` 支持 `user_data_dir` 持久化，解析与自适应选择器够用；但其 `solve_cloudflare` 对本场景无用，验证码判定须自己写（URL、页面文案、`#nc_1_wrapper`/baxia 等选择器），命中即停。分工：Scrapling 负责会话与解析，Playwright/CDP 负责登录与风控判定。
