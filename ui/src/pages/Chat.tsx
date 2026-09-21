import { useEffect, useRef, useState } from "react";
import { Session, api } from "../api";

type Msg = { role: "user" | "assistant"; content: string; trace?: { tool: string; args: any }[] };

const SUGGESTIONS = [
  "What's the status of the latest run?",
  "Why did the last run fail?",
  "Is the platform healthy?",
  "Draft a pipeline that runs a script daily at 6am with a sign-off gate",
  "Validate my pipeline yaml and fix any errors",
];

function storageKey(project: string) {
  return `bosun.chat.${project}`;
}

// Minimal renderer: fenced code blocks get <pre> + an "open in editor" button
// for yaml; prose gets **bold** / `inline code` / line breaks.
function Prose({ text }: { text: string }) {
  const bits = text.split(/(\*\*[^*]+\*\*|`[^`\n]+`)/g);
  return (
    <span style={{ whiteSpace: "pre-wrap" }}>
      {bits.map((b, i) => {
        if (b.startsWith("**") && b.endsWith("**")) return <b key={i}>{b.slice(2, -2)}</b>;
        if (b.startsWith("`") && b.endsWith("`"))
          return <code key={i} style={{ fontFamily: "var(--mono)", fontSize: 12.5, background: "rgba(109,141,255,0.12)", padding: "1px 4px", borderRadius: 4 }}>{b.slice(1, -1)}</code>;
        return <span key={i}>{b}</span>;
      })}
    </span>
  );
}

function Rendered({ text, onOpenYaml }: { text: string; onOpenYaml: (yaml: string) => void }) {
  const parts = text.split(/```(\w*)\n([\s\S]*?)```/g);
  const out: React.ReactNode[] = [];
  for (let i = 0; i < parts.length; i += 3) {
    if (parts[i]) out.push(<Prose key={i} text={parts[i]} />);
    if (i + 2 < parts.length) {
      const lang = parts[i + 1];
      const code = parts[i + 2];
      out.push(
        <div key={i + 1} style={{ margin: "8px 0" }}>
          <pre className="code">{code}</pre>
          {(lang === "yaml" || lang === "yml") && (
            <button className="btn small" style={{ marginTop: 6 }} onClick={() => onOpenYaml(code)}>
              ⤴ open in spec editor
            </button>
          )}
        </div>
      );
    }
  }
  return <>{out}</>;
}

export default function Chat({ session, goPipelines }: { session: Session; goPipelines: () => void }) {
  const [messages, setMessages] = useState<Msg[]>(() => {
    const raw = localStorage.getItem(storageKey(session.project));
    return raw ? JSON.parse(raw) : [];
  });
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem(storageKey(session.project), JSON.stringify(messages.slice(-40)));
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(text?: string) {
    const content = (text ?? input).trim();
    if (!content || busy) return;
    setError(null);
    setInput("");
    const next: Msg[] = [...messages, { role: "user" as const, content }];
    setMessages(next);
    setBusy(true);
    try {
      const resp = await api.post(
        `/projects/${session.project}/chat`,
        { messages: next.map((m) => ({ role: m.role, content: m.content })) },
        session.key
      );
      setMessages([...next, { role: "assistant", content: resp.reply, trace: resp.tool_trace }]);
    } catch (e: any) {
      setError(e.message);
      setMessages(next);
    } finally {
      setBusy(false);
    }
  }

  function openYaml(yaml: string) {
    localStorage.setItem("bosun.editor-draft", yaml);
    goPipelines();
  }

  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 120px)" }}>
      <h3>
        Assistant <span className="muted">— live platform access (read-only); it proposes, you apply</span>
        {messages.length > 0 && (
          <button className="btn ghost small" style={{ float: "right" }}
                  onClick={() => setMessages([])}>clear chat</button>
        )}
      </h3>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ flex: 1, overflowY: "auto", padding: "4px 2px" }}>
        {messages.length === 0 && (
          <div style={{ marginTop: 30, textAlign: "center" }}>
            <div className="muted" style={{ marginBottom: 14 }}>
              Ask about run status, failures, timings, gates — or have it write / fix pipeline YAML.
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "center" }}>
              {SUGGESTIONS.map((s) => (
                <button key={s} className="btn ghost small" onClick={() => send(s)}>{s}</button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ margin: "10px 0", display: "flex", justifyContent: m.role === "user" ? "flex-end" : "flex-start" }}>
            <div style={{
              maxWidth: "78%", padding: "10px 14px", borderRadius: 12, fontSize: 13.5,
              background: m.role === "user" ? "var(--accent)" : "var(--panel2)",
              color: m.role === "user" ? "#0b0e14" : "var(--text)",
              border: m.role === "user" ? "none" : "1px solid var(--border)",
            }}>
              {m.trace && m.trace.length > 0 && (
                <div className="muted" style={{ fontSize: 11.5, marginBottom: 6, fontFamily: "var(--mono)" }}>
                  {m.trace.map((t, j) => (
                    <div key={j}>🔧 {t.tool}({Object.entries(t.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v).slice(0, 40)}`).join(", ")})</div>
                  ))}
                </div>
              )}
              <Rendered text={m.content} onOpenYaml={openYaml} />
            </div>
          </div>
        ))}
        {busy && (
          <div style={{ margin: "10px 0" }}>
            <span className="pill running">assistant is checking the platform…</span>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
        <textarea
          style={{ minHeight: 44, maxHeight: 120, resize: "vertical", flex: 1 }}
          placeholder={`Ask about ${session.project}… (Enter to send, Shift+Enter for newline)`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="btn" disabled={busy || !input.trim()} onClick={() => send()}>Send</button>
      </div>
    </div>
  );
}
