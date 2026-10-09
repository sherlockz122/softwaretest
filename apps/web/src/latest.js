import { onScopeDispose, ref } from "vue";
export function useLatest(loader) {
  const data = ref(null);
  const loading = ref(false);
  const error = ref(null);
  let version = 0;
  let controller;
  let lastInput = Symbol("initial");
  async function load(input) {
    const current = ++version;
    if (input !== lastInput) data.value = null;
    lastInput = input;
    controller?.abort();
    controller = new AbortController();
    loading.value = true;
    error.value = null;
    try {
      const result = await loader(input, controller.signal);
      if (current === version) data.value = result;
    } catch (failure) {
      if (current === version && !controller.signal.aborted)
        error.value = failure;
    } finally {
      if (current === version) loading.value = false;
    }
  }
  onScopeDispose(() => {
    version += 1;
    controller?.abort();
  });
  return { data, loading, error, load };
}
