<script setup lang="ts">
import { state } from '../store/workbench'
import { keys, kinds } from '../lib/display'
import type { RecordData } from '../types'
import StatusPill from './StatusPill.vue'
defineProps<{ item: {key: string; entity: RecordData} }>()
</script>
<template><article class="artifact-card"><div class="artifact-card-head"><span>{{ keys[item.key] || kinds[item.entity.kind] || '知识产物' }}</span><StatusPill :status="item.entity.status"/></div><h3 class="artifact-name">{{ item.entity.name }}</h3><p class="artifact-desc">{{ item.entity.description || '暂无说明' }}</p><ul v-if="item.entity.attributes?.acceptance_criteria" class="attribute-list"><li v-for="criterion in item.entity.attributes.acceptance_criteria" :key="criterion">{{ criterion }}</li></ul><div class="artifact-meta"><span>知识版本 v{{ item.entity.version }}</span><span>{{ kinds[item.entity.kind] || item.entity.kind }}</span></div><button class="text-button" @click="state.artifactKey=item.key">查看完整产物 ↗</button></article></template>
