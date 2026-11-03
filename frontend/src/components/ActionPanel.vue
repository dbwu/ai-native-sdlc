<script setup lang="ts">
import { nextStep, state, openAction, openReview, allowed } from '../store/workbench'
import { actionNames } from '../lib/display'
</script>
<template><section class="next-card panel"><div class="next-copy"><p class="next-kicker">当前下一步</p><h3 class="next-title">{{ nextStep.title }}</h3><p class="next-description">{{ nextStep.description }}</p></div><button v-if="nextStep.review" class="button primary" :disabled="state.busy" @click="openReview">审阅变更 →</button><button v-else-if="nextStep.id" class="button primary" :disabled="state.busy || !allowed(nextStep.id)" @click="openAction(nextStep.id)">{{ state.busy ? '正在处理…' : actionNames[nextStep.id] }} →</button></section></template>
