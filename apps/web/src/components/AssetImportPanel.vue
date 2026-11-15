<script setup>
import { computed, ref } from "vue";
import { client } from "../api";

const emit = defineEmits(["imported"]);
const IDENTITY = [
  ["commit_id", "提交编号"],
  ["project", "项目"],
  ["buggy", "标签"],
  ["author_date", "作者时间"],
];
const NUMERIC = [
  ["la", "新增代码行"],
  ["ld", "删除代码行"],
  ["nf", "改动文件数"],
  ["nd", "改动目录数"],
  ["ns", "改动子系统数"],
  ["ent", "改动熵"],
  ["ndev", "历史作者数"],
  ["age", "文件年龄"],
  ["nuc", "唯一改动文件数"],
  ["aexp", "作者经验"],
  ["arexp", "作者近期经验"],
  ["asexp", "子系统近期经验"],
];
const FIELDS = [...IDENTITY, ...NUMERIC];
const licenses = ["CC-BY-4.0", "Apache-2.0", "MIT", "BSD-3-Clause"];

const content = ref("");
const sourceName = ref("");
const license = ref("CC-BY-4.0");
const header = ref([]);
const mapping = ref({});
const importing = ref(false);
const result = ref(null);
const error = ref(null);

const ready = computed(
  () =>
    header.value.length > 0 &&
    !importing.value &&
    FIELDS.every(([field]) => Boolean(mapping.value[field])),
);

function readText(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsText(file);
  });
}

async function choose(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  sourceName.value = file.name;
  content.value = await readText(file);
  const first = content.value.replace(/^\ufeff/, "").split(/\r?\n/)[0] || "";
  header.value = first
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  mapping.value = {};
  result.value = null;
  error.value = null;
}

async function submit() {
  if (!ready.value) return;
  importing.value = true;
  error.value = null;
  try {
    result.value = await client.request("/public-assets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_name: sourceName.value,
        license: license.value,
        content: content.value,
        mapping: mapping.value,
      }),
    });
    emit("imported");
  } catch (failure) {
    result.value = null;
    error.value = failure;
  } finally {
    importing.value = false;
  }
}
</script>

<template>
  <h2>导入公开数据集</h2>
  <p class="hint">
    选一份按提交粒度的逗号分隔值文件，为十六个必填字段各选一列。服务端不猜列，也不补零：
    缺字段或数值非法会直接拒绝并列出原因。
  </p>
  <label
    >数据文件<input
      type="file"
      accept=".csv,text/csv"
      :disabled="importing"
      @change="choose"
  /></label>
  <p v-if="sourceName" class="hint">
    {{ sourceName }} · {{ header.length }} 列 · {{ content.length }} 字符
  </p>
  <label
    >数据许可<select v-model="license" :disabled="importing">
      <option v-for="name in licenses" :key="name" :value="name">
        {{ name }}
      </option>
    </select></label
  >
  <fieldset v-if="header.length" :disabled="importing">
    <legend>列映射</legend>
    <label v-for="[field, label] in FIELDS" :key="field">
      {{ label
      }}<select v-model="mapping[field]">
        <option value="">请选择列</option>
        <option v-for="column in header" :key="column" :value="column">
          {{ column }}
        </option>
      </select>
    </label>
  </fieldset>
  <p v-if="header.length && !ready" class="hint">
    还有字段没选列，选齐后才能提交。
  </p>
  <button :disabled="!ready" @click="submit">
    {{ importing ? "正在导入…" : "导入为公开资产" }}
  </button>
  <div v-if="result" role="status">
    <p>
      已导入 {{ result.rows }} 条，标签分布 buggy {{ result.labels.buggy }} /
      clean {{ result.labels.clean }}。
    </p>
    <p class="hint">
      资产标识
      {{
        result.asset_id.slice(0, 12)
      }}，可在上方「公开数据来源」里选择后创建数据集。
    </p>
  </div>
  <p v-if="error" role="alert">
    {{ error.code }}：{{ error.message
    }}<span v-if="error.detail">（{{ error.detail }}）</span>
  </p>
</template>
