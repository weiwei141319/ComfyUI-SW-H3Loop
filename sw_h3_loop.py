# -*- coding: utf-8 -*-
"""
SW H3 Loop —— 海螺 H3「一采循环长视频」专用循环节点包（自研，完全替代官方 StartLoop/EndLoop）

================================================================================
★ 为什么可以「完全替代」官方循环节点（框架层证据）
================================================================================
ComfyUI 的循环机制**不认节点名，只认 schema 上的 loop_boundary 声明**：

    execution.py:1180
        boundary = class_def.GET_SCHEMA().loop_boundary \\
                   if issubclass(class_def, _ComfyNodeInternal) else None
        if boundary == "start": start_nodes.add(node_id)
        elif boundary == "end": end_nodes.add(node_id)

    comfy_api/latest/_io.py:1770
        loop_boundary: Literal["start", "end"] | None = None

所以只要自己的节点是 `_ComfyNodeInternal` 子类（即新版 API `io.ComfyNode`）
并在 schema 里声明 `loop_boundary="start"` / `"end"`，框架就会把它当作循环边界，
`comfy_execution/validation.py: validate_loops()` 会正常展开循环体。

⚠️ 关键约束：**必须用新版 API**（io.ComfyNode + define_schema + execute）。
    经典 API（INPUT_TYPES / NODE_CLASS_MAPPINGS）的 class 不是 _ComfyNodeInternal 子类，
    GET_SCHEMA() 取不到，loop_boundary 声明无效。

================================================================================
关于海螺 H3 的帧率逻辑（本包所有算术的根据，逐条来自官方源码）
================================================================================
    comfy_extras/nodes_minimax_h3.py
        FPS = 24                                  # 视频像素帧率
        AUDIO_LATENT_FPS = 40                     # 音频 latent 率
        def align_frame_count(n):                 # 合法帧数 = 5 + 17n
            while n % 17 != 5: n += 1
            return n
        MiniMaxH3AddGuide 的 image 输入：
            guide_frames = image.shape[0]
            if guide_frames < 5: guide_frames = 1
            else:
                while guide_frames % 17 != 5: guide_frames -= 1
            # → 合法 guide 档位 = 1 或 5, 22, 39, 56...

    comfy/ldm/minimax/model.py
        FRAME_RESCALE = 5.0 / 3.0                 # 双流时间轴缩放
        # 注释原文：the streams share one time axis:
        #   FRAME_RESCALE per pixel frame, 1.0 per audio latent frame

    comfy/ldm/minimax/audio_vae.py
        self.sample_rate = 32000
        self.hop_length = 2*4*4*5*5 = 800
        self.latents_per_second = 32000 // 800 = 40

推得：1 像素帧 = 5/3 音频 latent 帧；1 秒 = 24 像素帧 = 40 音频 latent 帧 ✅ 自洽

★ 由此得一条硬结论：
    段起点只能落在整数像素帧上 → 只能落在 n/24 秒的格点上。
    「10.01 秒」= 240.24 帧，物理上无法表达，只能吸附到 240 帧 = 10.0000 秒。

================================================================================
为什么要自研（官方节点做不到什么）
================================================================================
官方 StartLoop 只向循环体暴露一个整数 iterator `i`，所有随轮次变化的量只能写成
`f(i) = i × 固定系数`。于是：
    a) 步长被锁死成 (L-K)/24，改不成任意值；
    b) 切片起点带不了偏移（想要「段2 从 9.9167s 起」只能靠 i×step 硬凑）；
    c) 末轮无法特殊处理，成片长度只能靠循环外再裁。

本包把「轮数 N / 本轮序号 / 是否首轮 / 是否末轮 / 本轮音频绝对起点秒 / 本轮时长秒」
全部算好再交给循环体，循环体内每个节点拿到的都是绝对量。

★ 循环回灌（carry）的正确架构（2026-09-29 重写，修掉 dependency_cycle）：
    官方 EndLoop.next_iteration_value 只能接**循环体节点**的输出；框架在
    comfy_extras/nodes_loop.py:_expand_loop 里用 copied_link 把这条线改写成
    「当轮循环体副本」，从而跨轮链式传递 previous_carry。
    ❌ 旧版错误：把 next_iteration_value / output_value 接回 EndLoop 自己的 carry 输出
       → 静态图直接自环 → validate_inputs 判 dependency_cycle（红框「工作流存在循环节点连接」）。
    ✅ 正确做法：
        · 累积动作放在**循环体内**的 SW_H3Accumulate（读 Start.current_iteration_value 作上一轮 carry）；
        · SW_H3LoopEnd.next_iteration_value / output_value 都接 SW_H3Accumulate.carry（循环体节点）；
        · Start.current_iteration_value（第4槽）是框架回灌通道，循环体节点从它取上一轮 carry。

本版增强（1.N 条）：
    1.0 SW_H3LoopPlan  —— 循环体内「本轮计划」。接 iteration_index，算出本轮**绝对起点秒**。
    1.1 SW_H3LoopStart —— 自研 StartLoop。声明 loop_boundary="start"，execute 触发循环展开。
    1.2 SW_H3AudioSlice —— 绝对起点音频切片。起点是传入的绝对秒数，不是 i×步长。
    1.3 SW_H3TrimLead   —— 视听同步去重。★ 每轮都丢开头 K 帧。
    1.4 SW_H3SeedPick   —— 接力帧选择。首轮=参考图×K，其余轮=上轮累积末 K 帧。
    1.5 SW_H3LoopEnd    —— 自研 EndLoop。声明 loop_boundary="end"，严格按官方合约实现。
    1.6 SW_H3Accumulate —— 循环体内累积节点（替代旧版 EndLoop 自指 carry）。
    1.7 SW_H3UnpackCarry—— 把 [图,音] carry 拆成 image/audio。
    1.8 SW_H3FrameMath  —— 帧算术速算（只读参考）。
    1.9 所有帧算术走「向下吸附 5+17n」，任何输入都不产生非法帧数；
        秒↔帧↔音频latent 换算统一走同一套函数。
"""

import logging
from typing import Literal

import torch

import comfy.model_management
import comfy.nested_tensor
from comfy_api.latest import io
from comfy_extras.nodes_loop import _expand_loop

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 通用解包辅助（模块级，供多个节点共用）
# ---------------------------------------------------------------------------
def _cache_enabled(value):
    return value[0] if isinstance(value, list) else value


def _unwrap(x, default=None):
    """剥掉 is_input_list 包的那层壳。"""
    if isinstance(x, (list, tuple)):
        return x[0] if len(x) else default
    return x


