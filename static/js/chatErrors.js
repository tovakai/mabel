// Classify /api/chat_stream failures that happen before the stream starts.
//
// Classification is by the backend's explicit error code, never by message
// wording. Only MODEL_TOOLS_UNSUPPORTED may suggest that the model cannot use
// tools or switch the UI out of Agent mode; every other failure (expired
// approvals, authorization, ordinary server errors) keeps the user's mode,
// message and conversation untouched.

export const MODEL_TOOLS_UNSUPPORTED = 'MODEL_TOOLS_UNSUPPORTED';

const SECRET_PATTERNS = [
  /\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}/g,
  /\bghp_[A-Za-z0-9]{10,}/g,
  /\bxox[baprs]-[A-Za-z0-9-]{8,}/g,
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]+)?/g,
];

export function redactSecrets(text) {
  let out = String(text || '');
  for (const pattern of SECRET_PATTERNS) out = out.replace(pattern, '[redacted]');
  out = out.replace(/(bearer\s+)[A-Za-z0-9._~+/=-]{6,}/gi, '$1[redacted]');
  out = out.replace(/((?:api[_-]?key|token|password|secret)\s*[=:]\s*)\S+/gi, '$1[redacted]');
  return out;
}

function parseBody(bodyText) {
  const raw = typeof bodyText === 'string' ? bodyText : '';
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch { parsed = null; }
  let code = '';
  let message = '';
  const detail = parsed && typeof parsed === 'object' && 'detail' in parsed ? parsed.detail : parsed;
  if (detail && typeof detail === 'object') {
    code = typeof detail.error === 'string' ? detail.error : (typeof detail.code === 'string' ? detail.code : '');
    message = typeof detail.message === 'string' ? detail.message : '';
  } else if (typeof detail === 'string') {
    message = detail;
  } else if (!parsed && raw.trim() && raw.length < 300 && !raw.trim().startsWith('<')) {
    message = raw.trim();   // short plain-text body; never an HTML error page
  }
  return { code: code.trim(), message: redactSecrets(message).trim().slice(0, 300) };
}

export function classifyChatStreamError(status, bodyText) {
  const { code, message } = parseBody(bodyText);
  let category;
  if (code === MODEL_TOOLS_UNSUPPORTED) category = 'tool_capability';
  else if (code === 'TOOL_APPROVAL_NOT_ALLOWED') category = 'authorization';
  else if (code.startsWith('TOOL_APPROVAL_')) category = 'approval';
  else if (status === 401 || status === 403) category = 'authorization';
  else if (status === 429) category = 'rate_limited';
  else category = 'server';

  let text;
  switch (category) {
    case 'tool_capability':
      text = "This model doesn't support agent tools — switched to Chat mode. Try again.";
      break;
    case 'approval':
      text = (message || 'That approval is no longer valid.')
        + ' Send your request again to get a fresh approval prompt.';
      break;
    case 'authorization':
      text = message || "You're not allowed to do that here.";
      break;
    case 'rate_limited':
      text = message || 'Too many requests right now. Try again in a moment.';
      break;
    default:
      text = `Error ${status}` + (message ? `: ${message}` : '');
  }
  return {
    category,
    code,
    status,
    message,
    text,
    // Only an explicit capability failure may change the user's mode.
    switchToChat: category === 'tool_capability',
  };
}

export default { classifyChatStreamError, redactSecrets, MODEL_TOOLS_UNSUPPORTED };
