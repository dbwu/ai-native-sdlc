<script setup lang="ts">
import { computed } from 'vue'
import { stages } from '../lib/display'
const props = defineProps<{ stage: number; completed: { stage: number }[]; finished?: boolean }>()
const completedStages = computed(() => new Set(props.completed.map(item => item.stage)))
</script>
<template><div class="stage-track" aria-label="四阶段验证进度"><div v-for="(label,index) in stages" :key="label" class="stage" :class="{done:completedStages.has(index) || finished,current:index===stage && !finished}"><span class="stage-number">{{ completedStages.has(index) || finished ? '✓' : index+1 }}</span><span class="stage-info"><strong>{{ label }}</strong><small>{{ completedStages.has(index) || finished ? '已放行' : index === stage ? '当前阶段' : '尚未开始' }}</small></span></div></div></template>
