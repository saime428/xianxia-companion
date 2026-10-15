# 赌石记录与每日自动操作

2026-10-12：三个账号北京时间每天 03:10 / 03:13 / 03:16 各一次，使用各账号现有指令群绑定的话题。

每轮按该号设定的价位选一块：三块按售价排好（同价按编号），`cheapest` 取最便宜、`middle` 取中间、`priciest` 取最贵；选中的超出单价上限或预算就往便宜的退一档。默认 `cheapest`，用 `enable --pick` 改（不带 `--pick` 重新启用时保留原设定）。三个号分开选不同价位，是为了攒「价位和结果有没有关系」的样本——只选最便宜的话永远看不到贵的那两块切出来是什么。单块石价上限 5000，按一次入场和购买核对每号 5500 灵石的预算。静默无回包无法证明服务器没有处理首条切石，因此最多补发一次；若两条切石各自返回结果，停下核对实际花费。入场已经付费后才看得到石价，全部超预算时会损失入场费，但不继续买石。大涨、小涨是游戏标签，日报只写物品和花费，不认定盈利。

`xianxia-stone-gambling.service` 每两秒从现有群消息库归档，不另开 Telegram 会话。原群、现有绑定话题的赌石记录保存在 `tg_game.db` 的 `stone_messages` / `stone_rounds`，不受原消息 48 小时清理影响。每次保留三块石头的完整描述和售价，选中编号、账号、入场费是否有支付回包、切出物品和数量，以及原始消息与回复 ID。编辑回包更新同一样本，不增加次数。不明关联保留为 pending / ambiguous，不算进出货统计。

systemd 单元模板保存于 `tools/xianxia-stone-gambling.service`；在同一路径部署时复制到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 和 `systemctl enable --now xianxia-stone-gambling.service`。默认没有启用任何账号，使用下方 enable 命令配置后才会下注。迁移到其他路径时须调整单元的 WorkingDirectory、ExecStart 和 ReadWritePaths。

调度配置、每日执行记录、队列截止时间分别在 `stone_auto_config` / `stone_auto_runs` / `stone_auto_commands`。SQLite 事务同时写队列和执行状态，每号每个北京时间日期唯一。当天手动赌过则跳过自动操作；错过 15 分钟窗口不在白天补赌。指令进入原队列，沿用原全局暂停、夺舍锁、限流和网络暂停检查。

只接受该账号入场消息的选石回包，切石回复对应选石消息；只接受对应切石命令的游戏机器人结果。切石发出后满 30 秒仍没有任何游戏回包，且仍在选石消息的两分钟窗口内时，自动补发同一编号一次。补发次数持久化在原队列关联表，重启不会增加次数；补发沿用原选石回复、价格和预算检查，也不会被队列的同名去重吞掉。收到“正在切石”等任何游戏回包就等待结果；新回包或人工补发会在真正发送前再次检查，取消尚未发出的补发。

同一入场、同一原石的自动或手动补发归为一轮，结果可回复其中任意一条切石消息。不同编号，或两条切石分别收到结果，仍判为关联不明并停下核对。入场不自动重试；失败、超预算、不同选择的人工干预、最终超时或关联不明时关闭该账号自动赌石，等待核对；记录器仍持续工作。发送前再次检查截止时间和人工操作，记录器停机也不能让旧指令过期后发出。

日报仍按现有 23:50 北京时间投递到主号收藏夹，增加各号的切出物品、编号、花费、跳过或停止原因。已发日报不重复补发。

在 VPS 上这些命令要以 `xianxia` 身份跑（每个子命令都会打开生产库，root 打开过的库文件主服务会读不了）：前面加 `runuser -u xianxia --`。

