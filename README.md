# 破线监控

监控同花顺自选股的 5 日线 / 10 日线破线情况：每个交易日 10:30、14:30 自动统计破线数量与比例，判断 70% / 80% 阈值是否触发，并生成可视化监控表。

## 功能

- 实时行情 + 前复权日K（主：腾讯行情；备用：东方财富）
- 破线判定：现价 < MA5 / 现价 < MA10（交易时段内均线并入实时价，与行情软件盘中口径一致）
- 多分组独立监控：自选股按同花顺客户端分组（如"景气""收息"）分开统计与展示
- 汇总统计：各分组 + 总体的破 5 日线数量/比例、破 10 日线数量/比例
- 总览视图：4 项破线比例 100% 条（景气/收息 × 破5/破10），含 70%/80% 阈值刻度
- 趋势识别：各分组破线比例随时间变化的走势对比图
- 触发天数：各分组 4 项阈值分别展示"连续触发交易日数"与"累计触发交易日数"（仅统计交易日）
- 阈值预警：破 5 日线 ≥70%、≥80%；破 10 日线 ≥70%、≥80%（触发时发送 macOS 系统通知）
- 手机推送：阈值触发时推送到微信 / 飞书群（@所有人）/ iOS / 邮箱（Server酱 / 飞书机器人 / Bark / SMTP）
- 监控表：`output/dashboard.html`（自刷新），数据快照 `output/latest.json`，历史 `output/history.jsonl`
- 手机网页访问：`python3 serve.py` 启动网页服务，手机/其他设备随时查看
- 交易日自动跳过节假日（以上证指数当日是否有K线判断）

## 快速开始

### 1. 填写自选股（二选一）

**方式 A：手动填写**

编辑 `config.json` 的 `watchlist` 数组，填入 6 位股票代码：

```json
"watchlist": ["600519", "000001", "300750"]
```

**方式 B：从同花顺账号同步（推荐）**

方式 B-1：账号密码自动登录（最简单）

```bash
python3 ths_sync.py --username 你的同花顺账号 --password 你的密码
```

登录成功后会自动保存会话 Cookie，并拉取自选股列表写入 `config.json`。
后续可直接 `python3 ths_sync.py` 复用会话（Cookie 有效期内无需再次输入密码）。

方式 B-2：浏览器 Cookie

1. 浏览器打开并登录 <https://t.10jqka.com.cn/>
2. 按 `F12` → 打开 Network（网络）→ 刷新页面
3. 任选一个 `t.10jqka.com.cn` 请求 → Headers（标头）→ 复制 Request Headers 里的 `Cookie` 整串
4. 同步到本地配置：

```bash
python3 ths_sync.py --cookie "粘贴你的Cookie整串"
```

> 注意：Cookie 属于登录凭证，包含敏感信息，请勿外传；如需更换，重新执行即可覆盖。
> 若 Cookie 提示"未登录"，通常是因为复制时漏掉了 HttpOnly 的会话 Cookie（userid/sessionid）。
> 建议改用方式 B-1 账号密码登录，或在 Network 面板右键请求 → Copy as cURL 后把整段命令发给我提取完整 Cookie。

### 1.5 同步同花顺客户端分组（推荐）

Mac 同花顺客户端的自选股分组保存在云端，可通过客户端同账号接口同步：

```bash
python3 ths_client_sync.py                # 默认同步"景气""收息"两个分组
python3 ths_client_sync.py --groups 景气,收息,科技   # 自定义分组
```

同步结果写入 `config.json` 的 `groups` 字段；在客户端修改分组后重新运行即可。

### 2. 手动执行一次监控

```bash
python3 monitor.py
```

完成后打开 `output/dashboard.html` 查看监控表；终端会输出汇总统计。

### 3. 工作日 10:30 / 14:30 自动监控（二选一）

**方式 A：常驻调度器（推荐，简单）**

保持终端窗口运行：

```bash
python3 scheduler.py
```

**方式 B：macOS 开机自启服务（无需保持终端）**

```bash
bash install_launchd.sh     # 安装并启动
bash uninstall_launchd.sh   # 卸载
```

服务日志：`output/scheduler.log`、`output/launchd.log`。

## 手机查看监控结果（二选一）

### 方式 1：手机推送（推荐，最简单）

在 `config.json` 的 `push` 字段填入任一渠道密钥：

