import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  test: {
    include: ["src/**/*.test.js"],
    environment: "jsdom",
    reporters: [
      "default",
      ["junit", { outputFile: "../../runtime/acceptance/web-unit.xml" }],
    ],
  },
});
