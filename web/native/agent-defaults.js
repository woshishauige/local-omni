(() => {
  'use strict';

  const migrationKey = 'QwenLocal.agentDefaults.v2';
  const categoryKey = 'LlamaUi.disabledToolCategories';
  try {
    if (localStorage.getItem(migrationKey) === '1') return;
    const raw = localStorage.getItem(categoryKey);
    if (raw !== null) {
      const categories = JSON.parse(raw);
      if (!Array.isArray(categories)) return;
      // This native UI has no separate agent-mode flag. Enabled tools select its
      // agent loop. Reapply the requested default once to installations with v1;
      // keep per-conversation choices and individual execution permissions intact.
      const updated = categories.filter(category => category !== 'server');
      if (updated.length !== categories.length) {
        localStorage.setItem(categoryKey, JSON.stringify(updated));
      }
    }
    localStorage.setItem(migrationKey, '1');
  } catch (error) {
    console.warn('[QwenLocal] Could not apply agent defaults:', error);
  }
})();
