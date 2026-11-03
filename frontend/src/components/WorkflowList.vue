<script setup lang="ts">
import { useRoute } from 'vue-router'
import { state, openWorkflowForm, stageLabel } from '../store/workbench'
import { dateTime } from '../lib/display'
import StatusPill from './StatusPill.vue'
const route = useRoute()
</script>
<template><section class="flow-panel panel" aria-label="验证流程列表"><div class="panel-heading"><h2>验证流程 <span class="muted-count">{{ state.flows.length }}</span></h2><button class="text-button" :disabled="state.busy" @click="openWorkflowForm">新建 ＋</button></div><div class="flow-list"><div v-if="state.loading" class="list-loading">正在读取项目…</div><RouterLink v-for="flow in state.flows" v-else :key="flow.id" :to="`/workflows/${flow.id}/overview`" class="flow-item" :class="{selected:route.params.id === flow.id}" :aria-current="route.params.id === flow.id ? 'page' : undefined"><span class="flow-item-title">{{ flow.title }}</span><span class="flow-item-meta"><StatusPill :status="flow.status"/><span class="flow-item-date">{{ dateTime(flow.created_at,true) }}</span></span><div class="flow-item-step">{{ flow.status === 'COMPLETED' ? '四个阶段已完成' : `当前 · ${stageLabel(flow.state?.stage ?? 0)}` }}</div></RouterLink><div v-if="!state.loading && !state.flows.length" class="empty-inline">{{ state.projectId ? '此项目尚无验证流程。' : '先创建或选择项目。' }}<br><button class="text-button" @click="openWorkflowForm">创建第一个流程</button></div></div><div class="flow-panel-footer">每个流程保留独立的产物与审核记录</div></section></template>
