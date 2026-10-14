import { createRouter, createWebHistory } from "vue-router";
import { client } from "./api";
import LoginPage from "./views/LoginPage.vue";
import TasksPage from "./views/TasksPage.vue";
import TaskPage from "./views/TaskPage.vue";
import HealthPage from "./views/HealthPage.vue";
import RepositoriesPage from "./views/RepositoriesPage.vue";
import RepositoryPage from "./views/RepositoryPage.vue";
import DatasetsPage from "./views/DatasetsPage.vue";
import DatasetPage from "./views/DatasetPage.vue";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/datasets", component: DatasetsPage, meta: { auth: true } },
    { path: "/datasets/:id", component: DatasetPage, meta: { auth: true } },
    { path: "/", redirect: "/tasks" },
    { path: "/login", component: LoginPage },
    { path: "/tasks", component: TasksPage, meta: { auth: true } },
    { path: "/tasks/:id", component: TaskPage, meta: { auth: true } },
    {
      path: "/repositories",
      component: RepositoriesPage,
      meta: { auth: true },
    },
    {
      path: "/repositories/:id",
      component: RepositoryPage,
      meta: { auth: true },
    },
    { path: "/health", component: HealthPage },
    { path: "/:pathMatch(.*)*", redirect: "/tasks" },
  ],
});
router.beforeEach((to) => {
  if (to.meta.auth && !client.state.user)
    return { path: "/login", query: { next: to.fullPath } };
});
