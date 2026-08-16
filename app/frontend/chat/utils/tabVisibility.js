/**
 * Is one browser-pane tab switched on for this box?
 *
 * `window.LLAMABOT_VISIBLE_TABS` is written by the /chat route from the
 * `visible_tabs` and `enable_vscode` site settings. An absent or malformed
 * value means "show everything", so an older box (or a failed settings read)
 * keeps its old tab strip instead of losing every tab.
 *
 * The Code tab is the reason this helper exists as more than a display rule.
 * The VS Code editor container is off by default, so a hidden Code tab points
 * at a stopped editor. Loading that iframe would make the browser retry a dead
 * address in the background, so the caller must skip the src assignment too.
 *
 * Kept as a standalone pure function so the rule can be tested without standing
 * up IframeManager and the whole UI.
 *
 * @param {string} target  A tab's data-target name, e.g. "vsCodeFrame".
 * @param {?string[]} visibleTabs  window.LLAMABOT_VISIBLE_TABS.
 * @returns {boolean} True when the tab should render and load.
 */
export function isTabVisible(target, visibleTabs) {
  if (!Array.isArray(visibleTabs)) return true;
  if (target === 'liveSiteFrame') return true;
  return visibleTabs.includes(target);
}