def _unwrap_carry(x, default=None):
    """剥 carry 专用的壳。

    carry 值本身就是一个 list：[tensor, audio_dict]，框架又在外面包了一层
    → [ [tensor, audio_dict] ]。判据不是「是不是 list」，而是「剥一层后是不是
    [tensor, audio_dict] 形状」：外层 len==1 且 x[0] 有 .shape、x[0] 是 list、
    内层 x[0][0] 有 .shape → 剥一层；否则原样返回（兼容手动接裸 [图,音]）。
    """
    if x is None:
        return default
    if not isinstance(x, (list, tuple)):
        return x
    if len(x) >= 1 and hasattr(x[0], "shape"):
        return x
    if len(x) >= 1 and isinstance(x[0], (list, tuple)):
        inner = x[0]
        if len(inner) >= 1 and hasattr(inner[0], "shape"):
            return inner
    return x


def _empty_image():
    """空 IMAGE 张量（0 帧）。用于「首轮无 carry」这类合法空态，避免抛错。"""
    return torch.zeros((0, 64, 64, 3), dtype=torch.float32)


def _empty_audio():
    """空 AUDIO（0 采样）。同上。"""
    return {"waveform": torch.zeros((1, 1, 0), dtype=torch.float32), "sample_rate": 44100}


# ---------------------------------------------------------------------------
# ★ 容错取值（2026-09-30 新增，修「widget 值变成空串把整条输出废掉」）
# ---------------------------------------------------------------------------
# 【为什么需要】ComfyUI 前端用**位置数组** widgets_values 存 widget 值。当节点的
# schema 改动过（widget 数量/顺序变化）时，旧存档的数组会与新 schema 错位，
# 前端会把错位后的空位序列化成 **空字符串 ''** 发给后端。
# 对 FLOAT/INT 输入，后端在 execution.py:995-996 会 float('')/int('') 直接抛
# invalid_input_type → 整个 prompt 被丢弃（控制台：Prompt outputs failed
# validation / Output will be ignored，节点红框）。这跟循环逻辑无关，纯取值问题。
# 对策两层：
#   a) 工作流侧：把易错位的关键参数改成**由循环外 Primitive 节点连线驱动**
#      （连线走的是 link，不受 widgets_values 错位影响）；
#   b) 节点侧：本组函数把 ''/None/空白一律回落到默认值，杜绝二次踩坑。
def _as_float(value, default=0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str) and value.strip() == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default=0) -> int:
    try:
        if value is None:
            return int(default)
        if isinstance(value, str) and value.strip() == "":
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _as_bool(value, default=False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "":
            return bool(default)
        if s in ("true", "1", "yes", "y", "on", "是", "开"):
            return True
        if s in ("false", "0", "no", "n", "off", "否", "关"):
            return False
        return bool(default)
    return bool(value)


def _as_str(value, default="") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value if value.strip() != "" else default
    return str(value)


# ---------------------------------------------------------------------------
# H3 官方常数（与官方源码逐字一致）
# ---------------------------------------------------------------------------
H3_FPS = 24
H3_AUDIO_LATENT_FPS = 40
H3_FRAME_RESCALE = 5.0 / 3.0
H3_FRAME_BASE = 5
H3_FRAME_STEP = 17

H3_VALID_GUIDE_FRAMES = [1] + [H3_FRAME_BASE + H3_FRAME_STEP * k for k in range(0, 22)]

H3_TRAINED_MIN = 124
H3_TRAINED_MAX = 362


# ---------------------------------------------------------------------------
# 帧算术
# ---------------------------------------------------------------------------
def _snap_length(frames) -> int:
    """向下吸附到 5+17n（合法单段帧数）。输入 <5 时向上钳到 5（唯一允许变大的分支）。"""
    frames = int(frames)
    if frames < H3_FRAME_BASE:
        return H3_FRAME_BASE
    return frames - ((frames - H3_FRAME_BASE) % H3_FRAME_STEP)


def _snap_overlap(frames) -> int:
    """吸附到官方 guide 合法档位：1 或 5+17n。"""
    frames = int(frames)
    if frames <= 0:
        return 0
    if frames < H3_FRAME_BASE:
        return 1
    return frames - ((frames - H3_FRAME_BASE) % H3_FRAME_STEP)


def is_valid_length(frames) -> bool:
    f = int(frames)
    return f >= H3_FRAME_BASE and (f - H3_FRAME_BASE) % H3_FRAME_STEP == 0


def is_valid_overlap(frames) -> bool:
    f = int(frames)
    return f == 0 or f in set(H3_VALID_GUIDE_FRAMES)


def seconds_to_frames(seconds) -> int:
    return int(round(float(seconds) * H3_FPS))


def frames_to_seconds(frames) -> float:
    return int(frames) / H3_FPS


def frames_to_audio_latent(frames) -> int:
    return int(round(int(frames) * H3_FRAME_RESCALE))


def video_latent_t(frame_count: int) -> int:
    fc = _snap_length(frame_count) if frame_count < H3_FRAME_BASE else frame_count
    return 2 if fc <= 5 else ((fc - 5) // H3_FRAME_STEP) * 5 + 2


def _solve_count(target_seconds: float, seg_sec: float, step_sec: float,
                 tolerance_seconds: float = 0.5) -> int:
    """解出覆盖 target_seconds 所需的最小段数（带容差）。"""
    if seg_sec >= target_seconds - tolerance_seconds:
        return 1
    if step_sec <= 0:
        return 1
    n = 1
    while seg_sec + (n - 1) * step_sec < target_seconds - tolerance_seconds:
        n += 1
        if n > 10000:
            break
    return n


def solve_plan(target_seconds: float, segment_frames: int, overlap_frames: int,
               start_offset_seconds: float = 0.0) -> dict:
    seg = _snap_length(segment_frames)
    ovl = max(0, int(overlap_frames))
    if ovl >= seg:
        ovl = max(0, seg - H3_FRAME_STEP)
    guide = _snap_overlap(ovl)
    guide_snapped = (guide != ovl)

    step = seg - ovl
    step_sec = frames_to_seconds(step)
    seg_sec = frames_to_seconds(seg)
    ovl_sec = frames_to_seconds(ovl)

    target_seconds = max(0.0, float(target_seconds))
    count = _solve_count(target_seconds, seg_sec, step_sec)

    offset_frames = seconds_to_frames(start_offset_seconds)
    starts_frames = [i * step + offset_frames for i in range(count)]
    starts_seconds = [frames_to_seconds(f) for f in starts_frames]

    # ★ 成片 = 每轮都丢 K 帧 → N × (L-K) = N × step
    out_frames = count * step
    out_seconds = frames_to_seconds(out_frames)

    warnings = []
    if guide_snapped:
        warnings.append(
            "重叠 %d 帧不是官方 guide 合法档位（1/5/22/39/56...），建议改用 %d 帧。"
            % (ovl, guide))
    if not (H3_TRAINED_MIN <= seg <= H3_TRAINED_MAX):
        warnings.append(
            "单段 %d 帧超出官方训练区间 %d~%d。" % (seg, H3_TRAINED_MIN, H3_TRAINED_MAX))
    if abs(out_seconds - target_seconds) > 0.25:
        warnings.append(
            "成片 %.4fs 距目标 %.4fs 偏差 %.0fms（受 5+17n 网格限制）。"
            % (out_seconds, target_seconds, abs(out_seconds - target_seconds) * 1000))

    return {
        "target_seconds": target_seconds,
        "segment_frames": seg,
        "requested_segment_frames": int(segment_frames),
        "overlap_frames": ovl,
        "guide_frames": guide,
        "guide_snapped": guide_snapped,
        "step_frames": step,
        "step_seconds": step_sec,
        "segment_seconds": seg_sec,
        "overlap_seconds": ovl_sec,
        "count": count,
        "start_offset_seconds": start_offset_seconds,
        "starts_seconds": starts_seconds,
        "starts_frames": starts_frames,
        "starts_audio_latent": [frames_to_audio_latent(f) for f in starts_frames],
        "output_frames": out_frames,
        "output_seconds": out_seconds,
        "output_audio_latent": frames_to_audio_latent(out_frames),
        "video_latent_t": video_latent_t(seg),
        "warnings": warnings,
    }


def solve_best_plan(target_seconds: float, prefer_segment_frames: int = 0,
                    max_count: int = 6, start_offset_seconds: float = 0.0) -> dict:
    """全自动：在所有合法 (L, K, N) 组合里挑最接近目标时长的方案。"""
    target_seconds = max(0.5, float(target_seconds))
    target_frames = seconds_to_frames(target_seconds)

    valid_L = [H3_FRAME_BASE + H3_FRAME_STEP * k for k in range(1, 21)]
    valid_K = [1] + [H3_FRAME_BASE + H3_FRAME_STEP * k for k in range(0, 11)]

    cands = []
    for L in valid_L:
        for K in valid_K:
            if K >= L:
                continue
            step = L - K
            if step <= 0:
                continue
            for N in range(1, max_count + 1):
                out = N * step
                if out > target_frames + H3_FPS * 3:
                    continue
                if out < H3_FRAME_BASE:
                    continue
                err = abs(out - target_frames)
                short = 1 if out < target_frames else 0
                pref = 0 if prefer_segment_frames <= 0 else abs(L - _snap_length(prefer_segment_frames))
                trained = 0 if (H3_TRAINED_MIN <= L <= H3_TRAINED_MAX) else 1
                cands.append((err, short, pref, trained, N, L, K, out))

    if not cands:
        return solve_plan(target_seconds, 243, 5, start_offset_seconds)

    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3], c[4]))
    _, _, _, _, N, L, K, out = cands[0]
    return solve_plan(target_seconds, L, K, start_offset_seconds)


