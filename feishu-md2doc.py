#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu-md2doc.py — markdown 文件 → 飞书云文档(docx) / 飞书知识库(wiki) 节点。

cc-connect-phone-bridge skill 的配套脚本（原创）。配合 cc-connect 使用：当 bot
产出长 plan / 设计稿（落 repo 的 .md）时，把它镜像成飞书云内容，repo 里的 .md 仍是
source of truth，飞书侧只是审阅镜像。两种子命令：

  to-doc   —— 把 .md 镜像成一篇独立飞书云文档(docx)，按需共享、打印链接（旧默认行为）。
  to-wiki  —— 把 .md 归档进飞书知识库(wiki) space 的节点树（懒建路径节点 + 挂文档 +
             sidecar 幂等映射），用于知识库长期沉淀。

依赖：Python 3.11+（tomllib 内置）+ requests（Apache-2.0）。仅调用飞书开放平台公开 API。

用法：
  feishu-md2doc.py to-doc  --project <project> --md <plan.md> [--title "..."] [--perm edit]
  feishu-md2doc.py to-wiki --project <project> --md <plan.md> --space-id <id> \\
                   --path "项目/<proj>/<类别>" [--force]
  # 兼容：不带子命令 = to-doc（旧脚本行为，保留以免破坏既有调用）

凭证来源：读 cc-connect 的 config.toml 里该 <project> 的 feishu 平台 app_id / app_secret，
reviewer 取该平台 allow_from（逗号分隔的 open_id）。

前置（一次性，飞书开放平台）：
  - to-doc：给应用加 **应用身份** drive:drive 权限（实测 import 建 docx 仅需此、不需
            docx:document——脚本无 docx/v1 调用），**发布新版本**
            （只加权限不发版 / 只加「用户身份」都无效，会报 99991672）。
  - to-wiki：在 to-doc 前置之上**再加** wiki:wiki 权限并**再发版**；且 space 必须由
            user 人工建（team 类型），并把本应用 open_id 加为**可编辑成员**（bot 应用身份
            建不出 space）。

⚠️ 通知：本脚本用 **应用身份(tenant_access_token)**，飞书的 need_notification 参数
**仅 user_access_token 调用时有效**（官方文档）→ 共享不会自动通知 reviewer，请把打印的链接
手动发给对方（或经 cc-connect 把链接回到聊天）。
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
import requests

DEFAULT_CONFIG = os.path.expanduser("~/cc-connect/config.toml")

# F1（major）哨兵：adopt 现存同名 leaf（无 sidecar 佐证内容一致）时写进 sidecar 的占位
# content_hash。adopt 复用的是【旧】wiki 节点、未重导【新】.md 内容 → 绝不能写当前真 hash
# （会让下轮文档级幂等 `sidecar.content_hash == content_hash` 误判一致 → silent skip + exit 0
# → 陈旧内容永不刷新）。写哨兵使下轮比对「哨兵 != 真 hash」必不 skip → 走「内容已变需 --force」
# die，user --force 删旧建新写真 hash 收敛（与 major-3 可重入初衷一致）。
_ADOPT_UNVERIFIED = "<unverified-adopt-needs-force>"


