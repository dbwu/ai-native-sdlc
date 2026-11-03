<script setup lang="ts">
import { notify } from '../store/workbench'
const props = defineProps<{ title: string; source: string; log?: boolean }>()
async function copy() { try { await navigator.clipboard.writeText(props.source); notify('内容已复制。','success') } catch { notify('当前浏览器未允许复制，请手动选择内容。','warning') } }
</script>
<template><div><div class="code-header"><span>{{ title }}</span><button class="text-button" @click="copy">复制内容</button></div><pre class="code-block" :class="{'log-block':log}" tabindex="0">{{ source || '暂无内容' }}</pre></div></template>
