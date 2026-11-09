<script lang="ts">
export interface DesignDiagram {
  kind: 'sequence' | 'architecture' | 'collaboration' | 'use_case'
  title: string
  mermaid?: string
  nodes: { id: string; label: string; kind?: string; detail?: string }[]
  edges: { source: string; target: string; label: string; rationale?: string; requirements?: string[] }[]
}

let engine: Promise<typeof import('mermaid')['default']> | undefined

function loadEngine() {
  engine ??= import('mermaid').then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
      flowchart: { htmlLabels: false }, suppressErrorRendering: true,
    })
    return mermaid
  })
  return engine
}
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { kinds } from '../lib/display'

const props = defineProps<{ diagram: DesignDiagram }>()
const instance = useId()
const imageUrl = ref('')
const imageWidth = ref(400)
const rendering = ref(false)
const failed = ref(false)
let generation = 0
const nodes = computed(() => new Map(props.diagram.nodes.map(node => [node.id, node])))
const labels = computed(() => new Map(props.diagram.nodes.map(node => [node.id, node.label])))
const edges = computed(() => props.diagram.edges.filter(edge => nodes.value.has(edge.source) && nodes.value.has(edge.target)))
const diagramName = computed(() => ({ sequence: '业务时序图', architecture: '职责图', collaboration: '协作图', use_case: '用例图' }[props.diagram.kind]))
const architectureComponents = computed(() => {
  if (props.diagram.kind !== 'architecture') return []
  return props.diagram.nodes.filter(node => node.kind === 'ArchitectureComponent').map(component => {
    const incoming = edges.value.filter(edge => edge.target === component.id && edge.label === '由其负责')
    const ownership = edges.value.filter(edge => edge.target === component.id && edge.label === '关联产物归属')
    const outgoing = edges.value.filter(edge => edge.source === component.id && edge.label === '包含')
    return {
      ...component,
      requirements: endpointNames(incoming.map(edge => edge.source), 'Requirement'),
      ownershipRequirements: endpointNames(ownership.map(edge => edge.source), 'Requirement'),
      interfaces: endpointNames(outgoing.map(edge => edge.target), 'Interface'),
      dataModels: endpointNames(outgoing.map(edge => edge.target), 'DataModel'),
    }
  })
})

function endpointNames(ids: string[], kind: string) {
  return ids.flatMap(id => {
    const node = nodes.value.get(id)
    return node?.kind === kind ? [node.label] : []
  })
}

function kindLabel(kind?: string) {
  if (!kind) return ''
  return kinds[kind] || ({ Actor: '参与角色', UseCase: '用例', Participant: '参与方' } as Record<string, string>)[kind] || ''
}

function clearImage() {
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ''
}

watch(() => props.diagram.mermaid, async source => {
  const current = ++generation
  clearImage()
  failed.value = false
  rendering.value = !!source
  if (!source) return
  try {
    const mermaid = await loadEngine()
    if (current !== generation) return
    const { svg } = await mermaid.render(`knowledge-diagram-${instance}-${current}`, source)
    if (current !== generation) return
    const viewBox = svg.match(/\bviewBox="([^"]+)"/)?.[1]?.trim().split(/\s+/)
    const width = Number(viewBox?.[2])
    imageWidth.value = Number.isFinite(width) && width > 0 ? Math.min(2200, Math.max(240, Math.ceil(width))) : 400
    imageUrl.value = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }))
  } catch {
    if (current === generation) failed.value = true
  } finally {
    if (current === generation) rendering.value = false
  }
}, { immediate: true })

function imageFailed(event: Event) {
  if ((event.currentTarget as HTMLImageElement).getAttribute('src') !== imageUrl.value) return
  clearImage()
  failed.value = true
}

onBeforeUnmount(() => { ++generation; clearImage() })
</script>

