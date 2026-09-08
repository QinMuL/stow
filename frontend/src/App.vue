<script setup>
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<template>
  <router-view v-if="route.meta.bare" />
  <router-view v-else />
</template>
