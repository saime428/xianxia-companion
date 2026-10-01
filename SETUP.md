# 从零部署公开版（一步一步照做 + 排错）

这份文档写给「从 GitHub 拉下公开版、在自己机器上跑」的人。每一步都写了**怎么做**和**怎么确认做对了**，照着顺序来。

## 先说结论：为什么拉下来很多功能不正常

公开版的代码和作者线上跑的是同一份，功能本身没坏。缺的是**不在 git 里的东西**：数据库、登录态、几项手动设置。新机器上这些全是空的，所以会出现：

| 你看到的 | 真正的原因 | 在哪一步解决 |
|---|---|---|
| 天机阁那一栏没有地方贴 Cookie，按钮是灰的 | `.env` 没填 `AUTHORIZED_USER_ID`，程序里没有「管理员」 | 第 2、5 步 |
| 导航里没有「灵溪垂钓」，点进去被跳回角色页 | 没连天机阁，程序拿不到人物卡，不知道你背包里有钓竿 | 第 6 步 |
| 钓鱼、寻宝、噬金虫、闯塔、股市、天机试炼、历练、星宫一起报「洞府公共入口未找到」 | 没设「洞府入口群」。这一项作者是在网页上设的，存在数据库里 | 第 7 步 |
| 终端一直不提示输入手机号 | 旧 README 写错了，登录在网页 `/login` 里完成 | 第 5 步 |
| 自动任务一个都没跑 | 每个自动功能的开关都存在本机数据库里，新装全是关的 | 第 8、9 步 |
| 宗门、天星、问天这类「每天几点」的时间对不上 | 进程时区不是北京时间 | 第 3 步 |
| 能发指令但收不到 bot 回复 / 验证码一直发不出去 | 国内没配 SOCKS5 代理 | 第 2 步 |

---

## 0. 开始之前（很重要）

- **同一批 Telegram 号，同一时间只能在一台机器上跑自动化。** 两台机器各有一套数据库，会把同一条指令发两遍、互相抢冷却。换机器时先把旧机器停掉，或者在旧机器的「角色真身」页点「暂停全部自动化」。
- **不要从别的机器拷 `data/` 下的 `.session`、`.db`、或者 `.venv` 过来。** 新机器在网页上重新登录，生成自己的 session。
- 准备好：
  - Python 3.10 或更高。
  - Telegram 的 API ID / API Hash（在 https://my.telegram.org 申请）。
  - 每个要用的 Telegram 号都设好 **@用户名**：天机阁是按用户名找你的角色的。
  - 在国内：一个本地 **SOCKS5** 代理端口（比如 v2rayN 的 10808）。
  - 要用世界 BOSS / 落云灵树的话：装好 Google Chrome。

## 1. 克隆、建虚拟环境

```bash
git clone <本仓库地址>      # 就是你正在看这份文档的 GitHub 仓库：页面上绿色 Code 按钮里的 https 地址
cd xianxia-companion
```

Windows：

```powershell
python tools\setup_environment.py --install
```

macOS / Linux：

```bash
python3 tools/setup_environment.py --install
```

它会建 `data/`，把 `.env.example` 复制成 `.env`，建 `.venv` 并装依赖。

- **确认**：目录里出现 `.venv` 和 `.env`，最后打印 `Next commands`。
- 可选自检：`PYTHONPATH=app/src .venv/bin/python tools/test_dwelling_fishing_miniapp.py` 输出 `ok (31 tests)`（Windows 先 `$env:PYTHONPATH="app\src"` 再用 `.venv\Scripts\python.exe`）。
- 仓库里的文本文件是 CRLF 换行，`.py` 不受影响。macOS/Linux 上要用 `tools/*.sh`、`*.service` 的话，先 `dos2unix` 一遍。

## 2. 填 `.env`

用记事本 / nano 打开 `.env`。**所有 ID 只写纯数字**，填了 `@群名` 之类会启动就崩。

