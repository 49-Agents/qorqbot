'use strict';
// The Dock badge: how many things are waiting for the owner. The dashboard already reads this from
// GET /api/root/attention-queue every few seconds while it is open (sign-offs and questions not yet opened, due
// reminders, unread replies and side chats); the app reads the same answer, so the number matches what the dashboard
// shows as new. Plain Node with no Electron imports, so it is tested with node --test.

const ATTENTION_PATH = '/api/root/attention-queue';
const BADGE_MAX = 99;

/** The number of new items in an attention-queue response body, or null when the body is not one. */
function attentionCount(body) {
  if (!body || body.status !== 'ok' || body.error || !body.data || !Array.isArray(body.data.items)) return null;
  const chats = Array.isArray(body.data.side_chats) ? body.data.side_chats : [];
  return body.data.items.length + chats.filter((chat) => chat && chat.unread === true).length;
}

/** What the Dock shows for COUNT: nothing for none, the number, then 99+. */
function badgeText(count) {
  if (!Number.isInteger(count) || count < 1) return '';
  return count > BADGE_MAX ? `${BADGE_MAX}+` : String(count);
}

/** The address to read for SERVER_ORIGIN (an origin such as http://127.0.0.1:1049), or null without one. */
function attentionUrl(serverOrigin) {
  return serverOrigin ? new URL(ATTENTION_PATH, serverOrigin).href : null;
}

module.exports = { ATTENTION_PATH, BADGE_MAX, attentionCount, badgeText, attentionUrl };
