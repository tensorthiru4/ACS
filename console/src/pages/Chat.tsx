import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type Progress, type SessionInfo } from "../api";
import { describeError, Empty, ErrorBox, PageHead, Spinner, when } from "../ui";

interface Message {
  role: "user" | "assistant" | "error";
  text: string;
  tools?: string[];
  /** Set when the reply came from the fallback model rather than Gemini. */
  model?: string;
}

/** A turn still waiting for its reply, with the progress reported so far. */
interface Pending {
  startedAt: number;
  steps: { text: string; state: "running" | "ok" | "failed" | "info" }[];
  model?: string;
}

// The API keeps conversation state server-side but does not return past turns, so the
// transcript is cached per session in this browser only.
const store = {
  load(id: string): Message[] {
    try {
      return JSON.parse(localStorage.getItem(`acs.chat.${id}`) ?? "[]");
    } catch {
      return [];
    }
  },
  save(id: string, messages: Message[]) {
    try {
      localStorage.setItem(`acs.chat.${id}`, JSON.stringify(messages.slice(-100)));
    } catch {
      /* storage unavailable: transcript simply isn't kept */
    }
  },
  drop(id: string) {
    try {
      localStorage.removeItem(`acs.chat.${id}`);
    } catch {
      /* ignore */
    }
  },
};

const TOOL_LABELS: Record<string, string> = {
  start_store_audit: "Started audit",
  get_store_audit: "Checked audit",
  compile_policy_draft: "Compiled policy",
  request_refund_decision: "Rule engine decision",
  get_po_intake_status: "Checked PO intake",
};

/** Present-tense labels for a tool while it runs. */
const TOOL_RUNNING: Record<string, string> = {
  start_store_audit: "Starting the store audit",
  get_store_audit: "Checking the audit",
  compile_policy_draft: "Compiling the policy",
  request_refund_decision: "Asking the rule engine for a decision",
  get_po_intake_status: "Checking PO intake",
};

const SUGGESTIONS = [
  "What can you help me with?",
  "Order 1001 arrived with 2 broken bottles 3 days ago. Refund ₹2,900 INR?",
  "Audit https://shop.example.com/ for agent readiness",
  "Draft a policy: corked wine reported within 30 days gets a refund up to ₹15,000",
];

// Replies are model output: raw HTML in them is shown as text (react-markdown's default)
// and links open outside the console.
const MARKDOWN: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
};

/** What the conversation list calls a session, before and after the server has named it. */
function sessionTitle(s: SessionInfo): string {
  const first = store.load(s.session_id).find((m) => m.role === "user")?.text;
  return s.title || first?.replace(/\s+/g, " ").trim().slice(0, 48) || "New conversation";
}

