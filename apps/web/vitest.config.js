import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  test: {
    include: ["src/**/*.test.js"],
    environment: "jsdom",
    reporters: [
      "default",
      [
        "junit",
        {
          outputFile: process.env.DG_EVIDENCE_DIR
            ? `${process.env.DG_EVIDENCE_DIR}/web-unit.xml`
            : "../../runtime/acceptance/web-unit.xml",
        },
      ],
    ],
  },
});
