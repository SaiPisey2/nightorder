import { useMemo } from "react";
import { Background, Controls, Edge, Handle, Node, NodeProps, Position, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

type StepInfo = {
  id: string;
  depends_on: string[];
  status: string;
  executor: string;
  gate: boolean;
  fanOut: { done: number; total: number } | null;
};

function StepNode({ data, selected }: NodeProps<Node<{ step: StepInfo }>>) {
  const s = data.step;
  return (
    <div className={`dag-node ${s.status} ${selected ? "selected" : ""}`}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="t">{s.id}</div>
      <div className="s">
        <span className={`pill ${s.status}`}>{s.status}</span>
        {s.gate && " 🚧"}
        {s.fanOut && ` ⑂ ${s.fanOut.done}/${s.fanOut.total}`}
      </div>
      <div className="s">{s.executor}</div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { step: StepNode };

// Layered layout: level = longest path from a root, computed without deps.
function layout(steps: StepInfo[]): Node[] {
  const level: Record<string, number> = {};
  const byId = Object.fromEntries(steps.map((s) => [s.id, s]));
  const compute = (id: string, seen: Set<string>): number => {
    if (level[id] != null) return level[id];
    if (seen.has(id)) return 0; // cycle guard (validator prevents real cycles)
    seen.add(id);
    const deps = byId[id]?.depends_on?.filter((d) => byId[d]) ?? [];
    level[id] = deps.length ? 1 + Math.max(...deps.map((d) => compute(d, seen))) : 0;
    return level[id];
  };
  steps.forEach((s) => compute(s.id, new Set()));
  const perLevel: Record<number, number> = {};
  return steps.map((s) => {
    const l = level[s.id];
    const idx = perLevel[l] ?? 0;
    perLevel[l] = idx + 1;
    return {
      id: s.id,
      type: "step",
      position: { x: l * 240, y: idx * 105 },
      data: { step: s },
    } as Node;
  });
}

export default function Dag({ steps, onSelect }: { steps: StepInfo[]; onSelect: (id: string) => void }) {
  const nodes = useMemo(() => layout(steps), [JSON.stringify(steps)]);
  const edges: Edge[] = useMemo(
    () =>
      steps.flatMap((s) =>
        s.depends_on.map((d) => ({
          id: `${d}->${s.id}`,
          source: d,
          target: s.id,
          animated: s.status === "running",
          style: { stroke: "var(--border)", strokeWidth: 1.5 },
        }))
      ),
    [JSON.stringify(steps)]
  );

  return (
    <div className="dag-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => onSelect(node.id)}
        nodesDraggable={false}
        nodesConnectable={false}
        colorMode="dark"
      >
        <Background gap={24} color="#1c2230" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export type { StepInfo };
