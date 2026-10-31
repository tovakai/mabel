// Context occupancy describes one request; input_tokens may span many rounds.
export function contextOccupancy(metrics) {
  const capacity = Number(metrics.context_length) || 0;
  const percent = Number(metrics.context_percent) || 0;
  const count = Number(metrics.request_context_tokens);
  const hasCount = Number.isFinite(count) && count > 0;
  const used = hasCount ? count : (capacity && percent < 100 ? Math.round(capacity * percent / 100) : 0);
  return {
    used,
    capacity,
    estimated: !hasCount || metrics.context_usage_source === 'estimated_messages_only'
      || metrics.usage_source === 'estimated',
  };
}