<template>
  <figure class="diagram">
    <figcaption>
      <strong>{{ diagram.title }}</strong>
      <span v-if="diagram.title !== diagramName" class="diagram-kind">{{ diagramName }}</span>
    </figcaption>
    <p v-if="diagram.kind === 'architecture'" class="diagram-guide">
      <strong>怎么看这张图？</strong> 先找需求，再看哪个组件负责，最后看组件包含的接口和数据结构。连线表示分工或归属，处理先后可以看业务时序图。
    </p>
    <p v-else-if="diagram.kind === 'collaboration'" class="diagram-guide">
      <strong>怎么看这张图？</strong> 沿箭头看哪个组件或接口调用、读取、写入或依赖哪一方。关系清单列出每条关系的依据，交互先后可以看业务时序图。
    </p>
    <p v-if="rendering" class="diagram-message" role="status">正在渲染图示…</p>
    <div v-else-if="imageUrl" class="diagram-scroll" tabindex="0" role="region" :aria-label="`${diagram.title}图示区域`">
      <img :src="imageUrl" :width="imageWidth" :alt="`${diagramName}：${diagram.title}。可在图后查看文字说明。`" @error="imageFailed">
    </div>
    <p v-else class="diagram-message" role="status">{{ failed ? '图示暂时无法渲染，可查看文字说明。' : '本次记录没有 Mermaid 源码，可查看文字说明。' }}</p>
    <section v-if="architectureComponents.length" class="diagram-summary" aria-label="组件职责与归属">
      <h5>组件各自负责什么</h5>
      <ul>
        <li v-for="component in architectureComponents" :key="component.id">
          <strong>{{ component.label }}</strong>
          <p>{{ component.detail || '本图未记录该组件的职责说明。' }}</p>
          <dl>
            <div><dt>负责的需求</dt><dd>{{ component.requirements.join('、') || '本图未标注' }}</dd></div>
            <div v-if="component.ownershipRequirements.length"><dt>产物关联需求</dt><dd>{{ component.ownershipRequirements.join('、') }}</dd></div>
            <div><dt>拥有的接口</dt><dd>{{ component.interfaces.join('、') || '本图未列出' }}</dd></div>
            <div><dt>拥有的数据结构</dt><dd>{{ component.dataModels.join('、') || '本图未列出' }}</dd></div>
          </dl>
        </li>
      </ul>
    </section>
    <section v-if="diagram.kind === 'collaboration'" class="diagram-summary collaboration-summary" aria-label="协作关系及依据">
      <h5>协作关系及依据</h5>
      <ul v-if="edges.length">
        <li v-for="(edge, index) in edges" :key="index">
          <strong>{{ labels.get(edge.source) }}</strong>
          <span class="diagram-kind collaboration-kind">{{ edge.label }} →</span>
          <strong>{{ labels.get(edge.target) }}</strong>
          <dl>
            <div><dt>关联需求</dt><dd>{{ edge.requirements?.join('、') || '未记录' }}</dd></div>
            <div><dt>设计依据</dt><dd>{{ edge.rationale || '未记录' }}</dd></div>
          </dl>
        </li>
      </ul>
      <p v-else>本图未记录协作关系。</p>
    </section>
    <details v-else class="diagram-transcript" :open="failed || !diagram.mermaid">
      <summary>查看图示文字说明</summary>
      <p><strong>{{ diagram.kind === 'sequence' ? '参与方：' : '节点：' }}</strong>{{ diagram.nodes.map(node => kindLabel(node.kind) ? `${node.label}（${kindLabel(node.kind)}）` : node.label).join('、') }}</p>
      <ol v-if="diagram.kind === 'sequence' && edges.length">
        <li v-for="(edge, index) in edges" :key="index"><strong>{{ labels.get(edge.source) }} → {{ labels.get(edge.target) }}</strong>：{{ edge.label }}</li>
      </ol>
      <ul v-else-if="edges.length">
        <li v-for="(edge, index) in edges" :key="index"><strong>{{ labels.get(edge.source) }} {{ diagram.kind === 'use_case' ? '—' : '→' }} {{ labels.get(edge.target) }}</strong>：{{ edge.label }}</li>
      </ul>
      <p v-else>图中暂无关联。</p>
    </details>
    <details v-if="diagram.mermaid" class="diagram-transcript">
      <summary>查看 Mermaid 源码</summary>
      <pre><code>{{ diagram.mermaid }}</code></pre>
    </details>
  </figure>
</template>

<style scoped>
.diagram { margin: 10px 0 15px; padding: 10px; min-width: 0; border: 1px solid var(--line); border-radius: 9px; }
.diagram figcaption { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin: 0 0 8px; }
.diagram-kind { font-size: 11px; color: var(--muted); border: 1px solid var(--line); padding: 1px 6px; border-radius: 10px; }
.diagram-guide { margin: 8px 0 12px; font-size: 13px; line-height: 1.6; }
.diagram-scroll { max-width: 100%; max-height: 520px; overflow: auto; border: 1px solid var(--line); border-radius: 7px; background: #fff; }
.diagram-scroll img { display: block; max-width: none; height: auto; margin-inline: auto; }
.diagram-summary { margin-top: 14px; font-size: 13px; overflow-wrap: anywhere; }
.diagram-summary h5 { margin: 0 0 6px; font-size: 13px; }
.diagram-summary ul { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr)); gap: 8px; margin: 0; padding: 0; list-style: none; }
.diagram-summary li { min-width: 0; border: 1px solid var(--line); border-radius: 7px; padding: 8px 10px; }
.diagram-summary p { margin-top: 4px; white-space: pre-line; }
.diagram-summary dl { margin: 6px 0 0; font-size: 12px; }
.diagram-summary dl div { display: grid; grid-template-columns: 6.5em minmax(0, 1fr); gap: 4px; }
.diagram-summary dt { color: var(--muted); }
.diagram-summary dd { margin: 0; }
.collaboration-summary ul { grid-template-columns: minmax(0, 1fr); }
.collaboration-kind { display: inline-block; margin-inline: 5px; }
.diagram-message, .diagram-transcript { margin-top: 8px; font-size: 12px; overflow-wrap: anywhere; }
.diagram-transcript summary { cursor: pointer; }
.diagram-transcript p { margin-top: 6px; }
.diagram-transcript pre { max-height: 15rem; overflow: auto; white-space: pre; }
</style>
