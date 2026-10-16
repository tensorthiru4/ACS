import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api, type SessionInfo } from "../api";
import { describeError, Empty, ErrorBox, PageHead, Spinner, when } from "../ui";

interface Message {
  role: "user" | "assistant" | "error";
  text: string;
  tools?: string[];
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

const SUGGESTIONS = [
  "What can you help me with?",
  "Order 1001 arrived with 2 broken bottles 3 days ago. Refund ₹2,900 INR?",
  "Audit https://shop.example.com/ for agent readiness",
  "Draft a policy: corked wine reported within 30 days gets a refund up to ₹15,000",
];

export default function Chat() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const bottom = useRef<HTMLDivElement>(null);

  async function refresh(select?: string) {
    try {
      const list = await api.sessions();
      setSessions(list);
      const next = select ?? active ?? list[0]?.session_id ?? null;
      setActive(next);
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
  }, [messages, sending]);

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
      if (id === active) setActive(null);
      await refresh(id === active ? undefined : active ?? undefined);
    } catch (e) {
      setError(e);
    }
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || sending) return;
    setError(null);
    setSending(true);
    setDraft("");
    try {
      const id = active ?? (await newSession());
      const before = [...store.load(id), { role: "user", text: message } as Message];
      setMessages(before);
      store.save(id, before);
      try {
        const reply = await api.chat(id, message);
        const after = [...before, { role: "assistant", text: reply.reply, tools: reply.tool_calls } as Message];
        setMessages(after);
        store.save(id, after);
      } catch (e) {
        const after = [...before, { role: "error", text: describeError(e) } as Message];
        setMessages(after);
        store.save(id, after);
      }
    } catch (e) {
      setError(e);
    } finally {
      setSending(false);
    }
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send(draft);
    }
  }

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
                <span>
                  {store.load(s.session_id).find((m) => m.role === "user")?.text.slice(0, 28) || "Conversation"}
                  <br />
                  <span className="muted small">{when(s.last_update_time)}</span>
                </span>
                <button
                  className="btn ghost small"
                  aria-label="Delete conversation"
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
            {messages.length === 0 && !sending && (
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
                {m.text}
                {m.tools && m.tools.length > 0 && (
                  <div className="tools">
                    {m.tools.map((t, j) => (
                      <span key={j} className="badge accent">
                        ⚙ {TOOL_LABELS[t] ?? t}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
            {sending && (
              <div className="bubble assistant row">
                <Spinner /> Thinking…
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
            <button className="btn primary" disabled={sending || !draft.trim()}>
              Send
            </button>
          </form>
        </div>
      </div>
    </>
  );
}