| 键 | 必填 | 怎么拿 |
|---|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | 必填 | https://my.telegram.org → API development tools |
| `AUTHORIZED_USER_ID` | **必填** | 你自己（管理员号）的 TG 数字 ID。私聊 @userinfobot 就能看到；第 5 步登录后 `/login` 页也会显示「TG 用户 ID」。**不填就没有管理员，天机阁、钓鱼等一大片功能都用不了** |
| `TG_GAME_BOUND_CHAT_ID` | 必填 | 你平时发游戏指令的群。最通用的办法：浏览器打开 https://web.telegram.org/k/ ，进这个群，地址栏 `#` 后面的 `-100…` 就是。私有群也可以右键任意一条消息 →「复制消息链接」，得到 `t.me/c/1234567890/…`，群 ID 就是 `-1001234567890`（前面加 `-100`）。公开群的链接是 `t.me/群名/…`，看不出数字，用前一个办法 |
| `TG_GAME_BOUND_THREAD_ID` | 群开了「话题」才填 | 私有群话题里的消息链接形如 `t.me/c/1234567890/<话题ID>/<消息ID>`，取中间那个数 |
| `TG_GAME_BOUND_BOT_ID` | 必填 | 群里游戏 bot 的数字 ID。不知道就先空着，第 5 步登录后在「角色真身」页点「同步群 Bot」拿到，再补上并重启 |
| `TELEGRAM_PROXY` | **国内必填** | `socks5://127.0.0.1:<端口>`。Telegram 直连不会走系统代理；HTTP 代理往往只能发、收不稳，一定要 SOCKS5 |
| `TG_GAME_PORT` | 保持 | `8787`（不能留空） |
| `TG_GAME_HOST` | 保持 | `127.0.0.1`。`TG_GAME_DOMAIN` 留空，理由见第 10 步 |
| `TG_GAME_ESTATE_MINIAPP_FALLBACK_URL` | 建议 | 洞府小程序的备用入口，见第 7 步 |

填完检查一遍（只看必填项有没有值，填了假值也能过，所以它过了不代表配对了）：

```bash
.venv/bin/python tools/setup_environment.py --check --strict
```

> 以后每次改 `.env` 都要**重启**才生效，程序只在启动时读一次。

## 3. 让进程用北京时间

宗门、天星宗、问天、慕兰、斗法这些「每天几点」的逻辑按进程所在时区算。

- **Windows**：系统设置里把时区设成 (UTC+08:00) 北京。不要设 `TZ` 环境变量。
- **macOS / Linux**：启动时带上 `TZ=Asia/Shanghai`（systemd 里写 `Environment=TZ=Asia/Shanghai`）。只写进 `.env` 没用。
- **确认**：Windows 执行 `.venv\Scripts\python.exe -c "import time;print(time.strftime('%z'))"`，macOS/Linux 执行 `TZ=Asia/Shanghai .venv/bin/python -c "import time;print(time.strftime('%z'))"`（要和启动时一样带上 TZ），都应输出 `+0800`。

## 4. 启动

Windows：

```powershell
.venv\Scripts\python.exe run_services.py all
```

macOS / Linux：

```bash
TZ=Asia/Shanghai .venv/bin/python run_services.py all
```

**一定要带 `all`**：不带只起网页，钓鱼等所有自动调度都在另一个 telegram 进程里。

- **确认**：终端出现 `Started web`、`Started telegram`；浏览器打开 http://127.0.0.1:8787/health 返回 `status=ok`；`data/tg_game.db` 生成了。

## 5. 在网页里登录 Telegram（不是终端）

1. 打开 http://127.0.0.1:8787 ，会跳到 `/login`。
2. 「第一步 登录本地 Telegram 身份」：填手机号（`+86…`）→ 发送验证码 → 填验证码；开了二步验证再填密码。**先登录管理员号**（就是 `AUTHORIZED_USER_ID` 那个）。
3. 还有别的号：点「添加另一个 Telegram 账号」，逐个走一遍。

- **确认**：`/login` 显示 TG 账号和 TG 用户 ID；终端 5 秒内出现 `Started Telegram worker for profile=…` 和 `Telegram worker connected`。
- 如果 `AUTHORIZED_USER_ID` 之前空着：现在把页面上显示的「TG 用户 ID」填进 `.env`，Ctrl+C 停掉，再按第 4 步启动。
- **确认管理员生效**：`/login` 第二步出现「session Cookie」输入框；「角色真身」页有「同步群 Bot」按钮；导航里有「诸元神巡令」。
- 然后在「角色真身」页点一次「同步群 Bot」，把群里当前的游戏 bot 认全。`.env` 的 `TG_GAME_BOUND_BOT_ID` 还空着的话，从结果里挑主 bot 填上并重启。
- 核对「角色真身」页「当前锁定秘境」里的 Chat ID / Thread 和 `.env` 一致。

