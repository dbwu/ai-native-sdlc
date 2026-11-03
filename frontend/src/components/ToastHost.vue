<script setup lang="ts">import { state } from '../store/workbench'</script>
<template><div class="toast-region" aria-live="polite" aria-atomic="false"><div v-for="toast in state.toasts" :key="toast.id" class="toast" :class="toast.level">{{ toast.message }}</div></div></template>
