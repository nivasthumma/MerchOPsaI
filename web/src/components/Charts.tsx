// Inline SVG. Two forms, each chosen for the job its data does:
//
//   payment-method success change  → polarity → diverging bars on a zero axis
//   worst failure hours            → ranked magnitude → single-hue bars
//
// Revenue previous-vs-current is deliberately *not* a chart: two numbers with a
// percentage between them is a stat, and drawing two bars would add ink without
// adding information.
//
// Colours come from the .viz custom properties, which hold a validated
// blue↔red diverging pair. Identity is never carried by colour alone: every bar
// is directly labelled and the same numbers appear in the evidence table below.

import { useState } from "react";

interface Tip { x: number; y: number; label: string; value: string; note?: string }

function useTip() {
  const [tip, setTip] = useState<Tip | null>(null);
  const node = tip ? (
    <div className="viz-tip" style={{ left: tip.x, top: tip.y }}>
      <div>{tip.label}</div>
      <div><span className="k">{tip.note ?? "value"}</span> {tip.value}</div>
    </div>
  ) : null;
  return { tip, setTip, node };
}

export interface ChangePoint { label: string; value: number }

/** Diverging bars around a zero axis. Positive right in blue, negative left in
 *  red, neutral gap at zero — the pair reads as opposite rather than as two
 *  arbitrary series. */
export function ChangeChart({ points, unit = "pp" }: { points: ChangePoint[]; unit?: string }) {
  const { setTip, node } = useTip();
  if (!points.length) return null;

  const rowH = 30;
  const h = points.length * rowH + 26;
  const w = 640;
  const labelW = 108;
  const plotW = w - labelW - 56;
  const mid = labelW + plotW / 2;
  const max = Math.max(...points.map((p) => Math.abs(p.value))) || 1;
  // The value sits outside the end of its bar, so the longest bar has to stop
  // short enough to leave room for it. At `plotW / 2 - 8` the biggest negative
  // bar reached the label column and its number printed on top of the category
  // name — "upi" and "-18.6" rendered as one smear.
  const valueGutter = 46;
  const scale = (v: number) => (v / max) * (plotW / 2 - valueGutter);

  return (
    <div className="viz">
      <svg viewBox={`0 0 ${w} ${h}`} role="img"
           aria-label="Change in payment success rate by method, in percentage points">
        {points.map((p, i) => {
          const y = i * rowH + 8;
          const len = Math.abs(scale(p.value));
          const pos = p.value >= 0;
          const x = pos ? mid : mid - len;
          const r = Math.min(4, len);
          return (
            <g key={p.label} className="bar"
               onMouseMove={(e) => setTip({
                 x: e.nativeEvent.offsetX, y: e.nativeEvent.offsetY,
                 label: p.label,
                 value: `${p.value > 0 ? "+" : ""}${p.value} ${unit}`,
                 note: pos ? "improved" : "declined",
               })}
               onMouseLeave={() => setTip(null)}>
              <text className="label" x={labelW - 12} y={y + 14} textAnchor="end">{p.label}</text>
              {/* 4px rounded data-end, square against the zero axis. */}
              <path
                d={pos
                  ? `M${x} ${y + 3} h${len - r} a${r} ${r} 0 0 1 ${r} ${r} v${14 - 2 * r} a${r} ${r} 0 0 1 ${-r} ${r} h${-(len - r)} z`
                  : `M${x + len} ${y + 3} h${-(len - r)} a${r} ${r} 0 0 0 ${-r} ${r} v${14 - 2 * r} a${r} ${r} 0 0 0 ${r} ${r} h${len - r} z`}
                fill={pos ? "var(--viz-pos)" : "var(--viz-neg)"} />
              <text className="value" x={pos ? x + len + 8 : x - 8} y={y + 14}
                    textAnchor={pos ? "start" : "end"}>
                {p.value > 0 ? "+" : ""}{p.value}
              </text>
            </g>
          );
        })}
        <line className="zero" x1={mid} y1={4} x2={mid} y2={points.length * rowH + 4} />
        <text className="label" x={mid} y={points.length * rowH + 20} textAnchor="middle">
          no change
        </text>
      </svg>
      <div className="viz-legend">
        <span><i style={{ background: "var(--viz-pos)" }} /> improved</span>
        <span><i style={{ background: "var(--viz-neg)" }} /> declined</span>
        <span>percentage points, period over period</span>
      </div>
      {node}
    </div>
  );
}

export interface RankPoint { label: string; value: number; note?: string }

/** Ranked magnitude, one hue. Length carries the value; colour carries nothing,
 *  so there is no legend to read. */
export function RankChart(
  { points, unit = "%", caption }: { points: RankPoint[]; unit?: string; caption?: string },
) {
  const { setTip, node } = useTip();
  if (!points.length) return null;

  const rowH = 28;
  const h = points.length * rowH + 4;
  const w = 640;
  const labelW = 70;
  const plotW = w - labelW - 64;
  const max = Math.max(...points.map((p) => p.value)) || 1;

  return (
    <div className="viz">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={caption ?? "Ranked values"}>
        {points.map((p, i) => {
          const y = i * rowH + 6;
          const len = (p.value / max) * plotW;
          const r = Math.min(4, len);
          return (
            <g key={p.label} className="bar"
               onMouseMove={(e) => setTip({
                 x: e.nativeEvent.offsetX, y: e.nativeEvent.offsetY,
                 label: p.label, value: `${p.value}${unit}`, note: p.note ?? "failure rate",
               })}
               onMouseLeave={() => setTip(null)}>
              <text className="label" x={labelW - 12} y={y + 13} textAnchor="end">{p.label}</text>
              <path
                d={`M${labelW} ${y + 2} h${len - r} a${r} ${r} 0 0 1 ${r} ${r} v${12 - 2 * r} a${r} ${r} 0 0 1 ${-r} ${r} h${-(len - r)} z`}
                fill="var(--viz-single)" />
              <text className="value" x={labelW + len + 8} y={y + 13}>{p.value}{unit}</text>
            </g>
          );
        })}
      </svg>
      {caption ? <div className="viz-legend"><span>{caption}</span></div> : null}
      {node}
    </div>
  );
}
