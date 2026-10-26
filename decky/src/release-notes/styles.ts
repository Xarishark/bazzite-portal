/** Adapted from Bazzite Buddy; scoped to this plugin. See ../../NOTICE.md. */
export const releaseNotesStyles = `
.bazzite-portal-notes-page {
  box-sizing: border-box; height: 100vh; min-height: 0; width: 100%;
  display: flex; flex-direction: column; padding: 40px 32px 48px;
  color: #dcdedf; background: #1b2838;
}
.bazzite-portal-notes-toolbar { display: flex; align-items: center; gap: 20px; margin-bottom: 16px; flex-shrink: 0; }
.bazzite-portal-notes-toolbar h1 { flex: 1; font-size: 22px; margin: 0; }
.bazzite-portal-notes-status { margin: auto; text-align: center; max-width: 600px; }
.bazzite-portal-notes-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 20px 24px; scroll-behavior: auto; }
.bazzite-portal-notes-scroll > h2 { font-size: 24px; margin: 8px 0 24px; }
.bazzite-portal-notes-body { font-size: 17px; line-height: 1.55; overflow-wrap: anywhere; }
.bazzite-portal-notes-body h1, .bazzite-portal-notes-body h2, .bazzite-portal-notes-body h3,
.bazzite-portal-notes-body h4, .bazzite-portal-notes-body h5, .bazzite-portal-notes-body h6 { color: #67c1f5; line-height: 1.3; margin: 24px 0 12px; }
.bazzite-portal-notes-body h1 { font-size: 26px; }
.bazzite-portal-notes-body h2 { font-size: 23px; }
.bazzite-portal-notes-body h3, .bazzite-portal-notes-body h4,
.bazzite-portal-notes-body h5, .bazzite-portal-notes-body h6 { font-size: 20px; }
.bazzite-portal-notes-body p { margin: 12px 0; }
.bazzite-portal-notes-body a { color: #67c1f5; text-decoration: underline; }
.bazzite-portal-notes-body table { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 16px 0; }
.bazzite-portal-notes-body td, .bazzite-portal-notes-body th { padding: 10px; border: 1px solid #607080; text-align: left; vertical-align: top; }
.bazzite-portal-notes-body th { background: #263e50; }
.bazzite-portal-notes-body code { background: #2c3c4c; border-radius: 3px; padding: 2px 4px; }
.bazzite-portal-notes-body pre { background: #2c3c4c; padding: 12px; white-space: pre-wrap; }
.bazzite-portal-notes-body blockquote { border-left: 3px solid #67c1f5; margin-left: 0; padding-left: 16px; }
.bazzite-portal-notes-body img { max-width: 100%; height: auto; }
`;
