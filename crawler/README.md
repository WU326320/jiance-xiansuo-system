# 电商侵权线索采集模块（淘宝/天猫、京东、1688）

R1 采集层实现。代码位置：`/workspace/ip-monitor/crawler`。

- 采集框架：Scrapling 0.5.1（Playwright）；支持两种浏览器模式：
  - `playwright`（默认）：`StealthySession` + 持久化 profile；
  - `cdp`（京东默认；Windows profile 三平台默认）：连接系统真实 Chrome
    （`RemoteSession`/`connect_over_cdp`），共享人工登录后的浏览器与 profile，
    避免自动化 Chromium 被京东识别（418/黑屏）。
- 统一接口：每个平台都实现 `check_login()` / `search(keyword, max_pages)` / `fetch_detail(product_ids)`
- 输出：`out/{date}/{platform}.jsonl`（每行一个商品）+ `*.run.json` + `run_logs.jsonl`
- 风控原则：出现验证码/滑块/访问受限或登录失效，**立即停止该平台**，只记录不破解、不重试；
  同日命中后写 `state/risk/{platform}-{date}.json`，再次运行直接跳过该平台
- 测试：全部离线 fixture，不访问任何真实电商网站

## 1. 目录结构

```
crawler/
  cli.py                  # login / check-login / run / detail
  runner.py               # 运行编排、关键词矩阵、JSONL/运行记录/图片
  login.py                # 有界面人工登录、登录态检查
  config.py               # YAML 配置加载/校验/路径
  models.py               # Item / ItemPatch / RunRecord
  core/
    browser.py            # Scrapling 封装（playwright/cdp）+ 测试用 StaticBrowser
    chrome.py             # 真实 Chrome 定位/启动、CDP 端口探测
    cookies.py            # 登录 Cookie 只读判定（淘宝/1688 CDP 严格校验）
    throttle.py           # 翻页/关键词/详情间隔、RPM；采集入口关闭每日数量限制
    retry.py              # 网络/5xx/429 指数退避；风控不重试
    risk.py               # 登录页/验证码/滑块判定
    riskstate.py          # 同日风控熔断标记（state/risk）
    parse.py              # “1万+”下限、价格区间取最低、ID/URL 规范化、去重
    schema.py             # 输出字段校验、缺失 flags
    jsonl.py              # JSONL / 运行记录
    images.py             # 主图下载（平台 Referer）
    jsonutil.py / dom.py  # JSON 容错抽取、lxml DOM 工具
  platforms/
    base.py               # 统一流程
    taobao.py / jd.py / ali1688.py
    selectors.py          # 三平台选择器集中处
  tests/                  # 180 个离线单测 + fixtures
config/rules.yaml         # Linux/默认运行配置（含节流/重试、旧数量兼容键）
config/windows.yaml       # Windows 10：三平台真实 Chrome/CDP、独立 profile/端口
samples/2026-11-05/       # 离线生成的各平台 20 行样例
tools/make_samples.py     # 离线样例生成
```

## 2. 环境与依赖

项目自带 venv，已可直接运行：

```bash
cd /workspace/ip-monitor/crawler
source .venv/bin/activate
python -c "import scrapling; print(scrapling.__version__)"   # 0.5.1
```

Playwright 模式浏览器二进制在 `/workspace/tools/ms-playwright`。如果换 shell 后
Playwright 找不到浏览器：

```bash
export PLAYWRIGHT_BROWSERS_PATH=/workspace/tools/ms-playwright
```

Scrapling 0.5.1 没有 `DynamicFetcher`；Playwright 模式使用 `StealthySession` 的
`user_data_dir` 持久化 profile。CDP 模式不下载/启动 Playwright 浏览器，而是复用系统
安装的 Google Chrome。Linux 通过 `which google-chrome` / `google-chrome-stable` /
`chromium` 定位；Windows 依次查 `C:\Program Files\Google\Chrome\Application\chrome.exe`、
`C:\Program Files (x86)\...`、`%LOCALAPPDATA%\...`，最后用 Microsoft Edge 兜底；
也可以配置 `browser.chrome_executable`。登录/采集都不会导出 Cookie。

