import { Component, useEffect, useState, type ReactElement, type ReactNode } from "react";
import { can, logout, watchAccount, type Account, type Role } from "./firebase";
import Login from "./pages/Login";
import Chat from "./pages/Chat";
import Orders from "./pages/Orders";
import Intake from "./pages/Intake";
import Policies from "./pages/Policies";
import Audits from "./pages/Audits";
import Refunds from "./pages/Refunds";
import { Icon, Spinner } from "./ui";

interface Page {
  id: string;
  label: string;
  icon: string;
  minimum: Role;
  render: (account: Account) => ReactElement;
}

const PAGES: Page[] = [
  { id: "chat", label: "Assistant", icon: "chat", minimum: "viewer", render: () => <Chat /> },
  { id: "orders", label: "Draft orders", icon: "orders", minimum: "viewer", render: (a) => <Orders account={a} /> },
  { id: "intake", label: "PO intake", icon: "intake", minimum: "viewer", render: (a) => <Intake account={a} /> },
  { id: "policies", label: "Policies", icon: "policy", minimum: "admin", render: () => <Policies /> },
  { id: "audits", label: "Store audits", icon: "audit", minimum: "viewer", render: (a) => <Audits account={a} /> },
  { id: "refunds", label: "Refunds", icon: "refund", minimum: "analyst", render: () => <Refunds /> },
];

function currentHash(): string {
  return window.location.hash.replace(/^#\/?/, "") || "chat";
}

export default function App() {
  const [account, setAccount] = useState<Account | null | undefined>(undefined);
  const [page, setPage] = useState(currentHash());

  useEffect(() => watchAccount(setAccount), []);
  useEffect(() => {
    const onHash = () => setPage(currentHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (account === undefined) {
    return (
      <div className="login-wrap">
        <div className="row muted">
          <Spinner /> Loading…
        </div>
      </div>
    );
  }
  if (account === null) return <Login />;
  if (!account.merchantId || !account.role) return <NoAccess account={account} />;

  const visible = PAGES.filter((p) => can(account, p.minimum));
  const active = visible.find((p) => p.id === page) ?? visible[0];

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Main">
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          <div>
            <b>Merchant Console</b>
            <span>Autonomous Commerce Suite</span>
          </div>
        </div>
        {visible.map((p) => (
          <a
            key={p.id}
            href={`#/${p.id}`}
            className={`nav-item${p.id === active.id ? " active" : ""}`}
            aria-current={p.id === active.id ? "page" : undefined}
          >
            <Icon name={p.icon} />
            {p.label}
          </a>
        ))}
        <div className="spacer" />
        <div className="who">
          <b title={account.email}>{account.email}</b>
          merchant <span className="mono">{account.merchantId}</span> · {account.role}
          <button className="nav-item" style={{ padding: "8px 0", marginTop: 6 }} onClick={() => logout()}>
            <Icon name="logout" /> Sign out
          </button>
        </div>
      </nav>
      <main>
        <PageBoundary key={active.id}>{active.render(account)}</PageBoundary>
      </main>
    </div>
  );
}

/** Keeps a crash in one page from blanking the whole console. */
class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    console.error("page crashed", error);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="card stack">
        <h2>This page hit an error</h2>
        <p className="muted">The rest of the console still works. Reload to try again.</p>
        <button className="btn" onClick={() => window.location.reload()}>Reload</button>
      </div>
    );
  }
}

function NoAccess({ account }: { account: Account }) {
  return (
    <div className="login-wrap">
      <div className="card login stack">
        <h1>No merchant access yet</h1>
        <p className="muted">
          <b>{account.email}</b> signed in, but this account is not linked to a merchant. An
          operator must grant a role:
        </p>
        <pre className="mono small" style={{ whiteSpace: "pre-wrap" }}>
          acs-admin set-claims --uid {account.user.uid} --merchant m1 --role admin
        </pre>
        <p className="muted small">Then sign out and back in.</p>
        <button className="btn" onClick={() => logout()}>
          Sign out
        </button>
      </div>
    </div>
  );
}
