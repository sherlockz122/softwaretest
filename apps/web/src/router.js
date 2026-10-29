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
import ModelsPage from "./views/ModelsPage.vue";
import ModelPage from "./views/ModelPage.vue";
import SemanticAssetsPage from "./views/SemanticAssetsPage.vue";
import SemanticAssetPage from "./views/SemanticAssetPage.vue";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/semantic-assets",
      component: SemanticAssetsPage,
      meta: { auth: true },
    },
    {
      path: "/semantic-assets/:id",
      component: SemanticAssetPage,
      meta: { auth: true },
    },
    { path: "/models", component: ModelsPage, meta: { auth: true } },
    { path: "/model-runs/:id", component: ModelPage, meta: { auth: true } },
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
