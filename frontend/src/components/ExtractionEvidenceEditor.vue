<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'

export type ExtractionQuote = { quote: string; start: number | null }
const props = defineProps<{ content: string; readonly?: boolean }>()
const evidence = defineModel<ExtractionQuote[]>({ required: true })
const opened = ref(-1)
const highlight = ref<HTMLElement>()
function setHighlight(element: unknown) { highlight.value = element instanceof HTMLElement ? element : undefined }

function locate(item: ExtractionQuote) {
  const characters = Array.from(props.content)
  const quoteLength = Array.from(item.quote).length
  if (!item.quote.trim()) return { error: '请从原文复制支持此知识的完整引文。' }
  if (item.start !== null) {
    const start = Number(item.start)
    if (!Number.isInteger(start) || start < 0 || characters.slice(start, start + quoteLength).join('') !== item.quote) {
      return { error: '指定位置与引文不一致，请重新定位。' }
    }
    return { start, end: start + quoteLength }
  }
  const offset = props.content.indexOf(item.quote)
  if (offset < 0) return { error: '原文中未找到这段引文，请保留原有空白和标点。' }
  if (props.content.indexOf(item.quote, offset + 1) >= 0) return { error: '引文出现多次，请扩展引文或指定原文位置。' }
  const start = Array.from(props.content.slice(0, offset)).length
  return { start, end: start + quoteLength }
}

const preview = computed(() => {
  const item = evidence.value[opened.value]
  if (!item) return null
  const location = locate(item)
  if (location.error || location.start === undefined || location.end === undefined) return null
  const characters = Array.from(props.content)
  return { before: characters.slice(0, location.start).join(''), quote: item.quote, after: characters.slice(location.end).join('') }
})

async function show(index: number) {
  opened.value = opened.value === index ? -1 : index
  await nextTick()
  highlight.value?.scrollIntoView({ block: 'nearest' })
}

function setStart(item: ExtractionQuote, event: Event) {
  const value = (event.target as HTMLInputElement).value
  item.start = value === '' ? null : Number(value)
}
</script>

<template>
  <section class="extraction-evidence" aria-label="候选原文证据">
    <h5>原文证据</h5>
    <div v-for="(item, index) in evidence" :key="index" class="quote-item">
      <label class="field">引文 {{ index + 1 }}
        <textarea v-model="item.quote" rows="2" maxlength="4000" :readonly="readonly" @input="item.start = null"/>
      </label>
      <p v-if="locate(item).error" class="quote-error">{{ locate(item).error }}</p>
      <p v-else class="section-label">已匹配固定原文的第 {{ locate(item).start }} 至 {{ locate(item).end }} 个码点</p>
      <div class="inline-actions">
        <button type="button" class="text-button" :disabled="!!locate(item).error" :aria-expanded="opened === index" @click="show(index)">
          {{ opened === index ? '收起原文定位' : '查看原文定位' }}
        </button>
        <button v-if="!readonly && evidence.length > 1" type="button" class="text-button" @click="evidence = evidence.filter((_, position) => position !== index); opened = -1">移除此条引文</button>
      </div>
      <details v-if="!readonly" class="position-editor">
        <summary>指定重复引文的位置</summary>
        <label class="field">起始位置
          <input type="number" min="0" step="1" :value="item.start ?? ''" @input="setStart(item, $event)">
        </label>
        <p class="section-label">从 0 开始按 Unicode 码点计数。留空时自动定位唯一引文，中文和表情各按实际码点处理。</p>
      </details>
      <pre v-if="opened === index && preview" class="original-preview" tabindex="0" aria-label="证据原文高亮">{{ preview.before }}<mark :ref="setHighlight">{{ preview.quote }}</mark>{{ preview.after }}</pre>
    </div>
    <button v-if="!readonly && evidence.length < 5" type="button" class="text-button" @click="evidence = [...evidence, { quote: '', start: null }]">增加原文引文</button>
  </section>
</template>

<style scoped>
.extraction-evidence { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 10px; }
.extraction-evidence h5 { font-size: 13px; margin: 0; }
.quote-item { margin: 8px 0; }
.quote-error { color: #994636; font-size: 12px; margin: 4px 0; }
.position-editor { margin-top: 7px; font-size: 12px; }
.position-editor input { max-width: 170px; }
.original-preview { max-height: 17rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--bg); font: inherit; }
.original-preview mark { background: #ffe99b; color: #293441; }
</style>
