import js from "@eslint/js";
import vue from "eslint-plugin-vue";
import globals from "globals";
export default [
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "test-results/**",
      "playwright-report/**",
    ],
  },
  js.configs.recommended,
  ...vue.configs["flat/essential"],
  { languageOptions: { globals: { ...globals.browser, ...globals.node } } },
];
