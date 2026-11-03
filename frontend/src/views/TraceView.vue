<script setup lang="ts">
import { computed } from 'vue'
import { artifacts, state } from '../store/workbench'
import StatusPill from '../components/StatusPill.vue'
const names=computed(()=>new Map(artifacts.value.map(item=>[item.entity.id,item.entity.name])))
const types:Record<string,string>={CONTAINS:'包含',DERIVED_FROM:'派生自',DEPENDS_ON:'依赖',IMPLEMENTED_BY:'由其实现',VERIFIED_BY:'由其验证',CONSTRAINED_BY:'受其约束',IMPACTS:'影响',SUPERSEDES:'替代',CONFLICTS_WITH:'与其冲突'}
</script>
<template><template v-if="state.snapshot?.relationships.length"><div class="content-toolbar"><span class="section-label">{{ state.snapshot.relationships.length }} 条显式关系 · 版本与验证状态分别记录</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>源产物</th><th>关系</th><th>目标产物</th><th>状态</th></tr></thead><tbody><tr v-for="row in state.snapshot.relationships" :key="row.id"><td><div class="table-name">{{ names.get(row.source_id) || '关联知识' }}</div><div class="table-key">适用知识版本 {{ row.source_version }}</div></td><td>{{ types[row.type] || row.type }}<div v-if="row.rationale" class="table-key">{{ row.rationale }}</div></td><td><div class="table-name">{{ names.get(row.target_id) || '关联知识' }}</div><div class="table-key">适用知识版本 {{ row.target_version }}</div></td><td><StatusPill :status="row.status"/></td></tr></tbody></table></div><p class="section-label" style="margin-top:15px">待确认关系不代表已实现或已验证。最终门禁通过后，实现与测试关系才关联准确的证据版本。</p></template><div v-else class="empty-inline">初始化提案提交后，才能查看需求、设计、代码和测试之间的追溯关系。</div></template>
