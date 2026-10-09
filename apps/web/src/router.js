import { createRouter, createWebHistory } from "vue-router";
import { client } from "./api";
import LoginPage from "./views/LoginPage.vue";
import TasksPage from "./views/TasksPage.vue";
import TaskPage from "./views/TaskPage.vue";
import HealthPage from "./views/HealthPage.vue";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/tasks" },
    { path: "/login", component: LoginPage },
    { path: "/tasks", component: TasksPage, meta: { auth: true } },
    { path: "/tasks/:id", component: TaskPage, meta: { auth: true } },
    { path: "/health", component: HealthPage },
    { path: "/:pathMatch(.*)*", redirect: "/tasks" },
  ],
});
router.beforeEach((to) => {
  if (to.meta.auth && !client.state.user)
    return { path: "/login", query: { next: to.fullPath } };
});