def format_plan(plan: dict) -> str:
    p = plan
    lines = [
        "===== SW H3 循环计划 =====",
        "单段    : %d 帧 / %.4f s   (视频latent_t=%d, 音频latent_t=%d)"
        % (p["segment_frames"], p["segment_seconds"], p["video_latent_t"],
           frames_to_audio_latent(p["segment_frames"])),
        "重叠    : %d 帧 / %.4f s   (guide %d%s)"
        % (p["overlap_frames"], p["overlap_seconds"], p["guide_frames"],
           "，⚠️ 已吸附" if p["guide_snapped"] else " ✅合法"),
        "步长    : %d 帧 / %.4f s" % (p["step_frames"], p["step_seconds"]),
        "段数    : %d" % p["count"],
        "起点    : " + ", ".join("%.4fs(帧%d)" % (s, f)
                                  for s, f in zip(p["starts_seconds"], p["starts_frames"])),
        "成片    : %d 帧 / %.4f s   (音频latent %d)"
        % (p["output_frames"], p["output_seconds"], p["output_audio_latent"]),
        "目标    : %.4f s   (误差 %+.0f ms)"
        % (p["target_seconds"], (p["output_seconds"] - p["target_seconds"]) * 1000),
    ]
    for w in p["warnings"]:
        lines.append("⚠️ " + w)
    lines.append("==========================")
    return "\n".join(lines)


