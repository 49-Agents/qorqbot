// Relative due times use elapsed minutes, hours and days in every review surface.
function relativeDueTime(value, now = Date.now()) {
  if (!value) return '—';
  const difference = new Date(value).getTime() - now;
  if (!Number.isFinite(difference)) return '—';
  const amount = Math.abs(difference);
  if (amount < 30000) return 'now';
  const divisor = amount < 3600000 ? 60000 : amount < 86400000 ? 3600000 : 86400000;
  const count = Math.max(1, Math.round(amount / divisor));
  const unit = divisor === 60000 ? 'minute' : divisor === 3600000 ? 'hour' : 'day';
  const duration = `${count} ${unit}${count === 1 ? '' : 's'}`;
  return difference > 0 ? `in ${duration}` : `${duration} ago`;
}