## 6. 连上天机阁（人物卡；钓鱼等功能都靠它）

1. 用普通浏览器打开 https://asc.aiopenai.app/login ，点「以天机灵纹通行」，用 Telegram 授权登录。
2. 按 F12 → Application（应用）→ Cookies → `https://asc.aiopenai.app`，复制名为 `session` 的值。
3. 回到本地 `/login`，确认当前是**管理员号**，在「第二步 登录天机阁」粘贴 `session=<刚才的值>`（直接把整串 Cookie 贴进去也行，程序会自己截出 session 段），点「验证并登录天机阁」。
4. 其它号：切过去，点「使用管理员 Cookie 同步人物信息」。不点也行，最多 10 分钟后台会自动补上。

- **确认**：提示「天机阁登录成功，已同步人物卡并恢复自动调度」；「天机阁状态」显示已登录、最近错误是 `-`；刷新后导航按你的角色出现「宗门大殿」「灵溪垂钓」「小世界」等。（状态卡里的「角色数量」一直是 0，不用管它。）
- 复制完 Cookie 之后，**不要在天机阁网页上点「退出」**，否则这个 session 可能作废。
- 以后任何页面被踢回 `/login`，并提示「天机阁会话已失效」，就回到这一步给管理员号重贴一次 Cookie，**然后在「角色真身」页点「探寻全部元神」**（或者逐个切到小号点「使用管理员 Cookie 同步人物信息」）。过期的小号不会自己恢复。
- 报「当前 Telegram 账号未绑定用户名」或找不到角色：给这个 TG 号设 @用户名，再点「重新验证天机阁会话」。

## 7. 设置洞府入口（所有小程序的前提）

钓鱼、洞府寻宝、噬金虫、落云灵树、闯塔、股市、天机试炼、野外历练、星宫、天机命脉都要先拿到「洞府公共入口」。它是一条带「进入洞府」按钮的消息，按钮链接形如 `https://t.me/<bot>?startapp=df_…`。程序去一个群里读这条消息，**只读，不会往那个群发任何东西**。

入口消息在游戏的老公共群里（代码常量 `-1002083016447`，管理员置顶），不在现在发指令的群里。所以：

- **A. 每个要跑小程序的号都加入老公共群**（找群友要邀请），然后在「角色真身」页最上面「洞府入口群 ID」填 `-1002083016447`，点保存。这个设置所有号共用；只要有一个号不在群里，这个号的钓鱼、寻宝等就会报 `Could not find the input entity…`，而且**不会**去用 B 的备用入口。
- **B. 建议同时配备用入口**：在 Telegram 桌面版里，鼠标停在那条消息的「进入洞府」按钮上，或者右键复制链接，得到 `https://t.me/<bot>?startapp=df_…`。写进 `.env` 的 `TG_GAME_ESTATE_MINIAPP_FALLBACK_URL=`，然后重启。**这条 `df_` 链接别贴到公开的地方。** 备用入口只在「群读到了，但没找到入口消息」时才用得上。
- **C. 进不了老群**：把「洞府入口群 ID」留空保存，只靠 B。入口群填了一个进不去的群时会直接报错，不会去用备用入口。

新版程序会自动做两件事：新登录的号还不认识这个群时，先刷新一遍会话列表；入口消息太老、扫不到时，去翻置顶消息。一般做完 A 就够了，B 是保险。

- **确认**：随便触发一个小程序（比如第 8 步的试钓）之后，在项目目录执行：

  ```bash
  .venv/bin/python -c "import json,sqlite3;d=json.loads(sqlite3.connect('data/tg_game.db').execute('select value from app_runtime_state where key=?',('estate_public_entry_discovery',)).fetchone()[0]);print(d['channel'],d['last_scan_status'],d['discovery_source'],d['last_error'])"
  ```

  应看到 `ok`，`last_error` 为空（Windows 用 `.venv\Scripts\python.exe`）。

## 8. 钓鱼（灵溪垂钓）

**游戏里先满足这些**（程序替你做不了）：

