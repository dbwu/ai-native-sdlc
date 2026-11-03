<script setup lang="ts">
import { state } from '../store/workbench'
import { dateTime, actorName, eventNames } from '../lib/display'
import StatusPill from '../components/StatusPill.vue'
</script>
<template><div v-if="state.snapshot?.history.length" class="timeline"><article v-for="row in state.snapshot.history" :key="row.version" class="timeline-item"><span class="timeline-point"></span><div class="timeline-copy"><h3 class="timeline-title">{{ eventNames[row.action] || row.action }} <span class="muted timeline-version">流程事件 {{ row.version }}</span></h3><p class="timeline-detail">{{ row.reason || '未提供操作依据' }}<br>操作署名：{{ actorName(row.actor) }}</p><div style="margin-top:7px"><StatusPill :status="row.status"/></div></div><span class="timeline-date">{{ dateTime(row.recorded_at,true) }}</span></article></div><div v-else class="empty-inline">暂无流程操作记录。</div></template>
