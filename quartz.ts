import { loadQuartzConfig, loadQuartzLayout } from "./quartz/plugins/loader/config-loader"
import { componentRegistry } from "./quartz/components/registry"

// Preserve Johnny.Decimal numeric folder ordering in the explorer
componentRegistry.setOptionOverrides("explorer", {
  sortFn: (
    a: { isFolder: boolean; slugSegment: string },
    b: { isFolder: boolean; slugSegment: string },
  ) => {
    if ((!a.isFolder && !b.isFolder) || (a.isFolder && b.isFolder)) {
      return (a.slugSegment || "").localeCompare(b.slugSegment || "", undefined, {
        numeric: true,
        sensitivity: "base",
      })
    }
    return a.isFolder ? -1 : 1
  },
})

const config = await loadQuartzConfig()
export default config
export const layout = await loadQuartzLayout()
