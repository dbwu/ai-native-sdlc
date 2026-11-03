<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { state, refresh, loadSnapshot } from './store/workbench'
import AppShell from './components/AppShell.vue'
import ProjectForms from './components/ProjectForms.vue'
import ActionDialog from './components/ActionDialog.vue'
import ProposalDialog from './components/ProposalDialog.vue'
import ArtifactDialog from './components/ArtifactDialog.vue'
import ToastHost from './components/ToastHost.vue'
let polling:ReturnType<typeof setInterval>
onMounted(async()=>{await refresh();polling=setInterval(()=>{if(state.flowId && !state.busy && !state.loading && !state.actionId && !state.showProject && !state.showWorkflow && !state.showReview && !state.artifactKey && document.visibilityState==='visible')loadSnapshot(state.flowId,true).catch(()=>{})},8000)})
onUnmounted(()=>clearInterval(polling))
</script>
<template><AppShell/><ProjectForms/><ActionDialog/><ProposalDialog/><ArtifactDialog/><ToastHost/></template>