- 背包里有名字带「钓竿」的物品：青竹钓竿每天 5 竿，金竹钓竿每天 15 竿。买完在 `/login` 点「重新验证天机阁会话」，导航才会出现「灵溪垂钓」。
- 洞府灵脉至少 2 级（1 级会报 `fishing_site_unavailable`）。
- 至少有一位侍妾，而且两位不能同时在远航。只是一位在远航的话，程序会等她归航后再钓。
- 没有鱼材做饵的话，把页面上「缺饵时」改成「只买饵」（花灵石）。

**然后**：

1. 打开「灵溪垂钓」，选好钓点、鱼饵、缺饵时的做法，点「**MiniApp 试钓一竿**」。
2. 等它自己跑：状态依次是「等待 MiniApp 试钓」→ 试钓成功后直接变成「等待 MiniApp 钓满今日」→ 大约 2 分钟后「MiniApp 钓满今日中」→「今日竿数已满」。**这期间两个按钮都是灰的，正常，等着就行**，也别点「.鱼篓」（它的回包会覆盖掉试钓通过的标记）。
3. 到「今日竿数已满」、「真实 Canary」显示「已通过」之后，点「**开启每日自动钓鱼**」（默认每天 05:30）。

- **确认**：「每日自动」显示已开启，下次执行是明天 05:30；最近回包第一行形如 `MiniApp daily_limit｜5/5｜…`。
- 当天已经钓满了才装好：试钓会提示「今日竿数已满，未排队」，那就明天 05:30 之前再试钓一次。
- 钓鱼失败一次后当天会停下，原因写在钓鱼页的红字里，别只翻日志。修好之后再点一次试钓。

## 9. 其它自动功能：逐个打开

每个开关都存在本机数据库里，新装全是关的。**先做完第 6 步再开**：人物卡没同步时，冷却类任务会自己关掉（`最新 payload 缺少…冷却字段，已停止自动`）。

下面用的是左侧导航里的名字：

- **闭关洞府**：自动闭关 / 深度闭关、极阴、南陇、探寻裂缝、元婴。
- **宗门大殿**：宗门点卯；星宫；落云灵树；天星宗号还有天星配置（推命 / 改命）。
- **三界游历**：侍妾自动（远航、代卜、入梦、心劫、双修、剑阵、凝液……）、闯塔 / 天机试炼 / 噬金虫的每日开关、法宝、慕兰、世界 BOSS。**自动拼图**跟着「自动入梦」走，不用单独开：随行侍妾哪张残图四片凑齐了就自动 `.拼图`。
- **私人仙府**：洞府寻宝每日。
- **诸元神巡令**（只有管理员能进）：野外历练的定时和策略只能在这里设（群里的 `.野外历练` 已经失效）。寻宝、天机试炼、闯塔、噬金虫也可以在这里统一托管；托管之后，模块页上对应的每日开关会提示「已由诸元神巡令统一托管」，这是正常的。
- **落云灵树**：要先把「跃」和「飞」各跑一次试跑（Canary）才能开每日，而且需要下面的浏览器进程。

### 世界 BOSS / 落云灵树：另起一个浏览器验证进程

`run_services.py` 不会启动它，要另开一个终端常驻运行：

```powershell
# Windows
$env:PYTHONPATH="app\src"
.venv\Scripts\python.exe -B -m tg_game.features.world_boss.world_boss_browser --queue-dir data\world_boss\turnstile
```

```bash
# Linux（用普通用户跑；服务器没有桌面环境的话，先装 xvfb）
PYTHONPATH=app/src .venv/bin/python -B -m tg_game.features.world_boss.world_boss_browser --queue-dir data/world_boss/turnstile
```

- `--queue-dir` 不能省。
- 程序只在 PATH 里找 `google-chrome` / `chromium`；Windows 另外会找 `C:\Program Files\Google\Chrome\Application\chrome.exe`。装在别处的话，加 `--chrome <chrome 可执行文件路径>`。
- Linux 上用 root 跑，Chrome 会拒绝启动（`browser_launch_failed`）：换普通用户，或者加 `--no-sandbox`。
- macOS 目前不支持这一项：它要求 X 显示，Mac 默认没有。
- **确认**：先在命令后加 `--probe` 跑一次，看到 `token_generated`（只生成一次验证，不会真打）。正式运行后会出现 `data/world_boss/turnstile/browser-worker.log`。

