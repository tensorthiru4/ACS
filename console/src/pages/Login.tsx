import { useState, type FormEvent } from "react";
import { login, resetPassword } from "../firebase";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(email.trim(), password);
    } catch {
      // Never reveal whether the e-mail exists.
      setError("Sign-in failed. Check your e-mail and password.");
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setError("");
    if (!email.trim()) {
      setError("Enter your e-mail first.");
      return;
    }
    try {
      await resetPassword(email.trim());
    } catch {
      /* same message either way */
    }
    setNotice("If that account exists, a password-reset e-mail is on its way.");
  }

  return (
    <div className="login-wrap">
      <form className="card login stack" onSubmit={submit}>
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          <div>
            <b>Merchant Console</b>
            <span>Autonomous Commerce Suite</span>
          </div>
        </div>
        <label>
          E-mail
          <input type="email" autoComplete="username" required value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <div className="alert bad" role="alert">{error}</div>}
        {notice && <div className="alert info">{notice}</div>}
        <button className="btn primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <button type="button" className="btn ghost small" onClick={reset}>
          Forgot password?
        </button>
      </form>
    </div>
  );
}