```bash
# 查询样本统计和任务状态
.venv/bin/python tools/stone_gambling.py stats
.venv/bin/python tools/stone_gambling.py stats --own
.venv/bin/python tools/stone_gambling.py status
# 导出全部样本（三块石头详情在 stones_json）
.venv/bin/python tools/stone_gambling.py export data/stone-rounds.csv
# 停止某号或全部账号的自动操作；统计继续
.venv/bin/python tools/stone_gambling.py disable --profile 2
.venv/bin/python tools/stone_gambling.py disable
# 核对停止原因和游戏回包后，标记已核对，再显式启用
.venv/bin/python tools/stone_gambling.py review --run <run_id>
.venv/bin/python tools/stone_gambling.py enable --profile <id> --chat <group_id> --thread <topic_id> --time 03:10 --max-price 5000 --budget 5500 [--pick cheapest|middle|priciest]
```

测试：`tools/test_stone_gambling.py`；同时通过原日报、发送控制的离线回归。首次自然窗口已确认账号 3 的“小涨”和账号 2 手动补发后的“垮了”回包；自动补发机制仍待后续自然静默超时验证，没有为验收额外下注。

描述统计只覆盖玩家实际选择过的石头，存在选择偏差；样本少时不据此改策略。群里的其他玩家样本与自己的每日任务分开归属，统计持续累计。

## 已知问题与部署注意（2026-10-13 核查）

**记录器原来每 2 秒占一次库的写锁，10-13 已改成先读后锁。** 原来 watch 循环每轮先 `BEGIN IMMEDIATE`，再扫 `recorded_messages` 和 `bound_messages`（按 `updated_at` 过滤，这两张表没有 `updated_at` 索引，是全表扫），最后无条件写 `stone_meta` 的 `last_sync` 和 `worker_heartbeat`。读表的全程都握着写锁，主服务这段时间写不了库；一天约 4.3 万次写事务，绝大多数轮次没有任何赌石消息。10-13 在生产上实测两张表合计约 4.6 万行、扫一轮约 8 毫秒，耗时随行数线性增长。

现在每轮（`stone_gambling.watch_pass`）先在事务外读，只有下面三种情况才拿写锁：

- 读到了赌石相关的消息，要归档；
- 有进行中的 run，或者正处在某个已启用账号的 15 分钟窗口里（这段时间仍是每 2 秒推进一次）；
- 距上次写库满 30 秒，记一次心跳，同时推进游标 `last_sync`。

入队和状态变更仍在同一个写事务里，原来「队列和执行状态同时写」的保证不变。`status` 里的 `worker_heartbeat` 因此最多滞后 30 秒；它只用来显示，没有别的地方读。`status` 列出的 run 同时改成按编号倒序取最近 30 条（原来按账号排序，跑十来天后看不到最新的）。

还留着的一处已知上限：`rebuild_rounds` 每次有变化就全量重建，每条切石都要扫一遍全部入场记录，也在写锁里。样本现在只有几轮，无感；累计到几千轮后单次要按秒算，到时改成只重算受影响的那一轮。

部署时要注意：

- `rollout.py` 只重启主服务，不认识这个服务。改了 `stone_gambling.py` 或 `tools/stone_gambling.py`，要另外 `systemctl restart xianxia-stone-gambling.service`，否则记录器跑旧代码，而主服务里发送前的检查已经是新代码。
- 单元里写的是 `Wants=xianxia-companion.service`：主服务停着的时候启动或重启记录器，会把主服务一起拉起来。
- 主服务停了以后库并不静止，记录器至少每 30 秒还会写一次。做文件级的备份或恢复前，把它也停掉。
- 程序不查号上实际有多少灵石。不够时入场照付 500，切石回「囊中羞涩，买不起这块石头」，这句不在失败关键词里，要等 90 秒超时才停下并关掉该号（10-14 大号就是这样）。号与号之间转灵石用万宝楼：收钱的号 `.上架 玄铁剑 换 灵石20000`，出钱的号 `.购买 <挂单ID>`，不收税；单价不能超过天道估值的 10 倍，玄铁剑估值 2000，所以一把最多换 2 万。
- 维护窗口如果正好落在入场和切石之间，切石不会排队，60 秒后这次 run 停掉、该号的自动赌石关闭，需要核对后重新启用。
