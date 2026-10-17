import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Browsers compile <input pattern> with the `v` flag, where an unescaped "-" in a
// character class is a syntax error and the field silently stops validating.
describe("input pattern attributes", () => {
  const dir = join(__dirname, "pages");
  const patterns = readdirSync(dir).flatMap((file) =>
    [...readFileSync(join(dir, file), "utf8").matchAll(/pattern="([^"]+)"/g)].map((m) => [file, m[1]]),
  );

  it("finds the patterns", () => expect(patterns.length).toBeGreaterThan(0));

  it.each(patterns)("%s: %s compiles with the v flag", (_file, pattern) => {
    expect(() => new RegExp(`^(?:${pattern})$`, "v")).not.toThrow();
  });
});
