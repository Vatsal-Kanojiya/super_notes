/** The note editor's schema, shared by the editor and the read-only previews so both look alike. */
import TaskItem from '@tiptap/extension-task-item'
import TaskList from '@tiptap/extension-task-list'
import StarterKit from '@tiptap/starter-kit'

export const editorExtensions = [StarterKit, TaskList, TaskItem.configure({ nested: true })]
