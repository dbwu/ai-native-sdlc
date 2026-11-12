<script lang="ts">
export type ProjectModelChange = {
  op: 'added' | 'updated' | 'removed'
  kind: string
  label: string
  detail?: string
  change_flags?: string[]
}
</script>

<script setup lang="ts">
defineProps<{ changes: ProjectModelChange[] }>()

const actions = { added: '新增', updated: '修改', removed: '删除' }
const flags: Record<string, string> = {
  renamed: '改名', moved: '归属调整', revision_changed: '知识版本更新',
  endpoints_changed: '端点调整', reordered: '顺序调整',
}
const kindLabels: Record<string, string> = {
  baseline: '基线', project: '项目', architecture: '架构', node: '架构节点', domain: '领域', module: '模块',
  dependency: '依赖', process: '业务流程', step: '流程步骤', transition: '流程分支', scenario: '时序场景',
  participant: '参与方', requirement: '关联需求',
}
</script>

<template>
  <ul v-if="changes.length" class="model-changes">
    <li v-for="(change, index) in changes" :key="index">
      <span class="change-action" :class="change.op">{{ actions[change.op] }}</span>
      <strong>{{ change.label }}</strong>
      <span class="change-kind">{{ kindLabels[change.kind] || change.kind }}</span>
      <span v-for="flag in change.change_flags?.filter(value => flags[value])" :key="flag" class="change-kind">{{ flags[flag] }}</span>
      <p v-if="change.detail">{{ change.detail }}</p>
    </li>
  </ul>
  <p v-else class="section-label">本轮没有记录结构变化。</p>
</template>

<style scoped>
.model-changes { display: grid; gap: 7px; list-style: none; margin: 8px 0; padding: 0; }
.model-changes li { border: 1px solid var(--line); border-radius: 7px; padding: 7px 10px; overflow-wrap: anywhere; }
.model-changes p { margin: 4px 0 0; color: var(--muted); white-space: pre-wrap; }
.change-action { display: inline-block; margin-inline-end: 8px; padding: 1px 6px; border-radius: 5px; font-size: 11px; }
.change-action.added { background: #eaf7f0; color: #2c7257; }
.change-action.updated { background: #eaf3fa; color: #215987; }
.change-action.removed { background: #fceeee; color: #944545; }
.change-kind { margin-inline-start: 8px; color: var(--muted); font-size: 11px; }
</style>
