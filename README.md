# cc-connect-phone-bridge-skill

用手机（微信 / 飞书）远程指挥本地 Claude Code —— 一个 Claude Code **skill**：平台选型、搭建 SOP、实战避坑、安全收紧，外加一个把长文镜像成飞书云文档 / 知识库的脚本。

> **EN** — A Claude Code skill for driving your local coding agent from your phone via [cc-connect](https://github.com/chenhg5/cc-connect) (WeChat / Feishu-Lark bridge): platform comparison, setup SOP, battle-tested pitfalls, security hardening, plus `feishu-md2doc.py` for mirroring long markdown (plans / design docs) into Feishu cloud docs & wiki, with a comment fold-back subcommand. Docs are in Chinese.

## 这是什么

[cc-connect](https://github.com/chenhg5/cc-connect)（MIT）是把飞书 / 微信 / Slack / Telegram 等消息平台桥接到本地 coding agent 的 Go 程序。本仓库**不是** cc-connect 的文档复刻，而是围绕「手机 ↔ 本地 Claude Code」这个场景的**原创使用沉淀**：

| 文件 | 内容 |
|---|---|
| [`SKILL.md`](SKILL.md) | skill 主体：①微信 vs 飞书选型 ②搭建 SOP ③避坑清单（一号一活 bot / 飞书权限双身份 / `99991672` 等）④访问控制矩阵（`allow_from` 双闸 + mode-RCE 硬护栏）⑤单/多 bot 决策 ⑥长文 → 飞书云文档/知识库 ⑦远程仓 push/发版监控 → 主动通知 ⑧上线前安全 checklist |
| [`feishu-md2doc.py`](feishu-md2doc.py) | 配套脚本：`to-doc`（md → 独立飞书云文档）/ `to-wiki`（md → 知识库节点树，懒建路径 + sidecar 幂等）/ `read-comments`（飞书评论回捞回本地 md）/ `check`（端到端权限自检）/ `selftest`（离线自检） |

内容全部来自真实搭建与踩坑（含飞书权限「应用身份 vs 用户身份」这种靠后台截图才定位的坑），照单可避。

主线以 Claude Code 为被指挥的 agent（**全部实测基于它**）；cc-connect 的 agent 层可插拔（`codex` / `gemini` / `opencode` / `cursor` 等），平台侧内容对其他 agent 同样适用，差异与替换点见 `SKILL.md` §2.1。

## 安装为 Claude Code skill

```bash
git clone https://github.com/yulezheng/cc-connect-phone-bridge-skill.git
# skill 注册名不带 -skill 后缀（与 SKILL.md frontmatter 的 name 一致）：
ln -s "$(pwd)/cc-connect-phone-bridge-skill" ~/.claude/skills/cc-connect-phone-bridge
# 或不想用软链：cp -r cc-connect-phone-bridge-skill ~/.claude/skills/cc-connect-phone-bridge
```

装好后，当你对 Claude Code 说「想用手机远程指挥本地 Claude」之类的话时，skill 会被自动触发。其他 AI assistant（如 Codex CLI）同理：`SKILL.md` 是自包含 markdown，放进各自的 skills 目录（如 `~/.codex/skills/cc-connect-phone-bridge/`）即可，直接当普通文档读也行。

## feishu-md2doc.py 速览

依赖：Python 3.11+（`tomllib` 内置）+ `requests`。凭证复用 cc-connect 的 `config.toml`，仅调用飞书开放平台公开 API。

```bash
# 权限自检（首用前，端到端真实写入探测）
python3 feishu-md2doc.py check --project <name> --config <path/to/config.toml>

# md → 一篇独立飞书云文档（审阅一次性长文）
python3 feishu-md2doc.py to-doc --project <name> --md plan.md

# md → 飞书知识库 wiki 节点树（长期沉淀；懒建目录 + sidecar 幂等）
python3 feishu-md2doc.py to-wiki --project <name> --md plan.md \
  --space-id <id> --path "项目/<proj>/<类别>"

# 把飞书文档里的评论回捞回本地 md（review fold-back）
python3 feishu-md2doc.py read-comments --project <name> --md plan.md [--write]
```

前置权限（飞书开放平台，一次性）：`to-doc` 需**应用身份** `drive:drive` 并发版；`to-wiki` 再加 `wiki:wiki`。细节与坑见 `SKILL.md` §5。

## 前提与免责

- **从零开始即可，不需要预先装好 cc-connect 或建好 bot**——skill 会带着你从选型、装 cc-connect 到接平台 bot 走完全程（飞书自建应用由 onboarding 扫码自动创建，无需先去控制台手搓）；个别环节需要你本人配合：手机扫码登录、飞书控制台开云文档权限并发版等。`feishu-md2doc.py` 在搭好之后用（它从 cc-connect 的 `config.toml` 读凭证）。
- 飞书走官方 bot API，合规；**微信个人号（iLink 通道）自动化属平台灰色地带、有封号风险**，风险自负。
- `config.toml` 含 token / app_secret：`chmod 600`、放所有 repo 之外、绝不提交。

## License

MIT（见 [LICENSE](LICENSE)）。底层 [cc-connect](https://github.com/chenhg5/cc-connect) 为 MIT（独立项目，非本仓一部分）；`feishu-md2doc.py` 依赖 [requests](https://github.com/psf/requests)（Apache-2.0）。