依赖清单：`requirements.txt`（Windows Python 3.13 已核对 win_amd64/纯 Python wheel，
含 `tzdata`）。测试依赖 `pytest`、`pytest-cov`。

## 3. Windows 本机运行（PowerShell）

Windows 10 + Python 3.13，已安装 Google Chrome（默认
`C:\Program Files\Google\Chrome\Application\chrome.exe`）。`config/windows.yaml`
让淘宝/京东/1688 三个平台都走真实 Chrome 的 CDP 模式，各自独立 profile 和端口
（taobao=9222、jd=9223、1688=9224）。

```powershell
cd C:\path\to\ip-monitor\crawler
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` 里的 `tzdata` 用于 Windows 的 `Asia/Shanghai`。`scrapling[fetchers]`
仍会安装 Python 包 playwright/patchright，但 cdp 模式 **不需要** 执行
`playwright install` 下载 Playwright 自带浏览器；实际浏览器就是本机 Chrome。

三个平台分别人工登录（每个命令使用独立 `profiles\<platform>-chrome` 拉起或复用 Chrome）：

```powershell
python -m crawler login taobao --config .\config\windows.yaml
python -m crawler login jd     --config .\config\windows.yaml
python -m crawler login 1688   --config .\config\windows.yaml
```

检查登录态：

```powershell
python -m crawler check-login all --config .\config\windows.yaml
```

按显式关键词逐页采集到末页（不是单页试采）：

```powershell
python -m crawler run --config .\config\windows.yaml --platform taobao --keyword 葫芦兄弟
python -m crawler run --config .\config\windows.yaml --platform jd     --keyword 葫芦兄弟
python -m crawler run --config .\config\windows.yaml --platform 1688   --keyword 葫芦兄弟
```

Windows 说明：

- 不检查、也不设置 `DISPLAY`；`login` / `check-login` / `run` 都不需要 X server。
- Chrome 查找顺序：`browser.chrome_executable` → `Program Files` →
  `Program Files (x86)` → `%LOCALAPPDATA%` → PATH；找不到 Chrome 时用 Edge 兜底。
- 启动子进程使用 `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`，Chrome 独立于
  PowerShell 运行；程序退出只断开 CDP，不结束用户浏览器。
- CDP 登录检测：淘宝需 `_nk_`，或 `cookie2` 与 `unb` 同时存在；1688 必须
  `__cn_logon__=true`，跳到 `login.taobao.com` 不算成功；京东仍看
  `pt_key`/`thor` 或 URL 离开登录域。

## 4. 登录（有界面，仅人工）

Linux 上先确认 X server 可用。本机 `DISPLAY=:6` 可用；如果不可用：

```bash
Xvfb :6 -screen 0 1440x900x24 &
export DISPLAY=:6
```

打开指定平台的持久化 profile 登录页：

```bash
python -m crawler login taobao          # 位置参数写法（也支持 --platform taobao）
# 或 python -m crawler login --platform taobao --display :6 --timeout 600
```

程序会打开浏览器并等待人工登录。它不会输入账号、不会自动破解验证码/滑块；检测到验证码只会提示人工完成。
登录过程中只打开这一个登录页，之后不会重新导航或重新请求；程序在同一个页面上
被动观察 URL 是否离开登录域名、或同一浏览器 context 是否出现登录 Cookie
（京东为 `pt_key` / `thor`）。最长等待 10 分钟。Cookie 只留在 profile 目录内，
并写一个不含 Cookie 的 `login.json` 标记。

**京东默认 CDP 模式**（Windows profile 三平台都是 CDP）：`crawler login jd`
会启动系统真实 Chrome（有界面；Linux 使用 `DISPLAY=:6`，Windows/macOS 使用原生窗口），
参数只有：

```bash
google-chrome --user-data-dir=profiles/jd-chrome --remote-debugging-port=9223
```

不加 `--headless` / `--enable-automation` 等自动化参数；然后通过 CDP 打开
`https://passport.jd.com/new/login.aspx` **一次**，之后只被动检测 Cookie/URL。
退出时只断开 CDP、不删除 profile，Chrome 窗口保留（下次直接复用已登录会话）。
Playwright 模式的 profile 仍在 `profiles/{platform}/`；CDP 模式在
`profiles/{platform}-chrome/`，目录权限均为 0700。

