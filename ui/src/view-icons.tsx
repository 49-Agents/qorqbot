// Chat/Terminal artwork: Qorqut v1 globalChatViewToggle, dashboard
// cfbbd53c77758d50b521a32d1d4ce19b06ae5a54/code/js/global-chat.js.
// The pinned source carries no additional vector license notice.
const paths = {
  chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  terminal: 'm4 17 6-6-6-6M12 19h8',
  details: 'M3 4h18v16H3ZM15 4v16',
  agents: 'M4 6h16M4 12h16M4 18h16',
  send: 'M12 19V5m-6 6 6-6 6 6',
  computer: 'M3 4h18v13H3ZM12 17v4m-4 0h8',
  copy: 'M8 8h12v12H8ZM16 8V4H4v12h4',
  check: 'm5 12 4 4L19 6',
};
export function ViewIcon({ name }: { name: keyof typeof paths }) {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
