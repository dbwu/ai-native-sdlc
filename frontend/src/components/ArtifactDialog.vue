<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { artifacts, state } from '../store/workbench'
import { keys, fieldName, readable, actorName } from '../lib/display'
import StatusPill from './StatusPill.vue'
import CodeBlock from './CodeBlock.vue'
const dialog=ref<HTMLDialogElement>()
const entity=computed(()=>artifacts.value.find(item=>item.key===state.artifactKey)?.entity)
const attributes=computed(()=>Object.entries(entity.value?.attributes||{}).filter(([key,value])=>key!=='extensions' && value!==null && value!=='' && (!Array.isArray(value)||value.length) && (typeof value!=='object'||Object.keys(value as object).length)))
watch(()=>state.artifactKey,async value=>{await nextTick();if(value)dialog.value?.showModal();else dialog.value?.close()})
function close(){state.artifactKey=''}
</script>
<template><dialog ref="dialog" class="review-dialog" @cancel="close"><div class="dialog-heading"><div><p class="eyebrow">完整知识产物</p><h2>{{ keys[state.artifactKey] || '知识产物' }}</h2></div><button class="icon-button" aria-label="关闭" @click="close">×</button></div><div v-if="entity" class="review-content"><div class="content-toolbar"><h3>{{ entity.name }}</h3><StatusPill :status="entity.status"/></div><p class="dialog-description">{{ entity.description }}</p><div class="diff-fields"><div v-for="[key,value] in attributes" :key="key" class="diff-field"><div class="diff-field-label">{{ fieldName(`attributes.${key}`) }}</div><div class="diff-value">{{ readable(value) }}</div></div></div><CodeBlock v-if="entity.attributes?.extensions?.source" title="实现源码" :source="entity.attributes.extensions.source"/><CodeBlock v-if="entity.attributes?.extensions?.test_source" title="测试源码" :source="entity.attributes.extensions.test_source"/><div class="info-box" style="margin-top:17px">知识版本 {{ entity.version }} · 来源署名：{{ actorName(entity.provenance?.actor) }}<br>来源：{{ entity.provenance?.source_uri || '—' }}<br>来源版本：{{ entity.provenance?.source_revision || '—' }}<br>变更原因：{{ entity.provenance?.change_reason || '—' }}</div><details class="advanced"><summary>高级详情：查看原始知识记录</summary><pre>{{ JSON.stringify(entity,null,2) }}</pre></details></div><div class="review-actions"><button class="button secondary" @click="close">关闭</button></div></dialog></template>