```json
"push": {
  "serverchan_key": "SCTxxxxxxxxxxxxxxxx",   // 微信推送：sct.ftqq.com 扫码登录获取
  "serverchan_channel": "飞书群",            // 可选：sct.ftqq.com/forward 添加的通道名（转发到飞书群等）
  "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",  // 可选：飞书群机器人 Webhook，自动@所有人
  "feishu_secret": "机器人开启签名校验时填 Sign Secret",  // 可选：未开启签名校验可留空
  "dashboard_url": "https://你的用户名.github.io/poxian-monitor/",  // 可选：飞书消息里附带的监控大屏链接
  "bark_key": "xxxxxxxxxxxxxxxx",            // iOS 推送：App Store 安装 Bark 后获取
  "email": {
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "username": "你的邮箱@qq.com",
    "password": "SMTP授权码",
    "to": ["接收邮箱@qq.com"]
  }
}
```

测试推送：

```bash
python3 notify.py
```

配置后，交易日 10:30 / 14:30（北京时间）定时推送监控汇总到**飞书（@所有人）+ 微信（Server酱）**；
盘中任意时刻触发 70%/80% 阈值（总体或任一分组）也会即时推送预警。

**飞书群 @所有人 两种方式：**

- 方式 A（Server酱转发）：在 <https://sct.ftqq.com/forward> 添加"飞书群机器人"通道，
  把通道名填入 `serverchan_channel`，推送经 Server酱转发到飞书群；
- 方式 B（飞书机器人直推）：在飞书群里添加自定义机器人，把 Webhook 地址填入 `feishu_webhook`，
  推送直接发到飞书群并自动 @所有人。如果创建机器人时勾选了"签名校验"，需要把对应的
  **Sign Secret** 填入 `feishu_secret`，否则飞书会返回 `code 19021 sign match fail`。
  飞书消息以交互卡片展示：触发项与总体数字为普通字体，"景气/收息"等分组段落自动加粗，
  底部有"📊 查看监控大屏"按钮（指向 `dashboard_url`，默认你的 GitHub Pages 地址），
  点击即可在手机打开实时监控表。
  两个方式可同时配置，也可以只用其中一个。

### 方式 2：手机网页实时监控

Mac 上启动网页服务（保持终端运行）：

```bash
python3 serve.py
```

手机连同一 WiFi，浏览器打开终端显示的地址，例如：

```
http://192.168.1.72:8800/dashboard.html
```

页面每 60 秒自动刷新，交易时段内监控数据同步实时更新（可加 `--interval 30` 加快）。
如需在任何网络下访问，可用内网穿透（如 Tailscale、ngrok、cloudflared）把 8800 端口映射成公网网址；
或把本项目部署到云服务器（自选股已固化在 config.json，云服务器可直接运行 monitor/serve，无需同花顺登录）。

### 方式 3：公网网址（GitHub Pages，无需 Mac 开机、无需云服务器）

利用 GitHub 免费提供的 Actions + Pages：工作日 10:30/14:30 由 GitHub 云端自动运行监控，
生成监控表发布到公网网址，任何设备浏览器直接访问，Mac 关机也不影响。

部署步骤：

1. 注册 GitHub 账号（免费）：<https://github.com>
2. 新建仓库：点右上角 **+ → New repository**，仓库名随意（如 `poxian-monitor`），
   **Visibility 选 Public**（免费版 Pages 仅支持公开仓库），不要勾选初始化 README
3. 在 Mac 终端执行（把用户名和仓库名换成你自己的）：

```bash
cd "/Users/liangyonglong/Desktop/破线监控"
git init -b main
git add .
git commit -m "破线监控系统"
git remote add origin https://github.com/你的用户名/poxian-monitor.git
git push -u origin main
```

> 含同花顺 Cookie 的 `config.json` 已被 `.gitignore` 排除，不会上传到 GitHub。
> 云端使用的是脱敏配置 `config.workflow.json`（仅有自选股和阈值，无登录信息）。

4. 开启 Pages：仓库页面 **Settings → Pages**，Source 选 **Deploy from a branch**，
   分支选 `gh-pages`、目录 `/ (root)`，点 Save
5. 等 1~2 分钟，访问（替换成你的用户名和仓库名）：

```
https://你的用户名.github.io/poxian-monitor/
```

6. 自动更新：工作日 10:30 / 14:30（北京时间）自动生成；也可随时进仓库 **Actions** 标签页
   点 **Run workflow** 手动刷新。页面上的 **立即刷新** 按钮会直接在浏览器里拉取实时行情并
   重算 MA5/MA10（交易时段内有效），页面打开后每 60 秒自动刷新一次，不依赖服务器重新运行。
