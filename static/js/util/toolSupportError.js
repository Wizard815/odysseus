// Classifies whether an error message actually indicates the model/backend
// doesn't support tool calling.
//
// Previously both call sites in chat.js used a bare substring check --
// errText.includes('tool') || errText.includes('auto') -- to decide whether
// to show "This model doesn't support agent tools" and auto-switch the UI to
// Chat mode. That fires on ANY error that merely mentions "tool" or "auto"
// anywhere, regardless of cause. Confirmed real false positives:
//   - "Invalid tool approval decision." (400 from the tool-approval flow --
//     nothing to do with model capability)
//   - Any MCP validation/timeout message ("MCP tool 'x' timed out",
//     "Invalid arguments for 'y' ... This tool accepts: ...")
// A model that genuinely supports tools would get silently flipped to Chat
// mode because an unrelated request failed with a message containing "tool".
//
// A genuine "this model can't use tools" error, from an OpenAI-compatible
// backend, says so explicitly -- e.g. "does not support tools", "tool_choice
// is not supported", "function calling is not supported". Require that
// explicit negation instead of a bare keyword.
export function isUnsupportedToolsError(text) {
  if (!text || typeof text !== 'string') return false;
  return /\b(?:not|n't|unsupported)\b[^.]{0,30}\b(?:tools?|function[- ]calls?|function[- ]calling|tool[_ -]?choice)\b/i.test(text)
      || /\b(?:tools?|function[- ]calls?|function[- ]calling|tool[_ -]?choice)\b[^.]{0,30}\b(?:not|unsupported)\b/i.test(text);
}

export default isUnsupportedToolsError;
