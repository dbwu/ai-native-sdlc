<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { state, allowed, openAction, artifacts } from '../store/workbench'
import { actionNames, proposalNames, fieldName, flatten, readable, kinds, relationshipTypes } from '../lib/display'
import type { RecordData } from '../types'
const dialog=ref<HTMLDialogElement>()
watch(()=>state.showReview,async value=>{await nextTick(); if(value)dialog.value?.showModal();else dialog.value?.close()})
const actions=computed(()=>['validate_task','validate_proposal','reject_proposal','commit_proposal'].filter(allowed))
const entityNames=computed(()=>{
  const current=new Map<string,string>()
  for(const project of state.projects)current.set(project.id,project.name)
  for(const item of artifacts.value)current.set(item.entity.id,item.entity.name)
  const before=new Map(current),after=new Map(current)
  for(const change of state.diff?.changes || []){
    if(change.before?.name)before.set(change.id,change.before.name)
    if(change.after?.name)after.set(change.id,change.after.name)
  }
  return {before,after}
})
const changes=computed(()=> (state.diff?.changes || []).map((change:RecordData)=>{
  const before=change.before||{},after=change.after||change.data||{},old:RecordData={},candidate:RecordData={}
  const relationship=change.kind==='Relationship' || 'source_id' in after || 'source_id' in before
  for(const key of ['name','description','type','source_id','target_id','status','attributes','rationale','evidence','provenance','source_version','target_version']){
    if(key in before)old[key]=before[key]
    if(key in after)candidate[key]=after[key]
  }
  const oldFields=flatten(old),newFields=flatten(candidate)
  return {...change,before,after,relationship,fields:[...new Set([...Object.keys(oldFields),...Object.keys(newFields)])]
    .filter(key=>relationship && ['type','source_id','target_id'].includes(key) || JSON.stringify(oldFields[key])!==JSON.stringify(newFields[key]))
    .map(key=>({key,before:oldFields[key],after:newFields[key]}))}
}))
function display(value:unknown,side:'before'|'after'='after'):string {
  if(Array.isArray(value) && value.length && value.every(item=>item && typeof item==='object' && item.id && item.version))return value.map(item=>`${entityNames.value[side].get(item.id) || '固定版本证据'} · 知识版本 ${item.version}`).join('\n')
  if(typeof value==='string')return relationshipTypes[value] || entityNames.value[side].get(value) || readable(value)
  return readable(value)
}
function changeTitle(change:RecordData):string {
  if(!change.relationship)return change.name || change.after.name || change.before.name || kinds[change.kind] || '知识产物'
  const relation=change.after.type ? change.after : change.before
  return `${display(relation.source_id)} → ${display(relation.target_id)} · ${relationshipTypes[relation.type] || relation.type || '知识关系'}`
}
function close(){state.showReview=false}
</script>
<template><dialog ref="dialog" class="review-dialog" @cancel="close"><div class="dialog-heading"><div><p class="eyebrow">知识变更审阅</p><h2>{{ proposalNames[state.snapshot?.proposal_kind || ''] || '变更审阅' }}</h2></div><button class="icon-button" aria-label="关闭" @click="close">×</button></div><div class="review-content"><div v-if="state.diffLoading" class="list-loading">正在读取提案的原基线与候选内容…</div><div v-else-if="!state.diff" class="info-box error">暂时无法读取差异，请关闭后重新打开。</div><template v-else><div v-if="state.diff.stale" class="info-box warning" style="margin-bottom:12px">当前知识已经变化。这份提案仍按原基线展示差异，提交前需要复核冲突。</div><div class="info-box" style="margin-bottom:16px">修改前使用提案创建时记录的具体基线版本，修改后为这份不可变提案的候选内容。</div><article v-for="(change,index) in changes" :key="index" class="diff-change"><div class="diff-heading"><strong>{{ changeTitle(change) }}</strong><small>{{ change.op?.startsWith('create') ? '新增' : '修订' }} · {{ kinds[change.kind] || change.kind || '知识关系' }}</small></div><div class="diff-column-labels"><span>修改前{{ change.before.version ? ` · 知识版本 ${change.before.version}` : '' }}</span><span>提案候选内容</span></div><div class="diff-fields"><div v-for="field in change.fields" :key="field.key" class="diff-field"><div class="diff-field-label">{{ fieldName(field.key) }}</div><div class="diff-values"><div class="diff-value before">{{ display(field.before,'before') }}</div><div class="diff-value after">{{ display(field.after,'after') }}</div></div></div><p v-if="!change.fields.length" class="empty-inline">没有可展示的内容差异，可能仅记录来源或状态事件。</p></div></article><div v-if="!changes.length" class="empty-inline">当前提案没有内容变更。</div></template></div><div class="review-actions"><button class="button secondary" @click="close">关闭</button><button v-for="id in actions" :key="id" class="button" :class="id==='commit_proposal'?'primary':id.includes('reject')?'danger':'secondary'" :disabled="state.busy || state.diffLoading || !state.diff" @click="openAction(id)">{{ actionNames[id] }}</button></div></dialog></template>
