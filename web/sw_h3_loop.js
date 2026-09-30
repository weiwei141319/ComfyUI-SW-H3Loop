import { app } from "../../scripts/app.js";

const H3_FPS = 24;
const VALID_GUIDE = new Set([1, 5, 22, 39, 56, 73, 90, 107, 124, 141, 158, 175,
                             192, 209, 226, 243, 260, 277, 294, 311, 328, 345, 362]);

function snapLength(frames) {
    frames = Math.floor(frames);
    if (frames < 5) return 5;
    return frames - ((frames - 5) % 17);
}

function isValidGuide(frames) {
    return frames === 0 || VALID_GUIDE.has(frames);
}

// Python 端 _solve_count 的镜像：带 0.5s 容差，避免「恰好整除时多一段」。
// 例：目标 30s / L=243 / K=5 → 成片 29.75s，容差内即收口为 3 段。
const TOLERANCE_SEC = 0.5;

function solveCount(targetSec, segSec, stepSec) {
    if (segSec >= targetSec - TOLERANCE_SEC) return 1;
    if (stepSec <= 0) return 1;
    let n = 1;
    while (segSec + (n - 1) * stepSec < targetSec - TOLERANCE_SEC) {
        n += 1;
        if (n > 10000) break;
    }
    return n;
}

function computePlan(targetSec, segFrames, ovlFrames) {
    const seg = snapLength(segFrames);
    let ovl = Math.max(0, Math.floor(ovlFrames));
    if (ovl >= seg) ovl = Math.max(0, seg - 17);
    const step = seg - ovl;
    const segSec = seg / H3_FPS;
    const stepSec = step / H3_FPS;
    const count = solveCount(targetSec, segSec, stepSec);
    // ★ 成片 = N × (L-K) = N × step
    // 每轮都丢 K 帧：首轮丢参考图种子，其余轮丢上一轮重叠。
    // 钉帧数 == 丢帧数 → 相邻两段在 K 帧上融合为一份。
    const outFrames = count * step;
    const starts = [];
    for (let i = 0; i < count; i++) starts.push((i * step) / H3_FPS);
    return {
        seg, ovl, step, segSec, stepSec, count,
        outFrames, outSec: outFrames / H3_FPS,
        starts,
        guideOk: isValidGuide(ovl),
        guideSnap: ovl - ((ovl - 5) % 17),
        trained: seg >= 124 && seg <= 362,
    };
}

function getWidget(node, name) {
    if (!node.widgets) return undefined;
    return node.widgets.find((w) => w.name === name);
}

function readArgs(node) {
    const g = (n, d) => {
        const w = getWidget(node, n);
        return w ? Number(w.value) : d;
    };
    return {
        target: g("目标时长秒", 30.0),
        seg: g("单段帧数", 243),
        ovl: g("重叠帧数", 5),
    };
}

function planText(p) {
    const secs = (v) => v.toFixed(4);
    const lines = [
        `单段  ${p.seg} 帧 / ${secs(p.segSec)} s`,
        `重叠  ${p.ovl} 帧 / ${secs(p.ovl / H3_FPS)} s ${p.guideOk ? "(合法)" : "(⚠ 非法档位)"}`,
        `步长  ${p.step} 帧 / ${secs(p.stepSec)} s`,
        `段数  ${p.count}`,
        `起点  ${p.starts.map((s) => secs(s)).join(" / ")}`,
        `成片  ${p.outFrames} 帧 / ${secs(p.outSec)} s`,
    ];
    return lines.join("\n");
}

function ensurePanel(node) {
    if (node.__swH3Panel) return node.__swH3Panel;
    const el = document.createElement("div");
    el.style.cssText = [
        "position:absolute", "left:8px", "top:26px", "padding:6px 8px",
        "font:11px/1.5 ui-monospace,monospace", "white-space:pre",
        "border-radius:6px", "pointer-events:none", "z-index:10",
        "background:rgba(24,95,165,0.10)", "color:#185FA5",
        "border:0.5px solid rgba(24,95,165,0.35)",
    ].join(";");
    node.__swH3Panel = el;
    if (node.addDOMWidget) {
        try {
            const w = node.addDOMWidget("sw_plan", "div", el, { serialize: false });
            w.computeSize = () => [210, 96];
            w.draw = () => {};
        } catch (e) { /* 旧前端无此 API，忽略 */ }
    }
    return el;
}

function refreshPanel(node) {
    const el = ensurePanel(node);
    const a = readArgs(node);
    const p = computePlan(a.target, a.seg, a.ovl);
    el.textContent = planText(p);
    el.style.background = p.guideOk ? "rgba(24,95,165,0.10)" : "rgba(226,75,74,0.12)";
    el.style.color = p.guideOk ? "#185FA5" : "#A32D2D";
    el.style.borderColor = p.guideOk ? "rgba(24,95,165,0.35)" : "rgba(226,75,74,0.5)";
    node.setSize([Math.max(node.size[0], 260), Math.max(node.size[1], 240)]);
}

app.registerExtension({
    name: "SW.H3Loop.PlanPanel",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SW_H3LoopPlan") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated ? onCreated.apply(this, arguments) : undefined;
            const self = this;
            ensurePanel(self);
            for (const nm of ["目标时长秒", "单段帧数", "重叠帧数", "起点偏移秒", "起点微调帧"]) {
                const w = getWidget(self, nm);
                if (w && !w.__swHooked) {
                    w.__swHooked = true;
                    const cb = w.callback;
                    w.callback = function (v) {
                        const out = cb ? cb.apply(this, arguments) : undefined;
                        try { refreshPanel(self); } catch (e) { /* noop */ }
                        return out;
                    };
                }
            }
            setTimeout(() => { try { refreshPanel(self); } catch (e) { /* noop */ } }, 0);
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
            const self = this;
            setTimeout(() => { try { refreshPanel(self); } catch (e) { /* noop */ } }, 0);
            return r;
        };
    },
});

export { computePlan, snapLength, isValidGuide, solveCount };
