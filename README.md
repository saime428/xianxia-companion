# 🚀 Xianxia Companion 部署

> 基于 [jiven303toto/xianxia-companion](https://github.com/jiven303toto/xianxia-companion)（MIT）继续修改。测试里的账号、群号都是占位值。

> 第一次部署，或者拉下来发现很多功能不正常（钓鱼、洞府小程序、天机阁人物卡……），先看 **[SETUP.md](SETUP.md)**：从零部署的每一步、怎么确认做对了、出问题对照哪一行排错，都在里面。下面是最简版。

近期更新：洞府每日宝箱支持自动领取和立即领取，已领取当天跳过；寻宝按奖励优先用完神识，剑阵续阵会核对实际增益与冷却。青元子支持战后单独通知，配置和投递规则见 [DAILY-TASK-REPORT.md](DAILY-TASK-REPORT.md)。战斗时序增加持续延迟适应和乱序反馈保护；历史回放尚未显示命中率提升，偶发延迟尖峰仍可能影响完美命中。股市新增建议留档、后续报价复核及实验收益模型观察，实验结果不用于买卖提醒或自动下单，也不代表已提高预测准确率。

排查网络延迟时可手动使用 `tools/capture_world_boss_network.py`（Linux，需要已有 `tcpdump`），将输出保存到 `data/world_boss/`。运行数据目录和网络抓取文件已从 Git 中排除。

## 1. ✅ 准备

- Python 3.10+
- Telegram API ID / API Hash
- 目标会话 ID：`TG_GAME_BOUND_CHAT_ID`
- 目标 bot 数字 ID：`TG_GAME_BOUND_BOT_ID`
- 如果目标群启用 topic，再准备 `TG_GAME_BOUND_THREAD_ID`

进入仓库目录：

```powershell
cd <你的仓库目录>
```

## 2. ⚙️ 初始化

Windows：

```powershell
python tools/setup_environment.py --install
notepad .env
```

macOS / Linux：

```bash
python3 tools/setup_environment.py --install
nano .env
```

## 3. 📝 填写 `.env`

必填：

```dotenv
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TG_GAME_BOUND_CHAT_ID=
TG_GAME_BOUND_BOT_ID=


```

按需填写：

```dotenv

TG_GAME_HOST= 127.0.0.1
# 示例端口；可改成任意未占用端口
TG_GAME_PORT= 8787

# --- 管理员（必填：你自己 TG 的数字 ID；不填就没有管理员，天机阁 Cookie 贴不进去，钓鱼等功能都用不了） ---
AUTHORIZED_USER_ID= 

TG_GAME_BOUND_THREAD_ID=
TG_GAME_ALLOWED_BOT_IDS=

# 留空。填了之后网页会监听 0.0.0.0，/login 会把任何访问者直接当成已登录的账号（见 SETUP.md 第 10 步）
TG_GAME_DOMAIN=
TG_GAME_SSL_CERTFILE=
TG_GAME_SSL_KEYFILE=
```


检查配置：

```powershell
python tools/setup_environment.py --check --strict
```

## 4. ▶️ 启动

Windows：

```powershell
.venv\Scripts\python.exe run_services.py all
```

macOS / Linux：

```bash
.venv/bin/python run_services.py all
```

启动后终端不会提示输入手机号：在浏览器打开下面的地址，进 `/login` 页用手机号、验证码（和二步验证密码）登录。然后按 [SETUP.md](SETUP.md) 第 6、7 步连天机阁、设洞府入口，钓鱼和各个小程序才能用。

打开，端口按 `TG_GAME_PORT` 替换：

```text
http://127.0.0.1:8787
```

## 5. 🔎 验证

访问，端口按 `TG_GAME_PORT` 替换：

```text
http://127.0.0.1:8787/health
```

确认返回 `status=ok`。如果 `telegram_code_current=false`，重启 Telegram runtime。

## 6. 🔄 更新已部署实例

以下命令在已经完成初始化的部署机器上执行：

Windows：

```powershell
git pull
python tools/setup_environment.py --install
python tools/setup_environment.py --check --strict
.venv\Scripts\python.exe run_services.py all
```

macOS / Linux：

```bash
git pull
python3 tools/setup_environment.py --install
python3 tools/setup_environment.py --check --strict
.venv/bin/python run_services.py all
```

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。

---

**🤝 致谢与社区**

本项目永远感谢 [LINUX DO](https://linux.do/) 社区的支持与推广。
