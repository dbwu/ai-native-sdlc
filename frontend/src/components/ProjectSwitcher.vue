<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { state, selectProject, loadSnapshot, notify } from '../store/workbench'
const router = useRouter()
const switching=ref(false)
async function change(event: Event) { if(switching.value)return;const id=(event.target as HTMLSelectElement).value; switching.value=true;try { await selectProject(id);if(state.projectId!==id)return; if (state.flowId) { await loadSnapshot(); await router.push(`/workflows/${state.flowId}/overview`) } else await router.push(`/projects/${id}/knowledge`) } catch(error) { notify((error as Error).message,'error') }finally{switching.value=false} }
</script>
<template><div class="project-control"><label for="project-select">当前项目</label><select id="project-select" :value="state.projectId" :disabled="state.busy || state.loading || switching" @change="change"><option v-if="!state.projects.length" value="">{{ state.loading ? '加载项目…' : '尚无项目' }}</option><option v-for="project in state.projects" :key="project.id" :value="project.id">{{ project.name }}</option></select><button class="icon-button" :disabled="state.busy || switching" title="创建项目" aria-label="创建项目" @click="state.showProject=true">＋</button></div></template>
