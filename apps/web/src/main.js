import { createApp } from "vue";
import { ElButton, ElTag } from "element-plus";
import "element-plus/theme-chalk/base.css";
import "element-plus/theme-chalk/el-button.css";
import "element-plus/theme-chalk/el-tag.css";
import App from "./App.vue";
import { router } from "./router";
import "./style.css";

createApp(App)
  .use(router)
  .component("ElButton", ElButton)
  .component("ElTag", ElTag)
  .mount("#app");