检查登录态：

```bash
python -m crawler check-login          # 默认检查所有启用平台
python -m crawler check-login taobao
python -m crawler check-login all
# 兼容：--platform taobao / --platform all
```

退出码：0 正常，1 失效/风控，2 配置或浏览器错误。

## 5. 采集

搜索每页自动携带平台销量排序参数（淘宝 `sort=sale-desc`、京东 `psort=3`、
1688 `sortType=booked`），并尝试选择页面原生“销量”控件。
已选中的控件不重复点击，避免反转排序；只有同时观察到控件选中和列表更新才记录确认。
无法确认、控件缺失、点击或等待失败时继续按原有流程采集，并在运行 warning 和
商品 flags 中记录 `销量排序未确认`，不会因此停止采集。
确认列表更新前保留已有商品响应；点击无刷新或仅收到无关接口响应时，仍使用原数据继续采集。
排序切换后的响应按请求发起时间隔离；解析保持页面商品顺序，不再按解析出的模糊销量重排。
登录失效、验证码和访问受限仍沿用原有停止规则。

```bash
# 用 config/rules.yaml 的 IP 关键词矩阵，三平台串行
python -m crawler run all --date 2026-11-05
# 兼容：--platform all

# 单平台；显式关键词；逐页到末页
python -m crawler run --platform taobao --keyword 葫芦兄弟

# 不下载主图（调试）
python -m crawler run --platform jd --no-images
```

页数、商品数、关键词数、分段、每日数量和详情/店名补采数量均不截断。
旧 `--max-pages`、`--limit-keywords`、`--batch` 仍可解析，但不再限制本次采集。
仅明确末页或明确无结果完成关键词；重复列表、未知空页及分页异常保留未完成检查点。
未设置 `run.ends_at` 时没有整批默认时限；明确设置后按该时间停止新增搜索，
在途处理和补采继续收尾。请求间隔、单次请求异常处理、登录与风控检查仍保留。

CDP 模式下 `run` / `check-login` 走同一套连接逻辑：先探测
`http://127.0.0.1:<port>/json/version`，已有 Chrome 就复用；未运行则用与登录完全
相同的参数自动启动。端口 = `browser.port`（默认 9222）+ 平台序号
（taobao=9222, jd=9223, 1688=9224），也可用 `browser.ports` 固定单平台端口。

输出：

```
out/{date}/{platform}.jsonl       # 商品行，UTF-8，一行一个 JSON
out/{date}/{platform}.run.json    # 本次运行记录
out/{date}/run_logs.jsonl         # 运行记录追加流（供后端 run_logs）
out/{date}/images/{platform}/     # 主图（开启时）
logs/{date}_{platform}.log        # 每个请求的 URL/耗时/状态/条数
```

详情页补抓（由判定模块按规则触发，也可手工指定 ID）：

```bash
python -m crawler detail --platform taobao --ids-file ids.txt --date 2026-11-05
# 淘宝 ID 前加 tmall: 可指定天猫详情页，例如 tmall:123456789
# 输出 out/{date}/{platform}_detail.jsonl
```

## 6. 字段与口径

每行字段（顺序固定）：

`platform, product_id, url, title, shop_id, shop_name, shop_label_raw, shop_type_raw_hint,
price, price_raw, sales_raw, sales, comments_raw, comments, main_image_url, image_path,
category, ip_code, search_keyword, page, rank, crawled_at, listed, flags[]`

- 缺失一律 `null`，**不填 0**；`price` 是 JSON number，`crawled_at` 是带时区 ISO 8601。
- `product_id` 解析失败 → 丢弃并计入 `drop_reasons` / `errors`。
- `shop_id` 缺失不再标 `店铺ID缺失`；聚合以 `shop_name` 为准。店名为空才标 `店铺名缺失`。
- `sales` / `comments` 是原文的下限解析值，原文保留在 `*_raw`；模糊值加 `按下限估算`。
- 列表页缺销量（京东常见）→ `sales=null` + `销量缺失`。
- `shop_type_raw_hint` 只是平台原始线索（天猫/京东自营/1688实力商家等）；
  `企业/个体户/个人/未知` 的归一由判定模块按 `shop_type_mapping` 做，采集层不越权。