/** Folds one progress event into the pending turn's step list. */
function advance(pending: Pending, event: Progress): Pending {
  const steps = pending.steps.map((s) => (s.state === "running" ? { ...s, state: "ok" as const } : s));
  switch (event.type) {
    case "status":
      return { ...pending, steps: [...steps, { text: event.text, state: "running" }] };
    case "tool_call":
      return {
        ...pending,
        steps: [...steps, { text: TOOL_RUNNING[event.name] ?? event.name, state: "running" }],
      };
    case "tool_result": {
      // The tool step was just closed as ok above; mark it failed if the tool said so.
      const last = steps.length - 1;
      if (!event.ok && last >= 0) steps[last] = { ...steps[last], state: "failed" };
      return { ...pending, steps };
    }
    case "model":
      return {
        ...pending,
        model: event.model,
        steps: [...steps, { text: event.text, state: "info" }, { text: "Thinking", state: "running" }],
      };
    default:
      return pending;
  }
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

export default function Chat() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<Record<string, Pending>>({});
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const bottom = useRef<HTMLDivElement>(null);
  // Replies arrive after the user may have switched conversations; they must render only
  // in the conversation they belong to, so async code reads the current one from here.
  const activeRef = useRef<string | null>(null);
  activeRef.current = active;

  const turn = active ? pending[active] : undefined;
  const busy = creating || turn !== undefined;
  const now = useNow(turn !== undefined);

  async function refresh(select?: string) {
    try {
      const list = await api.sessions();
      setSessions(list);
      setActive(select ?? activeRef.current ?? list[0]?.session_id ?? null);
    } catch (e) {
      setError(e);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    setMessages(active ? store.load(active) : []);
  }, [active]);
  useEffect(() => {
    // Block body: scrollIntoView returns a Promise in current browsers, which React would
    // otherwise take for a cleanup function.
    void bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, turn?.steps.length]);

  async function newSession(): Promise<string> {
    const { session_id } = await api.createSession();
    await refresh(session_id);
    return session_id;
  }

  async function remove(id: string) {
    if (!confirm("Delete this conversation?")) return;
    try {
      await api.deleteSession(id);
      store.drop(id);
      if (id === activeRef.current) setActive(null);
      await refresh(id === activeRef.current ? undefined : activeRef.current ?? undefined);
    } catch (e) {
      setError(e);
    }
  }

  /** Appends to a conversation's transcript, and to the screen only if it is still open. */
  function append(id: string, message: Message) {
    const next = [...store.load(id), message];
    store.save(id, next);
    if (activeRef.current === id) setMessages(next);
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setError(null);
    setDraft("");
    let id = activeRef.current;
    if (!id) {
      setCreating(true);
      try {
        id = await newSession();
      } catch (e) {
        setError(e);
        return;
      } finally {
        setCreating(false);
      }
    }
    const sid = id;
    append(sid, { role: "user", text: message });
    setPending((p) => ({ ...p, [sid]: { startedAt: Date.now(), steps: [] } }));
    let model: string | undefined;
    try {
      const reply = await api.chat(sid, message, (event) => {
        if (event.type === "model") model = event.model;
        setPending((p) => (p[sid] ? { ...p, [sid]: advance(p[sid], event) } : p));
      });
      append(sid, { role: "assistant", text: reply.reply, tools: reply.tool_calls, model });
    } catch (e) {
      append(sid, { role: "error", text: describeError(e) });
    } finally {
      setPending((p) => {
        const rest = { ...p };
        delete rest[sid];
        return rest;
      });
      // Picks up the title the server gave the conversation and its new update time.
      void refresh();
    }
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send(draft);
    }
  }

  const elapsed = turn ? Math.max(0, Math.round((now - turn.startedAt) / 1000)) : 0;

  return (
    <>
      <PageHead title="Assistant" sub="Chat with the ACS orchestrator agent. It acts only for your merchant and role." />
      <ErrorBox error={error} />
      <div className="chat">
        <div className="card stack" style={{ padding: 12, minHeight: 0 }}>
          <button className="btn primary" onClick={() => newSession().catch(setError)}>
            + New conversation
          </button>
          <div className="sessions">
            {sessions.length === 0 && <p className="muted small">No conversations yet.</p>}
            {sessions.map((s) => (
              <div
                key={s.session_id}
                className={`session${s.session_id === active ? " active" : ""}`}
                onClick={() => setActive(s.session_id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setActive(s.session_id)}
              >
                <span className="session-text">
                  <span className="session-title" title={sessionTitle(s)}>
                    {sessionTitle(s)}
                  </span>
                  <span className="muted small">
                    {pending[s.session_id] ? <><Spinner /> working…</> : when(s.last_update_time)}
                  </span>
                </span>
                <button
                  className="btn ghost small"
                  aria-label="Delete conversation"
                  disabled={pending[s.session_id] !== undefined}
                  onClick={(e) => {
                    e.stopPropagation();
                    void remove(s.session_id);
                  }}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        </div>

        <div className="card conversation">
          <div className="messages" aria-live="polite">
            {messages.length === 0 && !turn && (
              <Empty>
                <h2>How can I help?</h2>
                <p>Ask about refunds, store audits, policies or purchase orders.</p>
                <div className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="chip" onClick={() => void send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </Empty>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`bubble ${m.role}`}>
                {m.role === "assistant" ? (
                  <div className="markdown">
                    <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN}>
                      {m.text}
                    </Markdown>
                  </div>
                ) : (
                  m.text
                )}
                {((m.tools && m.tools.length > 0) || m.model) && (
                  <div className="tools">
                    {m.tools?.map((t, j) => (
                      <span key={j} className="badge accent">
                        ⚙ {TOOL_LABELS[t] ?? t}
                      </span>
                    ))}
                    {m.model && <span className="badge warn">via {m.model} (fallback)</span>}
                  </div>
                )}
              </div>
            ))}
            {turn && (
              <div className="bubble assistant progress">
                {turn.steps.length === 0 && (
                  <div className="step running">
                    <Spinner /> Sending
                  </div>
                )}
                {turn.steps.map((step, i) => (
                  <div key={i} className={`step ${step.state}`}>
                    {step.state === "running" ? <Spinner /> : <span className="mark">{MARKS[step.state]}</span>}
                    {step.text}
                    {step.state === "running" ? "…" : ""}
                  </div>
                ))}
                <div className="muted small">
                  {elapsed}s{elapsed >= 20 ? " · the free-tier model can take a minute or two" : ""}
                </div>
              </div>
            )}
            <div ref={bottom} />
          </div>
          <form
            className="composer"
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              void send(draft);
            }}
          >
            <textarea
              aria-label="Message"
              placeholder="Message the assistant (Enter to send, Shift+Enter for a new line)"
              value={draft}
              maxLength={4000}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKey}
            />
            <button className="btn primary" disabled={busy || !draft.trim()}>
              Send
            </button>
          </form>
        </div>
      </div>
    </>
  );
}

const MARKS: Record<string, string> = { ok: "✓", failed: "✕", info: "ℹ" };
