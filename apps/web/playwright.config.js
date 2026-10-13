import { defineConfig } from "@playwright/test";
import path from "node:path";
const evidence =
  process.env.DG_EVIDENCE_DIR || path.resolve("../../runtime/acceptance");
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  expect: { timeout: 15000 },
  outputDir: path.join(evidence, "browser-output"),
  reporter: [
    ["list"],
    ["junit", { outputFile: path.join(evidence, "browser.xml") }],
    [
      "html",
      { outputFolder: path.join(evidence, "browser-report"), open: "never" },
    ],
  ],
  use: {
    baseURL: process.env.DG_E2E_BASE_URL || "http://127.0.0.1:18080",
    browserName: "chromium",
    channel: process.env.DG_BROWSER_CHANNEL || undefined,
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
    viewport: { width: 1280, height: 800 },
  },
});