# ===========================================================================
# 1.1 SW_H3LoopStart —— 自研 StartLoop（声明 loop_boundary="start"）
# ===========================================================================
class SW_H3LoopStart(io.ComfyNode):
    """H3 一采循环 · 起点（自研，完全替代官方 StartLoop）

    在 schema 里声明 loop_boundary="start"，框架即把它识别为循环入口
    （execution.py:1180 直接读 GET_SCHEMA().loop_boundary）。

    ★★★ 2026-09-29 关键修复 ★★★
        旧版 execute 只返回普通 NodeOutput，漏了 return expand=graph，
        → 框架从不展开循环体（body 节点从不实例化、current_iteration_value 恒为 None，
          成片只有 1 段长度）。
        本版 execute 完整复刻官方 StartLoop.execute：调用 _expand_loop 并
        return io.NodeOutput(..., expand=graph)，循环才真正展开。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3LoopStart",
            display_name="SW H3 循环·起点",
            category="SW/h3loop",
            description="自研 H3 循环入口。替代官方 StartLoop。"
                        "输出槽顺序与框架 LoopIteration 严格一致（iteration_index / is_first / "
                        "is_last / list_item / current_iteration_value）。每轮的具体起点/时长请接循环体内的 "
                        "SW_H3LoopPlan 计算。第4槽 current_iteration_value 是框架回灌通道，"
                        "循环体内累积节点从它取上一轮的 carry。",
            loop_boundary="start",
            is_input_list=True,
            inputs=[
                io.Int.Input(
                    "段数",
                    default=3, min=1, max=64, step=1,
                    tooltip="循环执行多少轮（= 分几段）。",
                ),
                io.MatchType.Input(
                    "initial_iteration_value",
                    io.MatchType.Template("carried_value"),
                    optional=True,
                    tooltip="首轮暴露给 current_iteration_value 的初值（一般留空）。",
                ),
            ],
            outputs=[
                io.Int.Output(display_name="iteration_index"),
                io.Boolean.Output(display_name="is_first"),
                io.Boolean.Output(display_name="is_last"),
                io.MatchType.Output(io.MatchType.Template("list_item"), id="list_item",
                                    display_name="list_item"),
                io.MatchType.Output(io.MatchType.Template("carried_value"),
                                    id="current_iteration_value",
                                    display_name="current_iteration_value"),
            ],
            hidden=[io.Hidden.dynprompt, io.Hidden.execution_list, io.Hidden.unique_id],
            enable_expand=True,
        )

    @classmethod
    def execute(cls, 段数, initial_iteration_value=None, **kwargs) -> io.NodeOutput:
        # is_input_list=True ⇒ 框架把每个输入都包成 list，先剥一层。
        if isinstance(段数, (list, tuple)):
            段数 = 段数[0] if len(段数) else 3
        if isinstance(initial_iteration_value, (list, tuple)):
            initial_iteration_value = initial_iteration_value[0] if len(initial_iteration_value) else None

        values = list(range(max(1, _as_int(段数, 3))))

        # ★ 触发循环展开（与官方 StartLoop.execute 逐行一致）
        dynprompt = cls.hidden.dynprompt
        execution_list = cls.hidden.execution_list
        unique_id = cls.hidden.unique_id
        loop = dynprompt.get_node(unique_id)
        body = set(loop["_loop_body"])
        close_id = loop["_loop_end"]
        graph = _expand_loop(
            dynprompt,
            unique_id,
            body,
            close_id,
            values,
            None,
            loop["inputs"].get("initial_iteration_value"),
            _cache_enabled(False),
        )
        close = dynprompt.get_node(close_id)
        close_inputs = close["inputs"].copy()
        for name in tuple(close_inputs):
            if name in ("output_value", "next_iteration_value") or name.startswith("termination"):
                del close_inputs[name]
        execution_list.add_node(close_id)
        execution_list.add_external_block(close_id)
        execution_list.inhibit_nodes(body)
        dynprompt.override_node(close_id, {"class_type": close["class_type"], "inputs": close_inputs})
        PromptServer = __import__("server").PromptServer
        PromptServer.instance.send_progress_text(f"Iteration 0 / {len(values)}", unique_id)
        return io.NodeOutput(None, False, not values, None, None, expand=graph)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        """★★★ 2026-09-30 关键修复：「第二次运行红框」的唯一根因 ★★★

        必须与官方 StartLoop 逐字一致地返回 float("NaN")，作用是**让本节点永远
        缓存不命中**，从而每次运行都真的调用 execute() → 每次运行都真的展开循环。

        【不写这段会发生什么（用户实测现象：第二次点 Run 节点变红框）】
          第 1 次运行：缓存为空 → execute() 被调用 → _expand_loop 展开 → 正常出片。
          第 2 次运行（参数没改）：输入 hash 与上次完全相同 → **缓存命中 →
            execute() 根本不被调用** → 返回的是上次的 output 值
            (None, False, ..., None, None)，但**缓存里不存 expand=graph**，
            于是框架不展开循环体；静态图里的循环体内节点（#33 SW_H3LoopPlan 等）
            被当作普通节点直接执行，其 iteration_index 输入拿到 Start 第 0 槽的
            占位值 None → int(None) 抛 TypeError → 该节点标红框。
          （同时也解释了为何「第一次能跑、第二次就坏」——纯缓存问题。）

        【为什么不能用输入变化来兜底】
          用户日常就是「同样的参数连跑两遍」，此时输入必然不变；官方因此在
          nodes_loop.py:StartLoop 里专门写了这一个 NaN 指纹来禁用缓存
          （LoopIteration / LoopProgress / LoopResult 同理）。
        """
        return float("NaN")


# ===========================================================================
# 1.1b SW_H3LoopPlan —— 循环体内：按 iteration_index 算本轮绝对起点
# ===========================================================================
class SW_H3LoopPlan(io.ComfyNode):
    """H3 一采循环 · 本轮计划（放在循环体内）。

    起点 = iteration_index × 步长 + 偏移，是**绝对秒数**，直接喂给 AudioSlice。
    这样第二段天然从 9.9167s 开始、第三段从 19.8333s 开始，不需要手工改。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3LoopPlan",
            display_name="SW H3 本轮计划（循环体内）",
            category="SW/h3loop",
            description="接 SW_H3LoopStart 的 iteration_index，算出本轮的绝对起点秒 / 时长秒 / guide帧数。",
            inputs=[
                io.Int.Input("iteration_index", default=0, min=0, max=10000, step=1,
                             force_input=True,
                             tooltip="接 SW_H3LoopStart 的 iteration_index。"),
                io.Float.Input("目标时长秒", default=30.0, min=0.5, max=600.0, step=0.01,
                               tooltip="成片目标时长（秒）。段数已由 Start 决定，这里只用于报告。"),
                io.Int.Input("单段帧数", default=243, min=0, max=2000, step=1,
                             tooltip="每段帧数，自动吸附到 5+17n。填 0 = 全自动。推荐 243。"),
                io.Int.Input("重叠帧数", default=5, min=0, max=200, step=1,
                             tooltip="相邻段重叠帧数，guide 合法档位 1/5/22/39/56...。推荐 5。"),
                io.Float.Input("起点偏移秒", default=0.0, min=-5.0, max=5.0, step=0.01,
                               tooltip="整体平移所有切片起点（吸附到整数帧）。"),
                io.Int.Input("起点微调帧", default=0, min=-240, max=240, step=1,
                             tooltip="在所有起点上 ±N 帧手动挪，用来对齐歌词/鼓点。"),
            ],
            outputs=[
                io.Float.Output(display_name="本轮起点秒"),
                io.Float.Output(display_name="本轮时长秒"),
                io.Int.Output(display_name="guide帧数"),
                io.Int.Output(display_name="单段帧数"),
                io.Int.Output(display_name="重叠帧数"),
                io.Int.Output(display_name="段数"),
                io.Boolean.Output(display_name="是否首轮"),
                io.Boolean.Output(display_name="是否末轮"),
                io.String.Output(display_name="计划摘要"),
            ],
        )

    @classmethod
    def execute(cls, iteration_index, 目标时长秒=30.0, 单段帧数=243, 重叠帧数=5,
                起点偏移秒=0.0, 起点微调帧=0) -> io.NodeOutput:
        # 未展开防御：正常情况下 iteration_index 一定是 int（由框架每轮注入）。
        # 若为 None，说明 Start 的 execute 压根没跑（缓存命中未展开），
        # 此时给出可诊断的错误而不是让 int(None) 抛一个看不懂的 TypeError。
        if iteration_index is None or (isinstance(iteration_index, str)
                                       and iteration_index.strip() == ""):
            raise ValueError(
                "[SW-H3Loop] iteration_index 收到 None/空 —— 循环未展开。"
                "这通常意味着 SW_H3LoopStart 的 fingerprint_inputs 未返回 NaN（缓存命中）；"
                "请确认节点包 sw_h3_loop.py 中 SW_H3LoopStart 已实现 fingerprint_inputs，"
                "改完需重启 ComfyUI。"
            )
        # 防御性强制转换（''/None 一律回落默认值），杜绝 invalid_input_type 之外的二次踩坑
        iteration_index = _as_int(iteration_index, 0)
        目标时长秒 = _as_float(目标时长秒, 30.0)
        单段帧数 = _as_int(单段帧数, 243)
        重叠帧数 = _as_int(重叠帧数, 5)
        起点偏移秒 = _as_float(起点偏移秒, 0.0)
        起点微调帧 = _as_int(起点微调帧, 0)

        if 单段帧数 <= 0:
            plan = solve_best_plan(目标时长秒, prefer_segment_frames=277,
                                   start_offset_seconds=起点偏移秒)
        else:
            plan = solve_plan(目标时长秒, 单段帧数, 重叠帧数, 起点偏移秒)

        n = int(plan["count"])
        off = seconds_to_frames(起点偏移秒) + int(起点微调帧)
        step = int(plan["step_frames"])

        idx = iteration_index
        if idx >= n:
            idx = n - 1
        start_frames = idx * step + off
        start_sec = frames_to_seconds(start_frames)
        seg_sec = plan["segment_seconds"]

        summary = ("第 %d/%d 段 · 起点 %.4fs（帧 %d）· 时长 %.4fs · 重叠 %d 帧(guide %d)"
                   % (idx + 1, n, start_sec, start_frames, seg_sec,
                      plan["overlap_frames"], plan["guide_frames"]))
        print("[SW-H3Loop] " + summary)

        return io.NodeOutput(
            start_sec,
            seg_sec,
            int(plan["guide_frames"]),
            int(plan["segment_frames"]),
            int(plan["overlap_frames"]),
            n,
            idx == 0,
            idx >= n - 1,
            summary,
        )


