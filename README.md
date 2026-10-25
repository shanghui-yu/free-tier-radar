# free-tier-radar 📡

**云厂商免费额度一改，全网教程就过期。这个小工具每天替你盯着它们。**

Cloud free tiers change silently — AWS retired its 12-month free instance plan in
2025 and thousands of tutorials still teach the old rules; Oracle quietly shrank
its Always Free Arm allotment. *free-tier-radar* polls the official free-tier and
cheap-VPS pages once a day, diffs them against yesterday's snapshot, and commits
a human-readable report. If a spec, a price, or a policy line changes, you see the
exact lines that moved.

## 它盯什么（默认源）

| 源 | 类型 |
|---|---|
| Oracle Cloud Always Free | 永久免费额度 |
| Google Cloud Free Tier | 永久免费层 |
| AWS Free Tier | 新手额度 |
| Azure Free Account | 新手额度 |
| RackNerd Specials | 年付特价 VPS |
| Hetzner Cloud Plans | 低价 VPS 定价 |

想加源？往 `sources.json` 里加一行 `{name, url, kind}` 提 PR 即可 —— 这是这个项目长大的方式。

## 怎么跑

零依赖，只要 Python 3.9+：

```bash
git clone <this repo> && cd free-tier-radar
python3 radar.py --seed   # 第一次：给所有源建基线快照
python3 radar.py          # 之后每次：抓取、对比、生成 LATEST.md
```

## 免费用 GitHub Actions 当服务器

Fork 本仓库，打开 Actions 页启用 `free-tier-radar` workflow —— 完事。
它每天 01:17 UTC 自动运行，有变化就把新快照和 `LATEST.md` 变更报告提交回仓库；
配合 GitHub 的 Watch（Releases/All activity）或仓库提交通知，额度政策一变你第一时间知道。
不需要 VPS，不需要数据库，不需要 API key。

## 设计原则

- **零依赖**：只用标准库（urllib + html.parser），任何机器 `python3 radar.py` 直接跑。
- **只存文本快照和哈希**：仓库本身就是历史档案，`git log` 就是变更史。
- ** diff 给人看**：报告截取变更行，不刷屏。

## 路线图（欢迎 PR）

- [ ] 变化时自动开 Issue 并 @ 订阅者
- [ ] 结构化抽取（价格/核数/内存字段级对比，而非全文 diff）
- [ ] RSS / Telegram / 邮件推送
- [ ] 更多源：阿里云、腾讯云轻量活动页、LowEndTalk 特价帖

## License

MIT