def die(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


class FeishuError(Exception):
    """飞书 API 传输层 / 响应解析异常（区别于业务 code != 0，后者由调用方按需 die）。"""


# 高频业务码 → 中文 hint（list/create/move/poll 的 die / raise 文案附上，便于排障）。
# 仅覆盖 wiki/drive 归档链路常撞的码；未列出的码原样透传 d。
_FEISHU_CODE_HINTS = {
    131005: "节点/资源不存在（not-found；token 失效或已删）",
    131006: "非空间成员或对该节点无权限（先把本应用 open_id 加为空间可编辑成员）",
    131003: "操作超限（限频/任务未就绪，稍后重试）",
    99991672: ("应用尚未开通所需的【应用身份】权限或未发版（加 wiki:wiki/drive:drive 后须创建版本"
               "→发布）。⚠️ 飞书搜 drive:drive 会出两条，务必开【应用身份】而非【用户身份】"
               "（开错身份发版后仍报本码）；可跑 `feishu-md2doc.py check --project <p>` 端到端自检"),
}


def _explain_feishu_code(code) -> str:
    """把高频飞书业务码映射成中文 hint；未知码返回空串。"""
    try:
        return _FEISHU_CODE_HINTS.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def _request(method: str, url: str, **kwargs) -> dict:
    """单次 HTTP + JSON 解析的统一入口。

    网络异常（连不上 / 超时 / 连接重置）和「响应不是 JSON」（如 502/网关返回 HTML）
    都抛 FeishuError（带方法+url+状态码+正文片段），避免裸 traceback。
    业务错误（飞书返回 code != 0）不在此判断，留给各调用方按语义 die / 容忍。
    """
    kwargs.setdefault("timeout", 30)
    try:
        r = requests.request(method, url, **kwargs)
    except requests.exceptions.RequestException as e:
        raise FeishuError(f"网络请求失败 [{method} {url}]: {e}") from e
    try:
        return r.json()
    except ValueError as e:
        body = (r.text or "").strip()[:200]
        raise FeishuError(f"响应非 JSON [HTTP {r.status_code}] {url}: {body!r}") from e


def load_feishu_creds(config_path: str, project: str):
    """从 config.toml 取指定 project 的 feishu app_id / app_secret / allow_from。"""
    try:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        die(f"读不了 config: {config_path}: {e}")

    for proj in cfg.get("projects", []):
        if proj.get("name") != project:
            continue
        for plat in proj.get("platforms", []):
            if plat.get("type") in ("feishu", "lark"):
                opt = plat.get("options", {})
                app_id = (opt.get("app_id") or "").strip()
                app_secret = (opt.get("app_secret") or "").strip()
                reviewers = [s.strip() for s in (opt.get("allow_from") or "").split(",")
                             if s.strip() and s.strip() != "*"]
                if not app_id or not app_secret:
                    die(f"project '{project}' 的 feishu 平台没有 app_id/app_secret")
                return app_id, app_secret, reviewers
        die(f"project '{project}' 没有 feishu/lark 平台")
    die(f"config 里找不到 project '{project}'")


class Feishu:
    def __init__(self, app_id: str, app_secret: str, host: str):
        self.base = f"https://{host}"
        self.app_id = app_id
        self.app_secret = app_secret
        self.token = self._tenant_token()
        self.h = {"Authorization": f"Bearer {self.token}"}

    def _tenant_token(self) -> str:
        d = _request(
            "POST",
            f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        if d.get("code") != 0:
            die(f"取 tenant_access_token 失败: {d}")
        return d["tenant_access_token"]

    def upload_md(self, md_path: str, file_name: str) -> str:
        """medias/upload_all 上传 .md，返回 file_token。"""
        size = os.path.getsize(md_path)
        with open(md_path, "rb") as fh:
            d = _request(
                "POST",
                f"{self.base}/open-apis/drive/v1/medias/upload_all",
                headers=self.h,
                data={
                    "file_name": file_name,
                    "parent_type": "ccm_import_open",
                    "size": str(size),
                    # 关键：import 上传必带 extra，否则 1061004 forbidden
                    "extra": json.dumps({"obj_type": "docx", "file_extension": "md"}),
                },
                files={"file": (file_name, fh, "text/markdown")},
                timeout=120,
            )
        if d.get("code") != 0:
            die(f"上传 .md 失败（检查应用是否有【应用身份】drive:drive 权限且已发版）: {d}")
        return d["data"]["file_token"]

    def import_to_docx(self, file_token: str, title: str, mount_key: str = "") -> str:
        """import_tasks 把 md file_token 转成 docx，返回 ticket。"""
        d = _request(
            "POST",
            f"{self.base}/open-apis/drive/v1/import_tasks",
            headers=self.h,
            json={
                "file_extension": "md",
                "file_token": file_token,
                "type": "docx",
                "file_name": title,
                "point": {"mount_type": 1, "mount_key": mount_key},
            },
            timeout=30,
        )
        if d.get("code") != 0:
            die(f"创建导入任务失败: {d}")
        return d["data"]["ticket"]

    def poll_import(self, ticket: str, tries: int = 40, interval: float = 1.5):
        """轮询 import 结果，成功返回 (docx_token, url)。

        单次轮询的网络抖动 / 网关错误不丢已提交的导入任务：捕获后 sleep 重试，
        只有重试预算耗尽才放弃。job_status 的业务错误仍立即 die。
        """
        transient = 0
        for _ in range(tries):
            try:
                d = _request(
                    "GET",
                    f"{self.base}/open-apis/drive/v1/import_tasks/{ticket}",
                    headers=self.h,
                    timeout=30,
                )
            except FeishuError as e:
                transient += 1
                print(f"… 轮询网络异常（第 {transient} 次），稍后重试: {e}", file=sys.stderr)
                time.sleep(interval)
                continue
            if d.get("code") != 0:
                die(f"查询导入结果失败: {d}")
            res = d["data"]["result"]
            status = res.get("job_status")
            if status == 0:
                return res["token"], res.get("url", "")
            if status in (1, 2):  # 进行中
                time.sleep(interval)
                continue
            die(f"导入失败 job_status={status}: {res.get('job_error_msg', '')}")
        die("导入超时（轮询多次仍未完成，含网络重试）")

    def share(self, docx_token: str, member_id: str, perm: str,
              member_type: str = "openid") -> bool:
        """把 docx 共享给某个成员（member_type: openid=单人 / openchat=整个群）。

        单个成员的网络/业务失败只 warn + 返回 False，不拖垮整体（其余成员
        与已生成的链接照常）。注意：应用身份共享不会自动通知对方，链接需手动发。
        member_type="openchat" + member_id=open_chat_id(oc_xxx) → 群里所有人(含日后新进群的)获得权限。
        """
        url = f"{self.base}/open-apis/drive/v1/permissions/{docx_token}/members?type=docx"
        try:
            d = _request(
                "POST", url, headers=self.h,
                json={"member_type": member_type, "member_id": member_id, "perm": perm},
                timeout=30,
            )
        except FeishuError as e:
            print(f"⚠️  共享给 {member_id} 网络异常: {e}", file=sys.stderr)
            return False
        # 1062506 = 已是协作者（实测：幂等重复共享返回此码），视为成功；其余报错
        if d.get("code") not in (0, 1062506):
            print(f"⚠️  共享给 {member_id} 失败: {d}", file=sys.stderr)
            return False
        return True

    def set_public(self, docx_token: str, link_share_entity: str,
                   comment_entity: str = "anyone_can_view") -> bool:
        """显式设「链接分享范围 + 评论权限」——不依赖企业默认（换企业/管理员改默认也稳）。

        link_share_entity: tenant_readable(企业内可阅读) / anyone_readable(互联网可阅读)
                           / closed(关闭链接分享，仅具名协作者)
        comment_entity:    anyone_can_view(可阅读者可评论) / anyone_can_edit(仅可编辑者评论)
        实测(2026-06)：tenant_readable + anyone_can_view = 企业内人员都可见且可评论。
        """
        url = f"{self.base}/open-apis/drive/v1/permissions/{docx_token}/public?type=docx"
        try:
            d = _request(
                "PATCH", url, headers=self.h,
                json={"link_share_entity": link_share_entity, "comment_entity": comment_entity},
                timeout=30,
            )
        except FeishuError as e:
            print(f"⚠️  设 public 权限网络异常: {e}", file=sys.stderr)
            return False
        if d.get("code") != 0:
            print(f"⚠️  设 public 权限失败: {d}", file=sys.stderr)
            return False
        return True

    # ──────────────────────────────────────────────────────────────────
    # Wiki（知识库）能力 —— 节点懒建 + 把 docx 挂进节点树
    # 接口前缀统一 /open-apis/wiki/v2/。全程 tenant_access_token（应用身份）。
    # ──────────────────────────────────────────────────────────────────

    def list_wiki_nodes(self, space_id: str, parent_node_token: str = ""):
        """列空间某父节点下的直接子节点（全量翻页），返回 node dict 列表。

        GET /open-apis/wiki/v2/spaces/{space_id}/nodes
          query: parent_node_token(可选,空=空间根) / page_size(≤50) / page_token
        翻页要点（官方实证）：app 因权限过滤可能返回**空 items 但 has_more=true** →
        必须按 has_more 循环，**不能**因某页 items 为空就停。
        """
        items, page_token = [], ""
        while True:
            params = {"page_size": 50}
            if parent_node_token:
                params["parent_node_token"] = parent_node_token
            if page_token:
                params["page_token"] = page_token
            d = _request(
                "GET",
                f"{self.base}/open-apis/wiki/v2/spaces/{space_id}/nodes",
                headers=self.h, params=params, timeout=30,
            )
            if d.get("code") != 0:
                hint = _explain_feishu_code(d.get("code"))
                die(f"列 wiki 节点失败（检查应用是否有 wiki:wiki 权限且已发版、"
                    f"且是空间成员）: {d}" + (f"（{hint}）" if hint else ""))
            data = d.get("data") or {}
            items.extend(data.get("items") or [])
            if data.get("has_more"):
                if data.get("page_token"):
                    page_token = data["page_token"]
                    continue
                # minor 修复：has_more=true 但缺 page_token → 无法取下一页 → fail-loud。
                # 旧实现会 silent 提前 return（按不完整列表去重 → 可能漏掉同名节点 →
                # 误判 0 个而新建 → 堆重复）。宁可 die 也不按残缺列表决策。
                die("列 wiki 节点：has_more=true 但缺 page_token，拒绝按不完整列表去重"
                    "（无法翻下一页，去重会漏判 → 可能堆重复节点）。请重试或排查权限。")
            return items

    def get_wiki_node(self, node_token: str):
        """按 node_token 直查节点（稳定主键），返回 node dict；**仅真 not-found(131005) 返回 None**。

        GET /open-apis/wiki/v2/spaces/get_node  query: token=<node_token>
        （注意官方此 path 不含 space_id，token 即主键。）用于 sidecar 缓存命中后免遍历。

        ⚠️ major-1 修复：旧实现把**任何** code!=0 当「节点不存在 → None」，会把
        99991672(权限未发版)/131006(非成员)/token 过期/5xx 等也静默吞成 None →
        幂等校验误判「旧节点失效」堆重复文档、delete 误判「已删」漏删旧 docx。
        现只对 **131005(真 not-found)** 返回 None；code==0 返 node；其余非零码 raise，
        让调用方（cmd_to_wiki / delete_wiki_obj）感知到「查不出 ≠ 不存在」。
        """
        d = _request(
            "GET",
            f"{self.base}/open-apis/wiki/v2/spaces/get_node",
            headers=self.h, params={"token": node_token}, timeout=30,
        )
        code = d.get("code")
        if code == 0:
            return (d.get("data") or {}).get("node")
        if code == 131005:
            # 真 not-found：节点被删 / token 失效 → 返回 None，让调用方按名重建
            return None
        # 其余非零码（权限/非成员/限频/服务端错…）不能当 not-found，必须暴露
        hint = _explain_feishu_code(code)
        raise FeishuError(f"get_node 异常: {d}" + (f"（{hint}）" if hint else ""))

    def create_wiki_node(self, space_id: str, title: str,
                         parent_node_token: str = "", obj_type: str = "docx") -> dict:
        """在空间建一个原始(origin)节点，返回 node dict（含 node_token / obj_token）。

        POST /open-apis/wiki/v2/spaces/{space_id}/nodes
          body: obj_type(docx/sheet/...) / node_type=origin / parent_node_token(可选) / title
        建出来即一篇空 docx 挂在节点上（中间分类层=索引 docx，W-2）。
        """
        body = {"obj_type": obj_type, "node_type": "origin", "title": title}
        if parent_node_token:
            body["parent_node_token"] = parent_node_token
        d = _request(
            "POST",
            f"{self.base}/open-apis/wiki/v2/spaces/{space_id}/nodes",
            headers=self.h, json=body, timeout=30,
        )
        if d.get("code") != 0:
            hint = _explain_feishu_code(d.get("code"))
            die(f"建 wiki 节点 '{title}' 失败: {d}" + (f"（{hint}）" if hint else ""))
        node = (d.get("data") or {}).get("node")
        if not node or not node.get("node_token"):
            die(f"建 wiki 节点 '{title}' 返回结构异常: {d}")
        return node

    def ensure_wiki_path(self, space_id: str, path_parts: list) -> str:
        """沿 path_parts 逐层 ensure 分类节点（懒建），返回最末层节点的 node_token。

        每层：list 父下同名子节点 → 计数 0 建 / 1 复用 / **≥2 视为 drift 异常 STOP**
        （不静默取第一个——可能是人/别的流程误建重名，需 user 介入）。
        path_parts 形如 ["项目","<proj>","<类别>"]；空列表=直接挂空间根（返回 ""）。
        """
        parent = ""  # 空 = 空间根
        for depth, name in enumerate(path_parts):
            name = name.strip()
            if not name:
                continue
            siblings = self.list_wiki_nodes(space_id, parent)
            matched = [n for n in siblings if (n.get("title") or "").strip() == name]
            if len(matched) >= 2:
                tokens = [n.get("node_token") for n in matched]
                die(f"wiki 路径漂移：父节点 {parent or '(根)'} 下有 {len(matched)} 个同名 "
                    f"'{name}' 节点 {tokens} —— 不静默取第一个，请人工确认后清理。"
                    f"（同名 ≥2 STOP 规则）")
            if matched:
                parent = matched[0]["node_token"]
                print(f"… wiki 路径层 [{depth}] '{name}' 复用已有节点 {parent}",
                      file=sys.stderr)
            else:
                node = self.create_wiki_node(space_id, name, parent)
                parent = node["node_token"]
                print(f"… wiki 路径层 [{depth}] '{name}' 新建节点 {parent}",
                      file=sys.stderr)
        return parent

    def find_leaf_node_by_title(self, space_id: str, parent_node_token: str, title: str):
        """在目标父节点下按 title 完全相同找现存 leaf 文档节点（major-3 半截链去重）。

        返回 (node | None, count)：count=同名数。调用方按 0/1/≥2 决策：
          0 → 新建；1 → 复用该 node（再按 content_hash 决定 skip / --force 刷新）；
          ≥2 → 与路径层同规则 STOP（不静默取第一个，重名需人工清理）。

        为什么需要：sidecar 写在链路最末，若 move 成功后、write_sidecar 前中断，重跑时
        leaf 没有 sidecar 保护 → 旧逻辑直接 upload/import/move 又建一份同名 leaf（堆第二份，
        违背「二次归档不重复节点」设计）。ensure_wiki_path 只 dedup 路径分类层、不管 leaf，
        故把「先 list 同名 → 0建/1复用/≥2STOP」同样用在 leaf 文档节点上。
        """
        siblings = self.list_wiki_nodes(space_id, parent_node_token)
        title_norm = (title or "").strip()
        matched = [n for n in siblings if (n.get("title") or "").strip() == title_norm]
        if len(matched) >= 2:
            tokens = [n.get("node_token") for n in matched]
            die(f"wiki leaf 漂移：父节点 {parent_node_token or '(根)'} 下有 {len(matched)} 个"
                f"同名文档 '{title_norm}' 节点 {tokens} —— 不静默取第一个，请人工确认后清理。"
                f"（同名 ≥2 STOP 规则，leaf 层同路径层）")
        if matched:
            return matched[0], 1
        return None, 0

    def move_docs_to_wiki(self, space_id: str, obj_token: str,
                          parent_wiki_token: str = "", obj_type: str = "docx"):
        """把一篇 drive 上的 docx 挂进 wiki 空间的父节点下。

        POST /open-apis/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki
          body: obj_type(必填) / obj_token(必填) / parent_wiki_token(可选,空=空间根) /
                apply(可选,无权限时是否申请)
        异步：返回 (wiki_token, task_id) —— 同步完成给 wiki_token、否则给 task_id 去轮询。
        ⚠️ 仅文档 owner 可发起；docx 由本应用(tenant)建 → 本应用即 owner，故应用身份可调。
        （API 已核实：open.feishu.cn 与 larksuite 两域一致，2026-06。）
        """
        body = {"obj_type": obj_type, "obj_token": obj_token}
        if parent_wiki_token:
            body["parent_wiki_token"] = parent_wiki_token
        d = _request(
            "POST",
            f"{self.base}/open-apis/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki",
            headers=self.h, json=body, timeout=30,
        )
        if d.get("code") != 0:
            hint = _explain_feishu_code(d.get("code"))
            die(f"move_docs_to_wiki 失败: {d}" + (f"（{hint}）" if hint else ""))
        data = d.get("data") or {}
        return data.get("wiki_token", ""), data.get("task_id", "")

    def poll_wiki_task(self, task_id: str, tries: int = 40, interval: float = 2.0) -> str:
        """轮询 move 异步任务至终态，返回挂好后的 node_token。

        GET /open-apis/wiki/v2/tasks/{task_id}  query: task_type=move
        返回 data.task.move_result[] 每项 {node{...}, status, status_msg}：
          status 0=success / 1=processing / -1=failure（status_msg 给原因）。
        单次轮询网络抖动不丢任务（捕获 FeishuError → sleep 重试，预算耗尽才放弃）。
        minor 修复：interval 调到 ≥2s（异步 move 实测需若干秒，1.5s 太密易撞限频）；
        对**瞬时业务码**（限频 131003 / 任务未就绪）也 sleep 重试而非立即 die。
        """
        # 瞬时业务码：任务刚提交未就绪 / 限频 → 该轮 sleep 重试而非 die
        TRANSIENT_CODES = {131003}
        transient = 0
        for _ in range(tries):
            try:
                d = _request(
                    "GET",
                    f"{self.base}/open-apis/wiki/v2/tasks/{task_id}",
                    headers=self.h, params={"task_type": "move"}, timeout=30,
                )
            except FeishuError as e:
                transient += 1
                print(f"… wiki 任务轮询网络异常（第 {transient} 次），重试: {e}",
                      file=sys.stderr)
                time.sleep(interval)
                continue
            code = d.get("code")
            if code != 0:
                if code in TRANSIENT_CODES:
                    transient += 1
                    hint = _explain_feishu_code(code)
                    print(f"… wiki 任务瞬时业务码 {code}（第 {transient} 次，{hint}），"
                          f"sleep 重试", file=sys.stderr)
                    time.sleep(interval)
                    continue
                hint = _explain_feishu_code(code)
                die(f"查 wiki 任务结果失败: {d}" + (f"（{hint}）" if hint else ""))
            results = ((d.get("data") or {}).get("task") or {}).get("move_result") or []
            if not results:
                time.sleep(interval)
                continue
            r = results[0]
            status = r.get("status")
            if status == 1:  # processing
                time.sleep(interval)
                continue
            if status == 0:  # success
                node = r.get("node") or {}
                token = node.get("node_token")
                if not token:
                    die(f"wiki 任务成功但缺 node_token: {r}")
                return token
            die(f"move_docs_to_wiki 任务失败 status={status}: {r.get('status_msg', '')}")
        die("move_docs_to_wiki 轮询超时（含网络重试仍未到终态）")

    def delete_wiki_obj(self, node_token: str) -> bool:
        """删 wiki 节点对应的实体 docx（删源 docx → 节点随之失效）。仅 --force 刷新调用。

        wiki v2 **无公开「删节点」API**（只有 list/get/create/move/update_title）；删一篇
        wiki 文档的官方路径 = 删其源 docx：
          DELETE /open-apis/drive/v1/files/{obj_token}?type={obj_type}
        ✅ 已核实：飞书删文件**进回收站、非永久删**（可恢复），type 合法值含 docx。
        飞书 import 不支持覆盖，故刷新只能删旧建新。删失败返回 False，
        调用方据此停止「建新」避免重复堆积（major-2）。
        ⚠️ 仍属改 user 飞书数据 → 默认不删，仅 --force 且明确日志（红线）。

        ⚠️ major-1 联动：get_wiki_node 现仅 131005 返 None（真 not-found→确已删，跳过 OK）；
        权限/非成员/服务端错等会 raise FeishuError 上抛 → 调用方按删失败处理（不会再误判
        「旧节点失效」而漏删却以为删了）。
        """
        node = self.get_wiki_node(node_token)  # 非 131005 的异常会上抛，不在此吞
        if not node:
            # 仅当真 not-found(131005) 才到这——旧 docx 确已不在，跳过删除是安全的
            print(f"⚠️  旧节点 {node_token} 已不存在(131005)，跳过删除", file=sys.stderr)
            return True
        obj_token = node.get("obj_token")
        obj_type = node.get("obj_type", "docx")
        if not obj_token:
            print(f"⚠️  旧节点 {node_token} 无 obj_token，无法删源 docx", file=sys.stderr)
            return False
        # 删 drive 上的源 docx → 对应 wiki 节点随之失效（孤儿节点留给 reconcile）
        url = (f"{self.base}/open-apis/drive/v1/files/{obj_token}"
               f"?type={obj_type}")
        try:
            d = _request("DELETE", url, headers=self.h, timeout=30)
        except FeishuError as e:
            print(f"⚠️  删旧 docx {obj_token} 网络异常: {e}", file=sys.stderr)
            return False
        if d.get("code") != 0:
            hint = _explain_feishu_code(d.get("code"))
            print(f"⚠️  删旧 docx {obj_token} 失败: {d}"
                  + (f"（{hint}）" if hint else ""), file=sys.stderr)
            return False
        return True

    def delete_drive_file(self, obj_token: str, obj_type: str = "docx") -> bool:
        """删 drive 上的独立文件（进回收站、可恢复）。cmd_check 清理测试 docx 用。
        DELETE /open-apis/drive/v1/files/{obj_token}?type={obj_type}（同 delete_wiki_obj
        的删除端点，但不经 wiki node 查询——直接对独立 docx_token 操作）。"""
        url = f"{self.base}/open-apis/drive/v1/files/{obj_token}?type={obj_type}"
        try:
            d = _request("DELETE", url, headers=self.h, timeout=30)
        except FeishuError as e:
            print(f"⚠️  删文件 {obj_token} 网络异常: {e}", file=sys.stderr)
            return False
        if d.get("code") != 0:
            hint = _explain_feishu_code(d.get("code"))
            print(f"⚠️  删文件 {obj_token} 失败: {d}"
                  + (f"（{hint}）" if hint else ""), file=sys.stderr)
            return False
        return True

    def get_comments(self, obj_token: str, file_type: str = "docx") -> list:
        """拉云文档全局评论（分页），返回 items（含 is_solved / quote / reply_list）。

        GET /open-apis/drive/v1/files/{obj_token}/comments?file_type=docx
          （workaround 已验通端点；obj_token = wiki node 的 docx obj_token）
        分页按 has_more/page_token 循环；缺 page_token 即停（评论只读回捞、拿到多少算
        多少——比 list 节点去重的 fail-loud 更合适，回捞不完整不会误堆节点）。
        """
        items = []
        page_token = ""
        while True:
            params = {"file_type": file_type, "page_size": 50}
            if page_token:
                params["page_token"] = page_token
            d = _request(
                "GET",
                f"{self.base}/open-apis/drive/v1/files/{obj_token}/comments",
                headers=self.h, params=params, timeout=30,
            )
            if d.get("code") != 0:
                hint = _explain_feishu_code(d.get("code"))
                die(f"拉评论失败 (obj_token={obj_token}): {d}"
                    + (f"（{hint}）" if hint else ""))
            data = d.get("data") or {}
            items.extend(data.get("items") or [])
            if data.get("has_more") and data.get("page_token"):
                page_token = data["page_token"]
                continue
            return items


def derive_title(md_path: str, given):
    if given:
        return given
    # 取首个 markdown 一级标题，否则用文件名
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("# "):
                    return s[2:].strip()
    except Exception:
        pass
    return os.path.splitext(os.path.basename(md_path))[0]


# ── wiki sidecar：repo .md 旁记 wiki_node_token + content_hash（核心设计）──

def md_content_hash(md_path: str) -> str:
    """md 文件内容 sha256（幂等 / 更新判定的指纹）。"""
    h = hashlib.sha256()
    with open(md_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sidecar_path(md_path: str) -> str:
    """同名 .wiki.json sidecar 路径（plan.md → plan.md.wiki.json）。"""
    return md_path + ".wiki.json"


def load_sidecar(md_path: str) -> dict:
    p = sidecar_path(md_path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  sidecar {p} 读取失败（当作无映射）: {e}", file=sys.stderr)
        return {}


def write_sidecar(md_path: str, space_id: str, node_token: str,
                  content_hash: str, path_str: str, title: str = ""):
    """写/更新 sidecar 映射。stored space_id 写真值——sidecar 落 repo 旁，
    space_id 本就需随 .md 走才能再归档定位。"""
    p = sidecar_path(md_path)
    data = {
        "wiki_space_id": space_id,
        "wiki_node_token": node_token,
        "content_hash": content_hash,
        "wiki_path": path_str,
        "wiki_title": title,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "_note": "feishu-md2doc.py to-wiki 生成的归档映射；repo .md 仍是 source of truth。",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"… sidecar 已写 {p}", file=sys.stderr)


def sidecar_drift(sidecar: dict, path_str: str, title: str) -> list:
    """幂等 skip 前比对 sidecar 记录的落点 vs 本次 --path/--title，返回漂移描述列表。

    content_hash 一致只证明【内容】没变，不证明【落点】没变——只比 hash 会把
    「user 想挪节点/改标题」silent skip 成「什么都没做」还 exit 0。
    字段缺失（老 sidecar 无 wiki_title）→ 跳过该项不误报（向后兼容）。
    """
    drifts = []
    stored_path = sidecar.get("wiki_path")
    if stored_path is not None and stored_path != path_str:
        drifts.append(f"path：sidecar 记录 '{stored_path}' ≠ 本次 '{path_str}'")
    stored_title = sidecar.get("wiki_title")
    if stored_title and stored_title != title:
        drifts.append(f"title：sidecar 记录 '{stored_title}' ≠ 本次 '{title}'")
    return drifts


def cmd_to_doc(args):
    """to-doc：markdown → 独立飞书云文档(docx) → 共享 → 打印链接（旧默认行为）。"""
    if not os.path.isfile(args.md):
        die(f"markdown 文件不存在: {args.md}")

    app_id, app_secret, reviewers = load_feishu_creds(args.config, args.project)
    reviewers = list(dict.fromkeys(reviewers + args.reviewer))  # 去重
    title = derive_title(args.md, args.title)
    file_name = f"{title}.md"

    try:
        fs = Feishu(app_id, app_secret, args.host)
        print(f"… 上传 {args.md}", file=sys.stderr)
        file_token = fs.upload_md(args.md, file_name)
        print("… 转换 markdown → docx", file=sys.stderr)
        ticket = fs.import_to_docx(file_token, title)
        docx_token, url = fs.poll_import(ticket)
    except FeishuError as e:
        die(f"飞书 API 调用失败: {e}")
    except (KeyError, TypeError) as e:
        # code==0 但响应结构异常（缺 data/result/token 等）：也别喷裸 traceback
        die(f"飞书 API 响应结构异常（缺字段 / 格式变化）: {e}")
    print(f"… 文档已生成: {docx_token}", file=sys.stderr)

    shared_any = False
    for oid in reviewers:
        if fs.share(docx_token, oid, args.perm):
            shared_any = True
            print(f"… 已共享给 {oid} ({args.perm})", file=sys.stderr)
    for cid in args.reviewer_chat:
        if fs.share(docx_token, cid, "view", member_type="openchat"):
            shared_any = True
            print(f"… 已共享给群 {cid} (view)", file=sys.stderr)

    # 链接分享范围：默认 inherit = 保持企业/管理员默认（尊重各企业策略，不强行统一）。
    # 文档创建时本就继承企业默认，只有显式给定才 PATCH 覆盖。
    if args.link_share == "inherit":
        print("… 链接分享: 保持企业默认（未改动，选择权在管理员）", file=sys.stderr)
    elif fs.set_public(docx_token, args.link_share, "anyone_can_view"):
        scope_cn = {"tenant_readable": "企业内可见+可评论",
                    "anyone_readable": "互联网可见+可评论",
                    "closed": "关闭链接分享（仅具名协作者）"}.get(args.link_share, args.link_share)
        print(f"… 已设链接分享: {scope_cn}", file=sys.stderr)

    if shared_any:
        print("ℹ️  飞书应用身份共享不会自动通知协作者——请把下面链接手动发给对方/群。", file=sys.stderr)

    if not url:
        print(f"⚠️  导入结果未返回 url；docx_token={docx_token}", file=sys.stderr)
    # stdout 只输出链接，方便 bot 直接取用
    print(url or f"(docx token: {docx_token})")


def cmd_to_wiki(args):
    """to-wiki：把 .md 归档进飞书知识库 space 的节点树（懒建 + sidecar 幂等）。

    链路：脱敏 gate → 算 content_hash → 查 sidecar 判幂等/更新 → ensure 路径节点 →
    **leaf 同名去重（major-3 半截链保护）** → upload_md → import_to_docx →
    move_docs_to_wiki → poll → 写 sidecar → 打印 node_token。
    """
    if not os.path.isfile(args.md):
        die(f"markdown 文件不存在: {args.md}")
    if not args.space_id:
        die("to-wiki 必须给 --space-id（space 由 user 人工建、本应用须是可编辑成员；"
            "bot 建不出 space）")

    # 1) content_hash + sidecar 幂等判定 + 先算出 title/path
    content_hash = md_content_hash(args.md)
    sidecar = load_sidecar(args.md)
    path_parts = [p for p in (args.path or "").split("/") if p.strip()]
    path_str = "/".join(path_parts)
    title = derive_title(args.md, args.title)
    if args.wiki_title_prefix:
        title = f"{args.wiki_title_prefix}{title}"

    app_id, app_secret, _ = load_feishu_creds(args.config, args.project)
    try:
        fs = Feishu(app_id, app_secret, args.host)

        # forced_delete_done：走 sidecar --force 删旧路径后置 True，表示「确定要建全新一份」，
        # 后续 leaf 去重不再 adopt（旧节点已进回收站、不会再撞名）。
        forced_delete_done = False

        # 1a) 文档级幂等：sidecar 有 node_token → 直查校验
        old_token = sidecar.get("wiki_node_token")
        if old_token:
            existing = fs.get_wiki_node(old_token)  # 非 131005 异常会上抛（major-1）
            if existing:
                if sidecar.get("content_hash") == content_hash and not args.force:
                    # 内容一致 ≠ 落点一致。--path/--title 变了却 silent skip 会让 user
                    # 误以为节点已迁移/改名。本工具不做自动 move（wiki 节点树内 move +
                    # update_title 是另两个 API，等真实高频需求再上）→ 显式 die 给指引。
                    drifts = sidecar_drift(sidecar, path_str, title)
                    if drifts:
                        die("内容未变但落点参数与 sidecar 记录不一致（"
                            + "；".join(drifts) + "）。本工具当前不自动迁移/改名节点：\n"
                            f"  - 只是重跑确认归档 → 请带 sidecar 记录的原参数重跑；\n"
                            f"  - 真要挪位置/改标题 → 加 --force（删旧 docx 节点 {old_token} "
                            f"进回收站 + 按本次参数重建），或在飞书端手动移动后自行更新 sidecar。")
                    print(f"✓ 内容未变（content_hash 一致）+ 已在 wiki 节点 {old_token}，"
                          f"skip（幂等）。", file=sys.stderr)
                    print(old_token)  # stdout 给 node_token
                    return
                # 内容变了（或 --force）→ 需刷新：删旧建新（飞书 import 不支持覆盖）。
                # ⚠️ 含 F1 场景：上轮是「未验证 adopt」(sidecar.content_hash==_ADOPT_UNVERIFIED) →
                # 真 hash 必不等于哨兵 → 落此分支，提示 --force 确认是否把 .md 刷新到那个 adopt 的
                # 旧节点（而非 silent skip 陈旧）。
                if not args.force:
                    adopted = sidecar.get("content_hash") == _ADOPT_UNVERIFIED
                    reason = ("上轮为「未验证 adopt」(复用了现存同名节点但未导入本 .md 内容)"
                              if adopted else "内容已变（content_hash 不一致）")
                    die(f"{reason}，刷新到 wiki 需删旧 docx 节点 {old_token}（不可逆，"
                        f"进回收站可恢复）→ 请加 --force 明确授权。"
                        f"（红线：删 user 数据要谨慎）")
                print(f"… --force 刷新：删旧 wiki docx 节点 {old_token}（进回收站）",
                      file=sys.stderr)
                # 删旧 docx 前 best-effort 存档评论（否则随 docx 进回收站一起丢）。
                _dump_comments_before_delete(fs, existing.get("obj_token"), args.md)
                # major-2 修复：删旧失败必须停，不能闷头建新（否则旧 docx 没删掉
                # + 新建一份 = 重复堆积，违背幂等）。get_wiki_node 现对权限/服务端错
                # 会 raise（major-1），那些走 FeishuError 上抛；返回 False = 删请求本身
                # 失败（无 obj_token / DELETE code!=0 / 网络异常）。
                if not fs.delete_wiki_obj(old_token):
                    die(f"--force 刷新失败：删旧 docx 节点 {old_token} 未成功，"
                        f"已停止（不建新文档以免与旧的重复堆积）。请人工检查该节点"
                        f"（权限/是否仍存在）后重试。")
                forced_delete_done = True
                # 删成功后继续走新建链路
            else:
                print(f"… sidecar 记的旧节点 {old_token} 已不存在(131005，被删/失效)，"
                      f"重新归档。", file=sys.stderr)

        # 2) ensure 路径节点（懒建；同名 0建/1复用/≥2 STOP）
        if path_parts:
            print(f"… ensure wiki 路径 '{path_str}'（space={args.space_id}）",
                  file=sys.stderr)
            parent_token = fs.ensure_wiki_path(args.space_id, path_parts)
        else:
            parent_token = ""  # 挂空间根
            print("… 未给 --path，文档将挂在空间根", file=sys.stderr)

        # 2b) major-3：leaf 文档节点半截链去重。move 成功但 write_sidecar 前中断 → 重跑时
        # leaf 无 sidecar 保护，旧逻辑会再建一份同名 → 堆第二份。这里在 import/move 之前，
        # 对目标父节点 list 同名 leaf：0→新建 / 1→复用(按 content_hash 决定 skip / --force 刷新)
        # / ≥2→STOP（helper 内 die）。forced_delete_done 时跳过（已删旧、确定要全新一份）。
        if not forced_delete_done:
            leaf, leaf_count = fs.find_leaf_node_by_title(
                args.space_id, parent_token, title)
            if leaf_count == 1:
                leaf_token = leaf.get("node_token")
                stored_hash = sidecar.get("content_hash")
                if stored_hash == content_hash and not args.force:
                    # 真幂等：现存同名 leaf + 内容未变（stored hash 命中）→ skip，顺手补/校 sidecar
                    print(f"✓ 父节点下已有同名文档 leaf {leaf_token} + 内容未变 → "
                          f"skip（幂等）；补写 sidecar 修复半截链。", file=sys.stderr)
                    write_sidecar(args.md, args.space_id, leaf_token, content_hash, path_str, title)
                    print(leaf_token)
                    return
                if not args.force:
                    # 找到同名 leaf 但无法证明内容一致（无 stored hash / hash 不符）：
                    # 复用现存节点（adopt）+ skip 重新 import，避免堆重复（major-3 核心）。
                    # 内容刷新属删旧建新=不可逆，须显式 --force（与 sidecar 路径红线一致）。
                    # F1（major）：adopt 复用的是【旧】wiki 节点、未重导【新】.md 内容 →
                    # sidecar 的 content_hash 必须写哨兵 _ADOPT_UNVERIFIED（**不是**当前真 hash），
                    # 否则下轮文档级幂等会拿「当前真 hash == sidecar 真 hash」误判一致 → silent
                    # skip + exit 0 → 陈旧内容永不刷新。写哨兵 → 下轮比对「哨兵 != 真 hash」必不
                    # skip → 触发「内容已变需 --force」die，user --force 删旧建新写真 hash 收敛。
                    print(f"✓ 父节点下已有同名文档 leaf {leaf_token}（无有效 sidecar 佐证），"
                          f"复用并补写 sidecar 修复半截链；**未重新导入内容**。", file=sys.stderr)
                    print(f"   ↳ sidecar 的 content_hash 标记为「未验证 adopt」（{_ADOPT_UNVERIFIED}）；"
                          f"下轮重跑会因 hash 不符要求 --force 确认是否把 .md 内容刷新到 wiki"
                          f"（删旧 docx 进回收站 + 建新）。", file=sys.stderr)
                    write_sidecar(args.md, args.space_id, leaf_token, _ADOPT_UNVERIFIED, path_str, title)
                    print(leaf_token)
                    return
                # --force：刷新现存同名 leaf → 删其源 docx 后建新（同 sidecar --force 红线）
                print(f"… --force 刷新：删现存同名 leaf {leaf_token} 的源 docx（进回收站）",
                      file=sys.stderr)
                if not fs.delete_wiki_obj(leaf_token):
                    die(f"--force 刷新失败：删现存同名 leaf {leaf_token} 未成功，已停止"
                        f"（不建新以免重复堆积）。请人工检查后重试。")
                # 删成功 → 继续走新建链路
            # leaf_count == 0 → 落到下方正常新建链路

        # 3) upload → import 成 docx（复用既有链路）
        file_name = f"{title}.md"
        print(f"… 上传 {args.md}", file=sys.stderr)
        file_token = fs.upload_md(args.md, file_name)
        print("… 转换 markdown → docx", file=sys.stderr)
        ticket = fs.import_to_docx(file_token, title)
        docx_token, _ = fs.poll_import(ticket)
        print(f"… docx 已生成 {docx_token}，挂进 wiki…", file=sys.stderr)

        # 4) move docx → wiki 节点（异步 → 轮询拿 node_token）
        wiki_token, task_id = fs.move_docs_to_wiki(
            args.space_id, docx_token, parent_token, obj_type="docx")
        if wiki_token:  # 同步即完成
            # ⚠️ major-2 标注：此「move 同步返回 wiki_token」分支**未经真实 e2e**
            # （实测样本均走异步 task_id 路径）。按官方语义 data.wiki_token 即挂好后的
            # node_token 处理；若日后实测发现语义不符（如 wiki_token≠node_token），在此修正。
            node_token = wiki_token
            print(f"… move 同步完成，node_token={node_token}（注：同步分支未经 e2e，按官方语义处理）",
                  file=sys.stderr)
        elif task_id:
            print(f"… move 异步（task_id={task_id}），轮询至终态…", file=sys.stderr)
            node_token = fs.poll_wiki_task(task_id)
        else:
            die("move_docs_to_wiki 既无 wiki_token 也无 task_id（响应异常）")

    except FeishuError as e:
        die(f"飞书 API 调用失败: {e}")
    except (KeyError, TypeError) as e:
        die(f"飞书 API 响应结构异常（缺字段 / 格式变化）: {e}")

    # 5) 写 sidecar 映射（node_token + content_hash）
    write_sidecar(args.md, args.space_id, node_token, content_hash, path_str, title)
    print(f"✓ 已归档到 wiki：path='{path_str or '(根)'}' title='{title}' "
          f"node_token={node_token}", file=sys.stderr)
    # stdout 只输出 node_token，方便 bot / 后续流程取用
    print(node_token)


def _print_perm_guide(host: str):
    """P1：云文档权限开通指引（cmd_check 失败 / 撞 99991672 时打印）。"""
    plat = "open.larksuite.com" if "larksuite" in host else "open.feishu.cn"
    print(
        "\n📋 云文档权限开通指引（飞书开放平台 → 你的应用 → 权限管理）：\n"
        "  1. 搜索并开通【应用身份】权限（⚠️ 同名条目还有【用户身份】，二者极易选错——\n"
        "     本脚本用 tenant_access_token=应用身份，开成用户身份发版后仍报 99991672）：\n"
        "       drive:drive    读写云空间文件 + import 建 docx（to-doc / to-wiki 必需）\n"
        "       wiki:wiki      知识库节点（仅 to-wiki 需要）\n"
        "       （docx:document 仅直接读写 docx 内容才需，本脚本不涉及、可不开）\n"
        "  2. 开通后【创建版本并发布】——只勾权限不发版 = 仍报 99991672。\n"
        "  3. to-wiki 另需把本应用 open_id 加为目标 space 的可编辑成员。\n"
        f"  开放平台：https://{plat}/app\n",
        file=sys.stderr)


# ── 飞书评论回捞（read-comments 子命令的纯逻辑 + 命令）─────────────────────

def extract_reply_text(reply: dict) -> str:
    """从一条 reply 的 content.elements[] 按 text_run 过滤拼纯文本（正文按 text_run）。

    评论正文是富文本 elements 数组，只取 type=='text_run' 的 text_run.text；
    @人 / 文档引用 / 表情等非文本元素跳过（不炸、不留占位）。
    """
    content = reply.get("content") or {}
    parts = []
    for el in content.get("elements") or []:
        if el.get("type") == "text_run":
            parts.append(((el.get("text_run") or {}).get("text")) or "")
    return "".join(parts).strip()


def format_comments(items: list, include_resolved: bool = False) -> str:
    """把 get_comments 的 items 渲染成可读 digest（Markdown）。纯函数、离线可测（selftest 靶）。

    每条 = 解决态 + 锚定引文(quote) + 各 reply（user_id / create_time / 正文）。
    默认只出未解决（is_solved 为假）；include_resolved=True 才含已解决。
    """
    open_items = [c for c in items if include_resolved or not c.get("is_solved")]
    total = len(items)
    shown = len(open_items)
    resolved = sum(1 for c in items if c.get("is_solved"))
    if include_resolved:
        header = f"> 回捞全部 {total} 条评论（已解决 {resolved}）"
    else:
        header = (f"> 回捞 {shown} 条未解决评论"
                  f"（共 {total}，已解决 {resolved} 未显示）")
    lines = [header, ""]
    if not open_items:
        lines.append("_（无待处理评论）_")
        return "\n".join(lines)
    for i, c in enumerate(open_items, 1):
        quote = (c.get("quote") or "").strip()
        solved = "✅已解决" if c.get("is_solved") else "🔲未解决"
        head = f"### {i}. {solved}"
        if quote:
            head += f" · 引文：「{quote}」"
        lines.append(head)
        replies = ((c.get("reply_list") or {}).get("replies")) or []
        if not replies:
            lines.append("_（无正文）_")
        for r in replies:
            txt = extract_reply_text(r)
            who = r.get("user_id") or "?"
            when = r.get("create_time") or ""
            lines.append(f"- **{who}**{f' @{when}' if when else ''}: {txt}")
        lines.append("")
    return "\n".join(lines)


def _write_review_section(md_path: str, digest: str):
    """把 digest 幂等写回 md 末尾「## 飞书评论回捞」段（已存在则从该 heading 起替换到 EOF，
    不重复堆积）。⚠️ 语义 = 回捞段总在文末、每次整段重生；勿在其后手写正文。"""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    heading = "## 飞书评论回捞"
    block = f"{heading}（{ts}）\n\n{digest}\n"
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    if text.startswith(heading):
        new = block
    else:
        idx = text.find(f"\n{heading}")
        base = text[:idx] if idx != -1 else text
        new = base.rstrip("\n") + "\n\n" + block
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(new)


def _dump_comments_before_delete(fs, obj_token: str, md_path: str):
    """to-wiki --force 删旧 docx 前 best-effort 存档评论。

    现状：--force 删 docx 进回收站、评论随之一起丢。删前把评论 dump 到
    <md>.comments-<ts>.json 存档。**best-effort**——拉取/写盘失败只 WARN 不阻断刷新
    （评论随 docx 进回收站本就可恢复，不因附加保险失败卡住主刷新）。
    """
    if not obj_token:
        return
    try:
        items = fs.get_comments(obj_token)
    except (FeishuError, SystemExit) as e:
        print(f"⚠️  --force 删前评论存档：拉取失败（{e}）——跳过存档，"
              f"删除的 docx 及评论仍在回收站可恢复。", file=sys.stderr)
        return
    if not items:
        print("… --force 删前评论存档：该 docx 无评论，跳过。", file=sys.stderr)
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = f"{md_path}.comments-{ts}.json"
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"obj_token": obj_token, "dumped_at": ts, "items": items},
                      f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"… --force 删前评论存档：{len(items)} 条 → {out}", file=sys.stderr)
    except OSError as e:
        print(f"⚠️  --force 删前评论存档写盘失败（{e}）——继续刷新；"
              f"评论仍在回收站可恢复。", file=sys.stderr)


def cmd_read_comments(args):
    """read-comments：把飞书云文档评论回捞回本地 md（fold-back）。

    链路：sidecar(wiki_node_token) → creds/client → get_wiki_node → obj_token →
    get_comments → format → 打印 or --write 回写。纯只读（拉评论）+ --write 只动本地 md，
    不删/不改飞书侧。
    """
    if not os.path.isfile(args.md):
        die(f"markdown 文件不存在: {args.md}")
    sidecar = load_sidecar(args.md)
    node_token = sidecar.get("wiki_node_token")
    if not node_token:
        die(f"{args.md} 无 sidecar 映射（未经 to-wiki 归档？）——read-comments 靠 sidecar "
            f"的 wiki_node_token 定位飞书文档。先 to-wiki 归档，或确认 "
            f"{sidecar_path(args.md)} 存在。")
    app_id, app_secret, _ = load_feishu_creds(args.config, args.project)
    fs = Feishu(app_id, app_secret, args.host)
    node = fs.get_wiki_node(node_token)
    if not node:
        die(f"wiki 节点 {node_token} 已不存在(131005)——文档被删 / token 失效？")
    obj_token = node.get("obj_token")
    if not obj_token:
        die(f"wiki 节点 {node_token} 无 obj_token，无法定位 docx 拉评论。")
    items = fs.get_comments(obj_token)
    digest = format_comments(items, include_resolved=args.include_resolved)
    if args.write:
        _write_review_section(args.md, digest)
        print(f"✓ 已把评论回捞写回 {args.md} 的「飞书评论回捞」段。", file=sys.stderr)
    else:
        print(digest)


def cmd_selftest(args):
    """离线 known-answer probe：canned 评论 fixture → format_comments → 断言
    关键子串。不触网（只测纯逻辑），供发版 gate / CI 冒烟。"""
    passed = 0
    total = 0

    def check(name, cond):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name}", file=sys.stderr)

    fixture = [
        {"comment_id": "c1", "is_solved": False, "quote": "旧的入口约定",
         "reply_list": {"replies": [
             {"user_id": "ou_a", "create_time": "1720000000",
              "content": {"elements": [
                  {"type": "text_run", "text_run": {"text": "这段"}},
                  {"type": "docs_link", "docs_link": {"url": "x"}},
                  {"type": "text_run", "text_run": {"text": "要改成生产入口"}}]}}]}},
        {"comment_id": "c2", "is_solved": True, "quote": "typo",
         "reply_list": {"replies": [
             {"user_id": "ou_b", "create_time": "",
              "content": {"elements": [
                  {"type": "text_run", "text_run": {"text": "已修"}}]}}]}},
    ]
    d1 = format_comments(fixture, include_resolved=False)
    check("text_run 拼接（跳过非文本 element）", "这段要改成生产入口" in d1)
    check("锚定引文(quote)出现", "旧的入口约定" in d1)
    check("默认过滤已解决评论", ("已修" not in d1) and ("typo" not in d1))
    check("未解决计数正确", "1 条未解决" in d1)
    d2 = format_comments(fixture, include_resolved=True)
    check("--include-resolved 含已解决", ("已修" in d2) and ("typo" in d2))
    d3 = format_comments([], include_resolved=False)
    check("空评论不炸 + 计数 0", "0 条未解决" in d3)
    print(f"\nselftest: PASS {passed}/{total}")
    if passed != total:
        sys.exit(1)


def cmd_check(args):
    """check：端到端真实写入探测（建测试 docx → 删），报云文档写入是否就绪。

    ⚠️ 用真实写入端点（实证：只读探针 root_folder/meta 无写权限也返
    code=0=假阳性，唯端到端真实创建作数）。测试产物用完即删（进回收站可恢复）。"""
    import tempfile
    app_id, app_secret, _ = load_feishu_creds(args.config, args.project)
    fs = Feishu(app_id, app_secret, args.host)  # 取 tenant_access_token 失败会 die
    probe = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False,
        prefix="feishu-md2doc-check-", encoding="utf-8")
    probe.write("# feishu-md2doc 权限自检探针\n\n临时探测文档，脚本会自动删除。\n")
    probe.close()
    docx_token = None
    try:
        try:
            ft = fs.upload_md(probe.name, "feishu-md2doc-check-probe.md")
            ticket = fs.import_to_docx(ft, "[自检] feishu-md2doc 权限探针")
            docx_token, _url = fs.poll_import(ticket)
        except (FeishuError, SystemExit) as e:
            # ⚠️ upload_md/import_to_docx/poll_import 的业务码失败用 die()(=SystemExit) 而非
            # raise FeishuError → 必须一并捕获 SystemExit，否则权限不足(99991672)时 P1 指引被
            # 绕过不打印（真机验发现：die 直接 sys.exit，except FeishuError 接不住）。
            _print_perm_guide(args.host)
            if isinstance(e, SystemExit):
                raise  # die 已打印具体 ❌（含飞书返回码）；补完指引后保持其原退出码
            die(f"云文档写入探测失败（{e}）—— 见上方开通指引。")
        print("✓ 云文档写入就绪：drive:drive【应用身份】已开通且发版（upload+import 链路真实 OK）。",
              file=sys.stderr)
        print("ℹ️ 本探测验 to-doc 写入链路；to-wiki 另需 wiki:wiki 权限 + 为目标 space 成员。",
              file=sys.stderr)
    finally:
        try:
            os.unlink(probe.name)
        except OSError:
            pass  # 探针删除失败不掩盖主异常 / 不阻断下方远端清理
        if docx_token:
            if fs.delete_drive_file(docx_token, "docx"):
                print(f"✓ 已清理测试产物 docx={docx_token}（进回收站，可恢复）。",
                      file=sys.stderr)
            else:
                print(f"⚠️ 测试产物 docx={docx_token} 删除失败，请手动到飞书回收站清理。",
                      file=sys.stderr)


