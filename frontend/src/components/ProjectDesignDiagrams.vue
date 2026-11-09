<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request } from '../api/client'
import { dateTime } from '../lib/display'
import DiagramPreview, { type DesignDiagram } from './DiagramPreview.vue'

type DiagramEntry = {
  requirement: { entity_id: string; name: string; version: number }
  design_id: string
  confirmed_at: string
  diagrams: DesignDiagram[]
}

const props = defineProps<{ projectId: string; refreshKey: number; openByDefault: boolean }>()
const expanded = ref(props.openByDefault)
const entries = ref<DiagramEntry[]>([])
const selectedId = ref('')
const loading = ref(false)
const error = ref('')
const selected = computed(() => entries.value.find(entry => entry.requirement.entity_id === selectedId.value))
let generation = 0

watch(() => props.openByDefault, open => { expanded.value = open })

watch(() => [props.projectId, props.refreshKey], async () => {
  const token = ++generation
  loading.value = true
  error.value = ''
  try {
    const path = '/workbench/projects/' + encodeURIComponent(props.projectId) + '/design-diagrams'
    const result = await request<{ entries: DiagramEntry[] }>(path)
    if (token !== generation) return
    entries.value = result.entries
    if (!selected.value) selectedId.value = entries.value[0]?.requirement.entity_id ?? ''
  } catch (failure) {
    if (token === generation) error.value = (failure as Error).message
  } finally {
    if (token === generation) loading.value = false
  }
}, { immediate: true })

function setExpanded(event: Event) {
  expanded.value = (event.currentTarget as HTMLDetailsElement).open
}
</script>

<template>
  <details class="project-diagrams" :open="expanded" @toggle="setExpanded">
    <summary>最新已确认图表</summary>
    <div v-if="expanded" class="project-diagram-body">
      <p>按需求查看最近确认设计的职责图、协作图、业务时序图和用例图。</p>
      <p v-if="loading" class="section-label" role="status">正在读取已确认图表…</p>
      <p v-else-if="error" class="info-box error" role="alert">{{ error }}</p>
      <p v-else-if="!entries.length" class="section-label">项目还没有可展示的已确认设计图表。</p>
      <template v-else-if="selected">
        <label class="field project-diagram-select">选择需求
          <select v-model="selectedId">
            <option v-for="entry in entries" :key="entry.requirement.entity_id" :value="entry.requirement.entity_id">
              {{ entry.requirement.name }} · v{{ entry.requirement.version }}
            </option>
          </select>
        </label>
        <div class="project-diagram-meta">
          <p class="section-label">需求 v{{ selected.requirement.version }} · {{ dateTime(selected.confirmed_at) }} 确认</p>
          <RouterLink class="button secondary" :to="{
            path: `/projects/${projectId}/knowledge`,
            query: { requirement: selected.requirement.entity_id, version: String(selected.requirement.version), design: selected.design_id },
          }">查看设计交付与导出</RouterLink>
        </div>
        <DiagramPreview v-for="(diagram, index) in selected.diagrams" :key="`${selected.design_id}-${index}`" :diagram="diagram"/>
        <p v-if="!selected.diagrams.length" class="section-label">这版已确认设计没有可展示的图表。</p>
      </template>
    </div>
  </details>
</template>

<style scoped>
.project-diagrams { margin-top: 16px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; }
.project-diagrams summary { cursor: pointer; font-weight: 700; }
.project-diagram-body { margin-top: 8px; }
.project-diagram-select { max-width: 34rem; margin-top: 10px; }
.project-diagram-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; margin: 10px 0; }
.project-diagram-meta p { margin: 0; }
</style>