- 同一输出文件内按 `(platform, product_id)` 去重；重复跑同一关键词不会产生重复行。

常见 flags：`销量缺失`、`评论缺失`、`店铺名缺失`、`价格缺失`、`主图缺失`、
`主图下载失败`、`按下限估算`、`商品下架`。

## 7. 配置说明（config/rules.yaml）

路径类相对路径按**运行目录**解析，也可用环境变量覆盖：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `crawl.max_pages` | 0 | 旧键兼容；逐页到末页，不截断 |
| `crawl.daily_max_pages` | 0 | 旧键兼容；搜索和详情入口均关闭每日数量截断 |
| `crawl.max_items_per_keyword` | 0 | 旧键兼容；不截断商品数 |
| `crawl.taobao.max_keywords_per_run` / `max_pages` | 0 / 0 | 旧分段键兼容；完整关键词范围逐页采集 |
| `crawl.taobao.detail.max_per_run` | 0 | 旧键兼容；处理全部符合补采规则的候选 |
| `crawl.ali1688.shop_name_backfill.max_per_run` | 0 | 旧键兼容；处理全部缺店名候选 |
| `crawl.rate_limit.requests_per_minute` | 20 | 每分钟请求上限 |
| `crawl.rate_limit.page_interval_seconds` | [4,9] | 翻页间隔随机区间 |
| `crawl.rate_limit.keyword_interval_seconds` | [20,40] | 关键词间隔 |
| `crawl.rate_limit.detail_interval_seconds` | [6,12] | 详情页间隔 |
| `crawl.retry.attempts` | 3 | 网络/5xx/429 最多尝试次数 |
| `crawl.retry.backoff_seconds` | [5,15,45] | 指数退避 |
| `crawl.timeout_ms` / `page_wait_ms` | 30000 / 1500 | 页面超时 / 加载后等待 |
| `crawl.keywords` | [] | 显式关键词；为空时用 `ips` 的名称/别名/变体 |
| `crawl.parallel_platforms` | false | 多平台并行（每平台独立浏览器） |
| `crawl.image.enabled` | true | 是否下载主图 |
| `browser.mode` | `playwright` | 全局浏览器模式：`playwright` / `cdp`；Windows 用 `config/windows.yaml` 三平台默认 `cdp` |
| `browser.platform_modes` | `{jd: cdp}` | 按平台覆盖；京东默认 cdp，写 `jd: playwright` 可退回 |
| `browser.port` | 9222 | CDP 基础端口；实际端口 = port + 平台序号 |
| `browser.ports` | `{}` | 按平台指定固定 CDP 端口（优先级最高） |
| `browser.display` | `:6` | Linux 启动真实 Chrome 的 X11 DISPLAY；Windows/macOS 忽略 |
| `browser.chrome_executable` | null | Chrome 路径；null 时按平台候选查找（Windows 见上，最后 Edge 兜底） |
| `browser.startup_timeout_seconds` | 20 | 等待 CDP 端口就绪的秒数 |
| `browser.poll_interval_seconds` | 0.2 | 启动期间端口轮询间隔 |
| `security.profile_dir` | `./profiles/{platform}` | 每平台持久化 profile；必须含 `{platform}` |
| `security.profile_key_env` | `CLUE_PROFILE_KEY` | 静态加密密钥环境变量（预留） |

环境变量：`CRAWLER_PROFILES_DIR`、`CRAWLER_OUTPUT_DIR`、`CRAWLER_LOG_DIR`、
`CRAWLER_STATE_DIR`、`CRAWLER_CONFIG`；`DISPLAY` 只在 Linux 使用。

注意：`/workspace/requirements/rules.example.yaml` 的 profile 路径是
`/data/profiles/{platform}`；如果本机没有 `/data`，请用本项目的
`config/rules.yaml`，或设置 `CRAWLER_PROFILES_DIR` 指向可写目录。

风控/登录失效时，`{platform}.run.json` 会记录：

```json
{"login_ok": false, "risk_blocked": true, "screenshot_path": "logs/screenshots/...png",
 "status": "risk_blocked", "errors": ["taobao 触发风控: ..."]}
```