def _add_common_args(p):
    """to-doc / to-wiki 共用参数。"""
    p.add_argument("--project", required=True,
                   help="config.toml 里的 project 名（取其 feishu 凭证）")
    p.add_argument("--md", required=True, help="markdown 文件路径")
    p.add_argument("--title", help="文档标题（默认取 md 首个一级标题 / 文件名）")
    p.add_argument("--config", default=DEFAULT_CONFIG,
                   help=f"cc-connect config 路径（常见默认 {DEFAULT_CONFIG}）")
    p.add_argument("--host", default="open.feishu.cn",
                   help="飞书 API host（国际版 Lark: open.larksuite.com）")


def build_parser():
    ap = argparse.ArgumentParser(
        description="markdown → 飞书云文档(docx, to-doc) / 飞书知识库(wiki, to-wiki)")
    sub = ap.add_subparsers(dest="cmd")

    # to-doc（旧默认行为）
    pd = sub.add_parser("to-doc", help="镜像成独立飞书云文档(docx) + 共享 + 打印链接")
    _add_common_args(pd)
    pd.add_argument("--perm", default="edit", choices=["view", "edit", "full_access"],
                    help="共享权限（默认 edit = 完整编辑，含评论/建议）")
    pd.add_argument("--reviewer", action="append", default=[],
                    help="额外具名 reviewer open_id（可多次；默认用 allow_from）")
    pd.add_argument("--reviewer-chat", action="append", default=[],
                    help="把整个群加成 view 协作者（open_chat_id oc_xxx，可多次）")
    pd.add_argument("--link-share", default="inherit",
                    choices=["inherit", "tenant_readable", "anyone_readable", "closed"],
                    help="链接分享范围（默认 inherit = 保持企业/管理员默认、脚本不主动改）")
    pd.set_defaults(func=cmd_to_doc)

    # to-wiki
    pw = sub.add_parser("to-wiki", help="归档进飞书知识库(wiki) space 节点树（懒建+幂等）")
    _add_common_args(pw)
    pw.add_argument("--space-id", required=True,
                    help="目标 wiki 空间 space_id（user 人工建的 team 空间、本应用须是成员；"
                         "持久化到配置/state，不靠名字反查）")
    pw.add_argument("--path", default="",
                    help="空间内归档路径，斜杠分隔（如 '项目/<proj>/<类别>'）；"
                         "逐层懒建分类节点；不给=挂空间根")
    pw.add_argument("--wiki-title-prefix", default="",
                    help="文档节点标题前缀（如自测用 '[测试] ' 便于清理）")
    pw.add_argument("--force", action="store_true",
                    help="内容已变时授权「删旧 docx 节点(进回收站) + 建新」刷新；"
                         "不加则内容变化时拒绝（红线：删 user 数据需明确授权）")
    pw.set_defaults(func=cmd_to_wiki)

    # check（权限自检 — 端到端真实写入探测）
    pc = sub.add_parser(
        "check", help="端到端真实写入探测，报云文档权限是否就绪（建测试 docx→删）")
    pc.add_argument("--project", required=True,
                    help="config.toml 里的 project 名（取其 feishu 凭证）")
    pc.add_argument("--config", default=DEFAULT_CONFIG,
                    help=f"cc-connect config 路径（常见默认 {DEFAULT_CONFIG}）")
    pc.add_argument("--host", default="open.feishu.cn",
                    help="飞书 API host（国际版 Lark: open.larksuite.com）")
    pc.set_defaults(func=cmd_check)

    # read-comments（飞书评论 fold-back 回本地 md）
    prc = sub.add_parser(
        "read-comments",
        help="把飞书云文档评论回捞回本地 md（打印 / --write 写回；靠 to-wiki sidecar 定位）")
    prc.add_argument("--project", required=True,
                     help="config.toml 里的 project 名（取其 feishu 凭证）")
    prc.add_argument("--md", required=True,
                     help="已 to-wiki 归档过的 markdown（读其 .wiki.json sidecar 定位文档）")
    prc.add_argument("--config", default=DEFAULT_CONFIG,
                     help=f"cc-connect config 路径（常见默认 {DEFAULT_CONFIG}）")
    prc.add_argument("--host", default="open.feishu.cn",
                     help="飞书 API host（国际版 Lark: open.larksuite.com）")
    prc.add_argument("--write", action="store_true",
                     help="把回捞的评论写回 md 末尾「飞书评论回捞」段（幂等替换）；默认只打印")
    prc.add_argument("--include-resolved", action="store_true",
                     help="含已解决评论（默认只回捞未解决）")
    prc.set_defaults(func=cmd_read_comments)

    # selftest（离线 known-answer probe，不触网）
    pst = sub.add_parser(
        "selftest", help="离线自检 format_comments 纯逻辑（known-answer，不触网）")
    pst.set_defaults(func=cmd_selftest)

    return ap


def main():
    ap = build_parser()
    # 兼容旧调用：无子命令（或第一个参数是 --xxx）= to-doc，避免破坏既有 bot/脚本调用。
    argv = sys.argv[1:]
    if not argv or (argv[0] not in ("to-doc", "to-wiki", "check", "read-comments",
                                    "selftest", "-h", "--help")
                    and argv[0].startswith("-")):
        argv = ["to-doc"] + argv
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        sys.exit(2)
    args.func(args)


if __name__ == "__main__":
    main()