# ===========================================================================
# 1.2 SW_H3AudioSlice
# ===========================================================================
class SW_H3AudioSlice(io.ComfyNode):
    """H3 一采循环 · 音频绝对起点切片。

    起点就是传进来的绝对秒数，不是 i×步长 —— 这是自研节点的核心价值。
    换算走 H3 双流时间轴：秒 → 像素帧 → 采样点（32000Hz）。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3AudioSlice",
            display_name="SW H3 音频切片（绝对起点）",
            category="SW/h3loop",
            description="按绝对秒数截取音频。起点来自 SW_H3LoopStart 的「本轮起点秒」。",
            inputs=[
                io.Audio.Input("audio"),
                io.Float.Input(
                    "起点秒", default=0.0, min=-60.0, max=3600.0, step=0.001,
                    force_input=True,
                    tooltip="来自 SW_H3LoopStart 的「本轮起点秒」，绝对时间轴。",
                ),
                io.Float.Input(
                    "时长秒", default=10.125, min=0.05, max=120.0, step=0.001,
                    force_input=True,
                    tooltip="来自 SW_H3LoopStart 的「本轮时长秒」。",
                ),
                io.Combo.Input(
                    "吸附到帧网格", options=["是", "否", ""], default="是",
                    tooltip="是 = 把起点/时长吸附到 1/24s 网格，与 H3 时间轴严格对齐。"
                            "★ 末尾那个空选项是「容错哨兵」：ComfyUI 前端在 widget 值丢失时会发"
                            "空字符串，若空串不在 options 里，execution.py:1070 会判 "
                            "value_not_in_list 让整条 prompt 作废（节点红框 / Output will be "
                            "ignored）。execute 里 _as_str 会把空串映射回「是」。",
                ),
            ],
            outputs=[
                io.Audio.Output(display_name="audio"),
                io.Float.Output(display_name="实际起点秒"),
                io.Float.Output(display_name="实际时长秒"),
            ],
        )

    @classmethod
    def execute(cls, audio, 起点秒=0.0, 时长秒=10.125, 吸附到帧网格="是") -> io.NodeOutput:
        if audio is None:
            raise ValueError("[SW-H3Loop] AudioSlice 收到空音频")

        waveform = audio["waveform"]
        sr = int(audio["sample_rate"])

        起点秒 = _as_float(起点秒, 0.0)
        时长秒 = _as_float(时长秒, 10.125)
        吸附到帧网格 = _as_str(吸附到帧网格, "是")

        start_s = float(起点秒)
        dur_s = float(时长秒)

        if 吸附到帧网格 == "是":
            start_s = seconds_to_frames(start_s) / H3_FPS
            dur_s = seconds_to_frames(dur_s) / H3_FPS

        start_sample = max(0, int(round(start_s * sr)))
        total_samples = waveform.shape[-1]

        if start_sample >= total_samples:
            logger.warning("[SW-H3Loop] 切片起点 %.4fs 超出音频长度 %.4fs，回退到 0",
                           start_s, total_samples / sr)
            start_sample = 0

        n_samples = int(round(dur_s * sr))
        if n_samples <= 0:
            n_samples = total_samples - start_sample
        end_sample = min(total_samples, start_sample + n_samples)

        avail = end_sample - start_sample
        if avail < int(0.05 * sr):
            raise ValueError(
                "[SW-H3Loop] 切片不足 0.05s（起点 %.4fs，音频总长 %.4fs）。"
                "请把音频补齐，或减小目标时长/段数。"
                % (start_s, total_samples / sr))

        sliced = waveform[..., start_sample:end_sample].clone()

        # ★★★ 2026-09-30 修（片尾把歌曲开头唱回去）：越界部分**补静音**，绝不让音频回绕 ★★★
        # 【为什么】工作流原本用 AudioConcat 把歌拼 4 遍喂进来，为的是让第 3 段的切片
        #   （21.25s 起、要 11.5417s → 覆盖到 32.79s）不越界。但歌曲只有 30.04s，
        #   于是第 3 段切片的尾部落到了「第 2 遍拷贝的开头」= 歌曲第 0~2.75s ——
        #   成片最后 0.876s 就又唱回了第一句（实测互相关：末尾 2s 与整首歌相关仅 0.17，
        #   而正常段落相关 0.99，铁证）。
        # 【修法】请求时长超过音频剩余长度时，用 0 采样补齐到请求时长：
        #   · 成片 = 歌曲[0.9167s : 末] + 静音尾巴，严格等长、干净收尾；
        #   · 模型听到的最后一段也从「重复第一句」变成「静音」，收尾更自然；
        #   · 音频总长因此不必再靠「多遍拼接」硬凑（#22 直接接 #44 即可）。
        pad_samples = n_samples - avail
        if pad_samples > 0:
            logger.info("[SW-H3Loop] 切片越界：可用 %.4fs，补静音 %.4fs（共 %.4fs）",
                        avail / sr, pad_samples / sr, n_samples / sr)
            zeros = torch.zeros(*sliced.shape[:-1], pad_samples, dtype=sliced.dtype)
            sliced = torch.cat([sliced, zeros], dim=-1)

        return io.NodeOutput(
            {"waveform": sliced, "sample_rate": sr},
            round(start_sample / sr, 6),
            round(sliced.shape[-1] / sr, 6),
        )


# ===========================================================================
# 1.3 SW_H3TrimLead
# ===========================================================================
class SW_H3TrimLead(io.ComfyNode):
    """H3 一采循环 · 视听同步去重（融合对齐的核心一步）。

    ★ 为什么首轮也要丢 K 帧？
      首轮 AddGuide 钉的 K 帧是「参考图 × K」的**种子**，不是模型生成的画面。
      它是用来给采样器当锚点的，绝不能出现在成片里。
      其余轮钉的 K 帧是上一轮的真实末帧 —— 保留它就等于保留了上一轮，
      所以这一轮的前 K 帧必须丢掉 —— 丢掉之后，上一轮那份就成为这段画面的唯一起点，
      两段就在这 K 帧上**融合为一份**（不是两份都留）。

      「钉 K 帧 + 丢 K 帧」必须严格相等，融合才不会错位。本节点保证这一点：
      首轮丢 K（种子）、其余轮丢 K（重叠）—— 每轮都丢同样的 K，
      于是成片 = 段数 × (单段帧数 - 重叠帧数)，干净利落。

      图像与音频共用同一份 drop_frames，严格 1:1 同步（K/24 秒）。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3TrimLead",
            display_name="SW H3 去重（视听同步）",
            category="SW/h3loop",
            description="每轮都丢掉开头的 K 帧（首轮=参考图种子，其余轮=上一轮重叠）。图像与音频同源同步。",
            inputs=[
                io.Image.Input("images"),
                io.Audio.Input("audio"),
                io.Boolean.Input("是否首轮", default=True, force_input=True),
                io.Int.Input(
                    "重叠帧数", default=5, min=0, max=2000, step=1,
                    force_input=True,
                    tooltip="来自 SW_H3LoopPlan 的计划；不接则用本参数。",
                ),
                io.Boolean.Input(
                    "首轮也丢", default=True,
                    tooltip="开=首轮同样丢掉 K 帧（在节点包内剔除参考图种子，成片不再混入照片）。"
                            "关=首轮保留整段（需在循环外自行裁掉种子帧）。",
                ),
            ],
            outputs=[
                io.Image.Output(display_name="images"),
                io.Audio.Output(display_name="audio"),
                io.Int.Output(display_name="丢掉帧数"),
            ],
        )

    @classmethod
    def execute(cls, images, audio, 是否首轮=True, 重叠帧数=5, 首轮也丢=True) -> io.NodeOutput:
        n_frames = images.shape[0]
        # ★ 容错：''/None 一律回落。首轮也丢 的默认值取 True（失败安全）——
        #   宁可多丢 K 帧（起手即真实画面），也绝不能把「参考图×K」的种子漏进成片。
        重叠帧数 = _as_int(重叠帧数, 5)
        是否首轮 = _as_bool(是否首轮, True)
        首轮也丢 = _as_bool(首轮也丢, True)
        drop_frames = min(重叠帧数, max(0, n_frames - 1))
        if 是否首轮 and not 首轮也丢:
            drop_frames = 0

        if drop_frames > 0:
            images = images[drop_frames:]

        sr = int(audio["sample_rate"])
        drop_samples = int(round((drop_frames / H3_FPS) * sr))
        waveform = audio["waveform"]
        if drop_samples > 0:
            if drop_samples >= waveform.shape[-1]:
                raise ValueError(
                    "[SW-H3Loop] 要去掉的音频（%.4fs）超过本段音频总长（%.4fs）。"
                    % (drop_frames / H3_FPS, waveform.shape[-1] / sr))
            waveform = waveform[..., drop_samples:]

        return io.NodeOutput(
            images,
            {"waveform": waveform, "sample_rate": sr},
            drop_frames,
        )