### 没有网页开关的几项（可选）

用下面的命令写开关。`<id>` 是账号编号，第一个登录的号是 1，可以在「角色真身」页的多账号切换里看到。「角色真身」页的「停止当前元神调度」会把这里的天机命脉、股市定时快照、野外历练日报、自动抢红包一起关掉（历次结算记录留着），之后要用再跑一遍对应的命令。

- **天机命脉**（洞府外府的每日塔罗）：开启后每天 00:01 自动问一次。三个命择的奖励是固定的：逆势改命 4 天机残痕、顺势承命 2、藏锋避劫 1，启牌另给 1。下面这条命令用 `"choice": "defy"` 选逆势改命（不写 choice 就是藏锋避劫）。逆势改命要在选完之后再打一局噬金虫才算完成，所以这个号要在「三界游历」页开着**自己的**噬金虫每日开关（交给诸元神巡令托管的不算）；没开，或者当天已经打过，会自动退回藏锋避劫。

  ```bash
  .venv/bin/python -c "import sqlite3,time;c=sqlite3.connect('data/tg_game.db');c.execute('insert or replace into app_runtime_state(key,value,updated_at) values(?,?,?)',('fate_cards:<id>','{\"enabled\": true, \"choice\": \"defy\"}',time.time()));c.commit()"
  ```

- **股市定时快照、提醒、日报**（发到你 TG 的收藏夹），1800 表示每 30 分钟一次：

  ```bash
  .venv/bin/python -c "import sqlite3,time;c=sqlite3.connect('data/tg_game.db');c.execute('insert or replace into app_runtime_state(key,value,updated_at) values(?,?,?)',('stock_market_snapshot_every_seconds:<id>','1800',time.time()));c.commit()"
  ```

- **野外历练日报**（发到这个号自己 TG 的收藏夹，不进游戏群）：当天的野外历练打满后发一份，逐场列出胜负、修为和掉落；天星宗的号还会列出推命命中几次、改命挡下几场败局，方便算天机值的账。一天只发一次。野外历练本身要先在诸元神巡令里开着：

  ```bash
  .venv/bin/python -c "import sqlite3,time;c=sqlite3.connect('data/tg_game.db');c.execute('insert or replace into app_runtime_state(key,value,updated_at) values(?,?,?)',('wild_experience_report:<id>','{\"enabled\": true}',time.time()));c.commit()"
  ```

- **自动抢 LDC 红包**（群里有人 `.发红包` 后 bot 发的【LDC 红包】，抢到的 LDC 进这个号绑定的 linux.do 论坛账户，先私聊 bot 发 `.绑定论坛 论坛ID 论坛用户名` 绑好）：只抢总额大于 `min_total`、至少 2 份的包；不抢第一个，看到别人抢到了、还有剩余，才随机等 `delay` 秒点一次；没人抢的包不碰；讨红包的按钮一律不碰。每次点完把 bot 的回复发到这个号的收藏夹。bot 回复里出现「绑定」「天牢」「封禁」，或者天道封禁点了这个号，会自动关掉开关。日志前缀 `LDC抢红包`：

  ```bash
  .venv/bin/python -c "import sqlite3,time;c=sqlite3.connect('data/tg_game.db');c.execute('insert or replace into app_runtime_state(key,value,updated_at) values(?,?,?)',('ldc_red_packet:<id>','{\"enabled\": true, \"min_total\": 200, \"delay\": [1, 3]}',time.time()));c.commit()"
  ```

- **天星宗斗法**（`tools/tianxing_duel_daily.py`）：群和话题读 `.env`，照文件头的示例加到计划任务里。只有天星宗号用得上。

## 10. 安全：网页绝不能直接暴露到公网

`/login` 会把任何访问者直接当成已登录的账号，等于把你的 TG 号交出去。

- `TG_GAME_HOST` 保持 `127.0.0.1`，`TG_GAME_DOMAIN` 留空：填了域名，程序会改成监听 `0.0.0.0`。
- 确实要远程访问，就放在**带认证的反向代理**后面，比如 Caddy 加 `basic_auth`。web 只绑内网地址。
- **确认**：从另一台机器访问 `http://<你的IP>:8787`，应该连不上。

