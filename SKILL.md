---
name: cc-connect-phone-bridge
description: 用于「想从手机（微信 / 飞书等）远程指挥本地 Claude Code」时。用 cc-connect(MIT) 把消息平台桥接到本地 coding agent —— 含平台选型（微信 vs 飞书）、单/多 bot 决策、搭建 SOP、实战避坑、安全收紧、以及把长文（plan/设计稿）镜像成飞书云文档的脚本。English keywords for matching：cc-connect feishu lark wechat weixin bot bridge remote control phone mobile messaging long-connection websocket systemd daemon ilink allow_from。
---

# 手机远程指挥 Claude Code（cc-connect-phone-bridge）

> **底层工具**：[cc-connect](https://github.com/chenhg5/cc-connect)（MIT 许可）—— 一个把飞书 / 微信 / Slack / Telegram 等消息平台桥接到本地 coding agent（Claude Code 等）的 Go 程序。
> 本 skill 是**使用 SOP + 选型 + 避坑沉淀**（原创，非 cc-connect 文档复制）。命令名 / 配置字段为事实性信息。
> ⚠️ **ToS（各平台 ToS 自负）**：飞书走**官方 bot API = 合规**；**微信个人号（iLink 通道）自动化属平台灰色地带、有封号风险**。生产 / 团队场景优先飞书。

## 何时用

用户想「人不在电脑前，用**手机**让本地 Claude Code 干活 / 答疑 / 出方案 / 跑长任务并被通知」。

---

## 1. 平台选型（先定这个）

| 维度 | 微信个人号（iLink 通道） | 飞书 / Lark（自建应用 bot） |
|---|---|---|
| 连接方式 | iLink 网关长轮询 | **WebSocket 长连接（无需公网 IP / 域名 / 反代）** |
| 一个账号多 bot | ❌ **同一时刻只能一个活 bot**（每次新扫码登录会挤掉上一个） | ✅ 多个自建应用并发 |
| 群聊 | ❌ **个人号 ilink 实测只支持 1:1 私聊**（`chat_id`/@chatroom 群配置即便文档提及也不生效；以你装的版本为准） | ✅ 原生群聊；session key 含 chat+user，多人可区分 |
| 长文审阅 | 切成多条 4000 字消息 | 可建**云文档**（见 §5） |
| 合规 | ⚠️ 灰色（封号风险） | ✅ 官方 API |

→ **一句话**：微信个人号 = **只能单聊 + 一号一活 bot**；**要群聊 / 多 bot 并行 / 长文云文档 / 任何复杂功能 → 选飞书**。只想要个随身「问答小助手」→ 微信也行（接受灰色风险）。

---

## 2. 搭建 SOP（通用）

1. **装 cc-connect**：源码编译（Go 版本以其 `go.mod` 声明为准；含前端则需 Node/npm `make build`）或装预编译二进制。
2. **config.toml**：放在**所有 repo 之外**（含平台 token / app_secret），`chmod 600`，**绝不提交进任何 repo**。
3. **每个 project**：一个 `work_dir`（agent 默认工作目录）+ 一个 agent（claudecode）+ 一个平台 bot。一个 cc-connect 进程可管多个 project。
4. **接平台 bot（建凭证 —— 别去控制台手搓应用）**：
   - **飞书**：用 cc-connect 自带 onboarding，**不要**先去 `open.feishu.cn` 控制台手动建应用：

     ```bash
     cc-connect feishu setup --project <name> [--qr-image qr.png] [--set-allow-from-empty]
     ```

     - **不带凭证 = NEW flow**：手机飞书**扫码**即在你的企业里**新建自建应用** + 写回 `app_id`/`app_secret` + 自动预配 IM 收发权限与长连接事件订阅（无需自己点权限/发版）。
     - **带 `--app <id:secret>` = BIND flow**：把已有 app 绑进来。
     - `--set-allow-from-empty`：扫完把你（owner）的 `open_id` 自动回填进 `allow_from`（open_id **按 app 隔离**，同一人每个 app 下不同 → 无法照搬别 bot 的 open_id）。
     - 同企业**再加一个 bot** = 再跑一次 `setup --project <新名>`（新 app、独立 app_id/secret/open_id）。
     - ⚠️ 仅当要用**云文档**（§5 `feishu-md2doc.py`）才需另去控制台加**应用身份** `drive:drive`（`to-wiki` 再加 `wiki:wiki`）并**发布新版本**——基础聊天 bot 的 onboarding 不含此权限（**setup 不会自动开也不提示**，易拖到发文档才撞 `99991672`）。装完用 `feishu-md2doc.py check --project <名>` **端到端自检**（详 §5）再用。
   - **微信个人号**：扫码登录 iLink 网关（二维码刷新快，抢第一张新鲜码，见 §3）。
5. **agent 配置项 —— 按需自选**（不要照抄别人的）：

   ```toml
   [projects.agent.options]
   work_dir         = "/abs/path/to/repo"
   model            = "<model>"            # 例: opus | sonnet | haiku | 具体 model id —— 自选
   reasoning_effort = "<effort>"           # low | medium | high | max —— 自选（越高越慢越贵）
   mode             = "<mode>"             # default(每次问) | acceptEdits | plan | auto | bypassPermissions —— 自选
   ```

   ⚠️ `auto` / `bypassPermissions` 会**自动执行工具（含 bash）**，方便但危险 → **务必配合 §3 的 allow_from 白名单收紧**，并确认 work_dir。
6. **长期常驻**（可选）：装成 **systemd user 服务**（非 root：`~/.config/systemd/user/`，无需 sudo）+ `loginctl enable-linger <user>`（关终端 / 退出登录都在；需 sudo 一次）。非 systemd 环境用 `nohup` / `tmux`。⚠️ WSL：Windows 重启 / `wsl --shutdown` 仍会停，开机自启需 Windows 侧另设。

### 2.1 agent 类型：不只 Claude Code（多 agent 注记）

cc-connect 的 agent 层是可插拔的：`[projects.agent] type` 除 `claudecode` 外，上游还支持 `codex` / `gemini` / `opencode` / `cursor` / `iflow` / `qwen`（以你装的版本为准）。想用手机指挥其他 coding agent，换 type 即可接同一套平台桥。

⚠️ **本 skill 全部实测基于 `claudecode`**，换其他 agent 时这样用本文：

- **平台侧内容 agent 无关、直接复用**：§1 选型、§2 搭建、§3 避坑与 §3.1 访问控制矩阵、§4 单/多 bot、§5 云文档脚本、§6 远程仓监控、§7 checklist 的平台项。
- **三处 claudecode 特定内容需按你的 agent 对应替换（未实测）**：
  1. §2 第 5 步的 agent options（`model` / `reasoning_effort` / `mode` 是 claudecode 字段，各 agent 的 options 不同，查 cc-connect 文档 / 源码）；
  2. §3 work_dir 段的 `CLAUDE.md` 只向上读加载机制（如 Codex 对应 `AGENTS.md`，加载语义不同）；
  3. §3.1 / §7 的 mode-RCE 护栏词汇（`auto` / `bypassPermissions`）——**原理通用**：自动执行工具 + 入站放开 = 任何可达的人能在你机器跑命令；把「低危 mode」映射成你的 agent 的等价权限模式即可。

---

## 3. 关键约束 / 避坑（实战踩出，照单避）

- **微信一号一活 bot**：要多个并发 bot 必须多个微信账号；扫码登录二维码刷新快（~3 次 / 几分钟窗口），**抢第一张新鲜码**最稳。
- **飞书权限分「应用身份 vs 用户身份」**：`tenant_access_token` **只认应用身份**。⚠️ **飞书搜 `drive:drive` 会出两条同名条目（应用身份 / 用户身份），极易选错**——开成「用户身份」即便发版仍报 `99991672`（实战踩过、靠看后台截图才定位）。只加「用户身份」或漏发版都会 `99991672 应用尚未开通所需的应用身份权限`。**加权限后必须「创建版本 → 发布」才生效**（光加不发版无效）。拿不准用 `feishu-md2doc.py check --project <名>` 端到端验（§5）。
- **访问控制（`allow_from`/`allow_chat`/`admin_from`/`group_only`）—— 双闸 AND，完整见 §3.1**：默认**锁 owner**（onboarding 的 `--set-allow-from-empty` 回填 owner open_id）；放开（`*`）必须配低危 mode（详 §3.1 矩阵 + 护栏）。给 bot 发 `/whoami` 拿 open_id / chat_id。
- **work_dir 选型与 CLAUDE.md 加载**：Claude Code 的 `CLAUDE.md` **只向上读**（cwd → 根）。所以：
  - 一个 bot 挂**仓库根** → 该仓 `CLAUDE.md`/`AGENTS.md` 启动即自动加载（上下文干净）。
  - 一个 bot 挂**父目录**（含多个子仓）→ 各子仓的 `CLAUDE.md` **启动时不预加载**（只在 agent 实际进入该子目录读文件时懒加载）；要预加载就在父目录放一个 `CLAUDE.md` 用 `@import` 拉子仓。

### 3.1 访问控制矩阵（双闸 AND · 源码 `feishu.go onMessage` 实证）

cc-connect 飞书鉴权是**双闸 AND**（不是「加群=群授权」的 OR）：

- **`allow_from`（人闸）**：**所有消息**（私聊+群）先过，发送人 open_id 不在 → 拒；`*`/空 = 任何人。
- **`allow_chat`（群闸）**：**仅群消息**再过，私聊**不走**这道闸。⚠️ 它**不是**「加群→群里人都能用」，而是「`allow_from=*` 时的**群范围收窄器**」——锁人（具名 `allow_from`）时它**多余**；放开人（`*`）时它把 bot 限定到指定群。
- **`group_only=true`**：挡**所有**私聊（不分人，含 owner）。
- **`admin_from`**：管特权命令（`/shell /restart /upgrade /commands addexec /cron addexec`），**默认 blocked**（所有人都不能）；⚠️ **挡不住** `auto`/`bypass` 下 Claude **自己**调 Bash（那受 `mode` 控制）。

> **群消息 = `allow_from(人) AND allow_chat(群)`**；`allow_from` 是**全局一个闸**（私聊+群共用），没分私聊/群两维 → 「群里开放、私聊只你」**做不到**（cc-connect 缺口，可提 upstream：allow_chat 做成群授权 / 或 allow_from 分两维）。

**配法矩阵**（按 bot 的 token 额度归属 + 信任度选）：

| 配法 | 群里其他人@ | 你私聊 | 陌生私聊 | 适用 |
|---|:---:|:---:|:---:|---|
| 🅰️ `allow_from=你 + allow_chat=群` | ❌ | ✅ | ❌ | 只你、限该群。**默认推荐、最稳** |
| 🅱️ `allow_from=* + allow_chat=群`（`group_only=false`） | ✅ | ✅ | ⚠️✅ | 该群里**任何人能@** + 私聊**开放**（你能私聊，但陌生人也能 → 敞口） |
| 🅲️ 🅱️ + `group_only=true` | ✅ | ❌ | ❌ | 该群里**任何人能@**（群行为**同🅱️**）+ 私聊**全关**（含你，只能在群里用） |
| 🅳️ `allow_from=* + allow_chat=*` | ✅ | ✅ | ✅ | 全开放。**仅 bot 用企业 token、企业内共享时** |

> 📌 **🅱️ vs 🅲️ 群行为完全相同**（都是该群里任何人能@，因 `allow_from=*` + `allow_chat=群`），唯一区别是 `group_only` 控制的**私聊开/关**。配法本质看两个**正交维度**：**① 谁能用**（`allow_from` 锁你/放开 + `allow_chat` 限群/全部）× **② 私聊开关**（`group_only`）。别用「群共享」一词混指。

🔴 **mode 联动硬护栏**：`allow_from` 放开（`*`，即 🅱️🅲️🅳️）+ `mode ∈ {auto, bypassPermissions}` = **任何可达的人能让你机器跑 Bash（RCE）**。开放配法**必须**配 `mode=plan`(只读)/`default`(每次问)，或确信所有可达的人可信。

> ⚠️ **`acceptEdits` 介于两者之间**：它**自动批文件编辑**（Write/Edit 不逐次问），但 **Bash 仍逐次问** → RCE 风险**低于** `auto`/`bypass`。但在开放配法（`allow_from=*`）下，任何可达的人仍能让 Claude **自动改 work_dir 里的文件**（无确认）→ 仍有副作用风险。故开放配法下 **仍建议降到 `plan`/`default`**，别因「不跑 Bash」就放心 `acceptEdits` 全开。

💰 **额度归属维度**（决定能否全开放）：bot 背后 token 是**私人订阅**（谁用都薅你额度、反噬卡你本机 CLI、个人订阅 ToS 未必许团队共用）还是**企业专用 key**。私人 → 锁（🅰️）；企业共享 → 🅳️ 可接受。

⚙️ **运维**：改 `allow_from`/`allow_chat` 要**整进程重启 daemon = 该机所有 bot 短暂断连**（批量加人一次改完再重启）；`/config reload` 只覆盖 display/providers/commands、**不含**这俩；`--set-allow-from-empty` 对**已是 `*`** 的 bot 是 **no-op**（收紧须手改 config + 重启）。

---

## 4. 一个项目用一个 bot 还是多个？

| 选**一个**（挂父目录 / 仓库根） | 选**多个**（各挂自己仓库根） |
|---|---|
| 多仓耦合紧、想要全局上下文 | 想**并行**驱动多仓（一个 bot 单会话串行；多 bot 才能多聊天并行） |
| 一次基本只动一个、常跨仓联动 | 各仓独立、很少一起改、想要各自**干净聚焦**的上下文 |

> 飞书无 bot 数量限制、加 bot 零成本 → **先一个跑着，真需要并行/隔离再拆多个**。work_dir 只是「主战场」非硬隔离，跨仓可按需明确指派（注意别两 bot 同时改同一批文件 / git 认准子仓）。

---

## 5. 长文（plan / 设计稿 / 调研）→ 飞书云文档 / 知识库

cc-connect 本身**无云文档能力**：长 agent 回复会被切成多条 4000 字聊天消息（能 `send --file` 但只是可下载附件，非可评论文档），不利审阅。本目录的 **`feishu-md2doc.py`** 把一个 markdown 文件镜像到飞书云。两个子命令：

- **`to-doc`**（§5.1/5.2）—— 镜像成**一篇独立飞书云文档（docx）**、按需共享、打印链接。**审阅一次性长文**用它。
- **`to-wiki`**（§5.3）—— 归档进**飞书知识库 wiki space 的节点树**（懒建目录 + sidecar 幂等），**长期沉淀成有目录的知识库**用它。

> 兼容：不带子命令直接 `--project ... --md ...` = `to-doc`（旧脚本/bot 调用不受影响）。

- **前置**（`to-doc`）：给该飞书应用加**应用身份** `drive:drive` 权限并**发布新版本**（实测 import 建 docx 仅需 drive:drive，**不需 docx:document**——脚本无 docx/v1 调用、真机关掉 docx:document 仍通；飞书 99991672 的 permission_violations 也只要 drive:drive）。（`to-wiki` 再加 `wiki:wiki`，详 §5.3。）
- **用法**：`python3 feishu-md2doc.py to-doc --project <project> --md <file.md> [--title ...] [--config <path>]`（**Python 3.11+** `tomllib` 内置；依赖 `requests`）。⚠️ daemon 的 config 常不在默认路径（`~/.cc-connect/config.toml` 可能只有 `[log]`），用 `--config` 指实际路径（如 `~/cc-connect/config.toml`，`cc-connect config path` 可查）。
- **权限自检（首用前强烈建议）**：`python3 feishu-md2doc.py check --project <project> [--config <path>]` —— **端到端真实写入探测**（建一个测试 docx 再删，进回收站可恢复）。⚠️ 只读探针（root_folder/meta）无写权限也返成功=**假阳性**，故必须真实写入；失败会打印权限开通指引（应用身份 + 发版）。

### 5.1 共享范围（2026-06 加）

**默认 = 保持企业默认**（脚本不主动改 link_share，尊重各企业管理员策略、不强行统一）。文档创建时本就继承企业默认。要特定范围才显式给参数：

| 想要 | 给什么参数 | 底层 API |
|---|---|---|
| **保持企业默认**（默认，推荐） | 不给 = `--link-share inherit` | 不 PATCH，继承企业/管理员设的默认 |
| 强制 **企业内可见+可评论** | `--link-share tenant_readable` | `PATCH .../public` `link_share_entity=tenant_readable` + `comment_entity=anyone_can_view` |
| 强制 **互联网可见** | `--link-share anyone_readable` | 同上，`anyone_readable` |
| **整个群可见**（含日后新进群的） | `--reviewer-chat <oc_xxx>` | `POST .../members` `member_type=openchat perm=view` |
| **指定个人** | `--reviewer <ou_xxx>`（`--perm view/edit`） | `members` `member_type=openid` |
| 仅具名协作者（关链接分享） | `--link-share closed` | `public` `link_share_entity=closed` |

- 群的 `open_chat_id` 就在 cc-connect session key 里：`feishu:oc_xxx:ou_yyy` 的 **`oc_` 段**。
- `--reviewer-chat` / `--reviewer`（加协作者）与 `--link-share`（链接分享范围）**正交、可叠加**。

> ⭐ **默认行为约定（2026-06，user 校正）**：用本脚本 / 飞书云文档能力时，**若无特殊说明 → 保持企业默认**（`--link-share inherit`，脚本不主动改链接分享），**把选择权留给各企业管理员、不强行统一**。文档创建本就继承企业默认（如本企业默认即「企业内可见+可评论」，自然大家可见可评论）。**仅当明确要某范围**才显式 `--link-share` / `--reviewer-chat`。

### 5.2 避坑（实测踩出）

- 媒体上传 `drive/v1/medias/upload_all` **必带** `extra={"obj_type":"docx","file_extension":"md"}`，否则 `1061004 forbidden`。
- 文档由 `tenant_access_token` 创建 → **归 bot 所有**；不加协作者、且 `link_share=closed` 则别人**看不到**。
- `allow_from="*"`（开放给所有人）会被脚本**过滤成空 reviewer** → 别指望靠 `allow_from` 共享，用 `--reviewer-chat` / `--link-share` 显式控制。
- **应用身份共享不会自动通知**：`need_notification` **仅 `user_access_token` 有效**（官方）→ 对方收不到推送，**把链接手动发群/对方**（或经 cc-connect 回链接到聊天）。
- 评论：`comment_entity=anyone_can_view` = 可阅读者可评论（默认开）；具名「只读(view)」协作者默认不能评论，靠 link_share 的 comment_entity 兜底。
- **方法论契合**：plan 仍落 repo 的 `.md`（source of truth / audit trail），飞书文档只是**审阅镜像**，评论手动 fold 回 `.md`。

### 5.3 归档进知识库 wiki（`to-wiki` 子命令）

`to-doc` 产出**散落的独立 docx**；要把多篇 plan/设计沉淀成**有目录树的知识库**（如「人机协作空间」），用同脚本的 **`to-wiki`** 子命令：把一篇 `.md` 归档进飞书 **wiki space** 的节点树，**懒建路径节点** + 挂文档 + **sidecar 幂等映射**。

```bash
python3 feishu-md2doc.py to-wiki \
  --project <project> --config <path/to/config.toml> \
  --md <plan.md> --space-id <wiki_space_id> \
  --path "项目/<proj>/<类别>" [--force] [--wiki-title-prefix "[测试] "]
# stdout = 归档后的 wiki node_token（便于 bot / 后续流程取用）
```

- **前置**（比 `to-doc` 多两项）：① 应用在 `drive:drive` 之外**再加 `wiki:wiki`** 权限并**再发版**（否则 `99991672`）；② **space 由 user 人工建**（必须 **team 类型**，person 空间不支持加 app），user 把本应用 `open_id` 加为**可编辑成员**——**bot 应用身份建不出 space**（飞书 `space/create` 仅 `user_access_token`），故 `--space-id` 必填、不靠名字反查。
- **可编辑性（W-5，首次归档后请验一次）**：归档的 docx 由 bot(`tenant_access_token`)建、归 bot 所有 → **首次归档后请打开 wiki 文档确认 user 侧可编辑**；若发现**仅可读/可评**，需对该 docx 追加 `transfer_owner`（`docx` 是合法 transfer type，与 folder 不同——folder 无此 type 故当初证伪）。
- **懒建路径**：`--path` 斜杠分隔，逐层在 space 里 `list` 同名子节点计数 → **0 建 / 1 复用 / ≥2 视为 drift 异常 STOP**（不静默取第一个，重名需人工清理）。中间分类层 = 索引 docx（wiki 无纯目录节点类型）。
- **sidecar 幂等**：`.md` 旁生成同名 `*.wiki.json`，记 `wiki_node_token`+`content_hash`(sha256)+`wiki_space_id`+落点（`wiki_path`/`wiki_title`）。再归档先查 sidecar：内容未变**且落点参数未变** → **skip**（幂等）；**内容未变但 `--path`/`--title` 变了 → 显式报错停**（不 silent skip 也不自动 move——真要挪节点/改标题用 `--force` 删旧建新，或飞书端手动移动后自行更新 sidecar）；半截链重跑靠 `node_token` 直查（且 import/move 前对父节点按 title 二次去重，`node_token` 丢失也不堆第二份），**不重复堆节点**。⚠️ **`*.wiki.json` 默认 gitignore**（含 `space_id` 团队内部句柄，不入公开仓）；换机器首次归档会**重建映射**（按 `--path` 找回同名节点复用，不会重复建）。
- **刷新（内容变了）**：飞书 import **不支持覆盖** → 只能「删旧 docx 节点 + 建新 + move 回同父」。删节点不可逆（**进回收站、可恢复**）→ **默认拒绝**，须显式 `--force` 授权 + 明确日志（红线：删 user 飞书数据需明确授权）。⚠️ **`--force` 刷新非原子**：删旧成功后若建新/move 中途失败，旧文档已在**回收站**、新的没建好 → 需**重跑或人工从回收站恢复**（脚本已做：删旧失败即停、不闷头建新，避免新旧重复堆积）。
- **内容与可见范围匹配（自查）**：脚本不做内容脱敏检查——归档前自行确认文档内容适合目标 space 的受众（space 可见性自管，见下「私有性」条）。
- **底层 API**（均 `tenant_access_token`，已实测）：

  | 步骤 | API |
  |---|---|
  | list 子节点（翻页） | `GET /open-apis/wiki/v2/spaces/{space_id}/nodes`（`parent_node_token`/`page_size`≤50/`page_token`；⚠️ 权限过滤下可能空 items+`has_more=true`，须按 `has_more` 循环） |
  | 建节点 | `POST /open-apis/wiki/v2/spaces/{space_id}/nodes`（`obj_type=docx`/`node_type=origin`/`parent_node_token?`/`title`） |
  | 按 token 直查 | `GET /open-apis/wiki/v2/spaces/get_node?token={node_token}` |
  | 挂 docx 进 wiki（异步） | `POST /open-apis/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki`（`obj_type`/`obj_token`/`parent_wiki_token?`/`apply?` → 返回 `data.wiki_token`(同步) 或 `data.task_id`(异步)） |
  | 轮询 move 任务 | `GET /open-apis/wiki/v2/tasks/{task_id}?task_type=move` → `data.task.move_result[].status`（0 成功 / 1 进行中 / -1 失败）+ `node.node_token` |
  | 刷新删旧 docx | `DELETE /open-apis/drive/v1/files/{obj_token}?type=docx`（进回收站、非永久删） |

- **权限对照表**（user vs bot 身份能力边界，实测）：

  | 能力 | `tenant_access_token`（bot/本脚本） | `user_access_token`（人） |
  |---|---|---|
  | 建 wiki space | ❌ 建不出（演进②） | ✅ |
  | 设/转 space 管理员(owner) | ❌ 无转移 API | ✅ 创建者即管理员 |
  | space 内建节点 / 挂文档 / move | ✅（须先被加为可编辑成员） | ✅ |
  | 删节点对应 docx | ✅（进回收站） | ✅ |

  → **分工**：space 归属/可见性/成员准入 = **user 人工**一次性；节点+文档懒建+归档 = **bot 运行时**。**跨 app 多项目写同一 space** 需各 app 各自被加为成员；**新 app 首跑前先验成员资格**——用该 app 凭据对目标 space 只读 list 一次（未加成员时报无权限 / 空结果即暴露，验过再正式归档），或飞书端 space 成员列表人工核（孤儿节点 `reconcile` 对账暂未实现）。

- **私有性（user 自管，脚本不强制）**：space 可见性由 **user 在飞书侧自管**，脚本**不强制设私有**（无 `user_access_token` 不动 space 级配置）。建议**建 team 空间后用一个非成员账号验一次访问被拒**（负向验收），确认私有边界符合预期再放敏感内容。
- **方法论契合**：与 `to-doc` 同 —— repo `.md` 仍是 source of truth，wiki 只是镜像；**单向镜像**，评论 fold-back 可用 `read-comments` 子命令半自动回捞（§5.4），采纳与否仍人工判断。

### 5.4 回捞飞书评论到本地（`read-comments` 子命令）

`to-wiki` 单向镜像后，user 常在飞书文档**评论**里逐点 review。`read-comments` 把这些评论**回捞**回本地 `.md`（fold-back），免手写脚本调 API。

```bash
python3 feishu-md2doc.py read-comments \
  --project <project> --config <path/to/config.toml> \
  --md <已 to-wiki 归档过的 plan.md> \
  [--write] [--include-resolved]
```

- **定位**：靠该 `.md` 旁 `*.wiki.json` sidecar 的 `wiki_node_token` → `get_node` 取 docx `obj_token` → `GET /open-apis/drive/v1/files/{obj_token}/comments?file_type=docx`。**前置 = 该 md 先经 `to-wiki` 归档过**（有 sidecar），否则 die 提示。
- **权限**：同 `to-wiki`（应用身份 `drive:drive` 已发版）；纯**只读**拉评论、`--write` 只动本地 md，不删/改飞书侧。
- **默认**：打印可读 digest 到 stdout（每条 = 解决态 + 锚定引文 quote + 各 reply 正文/作者/时间；正文按 `text_run` 过滤）；**只出未解决**评论。`--include-resolved` 才含已解决。
- **`--write`**：digest 幂等写回 md 末尾 `## 飞书评论回捞（<ts>）` 段（已存在则整段替换、不堆积；⚠️ 该段总在文末每次重生，勿在其后手写正文）。
- **配套 `to-wiki --force` 自动存档**：`--force` 刷新删旧 docx 前 best-effort 把评论 dump 到 `<md>.comments-<ts>.json`（现状删 docx 进回收站连评论一起丢）；拉取/写盘失败只 WARN 不阻断刷新。⚠️ dump 含评论作者 open_id 等团队内部句柄，**同 `*.wiki.json` 建议 gitignore**（加 `*.comments-*.json` 规则，不入公开仓）。
- **离线自检**：`python3 feishu-md2doc.py selftest`（`format_comments` known-answer probe，不触网）。
- **方法论契合**：仍是**单向镜像 + 人工 fold-back**——`read-comments` 只把「手动逐条抄回」自动成「拉取 + 打印/写回」，采纳与否、怎么改 `.md` 仍由人判断（repo `.md` = source of truth）。

---

## 6. 远程仓监控 → 主动通知（cron + 一个脚本，有变化才推送）

前面几节是「人发消息 → bot 答」（**入站、被动**）。这一节反过来：让 cc-connect 的 **cron** 定时跑一个 bash 脚本，盯住**远程 git 仓库**的 push 与 版本(tag) 发布，**仅当有新变化时**把简报**主动 send** 到一个专门的飞书「通知群」。无变化则全程静默（不刷屏）。

> 适用：想在手机上「不主动问也能知道远程仓有人 push 了 / 发新版本了」。底层只用 cc-connect 已有的 `cron add --exec` + `send` 两个能力 + 一个自带状态文件的脚本，**无需 webhook / 公网回调**。

### 6.1 机制总览

| 部件 | 作用 |
|---|---|
| **cc-connect cron job**（`cron add --exec` 建 + `cron edit <id> mute true` 静音） | 定时（约每 5 分钟）跑脚本；开 **mute** 后吞掉「无输出」时 cron 自己的运行 banner → 没变化时群里**零打扰**。⚠️ mute 是建好后用 `cron edit` 设的，`cron add` **没有** add-time 的 mute flag；`--silent` 只抑制 job *启动*那一次通知、**不等于**逐次运行静音 |
| **一个 bash 脚本** | `git fetch` → 对比自带状态文件 → 仅有变化时 `cc-connect send` 推简报；stdout 仅用于 cron banner（已被 mute 吞掉） |
| **两个状态文件** | `state-refs.txt`（上次已通知的各分支 SHA）+ `state-tags.txt`（上次已通知的各 tag），**与 git 实际 refs 解耦** |
| **通知群**（飞书） | 专收监控简报，和日常开发交互群分开（§6.6） |

**为什么状态文件而非直接看 git refs**：本机别的进程（交互 session、collab worktree）可能也在 `git fetch`，会让本地 `origin/*` 随时前进。若直接拿「本地 ref 变没变」判断，会漏报/错报。脚本维护**自己的**「上次已通知 SHA」基线，因此**对每次远程前进恰好通知一次**，与谁在 fetch 无关。

### 6.2 脚本主逻辑（分支 push）

每个 tick：

1. `git fetch origin --prune --tags --quiet` —— 失败（网络抖动）则**静默退出**、写 `last-run.txt=fetch-failed`、下一 tick 重试（不报错刷屏）。
2. 取当前快照：`git for-each-ref --format='%(refname:short) %(objectname)' refs/remotes/origin`，逐分支与 `state-refs.txt` 比对：
   - state 里没有该分支 → 🌱 **新分支**（附最近几条 commit）
   - SHA 变了 → 🔔 **前进**（commit 列表用 `git log <old>..<new>`、文件统计用 `git diff --shortstat <old> <new>` 两参形式；commit 多则截断「…及更多 N 个」）
3. 反向扫 state：在 state 但当前快照没有的 → 🗑️ **远程分支已删除**。
4. **无论有无变化都刷新 `state-refs.txt`**（写回当前快照），避免下次重复通知。
5. 有变化才拼简报 `cc-connect send` 出去；`CHANGES=0` 则什么都不发。

> 🔑 **关键坑①（裸 `origin` 行）**：`origin/HEAD` 是符号引用，它的 `%(refname:short)` 会渲染成**裸的 `origin`**（不是 `origin/HEAD`）。若不过滤，默认分支每次 push 都会被当成「`origin` 这条 ref 变了」**重复通知一次**。必须反向过滤：
>
> ```bash
> | grep -vE '^origin(/HEAD)? '   # 同时挡裸 origin 与 origin/HEAD
> ```

可选：脚本顶部留一个 `EXCLUDE_RE`（ERE，匹配 `origin/<branch> ` 行首）排除不想被通知的分支；置空 = 监控全部。

### 6.3 版本(tag) 监控（隔离的 tag 块，置顶 🚀）

同一个脚本里**另起一段隔离的 tag 块**——刻意**不碰上面的分支逻辑**，保持回归隔离（分支逻辑已跑通，发版监控是后加挂的，互不影响）。

- 用 **`git ls-remote --tags origin`** 取 tag（**只追已 push 到 origin 的 tag = 已发布**），而非本地 `git tag -l`：避免本地打了还没推的 tag 被误报成「发版了」。
  - 记得 `grep -v '\^{}$'` 去掉 annotated tag 的解引用行（`<sha> refs/tags/v1^{}`）。
- 与**独立**的 `state-tags.txt` 比对：新 tag → 🚀 **置顶横幅**（tag 名 + annotated message + 指向 commit 短 SHA + 距上一版多少 commit）；同名被 force-retag → 🔄。
- 简报里 **版本块排在普通 push 之上**（发版比日常 push 更值得先看见）。可顺带在分支 push 触及 `CHANGELOG.md` 时打个 `📝` 标记当发版前奏信号。

> 🔑 **关键坑③（ls-remote 失败别清基线）**：把整个 tag 块包在 `if [ -n "$REMOTE_TAGS" ]` 里。`ls-remote` 失败时 `REMOTE_TAGS` 为空 → **整段跳过、不写 `state-tags.txt`**。否则会用「空」覆盖基线，下一 tick 把所有历史 tag 当新版本**集体误报**。

### 6.4 关键坑②：cron 环境里 `send` 报 socket not found

这是最隐蔽的一个，**务必照避**。

- cron 的 `--exec` 进程**继承 daemon 的环境**，而 daemon 环境里**没有** `CC_DATA_DIR` 这个变量 → `cc-connect send` 回退到默认 data-dir（`~/.cc-connect`）→ 找不到 daemon 的 socket → **`socket not found`**。
- 修法：脚本里 `send` **显式传 `--data-dir`** 指向 daemon **实际**的 data 目录（socket 在其下 `run/api.sock`）。用 `ss -xlp | grep api.sock` 查 daemon 真正监听的 socket 路径：

  ```bash
  printf '%s' "$report" | "$CC_CONNECT" send \
    --data-dir "$CC_DATA_DIR_TARGET" \
    --project "$CC_PROJECT_TARGET" --session "$CC_SESSION_TARGET" --stdin
  # 三个 CC_*_TARGET 变量的取值与含义见 §6.7 落地模板的参数块
  ```

> ⚠️ **cross-verify 教训（手动测 ≠ cron 测）**：你在**手动 shell** 里跑 `send` 往往**成功**——因为交互 shell 恰好 source 过 `CC_DATA_DIR`，而 **daemon 没有**。「0 变化的 cron 跑」也测不到 send 路径（根本没触发 send）。所以**必须制造一次真实变化**（push 一个 commit / 临时清掉 state 文件触发 baseline 一条）才能真正暴露 cron 环境下的 send 是否通。对应 systematic-debugging 的 **B-cross-verify-entry**：别拿测试入口（手动 shell）的成功，去断定生产入口（daemon cron）也成功。

### 6.5 可测性 + 观测（不碰线上、不真发飞书也能验）

脚本留两个钩子，让你在**不动线上状态、不真发飞书**的前提下验证逻辑：

| 钩子 | 作用 |
|---|---|
| `DRY_RUN=1` | `send` 只把简报**打印到 stdout**，不真正调 `cc-connect send`（验简报内容/格式） |
| `MONITOR_STATE_DIR=<tmp>`（可覆盖的临时状态目录） | 把状态文件指到临时目录，**不碰线上 `state-refs.txt`/`state-tags.txt` 基线**（验首跑 baseline / 二跑增量） |

典型验证：`MONITOR_STATE_DIR=/tmp/mon-test DRY_RUN=1 bash check-pushes.sh`（首跑建临时基线 + 打印启动确认）→ 再跑一次（无变化 → 静默）。

**观测产物**（每次运行落地，便于事后排障）：

- `last-run.txt` —— 单行心跳：`run-ok · changes=N` / `fetch-failed` / `baseline-established`，一眼看出「上次跑成没、报了几条」。
- `monitor.log` —— 记 `send` 结果与错误（送达成功/失败、fetch 失败时间戳），是查坑②的第一现场。

### 6.6 通知群分流 + 入站收紧（与 §3.1 配合）

- **发到专门「通知群」**，和日常开发交互群分开 —— 否则简报会淹没你跟 bot 的对话。
- **监控是主动 `send`，不经入站 `allow_from`/`allow_chat` 检查**（那俩只管「别人发进来的消息」，见 §3.1）。所以**把通知群的入站权限收紧成只读/不可用，完全不影响监控投递**。推荐：通知群里 bot 入站基本关掉（你只看，不在这群指挥），指挥放到开发交互群。
- 拿群 `chat_id`：飞书 `GET im/v1/chats`（列 bot 所在的群），或给 bot 发 `/whoami` / 在群里看 session key 的 `oc_` 段。`send` target 形如 `feishu:oc_xxx:owner`（发群只认 `chat_id`，`:owner` 这个 user 段会被忽略，占位即可）。

### 6.7 落地模板

1. **写脚本**（含上面全部坑的避法）。⚠️ **本节是手写脚手架的指引（步骤 + 参数约定），skill 不随附现成 `check-pushes.sh`**——§6.2-6.6 的片段加本模板的变量块拼起来就是完整脚本，别在 skill 目录里找 drop-in 文件。放在 repo 外的私有目录，例 `~/.your-monitor/check-pushes.sh`。脚本顶部用环境变量参数化，便于复用与测试：

   ```bash
   REPO="${MONITOR_REPO:-<abs/path/to/repo>}"        # 被监控的本地 clone
   STATE_DIR="${MONITOR_STATE_DIR:-~/.your-monitor}"  # 状态/日志目录（测试可覆盖）
   CC_CONNECT="${CC_CONNECT:-<abs/path/to/cc-connect>}"
   CC_DATA_DIR_TARGET="<daemon-data-dir>"            # ss -xlp 查 daemon socket 所在
   CC_PROJECT_TARGET="<project>"
   CC_SESSION_TARGET="feishu:oc_xxx:owner"           # 通知群 chat_id
   EXCLUDE_RE=''                                      # ERE，置空=监控全部分支
   ```

   被监控的 `REPO` 是一个**本地 clone**，其 `origin` 指向远程（如 `git@github.com:<org>/<repo>`）；daemon 跑在哪个 user 下，就确保那个 user 能无交互 `git fetch`（passphraseless deploy key / 已配 SSH，**别**依赖交互 shell 里临时起的 ssh-agent）。

2. **建 cron job + 开静音**（两步——`cron add` 没有 add-time 的 mute flag）：

   ```bash
   # ① 建 job：周期用 --cron、命令用 --exec（接整条带引号命令，不是 -- bash 尾参）、超时用 --timeout-mins（整数分钟）
   cc-connect cron add --project <project> \
     --cron '*/5 * * * *' --timeout-mins 4 \
     --exec 'bash ~/.your-monitor/check-pushes.sh' \
     --desc '远程仓 push/版本监控' --silent
   # ② cron list 拿到 job id，开 mute 让无变化时逐次运行都静默：
   cc-connect cron edit <job-id> mute true
   ```

   - flag 名对齐 `cc-connect cron add --help` 实证：`--cron`（**不是** `--schedule`）、`--timeout-mins 4`（整数分钟，**不是** `--timeout 4m`）、`--exec '整条命令'`（**不是** `-- bash ...` 尾参）。
   - `--silent` 只抑制 job **启动**那一次通知；**逐次运行的静音靠 `cron edit <id> mute true`**——这才是「无变化零打扰」的关键。

3. **首跑建基线**：第一次跑会写两个 state 文件 + 发**一条启动确认**（「监控已启动 / 当前 N 个分支」「版本监控已加挂 / 当前最新 vX」），**不补报历史** push/tag。之后只报增量。

4. **管理**：`cc-connect cron list` 查；`cron edit <id> enabled false` 暂停；`cron del <id>` 删除。改脚本不需动 cron（cron 只是定时 exec）。

> ⚠️ **在你自己机器的本地 shell 建这个 job**（直接跑 `cc-connect cron add ...`），不要从聊天里发指令建——最稳也最安全。注意区分：本地 CLI 子命令是 **`cron add --exec`**（cc-connect **无** `addexec` 子命令）；聊天里对应的是 admin-gated 的 slash 命令 **`/cron addexec`**（受 `admin_from` 管、默认 blocked，见 §3.1）。两者别混。

---

## 7. 安全 checklist（上线前过一遍）

- [ ] **访问控制按 §3.1 矩阵选定**（默认 🅰️ 锁 owner）；`auto`/`bypass` mode → `allow_from` **严禁 `*`/空**（须具名 id，或 `group_only`+`allow_chat` 锁群）
- [ ] `acceptEdits` mode（自动批文件编辑、Bash 仍逐次问）→ 开放配法（`allow_from=*`）下仍能让人**自动改 work_dir 文件** → **仍建议降 `plan`/`default`**（别因「不跑 Bash」就放心全开，§3.1）
- [ ] 若用开放配法（`allow_from=*`）：已确认 bot 用**企业 token**（非私人订阅薅额度）+ mode 已降 `plan`/`default`
- [ ] `config.toml` `chmod 600` + 放 repo 外（含 token，**绝不提交**）
- [ ] `auto`/`bypassPermissions` mode 已知会**自动跑 bash**，work_dir + 白名单确认无误
- [ ] 微信个人号方案已知 ToS 灰色 / 封号风险，自负
- [ ] **（若启用 §6 远程仓监控）** 用**专门通知群**、与开发交互群分开；监控是主动 send 不经入站闸，故通知群入站权限可收紧成只读、不影响投递
- [ ] **（监控）** cron 脚本的 `cc-connect send` 显式传 `--data-dir <daemon-data-dir>`（cron `--exec` 继承的 daemon 环境无 `CC_DATA_DIR`，否则 `socket not found`），且**制造一次真实变化在 cron 环境实测 send**（手动 shell 测成功 ≠ cron 成功）
- [ ] **（监控）** 建监控 job 在**本机本地 shell** 跑 `cc-connect cron add --exec ...`（不从聊天发指令建；聊天侧 `/cron addexec` 是 admin-gated）

---

## 署名 / license

- **cc-connect** —— MIT，https://github.com/chenhg5/cc-connect （底层桥接工具）
- **feishu-md2doc.py** —— 原创（本仓），调用飞书开放平台公开 API，依赖 `requests`（Apache-2.0）