7. 可选：云端自动跟随客户端分组。仓库 **Settings → Secrets and variables → Actions →
   ** New repository secret，Name 填 `THS_COOKIE`，Value 填你本地 `config.json` 里
   `ths_cookie` 的整串值。之后每次云端运行都会先同步你同花顺客户端的最新分组。
   **Cookie 自动刷新闭环**：交易日 10:00 / 14:00（北京时间）云端会自动检查 Cookie；
   失效时用账号密码重新登录并更新密钥，全程自检，失败会自动推送飞书/微信告警。
   如需启用自动刷新，再添加两个 Secret：
   - `THS_USERNAME`：同花顺登录账号（手机号或用户名）
   - `THS_PASSWORD`：同花顺登录密码
   - `GH_PAT`：GitHub 令牌（用于云端更新 `THS_COOKIE` Secret；已有仓库写权限的令牌即可）
8. 可选：云端推送（触发 70%/80% 阈值时）。仓库 **Settings → Secrets and variables → Actions →
   New repository secret**，可依次添加：
   - `SERVERCHAN_KEY`：Server酱 SendKey（微信推送，或配合通道转发飞书群）
   - `SERVERCHAN_CHANNEL`：sct.ftqq.com/forward 里配置的通道名（转发到飞书群等）
   - `FEISHU_WEBHOOK`：飞书群机器人 Webhook（直接推送并 @所有人）
   - `FEISHU_SECRET`：飞书机器人的 Sign Secret（创建机器人开启"签名校验"时必填）
   - `BARK_KEY`：Bark 的 Key（iOS 推送）
  之后每次触发阈值，云端会自动推送，与 Mac 无关。交易日 10:30/14:30 定时推送汇总
  到飞书 + 微信（`push_on_every_run: true`），不用再单独配置。
   推送触发范围 = 总体 + 两个分组（景气/收息），任一组合的 70%/80% 破线达标即推送，
   消息会标注触发来源（如“破10日线 ≥ 80%（收息）”）。
   GitHub 定时调度偶尔会延迟/漏跑，已配置 10:45/14:45 兜底任务，重复触发自动去重不会重复推送。

> 隐私提醒：公开仓库意味着自选股与监控数据对公网可见。若需私有 + 公网访问，
> 需 GitHub Pro（约 $4/月）或改用云服务器方案。

## 指标口径

| 指标 | 说明 |
| --- | --- |
| MA5 | 最近 5 个交易日收盘价（前复权）均值，交易时段内含实时价 |
| MA10 | 最近 10 个交易日收盘价（前复权）均值，交易时段内含实时价 |
| 破线 | 现价低于对应均线 |
| 比例 | 破线只数 ÷ 有效样本只数（上市不足 5/10 个交易日的不计入分母） |
| 分组 | 按同花顺客户端"景气""收息"等自选股分组独立统计 |

## 配置项

| 字段 | 说明 |
| --- | --- |
| `watchlist` | 自选股 6 位代码数组 |
| `groups` | 分组监控配置（分组名 -> 代码数组），由 ths_client_sync.py 同步 |
| `ths_cookie` | 同花顺登录 Cookie（用于自动同步自选股） |
| `ma_periods` | 均线周期（默认 5、10） |
| `thresholds` | 预警阈值百分比（默认 70、80） |
| `check_times` | 每日监控时间（默认 10:30、14:30） |
| `notify_on_hit` | 阈值触发时是否发送系统通知 |
| `timezone` | 时区（默认 Asia/Shanghai） |

## 目录结构

```
破线监控/
├── config.json         # 配置（自选股、Cookie、时间）
├── stock_data.py       # 行情数据获取（腾讯/东方财富）
├── monitor.py          # 破线监控主程序
├── scheduler.py        # 工作日定时调度器
├── serve.py            # 手机网页服务 + 盘中实时刷新
├── notify.py           # 手机推送（Server酱/Bark/邮件）
├── ths_client_sync.py  # 同花顺客户端分组同步（景气/收息）
├── ths_sync.py         # 同花顺自选股同步
├── install_launchd.sh  # macOS 开机自启安装
├── uninstall_launchd.sh
├── output/             # 运行时生成：dashboard.html / latest.json / history.jsonl
└── README.md
```

> 免责声明：本工具数据来自公开行情接口，仅供研究参考，不构成投资建议。请自行核实行情准确性。
