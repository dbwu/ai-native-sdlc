<script setup lang="ts">
import { artifacts, state } from '../store/workbench'
import ArtifactViewer from '../components/ArtifactViewer.vue'
import StatusPill from '../components/StatusPill.vue'
</script>
<template><div class="section-stack"><div v-if="state.snapshot?.task" class="task-overview"><span class="section-label">当前阶段任务</span><strong>{{ state.snapshot.task.title }}</strong><StatusPill :status="state.snapshot.task.status"/></div><template v-if="artifacts.length"><div class="content-toolbar"><span class="section-label">{{ artifacts.length }} 项正式知识产物 · 内容固定在具体版本</span><span class="section-label">查看完整内容与来源</span></div><div class="artifact-grid"><ArtifactViewer v-for="item in artifacts" :key="item.key" :item="item"/></div></template><div v-else class="empty-inline">初始化知识尚未提交。<br>请先审阅并确认初始化提案，再查看正式产物。</div></div></template>
