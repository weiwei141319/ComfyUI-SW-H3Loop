# -*- coding: utf-8 -*-
"""SW H3 Loop —— 海螺 H3 一采循环长视频专用循环节点包（自研，完全替代官方 StartLoop/EndLoop）

为什么能替代官方循环节点（框架层证据见 sw_h3_loop.py 顶部注释）：
    execution.py:1180 直接读 GET_SCHEMA().loop_boundary 来识别循环边界，不认节点名。
    因此自研节点只要声明 loop_boundary="start"/"end" 即可被当成循环边界。

★ 注册方式必须走 NODE_CLASS_MAPPINGS（本文件曾被这里坑过两次，别再改）：
    ComfyUI custom_nodes 加载器 nodes.py:2294-2338 只有两条路，且**有先后顺序**：
        (1) 模块有 NODE_CLASS_MAPPINGS 且不为 None  —— 经典 API，先判、命中即 return
        (2) else 模块有 comfy_entrypoint            —— 新版 API（V3 Extension）
    官方 comfy_extras/nodes_loop.py 里的 StartLoop/EndLoop/LoopIteration/LoopResult
    全部是 io.ComfyNode 子类，注册方式恰恰就是文件尾部的
        NODE_CLASS_MAPPINGS = {...}
    所以本包沿用同一写法即可，**不提供 comfy_entrypoint**。

    ⚠️ 曾经的坑：本文件早期写的是
        from .sw_h3_loop import SWH3LoopExtension
        def comfy_entrypoint(): return SWH3LoopExtension()
    后来 sw_h3_loop.py 改成 NODE_CLASS_MAPPINGS 注册、删掉了 SWH3LoopExtension 类，
    但本文件没同步 → 加载时 ImportError: cannot import name 'SWH3LoopExtension'
    → 整个包加载失败（日志 [ERROR] [SW-H3Loop] 加载 sw_h3_loop 失败），
    工作流里所有 SW_H3* 节点变红、工作流加载失败。
    结论：注册方式只保留 NODE_CLASS_MAPPINGS 一种，两文件必须同步。

安装：
    把整个 SW_H3Loop 文件夹拷到 ComfyUI/custom_nodes/，**重启** ComfyUI。
"""

import logging

logger = logging.getLogger(__name__)

try:
    from .sw_h3_loop import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
except Exception:
    logger.exception("[SW-H3Loop] 加载 sw_h3_loop 失败")
    raise

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print("[SW-H3Loop] 已加载 %d 个节点：%s" % (
    len(NODE_CLASS_MAPPINGS), " / ".join(NODE_CLASS_MAPPINGS.keys())))
