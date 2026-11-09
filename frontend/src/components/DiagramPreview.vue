<script lang="ts">
export interface DesignDiagram {
  kind: 'sequence' | 'architecture' | 'use_case'
  title: string
  mermaid?: string
  nodes: { id: string; label: string; kind?: string }[]
  edges: { source: string; target: string; label: string }[]
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
const labels = computed(() => new Map(props.diagram.nodes.map(node => [node.id, node.label])))
const edges = computed(() => props.diagram.edges.filter(edge => labels.value.has(edge.source) && labels.value.has(edge.target)))
const diagramName = computed(() => ({ sequence: '业务时序图', architecture: '架构图', use_case: '用例图' }[props.diagram.kind]))

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
      <span class="diagram-kind">{{ diagramName }}</span>
    </figcaption>
    <p v-if="rendering" class="diagram-message" role="status">正在渲染图示…</p>
    <div v-else-if="imageUrl" class="diagram-scroll" tabindex="0" role="region" :aria-label="`${diagram.title}图示区域`">
      <img :src="imageUrl" :width="imageWidth" :alt="`${diagramName}：${diagram.title}。可在图后查看文字说明。`" @error="imageFailed">
    </div>
    <p v-else class="diagram-message" role="status">{{ failed ? '图示暂时无法渲染，可查看文字说明。' : '本次记录没有 Mermaid 源码，可查看文字说明。' }}</p>
    <details class="diagram-transcript" :open="failed || !diagram.mermaid">
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
.diagram-scroll { max-width: 100%; max-height: 520px; overflow: auto; border: 1px solid var(--line); border-radius: 7px; background: #fff; }
.diagram-scroll img { display: block; max-width: none; height: auto; margin-inline: auto; }
.diagram-message, .diagram-transcript { margin-top: 8px; font-size: 12px; overflow-wrap: anywhere; }
.diagram-transcript summary { cursor: pointer; }
.diagram-transcript p { margin-top: 6px; }
.diagram-transcript pre { max-height: 15rem; overflow: auto; white-space: pre; }
</style>
