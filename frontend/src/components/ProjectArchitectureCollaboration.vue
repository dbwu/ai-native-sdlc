<script lang="ts">
export type ArchitecturePort = {
  key: string
  name: string
  kind: 'interface' | 'data_model'
  knowledge: { id: string; version: number }
}
export type ProjectCollaborationLink = {
  key: string
  source: string
  target: string
  type: 'calls' | 'reads' | 'writes' | 'depends_on'
  label: string
  rationale: string
  process_key?: string | null
  scenario_keys: string[]
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import DiagramPreview, { type DesignDiagram } from './DiagramPreview.vue'
import type { ArchitectureNode } from './ProjectArchitectureTree.vue'

const props = defineProps<{
  diagram?: DesignDiagram | null
  nodes: ArchitectureNode[]
  ports: ArchitecturePort[]
  links: ProjectCollaborationLink[]
  processes: { key: string; name: string; scenarios: { key: string; name: string }[] }[]
  projectId: string
}>()
const emit = defineEmits<{ openProcess: [process: string, scenario?: string] }>()
const types = { calls: '调用', reads: '读取', writes: '写入', depends_on: '依赖' }
const endpoints = computed(() => new Map([...props.nodes, ...props.ports].map(item => [item.key, item.name])))
const ports = computed(() => new Map(props.ports.map(item => [item.key, item])))
const processes = computed(() => new Map(props.processes.map(item => [item.key, item])))
const related = (link: ProjectCollaborationLink) => link.process_key ? processes.value.get(link.process_key) : undefined
const evidence = (link: ProjectCollaborationLink) =>
  [ports.value.get(link.source), ports.value.get(link.target)].filter((port): port is ArchitecturePort => !!port)
</script>

<template>
  <section aria-label="接口与协作" class="collaboration-view">
    <template v-if="diagram">
      <p class="section-label">沿箭头看发起方与目标。处理先后和拒绝分支可在关联流程或时序中查看。</p>
      <DiagramPreview :diagram="diagram" compact/>
      <h4>关系与依据</h4>
      <details v-for="link in links" :key="link.key" class="collaboration-relation">
        <summary>
          <span class="relation-type" :class="link.type">{{ types[link.type] }}</span>
          <strong>{{ link.label }}</strong>
          <span class="relation-endpoints">{{ endpoints.get(link.source) }} → {{ endpoints.get(link.target) }}</span>
        </summary>
        <div class="relation-detail">
          <p>{{ link.rationale }}</p>
          <ul v-if="evidence(link).length" class="reference-links">
            <li v-for="port in evidence(link)" :key="port.key">
              <RouterLink :to="{ path: `/projects/${projectId}/knowledge`, query: {
                entity: port.knowledge.id, version: String(port.knowledge.version),
              } }">{{ port.kind === 'interface' ? '接口' : '数据依据' }} · {{ port.name }} · v{{ port.knowledge.version }}</RouterLink>
            </li>
          </ul>
          <div v-if="related(link)" class="scenario-links">
            <button type="button" class="text-button" @click="emit('openProcess', related(link)!.key)">查看流程 · {{ related(link)!.name }}</button>
            <button v-for="scene in related(link)?.scenarios.filter(item => link.scenario_keys.includes(item.key))" :key="scene.key"
              type="button" class="text-button" @click="emit('openProcess', related(link)!.key, scene.key)">时序 · {{ scene.name }}</button>
          </div>
        </div>
      </details>
    </template>
    <p v-else class="info-box">此基线尚未记录项目协作关系。可在「最新已确认图表」按需求查看已确认的设计协作图。</p>
  </section>
</template>

<style scoped>
.collaboration-view { min-width: 0; }
.collaboration-relation { margin-top: 7px; border: 1px solid var(--line); border-radius: 7px; overflow-wrap: anywhere; }
.collaboration-relation summary { padding: 9px; cursor: pointer; }
.collaboration-relation summary:hover { background: #f4f8fb; }
.relation-type { margin-inline: 4px 7px; padding: 2px 6px; border-radius: 4px; font-size: 12px; background: #edf1f5; }
.relation-type.calls { color: #225582; background: #eaf3fa; }
.relation-type.reads { color: #256244; background: #eaf7ef; }
.relation-type.writes { color: #745420; background: #fcf2dc; }
.relation-endpoints { color: var(--muted); font-size: 12px; margin-inline-start: 8px; }
.relation-detail { border-top: 1px solid var(--line); padding: 8px 12px 10px; }
.relation-detail p { white-space: pre-wrap; }
.reference-links { padding-inline-start: 17px; }
.scenario-links { display: flex; flex-wrap: wrap; gap: 6px 14px; }
</style>
