import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./firebase", () => ({ auth: { currentUser: { getIdToken: async () => "tok" } } }));

const { tracked, TRACKING, ApiError } = await import("./api");

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** A POST that resolves when `finish` is called, plus a scripted progress endpoint. */
function server(progress: (n: number) => Response) {
  let finish: (r: Response) => void = () => undefined;
  const posts: RequestInit[] = [];
  let polls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      if (init.method === "POST") {
        posts.push(init);
        return new Promise<Response>((resolve, reject) => {
          finish = resolve;
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      expect(url).toMatch(/\/v1\/console\/progress\/[0-9a-f-]{36}$/);
      return Promise.resolve(progress(++polls));
    }),
  );
  return { posts, finish: (r: Response) => finish(r), polls: () => polls };
}

const tick = () => new Promise((r) => setTimeout(r, 15));

describe("tracked", () => {
  beforeEach(() => Object.assign(TRACKING, { pollMs: 5, stallMs: 60, limitMs: 5_000 }));
  afterEach(() => vi.unstubAllGlobals());

  it("passes each event on once, in order, then returns the POST's body", async () => {
    const events = [
      { seq: 1, type: "status", text: "Thinking", elapsed_s: 0 },
      { seq: 2, type: "tool_call", name: "compile_policy_draft", elapsed_s: 1 },
    ];
    const s = server((n) =>
      n === 1 ? json(404, { error: "not found" }) : json(200, { events: events.slice(0, n - 1), elapsed_s: n, done: false }),
    );
    const seen: unknown[] = [];
    const call = tracked<{ ok: boolean }>("/v1/policies/compile", { a: 1 }, (p) => seen.push(p));
    while (s.polls() < 4) await tick();
    s.finish(json(200, { ok: true }));

    expect(await call).toEqual({ ok: true });
    const real = seen.filter((p: any) => p.type !== "heartbeat");
    expect(real).toEqual(events);
    expect(seen.some((p: any) => p.type === "heartbeat")).toBe(true);
    const headers = s.posts[0].headers as Record<string, string>;
    expect(headers["X-Progress-Id"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("surfaces the POST's own error", async () => {
    const s = server(() => json(404, {}));
    const call = tracked("/v1/policies/compile", {}, () => undefined);
    await tick();
    s.finish(json(503, { error: "The AI model is busy or out of quota. Try again shortly." }));
    await expect(call).rejects.toMatchObject({ status: 503, message: expect.stringMatching(/out of quota/) });
  });

  it("gives up when the server's progress stops moving", async () => {
    server(() => json(200, { events: [], elapsed_s: 3, done: false }));
    const call = tracked("/v1/policies/compile", {}, () => undefined);
    await expect(call).rejects.toBeInstanceOf(ApiError);
    await expect(call).rejects.toMatchObject({ status: 504, message: expect.stringMatching(/stopped responding/) });
  });

  it("does not call a server it never heard from lost before the limit", async () => {
    TRACKING.limitMs = 200;
    const s = server(() => json(404, {}));
    const call = tracked("/v1/policies/compile", {}, () => undefined);
    await new Promise((r) => setTimeout(r, 120)); // past stallMs, still within the limit
    s.finish(json(200, { ok: 1 }));
    expect(await call).toEqual({ ok: 1 });
  });
});
