import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts", "src/**/*.test.ts", "src/**/*.test.tsx", "worker/**/*.test.ts"],
  },
});
