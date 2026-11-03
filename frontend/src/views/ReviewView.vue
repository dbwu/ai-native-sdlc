<script setup lang="ts">
import { computed } from 'vue'
import { state, allowed, openAction, openReview } from '../store/workbench'
import { actionNames, proposalNames } from '../lib/display'
import StatusPill from '../components/StatusPill.vue'
const proposal=computed(()=>state.snapshot?.proposal)
const actions=computed(()=>['validate_task','validate_proposal','commit_proposal','reject_proposal','reject_task_and_retry'].filter(allowed))
</script>
<template><div v-if="proposal" class="section-stack"><div class="review-summary"><span class="review-summary-icon">▤</span><div><h3>{{ proposalNames[state.snapshot?.proposal_kind || ''] || '知识变更提案' }}</h3><p>{{ proposal.title }} · 提案事件版本 {{ proposal.version }}</p></div><button class="button soft" :disabled="state.busy" @click="openReview">查看修改前后</button></div><div class="content-toolbar"><StatusPill :status="proposal.status"/><span class="section-label">提案创建后保留原样，拒绝或重试会保留旧记录。</span></div><div v-if="proposal.status==='committed'" class="info-box">这份提案已经提交。请按“当前下一步”继续确认任务或推进阶段；提交不会自动等于整个阶段完成。</div><div v-if="actions.length" class="inline-actions"><button v-for="id in actions" :key="id" class="button" :class="id==='commit_proposal'?'primary':id.includes('reject')?'danger':'secondary'" :disabled="state.busy" @click="openAction(id)">{{ actionNames[id] }}</button></div><div class="info-box">校验检查结构与语义；确认提交才写入知识。Agent 的成功报告、提案校验和阶段放行分别记录。</div></div><div v-else class="empty-inline">当前尚无可审阅提案。运行本阶段或生成门禁后再查看。</div></template>