## 11. 以后更新

```bash
# 先停掉服务（Ctrl+C）
git stash            # 只有你改过仓库里的文件时才需要
git pull
python3 tools/setup_environment.py --install          # Windows 用 python
python3 tools/setup_environment.py --check --strict
# 按第 4 步重新启动；需要的话 git stash pop
```

数据库和 `.env` 不在 git 里，不会被覆盖。更新后打开 `/health`，`telegram_code_current=true` 说明 telegram 进程已经在跑新代码。

---

## 附：出问题时对照这张表

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `/login` 第二步只有灰按钮「使用管理员 Cookie 同步人物信息」，提示「管理员尚未配置可用 Cookie」 | 没有管理员 | 第 2、5 步：填 `AUTHORIZED_USER_ID`（等于登录号的 TG 数字 ID），重启 |
| 提交 Cookie 报「只识别 session=… 形式」 | 贴的不是 `session=` 那段 | 第 6 步，贴 `session=<值>` 或整串 Cookie |
| 首页或导航看不到钓鱼 / 点钓鱼跳回角色页 | 人物卡没同步，或背包没钓竿 | 第 6 步，再检查游戏里有没有钓竿 |
| 钓鱼红字 `MiniApp failed｜…｜洞府公共入口未找到` | 入口群没设或读不到入口 | 第 7 步 A + B |
| 报 `Could not find the input entity for PeerChannel(…)` | 报错的这个号不在入口群里（入口群设置所有号共用） | 第 7 步：每个跑小程序的号都加群，或者按 C 只用备用入口 |
| 报「配置的洞府 fallback URL 无效」 | 备用链接格式不对 | 必须是 `https://t.me/<bot>?startapp=df_…` 原样链接 |
| 「开启每日自动钓鱼」是灰的 / 返回 409「请先完成一次真实 MiniApp 试钓」 | 还在把当天的竿钓完（状态是「等待 / MiniApp 钓满今日中」），或者还没试钓成功，或者试钓后点了 `.鱼篓` | 前一种等它到「今日竿数已满」；后两种按第 8 步重新试钓 |
| 天机阁重贴 Cookie 之后，小号页面还是跳回 `/login` | 小号的状态已经标成过期，不会自己恢复 | 「角色真身」页点「探寻全部元神」 |
| 钓鱼报 `fishing_site_unavailable` / `companion_missing` | 灵脉 < 2 / 没有侍妾 | 游戏里补齐前置 |
| 「已制饵但仍无鱼饵」 | 没有鱼材 | 「缺饵时」改「只买饵」 |
| 点按钮返回 400「当前角色没有侍妾」「不能发送 X 专属命令」 | 人物卡没同步，程序不知道你的宗门和侍妾 | 第 6 步，然后重新打开被关掉的自动任务 |
| 按钮返回 400「Chat ID not configured」 / 钓鱼按钮全灰 | `.env` 没填绑定群 | 第 2 步填 `TG_GAME_BOUND_CHAT_ID`，重启 |
| 状态一直是「等待 MiniApp 试钓」 | telegram 进程没起来 / 号没登录 / 点过「暂停全部自动化」 | 用 `run_services.py all`；完成网页登录；「角色真身」页点「恢复全部自动化」 |
| 验证码发不出去，或者能发指令收不到回复 | 国内没配 SOCKS5，或者用上了 HTTP 代理 | `.env` 写 `TELEGRAM_PROXY=socks5://127.0.0.1:<端口>`，重启 |
| 启动就崩：`invalid literal for int()` | `.env` 的 ID / 端口填了非数字或留空 | 只写纯数字，`TG_GAME_PORT=8787` |
| 发送验证码时报 `invalid literal for int() with base 10: ''` | 没填 `TELEGRAM_API_ID` | 第 2 步 |
| 离线超过 4 小时后，自动任务提示「恢复保护」 | 防止一开机几十个任务同时发 | 正常，过一会儿会自己跑 |
| 群里同一条指令出现两遍 | 两台机器同时在跑同一批号 | 第 0 步：只留一台 |

**还是不行的话**，把这几样发给维护的人：系统和是否在国内；`/login` 第二步显示的文字；点「灵溪垂钓」是跳回角色页还是能打开；钓鱼页红字的第一行；第 7 步那条检查命令的输出。
