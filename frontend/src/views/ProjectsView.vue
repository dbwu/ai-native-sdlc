<script setup lang="ts">
import { state, openWorkflowForm } from '../store/workbench'
</script>
<template>
  <div class="panel empty-state">
    <div class="empty-icon">◇</div>
    <h2>{{ state.projectId ? '选择一个验证流程' : '创建你的第一个验证项目' }}</h2>
    <p>{{ state.projectId ? '在流程列表选择一条流程，或点击「新建 ＋」绑定已有知识并开始验证。' : '建立项目后，可从知识维护开始，通过页面逐步完成研发验证。' }}</p>
    <button v-if="!state.projectId" class="button primary" :disabled="state.busy" @click="openWorkflowForm">创建验证项目</button>
  </div>
  <section class="panel getting-started"><h2>页面验证顺序</h2><ol><li><strong>确认知识与流程基线</strong><span>建立项目知识，审阅提案，绑定本轮使用的已有知识版本。</span></li><li><strong>逐阶段提交和审阅</strong><span>人工提交产物或运行所选适配器，查看差异，确认任务与门禁。</span></li><li><strong>检查代码、测试与追溯</strong><span>核对真实执行证据、固定版本关系和操作记录，选课流程完成后可打开业务应用。</span></li></ol></section>
</template>
