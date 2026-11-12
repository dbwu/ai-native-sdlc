<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { flatten,fieldName,readable,relationshipTypes,kinds } from '../lib/display'
import type { RecordData } from '../types'
import ProjectModelChanges from './ProjectModelChanges.vue'
import ExtractionReview from './ExtractionReview.vue'
import DiagramPreview, { type DesignDiagram } from './DiagramPreview.vue'
import DesignModelExchange from './DesignModelExchange.vue'
const props=defineProps<{diff:RecordData|null}>()
const previewOpen = ref(false)
const selectedDiagram = ref(0)
const previews = computed<DesignDiagram[]>(() => props.diff?.project_model_preview || [])
watch(() => props.diff?.id, () => { previewOpen.value = false; selectedDiagram.value = 0 })
const names=computed(()=>new Map((props.diff?.changes||[]).filter((c:RecordData)=>c.after?.name||c.before?.name).map((c:RecordData)=>[c.id,c.after?.name||c.before?.name])))
function value(v:unknown):string{return typeof v==='string'?(relationshipTypes[v]||String(names.value.get(v)||readable(v))):readable(v)}
const changes=computed(()=>(props.diff?.changes||[]).map((c:RecordData)=>{const old:RecordData={},next:RecordData={};for(const key of ['name','description','status','attributes','type','source_id','target_id','rationale','source_version','target_version','evidence','provenance']){if(c.before&&key in c.before)old[key]=c.before[key];if(c.after&&key in c.after)next[key]=c.after[key]}const before=flatten(old),after=flatten(next);return {...c,fields:[...new Set([...Object.keys(before),...Object.keys(after)])].filter(k=>JSON.stringify(before[k])!==JSON.stringify(after[k])).map(key=>({key,old:value(before[key]),next:value(after[key])}))}}))
</script>
<template>
  <section v-if="diff?.extraction_review" class="project-model-review" aria-label="已保存的增量审阅记录">
    <ExtractionReview :report="diff.extraction_review" :project-id="diff.project_id || ''" readonly/>
  </section>
  <section v-if="diff?.project_model_changes?.length" class="project-model-review" aria-label="项目基线变更">
    <h3>项目基线变更</h3>
    <template v-if="diff.project_model_origin">
      <p>模型分析 · {{ diff.project_model_origin.model }} · 来源设计 v{{ diff.project_model_origin.design_version }}</p>
      <details v-if="previews.length" :open="previewOpen" @toggle="previewOpen = ($event.currentTarget as HTMLDetailsElement).open">
        <summary>预览模型生成的候选图表</summary>
        <template v-if="previewOpen">
          <label class="field">候选图表<select v-model="selectedDiagram">
            <option v-for="(diagram, index) in previews" :key="index" :value="index">{{ diagram.title }}</option>
          </select></label>
          <DiagramPreview v-if="previews[selectedDiagram]" :diagram="previews[selectedDiagram]"/>
        </template>
      </details>
      <DesignModelExchange v-if="diff.has_project_model_exchange" :key="diff.id"
        :path="'/workbench/proposals/' + diff.id + '/project-model-exchange'" :available="true" :pending="false"/>
    </template>
    <ProjectModelChanges :changes="diff.project_model_changes"/>
  </section>
  <article v-for="c in changes" :key="c.id" class="diff-change"><h3>{{ c.after?.name || c.before?.name || relationshipTypes[c.after?.type] || '知识关系' }} · {{ kinds[c.kind]||'知识关系' }}</h3><p>{{ c.before ? `修改知识 · 原基线 v${c.before.version} → 候选 v${c.after.version}` : '新增知识，尚无旧版本 → 候选 v1' }}</p><div v-for="field in c.fields" :key="field.key"><div class="diff-field"><strong>{{ fieldName(field.key) }}</strong><div class="diff-values"><div class="diff-value before"><details v-if="field.old.length>350"><summary>展开原内容</summary><pre>{{ field.old }}</pre></details><span v-else>{{ field.old }}</span></div><div class="diff-value after"><details v-if="field.next.length>350"><summary>展开候选内容</summary><pre>{{ field.next }}</pre></details><span v-else>{{ field.next }}</span></div></div></div></div><details><summary>高级详情：原始版本与候选 JSON</summary><pre>{{ JSON.stringify({before:c.before,after:c.after},null,2) }}</pre></details></article>
</template>

<style scoped>
.project-model-review { margin-top: 14px; }
</style>