截图只保存在本地，供人工复盘；程序不会尝试过验证码，也不会当天自动重试该平台。

## 8. 测试

测试使用 `crawler/tests/fixtures/` 的 HTML/JSON，不访问真实站点。
排序浏览器用例通过本地 Chrome 回放页面及接口，未安装 Chrome 时自动跳过：

```bash
cd /workspace/ip-monitor/crawler
.venv/bin/python -m pytest -q
# pytest.ini 已默认带 --cov=crawler --cov-report=term-missing，会打印覆盖率表
.venv/bin/python -m pytest --cov=crawler --cov-report=html:htmlcov
```

早期交付记录：Linux **180 passed**；Windows 10 **179 passed + 1 skipped**（仅 X11 的
`test_display_validation` 在 Windows/macOS 合理跳过）；源码覆盖率 **85%**
（`.coveragerc` 排除测试文件）。
覆盖：模糊值（`1万+`→10000、`2.5万+`→25000 等）、价格区间、GBK、三平台 JSON/DOM、
详情页、去重、配置、JSONL、节流、每日上限、重试、风控/登录停平台、图片、CLI；
CDP 部分用假 Chrome 进程 / 假 `RemoteSession` 覆盖端口探测、精确启动参数、
京东默认 cdp、登录只导航一次、被动 Cookie/URL 检测和失败清理，不访问真实网站。

2026-11-11 不限量修改的分组回归、浏览器回放及真实验证边界见
[不限量采集实施计划](../doc/不限量采集实施计划_20261111.md)；上述早期数字不是最新验收结果。

离线生成样例：

```bash
.venv/bin/python tools/make_samples.py
# samples/2026-11-05/{taobao,jd,1688}.jsonl 各 20 行
```

## 9. 已知限制（需要实测确认）

1. 三平台真实 DOM/接口字段随改版变化。选择器集中在 `platforms/selectors.py`，
   JSON 键名集中在各平台 `_from_json`。京东/1688 已加 DOM、内嵌 JSON、XHR/JSONP
   多策略兜底；解析 0 条时自动落 `logs/debug/<platform>_<时间>.html/.png`，
   首次实跑仍建议按该快照校准。
2. 淘宝搜索数据可能来自需要签名的 mtop 接口；本实现监听浏览器自身发出的 JSON 响应，
   不自行伪造签名。若接口结构/签名策略变化，需实测调整。
3. 京东列表价格依赖滚动 + `p.3.cn` 价格接口；真实页面的滚动次数、懒加载和价格接口
   需要实跑验证。`page` 参数按京东惯例使用 `1,3,5,...`。2026 版卡片以
   `[data-sku]` 为锚点，已兼容无 `<a href>` 的 hash class 结构。
4. 1688 中文关键词按 GBK 百分号编码（单测覆盖）；已兼容
   `a.search-offer-item`、`#sm-offer-list > div` / `mojar-*`；真实页面如再改版，
   以 debug 快照为准调整。
5. 主图下载使用平台 Referer；防盗链/图片域名变化需实测。
6. profile 目录当前只做 0700 权限，未实现静态加密；`CLUE_PROFILE_KEY` 仅预留。
7. 登录必须人工完成，测试无法覆盖真实登录；风控截图在 headless 下的效果需实跑确认。
   CDP 模式已用真实 Chrome 做过轻量冒烟（仅打开京东登录页，`title=京东-欢迎登录`，
   DOM 正常加载），未做登录后的真实采集。
8. CDP 模式依赖本机已安装 Chrome（Windows 可用 Edge 兜底）；Linux 还需要可用
   X server，Windows/macOS 不需要。程序退出只断开 CDP，不主动关闭 Chrome
   （避免影响用户/1688 等其他窗口）。需要释放端口时手动关闭对应窗口。
9. `shop_type` 归一、金额、聚合、详情触发策略属于判定模块；本模块只提供
   `shop_type_raw_hint` 和 `fetch_detail()` 接口。
10. SMTP 报警、数据库写入不在本模块；本模块只产出运行记录供后端消费。
11. 拼多多未启用（`pdd.enabled=false`）。

更完整的交付说明、覆盖率表、借鉴项目和验收对照见 `DELIVERY.md`。
