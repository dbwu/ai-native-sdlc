<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  changes: { path: string; before: string | null; after: string | null }[]
}>()
const selectedPath = ref('')
const selectedChange = computed(() => props.changes.find(change => change.path === selectedPath.value))

watch(() => props.changes, changes => {
  if (!changes.some(change => change.path === selectedPath.value)) selectedPath.value = changes[0]?.path || ''
}, { immediate: true })

function changeLabel(change: { before: string | null; after: string | null }) {
  if (change.before === null) return '新增'
  if (change.after === null) return '删除'
  return '修改'
}
</script>

<template>
  <div class="code-review">
    <label class="field">查看文件变更（{{ changes.length }} 项）
      <select v-model="selectedPath">
        <option v-for="change in changes" :key="change.path" :value="change.path">
          {{ changeLabel(change) }} · {{ change.path }}
        </option>
      </select>
    </label>
    <div v-if="selectedChange" :key="selectedChange.path" class="code-review-columns">
      <section aria-label="变更前的文件">
        <h4>变更前</h4>
        <p v-if="selectedChange.before === null">原快照没有此文件</p>
        <pre v-else class="code-block" tabindex="0">{{ selectedChange.before || '（空文件）' }}</pre>
      </section>
      <section aria-label="候选文件">
        <h4>变更后</h4>
        <p v-if="selectedChange.after === null">此文件将在新快照中删除</p>
        <pre v-else class="code-block" tabindex="0">{{ selectedChange.after || '（空文件）' }}</pre>
      </section>
    </div>
    <p v-else>本次候选没有文件变更。</p>
  </div>
</template>

<style scoped>
.code-review { min-width: 0; }
.code-review-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.code-review-columns section { min-width: 0; }
.code-review-columns h4 { margin-bottom: 6px; }
.code-review-columns pre { margin: 0; max-height: 450px; min-height: 44px; overflow: auto; white-space: pre; tab-size: 4; }
@media (max-width: 760px) { .code-review-columns { grid-template-columns: 1fr; } }
</style>