# ===========================================================================
# 1.4 SW_H3SeedPick —— 接力帧二选一（首轮=参考图种子 / 其余=上轮末帧）
# ===========================================================================
class SW_H3SeedPick(io.ComfyNode):
    """H3 一采循环 · 接力帧选择（循环体内）。

    首轮：AddGuide 要钉「参考图 × K 帧」当作起始锚点。
    其余轮：AddGuide 要钉「上一轮累积图像的末尾 K 帧」，才能与上段无缝融合。

    正确做法是「二选一」而不是「相加」——本节点就干这件事：
      is_first=True  → 输出 种子帧（外部按需再做 RepeatImageBatch）
      is_first=False → 从累积图像里取末尾 K 帧
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3SeedPick",
            display_name="SW H3 接力帧选择（循环体内）",
            category="SW/h3loop",
            description="首轮输出参考图种子，其余轮输出上一轮累积图像的末尾 K 帧。"
                        "严格 K 帧，绝不叠加。",
            inputs=[
                io.Image.Input("种子帧",
                               tooltip="首轮用的锚点。通常是「参考图 × K 帧」（RepeatImageBatch）。"),
                io.Image.Input("累积图像", optional=True,
                               tooltip="接 SW_H3UnpackCarry.image（来自 Start.current_iteration_value 拆出的"
                                       "上一轮累积图像）。首轮为空。"),
                io.Boolean.Input("是否首轮", default=True, force_input=True),
                io.Int.Input("重叠帧数", default=5, min=1, max=200, step=1, force_input=True,
                             tooltip="从累积图像末尾取多少帧。必须与 TrimLead 丢帧数一致。"),
            ],
            outputs=[
                io.Image.Output(display_name="接力帧"),
                io.Int.Output(display_name="实际帧数"),
                io.Boolean.Output(display_name="来自累积"),
            ],
        )

    @classmethod
    def execute(cls, 种子帧, 累积图像=None, 是否首轮=True,
                重叠帧数=5) -> io.NodeOutput:
        K = max(1, _as_int(重叠帧数, 5))
        是否首轮 = _as_bool(是否首轮, True)

        # 兼容两种上游：
        #   a) 直接是 IMAGE 张量
        #   b) Start.current_iteration_value 拆出的图像（已通过 SW_H3UnpackCarry）
        acc = _unwrap_carry(累积图像)
        if isinstance(acc, (list, tuple)) and len(acc) >= 1 and hasattr(acc[0], "shape"):
            acc = acc[0]
        elif isinstance(acc, (list, tuple)) and len(acc) >= 1:
            acc = acc[0]

        acc_n = int(getattr(acc, "shape", [0])[0]) if acc is not None else 0

        if 是否首轮 or acc_n == 0:
            picked, from_acc = 种子帧, False
            note = "参考图种子"
        else:
            take = min(K, acc_n)
            picked = acc[acc_n - take:]
            from_acc = True
            note = "上轮累积末 %d 帧" % take

        n = int(picked.shape[0]) if picked is not None else 0
        print("[SW-H3Loop] 接力帧：%d 帧（%s）" % (n, note))
        return io.NodeOutput(picked, n, from_acc)


# ===========================================================================
# 1.5 SW_H3LoopEnd —— 自研 EndLoop（声明 loop_boundary="end"，严格按官方合约）
# ===========================================================================
class SW_H3LoopEnd(io.ComfyNode):
    """H3 一采循环 · 累积出栈（自研，完全替代官方 EndLoop）

    ★★★ 2026-09-29 重写（修 dependency_cycle）★★★
    严格按官方 EndLoop 的 loop 合约实现：
      - loop_boundary="end"：框架在 validate_loops 里把它配对为循环出口；
      - output_value / next_iteration_value 接**循环体内**的 SW_H3Accumulate.carry
        （绝不接自己的输出！旧版接自己 → 静态图自环 → 红框「工作流存在循环节点连接」）；
      - execute 走官方外部块回收路径（get_external_block_result），
        由 LoopResult 把「最后一轮的 output_value」注入为循环结果返回。
    累积动作本身在 SW_H3Accumulate（循环体内）完成。
    """

    @classmethod
    def define_schema(cls):
        output_type = io.MatchType.Template("output_value")
        carried_type = io.MatchType.Template("carried_value")
        terminations = io.Autogrow.TemplatePrefix(
            io.AnyType.Input(
                "termination",
                tooltip="连接每一轮都必须执行一次的预览/副作用输出（其返回值不返回）。",
            ),
            prefix="termination",
            min=0,
            max=50,
        )
        return io.Schema(
            node_id="SW_H3LoopEnd",
            display_name="SW H3 循环·累积出栈",
            category="SW/h3loop",
            description="自研 H3 循环出口。output_value / next_iteration_value 都接循环体内 "
                        "SW_H3Accumulate 的 carry（图+音打包）。框架把最后一轮的 carry 作为成片返回。",
            loop_boundary="end",
            is_input_list=True,
            inputs=[
                io.MatchType.Input(
                    "output_value",
                    output_type,
                    optional=True,
                    tooltip="接 SW_H3Accumulate.carry。框架据此收集最后一轮的累积作为成片。",
                ),
                io.MatchType.Input(
                    "next_iteration_value",
                    carried_type,
                    optional=True,
                    tooltip="接 SW_H3Accumulate.carry。框架把本轮累积回灌给下一轮 "
                            "（nodes_loop.py:76-79），实现跨轮累加。",
                ),
                io.Boolean.Input(
                    "accumulate",
                    default=False,
                    tooltip="开=返回每一轮的 output_value；关=只返回最后一轮。",
                ),
                io.Autogrow.Input(
                    "terminations",
                    template=terminations,
                    optional=True,
                    tooltip="连接每一轮都要执行的预览/副作用节点。",
                ),
            ],
            outputs=[
                io.MatchType.Output(
                    output_type,
                    id="outputs",
                    is_output_list=True,
                    display_name="累积(out)",
                    tooltip="最后一轮的 carry = [累积图像, 累积音频]。下游用 SW_H3UnpackCarry 拆包。",
                ),
            ],
            hidden=[io.Hidden.execution_list, io.Hidden.unique_id],
        )

    @classmethod
    def execute(cls, accumulate, **kwargs):
        outputs = cls.hidden.execution_list.get_external_block_result(cls.hidden.unique_id)
        return io.NodeOutput([value for output in outputs for value in output])


# ===========================================================================
# 1.6 SW_H3Accumulate —— 循环体内累积（替代旧版 EndLoop 自指 carry）
# ===========================================================================
class SW_H3Accumulate(io.ComfyNode):
    """H3 一采循环 · 累积（放在循环体内，每轮执行一次）。

    ★ 为什么必须放在循环体内、不能放在 EndLoop 里：
       ComfyUI 的回灌通道（EndLoop.next_iteration_value）只能接**循环体节点**的输出
       （nodes_loop.py 的 copied_link 会把指回循环体节点的线改写成当轮副本，
       从而跨轮链式传递）。如果接 EndLoop 自己的输出 → 静态图自环 → dependency_cycle。
       所以累积动作必须在循环体内完成，上一轮的 carry 从 Start.current_iteration_value 取。

    输入：
      current_iteration_value —— 来自 SW_H3LoopStart 第4槽（框架回灌的上一轮 carry=[图,音]）
     本轮图像 / 本轮音频        —— 本段成品（已去重）
    输出：
      累积图像 / 累积音频       —— 上一轮 + 本轮 拼接
      carry                     —— [累积图像, 累积音频]，专供 EndLoop 回灌
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3Accumulate",
            display_name="SW H3 累积（循环体内）",
            category="SW/h3loop",
            description="循环体内累积节点。current_iteration_value 接 Start 的 carried 槽，"
                        "把上一轮 carry 与本段成品拼接，输出新的 carry 给 EndLoop 回灌。",
            inputs=[
                io.MatchType.Input(
                    "current_iteration_value",
                    io.MatchType.Template("carried_value"),
                    optional=True,
                    tooltip="接 SW_H3LoopStart 的 current_iteration_value（=上一轮 carry=[图,音]）。首轮为 None。",
                ),
                io.Image.Input("本轮图像",
                               tooltip="本段成品图像（已去重），来自 SW_H3TrimLead.images。"),
                io.Audio.Input("本轮音频",
                               tooltip="本段成品音频（已去重），来自 SW_H3TrimLead.audio。"),
            ],
            outputs=[
                io.Image.Output(display_name="累积图像"),
                io.Audio.Output(display_name="累积音频"),
                io.MatchType.Output(io.MatchType.Template("carried"),
                                    display_name="carry",
                                    tooltip="[累积图像, 累积音频]，接 SW_H3LoopEnd 的 output_value / next_iteration_value。"),
            ],
        )

    @classmethod
    def execute(cls, current_iteration_value=None, 本轮图像=None, 本轮音频=None) -> io.NodeOutput:
        prev = _unwrap_carry(current_iteration_value)
        prev_img, prev_aud = None, None
        if isinstance(prev, (list, tuple)) and len(prev) >= 2:
            prev_img = prev[0]
            prev_aud = prev[1]

        # ---- 图像累积 ----
        if prev_img is not None and getattr(prev_img, "shape", [0])[0] > 0:
            fb = prev_img.shape[1:]
            cur = 本轮图像
            if cur.shape[1:] != fb:
                cur = torch.nn.functional.interpolate(
                    cur.movedim(-1, 1), size=fb[:2], mode="bilinear", align_corners=False
                ).movedim(1, -1)
            images = torch.cat([prev_img, cur], dim=0)
        else:
            images = 本轮图像

        # ---- 音频累积 ----
        wave_b = 本轮音频["waveform"]
        sr = int(本轮音频["sample_rate"])
        if prev_aud is not None and prev_aud["waveform"].shape[-1] > 0:
            wave_a = prev_aud["waveform"]
            if wave_a.shape[0] != wave_b.shape[0]:
                ch = min(wave_a.shape[0], wave_b.shape[0])
                wave_a, wave_b = wave_a[:ch], wave_b[:ch]
            waveform = torch.cat([wave_a, wave_b], dim=-1)
        else:
            waveform = wave_b

        carried = [images, {"waveform": waveform, "sample_rate": sr}]
        return io.NodeOutput(images, {"waveform": waveform, "sample_rate": sr}, carried)


