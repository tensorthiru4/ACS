import { initializeApp } from "firebase/app";
import {
  getAuth,
  onIdTokenChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut,
  type User,
} from "firebase/auth";

const app = initializeApp({
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
});

export const auth = getAuth(app);

export type Role = "viewer" | "analyst" | "admin";

export interface Account {
  user: User;
  email: string;
  merchantId: string | null;
  role: Role | null;
}

const RANK: Record<Role, number> = { viewer: 0, analyst: 1, admin: 2 };

/** Mirrors the orchestrator's minimum-role table (security/principals.py). */
export function can(account: Account, minimum: Role): boolean {
  return account.role !== null && RANK[account.role] >= RANK[minimum];
}

/** Reads the merchant_id / role custom claims the orchestrator authorizes on. */
export async function toAccount(user: User): Promise<Account> {
  const token = await user.getIdTokenResult();
  const role = token.claims.role;
  const merchant = token.claims.merchant_id;
  return {
    user,
    email: user.email ?? "",
    merchantId: typeof merchant === "string" ? merchant : null,
    role: role === "viewer" || role === "analyst" || role === "admin" ? role : null,
  };
}

export function watchAccount(callback: (account: Account | null) => void): () => void {
  return onIdTokenChanged(auth, async (user) => callback(user ? await toAccount(user) : null));
}

export const login = (email: string, password: string) =>
  signInWithEmailAndPassword(auth, email, password);
export const logout = () => signOut(auth);
export const resetPassword = (email: string) => sendPasswordResetEmail(auth, email);
