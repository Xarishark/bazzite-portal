export const styles = `
@keyframes portal-throb { to { transform: rotate(360deg); } }
.portal-panel .portal-panel-button { width: 100%; min-width: 0; box-sizing: border-box; margin: 4px 0 !important; }
.portal-settings-info { padding: 12px 0 4px; font-size: 12px; line-height: 1.6; opacity: 0.7; }
.portal-plugin-gate { display: flex; align-items: center; justify-content: center; min-height: var(--portal-panel-height, 100%); box-sizing: border-box; padding: 20px 16px; }
.portal-plugin-gate-content { width: 100%; max-width: 320px; min-width: 0; text-align: center; }
.portal-plugin-gate-description { margin: 12px 0 0; font-size: 14px; line-height: 1.5; color: #b8bcbf; overflow-wrap: anywhere; }
.portal-throbber { display: inline-block; width: 16px; height: 16px; box-sizing: border-box; flex: 0 0 16px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: portal-throb 0.7s linear infinite; }
.portal-action-label { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.portal-update-label { display: inline-flex; align-items: center; gap: 10px; }
.portal-update-symbol { display: inline-flex; align-items: center; font-size: 24px; width: 24px; height: 24px; flex: 0 0 24px; }
.portal-action[data-unavailable="true"] button, .portal-action[data-unavailable="true"] [role="button"] { opacity: 0.5; }
.portal-operation-modal { width: 100%; max-width: 640px; min-width: 0; box-sizing: border-box; }
.portal-operation-footer-wrap { width: 100%; min-width: 0; box-sizing: border-box; }
.portal-operation-footer { display: flex; flex-direction: row; align-items: stretch; justify-content: flex-end; gap: 12px; width: 100%; min-width: 0; box-sizing: border-box; }
.portal-operation-button { flex: 1 1 0 !important; min-width: 0 !important; width: auto !important; max-width: 100%; min-height: 40px; padding: 8px 14px !important; font-size: 16px !important; line-height: 1.3 !important; white-space: normal; box-sizing: border-box; }
.portal-operation-footer[data-single="true"] .portal-operation-button { flex: 0 1 240px !important; }
.portal-operation-status { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.portal-operation-logs { max-height: 40vh; width: 100%; min-width: 0; box-sizing: border-box; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 13px/1.5 monospace; padding: 12px; background: #171a21; }
.portal-result-complete button, .portal-result-complete [role="button"] { background: #237d4a !important; color: #ffffff !important; }
.portal-result-failed button, .portal-result-failed [role="button"] { background: #a8323e !important; color: #ffffff !important; }
.portal-result-icon-success { color: #70e5ad; }
.portal-result-icon-failed { color: #ff7b88; }
.portal-update-button button, .portal-update-button [role="button"] { background: #8A2BE2 !important; color: #ffffff !important; }
.portal-update-button button:hover, .portal-update-button [role="button"]:hover { background: #7424C5 !important; }
.portal-update-button .portal-panel-button.gpfocus, .portal-update-button .portal-panel-button:focus-visible { background: #a45af0 !important; box-shadow: inset 0 0 0 2px #ffffff !important; }
.portal-update-button button:disabled, .portal-update-button [aria-disabled="true"] { opacity: 0.5; }
.portal-panel-status { font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; padding: 4px 0 8px; }
.portal-panel-component { display: flex; align-items: center; gap: 12px; width: 100%; max-width: 100%; min-width: 0; box-sizing: border-box; margin: 10px 0; }
.portal-step-indicator { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; flex: 0 0 24px; }
.portal-step-indicator svg { display: block; width: 24px; height: 24px; }
.portal-step-unverified { opacity: 0.55; }
.portal-step-content { min-width: 0; flex: 1; }
.portal-panel-component strong { display: block; margin-bottom: 4px; line-height: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.portal-step-detail { display: block; min-width: 0; max-width: 100%; line-height: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; overflow-wrap: normal; }
.portal-panel-activity { display: flex; align-items: center; gap: 8px; }
.portal-panel-activity > svg { width: 20px; height: 20px; flex-shrink: 0; }
.portal-logs-page { position: absolute; inset: 40px 0 0; box-sizing: border-box; display: flex; flex-direction: column; padding: 24px; background: #1b2838; color: #fff; }
.portal-logs-scroll { overflow: auto; min-height: 0; flex: 1; white-space: pre-wrap; overflow-wrap: anywhere; font: 14px/1.5 monospace; }
`;
