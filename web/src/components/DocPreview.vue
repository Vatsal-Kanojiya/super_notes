<script setup lang="ts">
/** A document shown read-only, in the editor's style (the before/after of a format). */
import { EditorContent, useEditor } from '@tiptap/vue-3'
import { watch } from 'vue'
import type { DocNode } from '../api/types'
import { editorExtensions } from '../lib/editorExtensions'

const props = defineProps<{ doc: DocNode }>()

const editor = useEditor({ extensions: editorExtensions, content: props.doc, editable: false })

watch(
  () => props.doc,
  (doc) => editor.value?.commands.setContent(doc, { emitUpdate: false }),
)
</script>

<template>
  <EditorContent :editor="editor" class="editor preview-doc" />
</template>