# ===========================================================================
# 1.7 SW_H3UnpackCarry —— 把 [图, 音] carry 拆成 image / audio
# ===========================================================================
class SW_H3UnpackCarry(io.ComfyNode):
    """H3 一采循环 · 拆包 carry。

    把 SW_H3LoopEnd 返回的 [累积图像, 累积音频] 拆成两条独立输出，
    供循环外的 ImageFromBatch / 音频截断节点使用。循环体内也可用它将
    Start.current_iteration_value 拆给需要纯图像的节点（如 SW_H3SeedPick）。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3UnpackCarry",
            display_name="SW H3 拆包(carry→图/音)",
            category="SW/h3loop",
            description="把 [图像, 音频] 形式的 carry 拆成 image / audio 两条输出。",
            inputs=[
                io.MatchType.Input("carry", io.MatchType.Template("carried"),
                                   tooltip="接 SW_H3LoopEnd.累积(out) 或 SW_H3LoopStart.current_iteration_value。"),
            ],
            outputs=[
                io.Image.Output(display_name="image"),
                io.Audio.Output(display_name="audio"),
            ],
        )

    @classmethod
    def execute(cls, carry) -> io.NodeOutput:
        """★ 2026-09-30 修（首轮 None 崩溃）：Start.current_iteration_value 在**第 0 轮必然是 None**
        —— 那时还没有任何一轮跑过、没有任何 carry 可回灌。旧版这里直接 raise，
        导致 `iteration 0/3` 第一轮就炸（报错：UnpackCarry 收到空或非 [图,音] 的 carry: None）。
        正确语义是「空态合法」：返回空 IMAGE/AUDIO，
        下游 SW_H3SeedPick 见到 0 帧累积会自动回落到「参考图种子帧」（其 acc_n == 0 分支），
        与「首轮 AddGuide 钉种子帧」的设计完全一致。
        """
        c = _unwrap_carry(carry)

        # 空态：首轮 / 首次调用。合法，返回空而不是抛错。
        if c is None or (isinstance(c, (list, tuple)) and len(c) == 0):
            print("[SW-H3Loop] UnpackCarry：本轮无 carry（首轮），按空处理；"
                  "下游 SeedPick 将回落到参考图种子帧。")
            return io.NodeOutput(_empty_image(), _empty_audio())

        # 标准形态：[图像, 音频]
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            return io.NodeOutput(c[0], c[1])

        # 容错：只给了裸图像（没有音频），音频按空处理，别让整条链断掉
        if hasattr(c, "shape"):
            print("[SW-H3Loop] UnpackCarry：carry 只有图像、无音频，音频按空处理。")
            return io.NodeOutput(c, _empty_audio())

        raise ValueError("[SW-H3Loop] UnpackCarry 收到非 [图,音] 的 carry：%r" % (c,))


# ===========================================================================
# 1.8 SW_H3FrameMath
# ===========================================================================
class SW_H3FrameMath(io.ComfyNode):
    """H3 帧算术速算（只读参考，不接图）。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SW_H3FrameMath",
            display_name="SW H3 帧算术速算",
            category="SW/h3loop",
            description="输入目标时长/单段/重叠，实时输出段数、步长、成片长度与告警。",
            inputs=[
                io.Float.Input("目标时长秒", default=30.0, min=0.5, max=600.0, step=0.01),
                io.Int.Input("单段帧数", default=243, min=0, max=2000, step=1),
                io.Int.Input("重叠帧数", default=5, min=0, max=200, step=1),
            ],
            outputs=[
                io.Int.Output(display_name="段数"),
                io.Float.Output(display_name="步长秒"),
                io.Int.Output(display_name="成片帧数"),
                io.Float.Output(display_name="成片秒数"),
                io.String.Output(display_name="摘要"),
            ],
        )

    @classmethod
    def execute(cls, 目标时长秒, 单段帧数, 重叠帧数) -> io.NodeOutput:
        if int(单段帧数) <= 0:
            plan = solve_best_plan(目标时长秒)
        else:
            plan = solve_plan(目标时长秒, 单段帧数, 重叠帧数)
        summary = format_plan(plan)
        print("[SW-H3Loop]\n" + summary)
        return io.NodeOutput(
            plan["count"], plan["step_seconds"], plan["output_frames"],
            plan["output_seconds"], summary,
        )


