import { describe, expect, it, vi } from "vitest";

vi.mock("./firebase", () => ({ auth: { currentUser: null } }));

const { errorMessage } = await import("./api");
const { money } = await import("./ui");

describe("errorMessage", () => {
  it("prefers the orchestrator's error, then FastAPI detail, then gateway message", () => {
    expect(errorMessage(409, { error: "draft is incomplete" })).toBe("draft is incomplete");
    expect(errorMessage(504, { detail: "the assistant timed out" })).toBe("the assistant timed out");
    expect(errorMessage(401, { message: "Jwt is expired" })).toBe("Jwt is expired");
  });

  it("falls back to a readable message per status", () => {
    expect(errorMessage(401, undefined)).toMatch(/sign in again/i);
    expect(errorMessage(403, {})).toMatch(/role/i);
    expect(errorMessage(503, "oops")).toMatch(/unavailable/i);
  });
});

describe("money", () => {
  it("formats minor units", () => {
    expect(money(290000, "INR")).toContain("2,900");
    expect(money(1250, "USD")).toContain("12.50");
    expect(money(500, "JPY")).toContain("500");
    expect(money(null, "INR")).toBe("—");
  });
});
