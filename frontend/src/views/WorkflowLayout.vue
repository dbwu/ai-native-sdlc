<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { state, loadSnapshot, selectProject, diagnostics, openAction } from '../store/workbench'
import { dateTime } from '../lib/display'
import StatusPill from '../components/StatusPill.vue'
import StageStepper from '../components/StageStepper.vue'
import ActionPanel from '../components/ActionPanel.vue'
import SubmissionForm from '../components/SubmissionForm.vue'
const route=useRoute(), loading=ref(false)
let routeGeneration=0
const workflow=computed(()=>state.snapshot?.workflow)
const tabs=[['overview','阶段产物'],['review','变更审阅'],['execution','代码与测试'],['trace','知识追溯'],['history','操作记录']]
const secondary=computed(()=>state.snapshot?.actions.filter(item=>['pause','resume','retry','cancel','reject_task_and_retry'].includes(item.id)) || [])
watch(()=>[route.params.id,state.initialized],async([id,initialized])=>{const generation=++routeGeneration; if(!initialized || !id || state.snapshot?.workflow.id===id)return;loading.value=true;try{await loadSnapshot(String(id));if(generation!==routeGeneration || route.params.id!==id)return;const projectId=state.snapshot?.workflow.project_id;if(projectId && projectId!==state.projectId){await selectProject(projectId);if(generation!==routeGeneration || route.params.id!==id)return;await loadSnapshot(String(id))}}catch{}finally{if(generation===routeGeneration)loading.value=false} },{immediate:true})
</script>
<template><template v-if="workflow && workflow.id===route.params.id"><section class="panel flow-summary"><div class="flow-title-line"><h2>{{ workflow.title }}</h2><StatusPill :status="workflow.status"/></div><p class="flow-goal">{{ workflow.definition?.goal || '' }}</p><div class="flow-meta"><span>创建于 {{ dateTime(workflow.created_at) }}</span><span>流程事件版本 {{ workflow.version }}</span><span>已放行 {{ workflow.state?.completed_stages?.length || 0 }} / 4 阶段</span></div><StageStepper :stage="workflow.state?.stage ?? 0" :completed="workflow.state?.completed_stages || []" :finished="workflow.status==='COMPLETED'"/></section><ActionPanel/><SubmissionForm/><div v-for="message in diagnostics" :key="message" class="info-box warning" role="status"><p class="diagnostic-title">当前检查需要处理</p>{{ message }}</div><section class="panel content-panel"><nav class="tabs" aria-label="流程内容"><RouterLink v-for="[page,label] in tabs" :key="page" :to="`/workflows/${workflow.id}/${page}`" class="tab">{{ label }}</RouterLink></nav><div class="tab-content"><RouterView/></div><div v-if="secondary.length" class="action-strip"><span class="action-strip-label">流程管理</span><button v-for="item in secondary" :key="item.id" class="button" :class="item.id==='cancel' || item.id.includes('reject') ? 'danger':'secondary'" :disabled="state.busy || !item.enabled" :title="item.reason || ''" @click="openAction(item.id)">{{ item.label }}</button></div></section></template><div v-else class="panel empty-state"><div class="empty-icon">◇</div><h2>{{ loading || !state.initialized ? '正在读取验证流程…' : '暂时无法读取此流程' }}</h2><p>{{ loading || !state.initialized ? '读取当前版本、产物与可执行的下一步。' : '请检查上方提示，或从左侧重新选择一个流程。' }}</p><RouterLink v-if="!loading && state.initialized" to="/projects" class="button secondary">返回项目</RouterLink></div></template>