# ===========================================================================
# ★ 注册（与官方 StartLoop 同模式：io.ComfyNode 类 + NODE_CLASS_MAPPINGS）
# ---------------------------------------------------------------------------
# ⚠️ 为什么不用 ComfyExtension / get_node_list：
#    custom_nodes 加载器只扫描模块的 NODE_CLASS_MAPPINGS 字典来注册节点；
#    ComfyExtension 是 ComfyUI 内置「扩展」(extensions) 机制，custom_nodes 目录
#    下的文件不会被当成扩展加载。若用 ComfyExtension，工作流里这些节点会全部
#    变红/缺失（class_type 找不到）。官方 comfy_extras/nodes_loop.py 的
#    StartLoop/EndLoop 正是 io.ComfyNode 子类 + NODE_CLASS_MAPPINGS 注册。
#    本包节点同为 io.ComfyNode 子类，沿用同一注册方式即可被正常加载。
NODE_CLASS_MAPPINGS = {
    "SW_H3LoopStart": SW_H3LoopStart,
    "SW_H3LoopPlan": SW_H3LoopPlan,
    "SW_H3AudioSlice": SW_H3AudioSlice,
    "SW_H3TrimLead": SW_H3TrimLead,
    "SW_H3SeedPick": SW_H3SeedPick,
    "SW_H3LoopEnd": SW_H3LoopEnd,
    "SW_H3Accumulate": SW_H3Accumulate,
    "SW_H3UnpackCarry": SW_H3UnpackCarry,
    "SW_H3FrameMath": SW_H3FrameMath,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SW_H3LoopStart": "SW H3 循环·起点",
    "SW_H3LoopPlan": "SW H3 本轮计划（循环体内）",
    "SW_H3AudioSlice": "SW H3 音频切片（绝对起点）",
    "SW_H3TrimLead": "SW H3 去重（视听同步）",
    "SW_H3SeedPick": "SW H3 接力帧选择（循环体内）",
    "SW_H3LoopEnd": "SW H3 循环·累积出栈",
    "SW_H3Accumulate": "SW H3 累积（循环体内）",
    "SW_H3UnpackCarry": "SW H3 拆包(carry→图/音)",
    "SW_H3FrameMath": "SW H3 帧算术速算",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
