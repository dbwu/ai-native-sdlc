<script setup lang="ts">
import { statuses, statusColor } from '../lib/display'
defineProps<{ status: string }>()
</script>
<template><span class="status-pill" :class="statusColor(status)">{{ statuses[status] || status || '尚未开始' }}</span></template>
